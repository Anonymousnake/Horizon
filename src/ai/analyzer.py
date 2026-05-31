"""Content analysis using AI."""

import asyncio
import json
import re
from typing import List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from .client import AIClient
from .prompts import CONTENT_ANALYSIS_BATCH_USER, CONTENT_ANALYSIS_SYSTEM, CONTENT_ANALYSIS_USER
from .utils import parse_json_response
from ..models import ContentItem

DEFAULT_THROTTLE_SEC = 0.0


class ContentAnalyzer:
    """Analyzes content items using AI to determine importance."""

    def __init__(self, ai_client: AIClient):
        self.client = ai_client

    @staticmethod
    def _parse_json_response(response: str) -> Optional[dict]:
        """Try multiple strategies to extract a JSON object from an AI response.

        Returns the parsed dict, or None if all strategies fail.
        """
        return parse_json_response(response)

    def _get_throttle_sec(self) -> float:
        """Return the configured inter-item throttle, clamped to zero or above."""
        config = getattr(self.client, "config", None)
        throttle_sec = getattr(config, "throttle_sec", DEFAULT_THROTTLE_SEC)
        return max(throttle_sec, 0.0)

    def _get_concurrency(self) -> int:
        """Return the configured analysis concurrency, clamped to 1 or above."""
        config = getattr(self.client, "config", None)
        concurrency = getattr(config, "analysis_concurrency", 1)
        return max(concurrency, 1)

    def _get_batch_size(self) -> int:
        """Return how many items to send in one AI request."""
        config = getattr(self.client, "config", None)
        batch_size = getattr(config, "analysis_batch_size", 1)
        return max(int(batch_size or 1), 1)

    async def analyze_batch(self, items: List[ContentItem]) -> List[ContentItem]:
        throttle_sec = self._get_throttle_sec()
        concurrency = self._get_concurrency()
        batch_size = self._get_batch_size()
        semaphore = asyncio.Semaphore(concurrency)
        chunks = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
        if batch_size > 1:
            print(f"Analyzing {len(items)} items in {len(chunks)} AI request batch(es)")

        async def _process(chunk: List[ContentItem], index: int, progress_task) -> List[ContentItem]:
            async with semaphore:
                try:
                    if len(chunk) == 1:
                        await self._analyze_item(chunk[0])
                    else:
                        await self._analyze_items(chunk)
                except Exception as e:
                    print(f"Error analyzing batch {[item.id for item in chunk]}: {e}")
                    for item in chunk:
                        item.ai_score = 0.0
                        item.ai_reason = "Analysis failed"
                        item.ai_summary = item.title
                        item.ai_tags = []
                if throttle_sec > 0 and index < len(chunks) - 1:
                    await asyncio.sleep(throttle_sec)
            progress.advance(progress_task, advance=len(chunk))
            return chunk

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Analyzing", total=len(items))
            coros = [
                _process(chunk, i, task) for i, chunk in enumerate(chunks)
            ]
            analyzed_chunks = await asyncio.gather(*coros)

        return [item for chunk in analyzed_chunks for item in chunk]

    def _format_item_for_prompt(self, item: ContentItem) -> str:
        content_section = ""
        if item.content:
            content_text = item.content
            if "--- Top Comments ---" in content_text:
                main, _comments_part = content_text.split("--- Top Comments ---", 1)
                content_section = f"Content: {main.strip()[:800]}"
            else:
                content_section = f"Content: {content_text[:1000]}"

        discussion_parts = []
        if item.content and "--- Top Comments ---" in item.content:
            comments_part = item.content.split("--- Top Comments ---", 1)[1]
            discussion_parts.append(f"Community Comments:\n{comments_part[:1200]}")

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("bookmarks"):
            engagement_items.append(f"{meta['bookmarks']} bookmarks")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")
        if meta.get("community_note"):
            discussion_parts.append(f"Community Note: {meta['community_note']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""
        return "\n".join(
            part
            for part in [
                f"ID: {item.id}",
                f"Title: {item.title}",
                f"Source: {item.source_type.value}",
                f"Author: {item.author or 'Unknown'}",
                f"URL: {item.url}",
                content_section,
                discussion_section,
            ]
            if part
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_items(self, items: List[ContentItem]) -> None:
        item_blocks = [self._format_item_for_prompt(item) for item in items]
        response = await self.client.complete(
            system=CONTENT_ANALYSIS_SYSTEM,
            user=CONTENT_ANALYSIS_BATCH_USER.format(items="\n\n---\n\n".join(item_blocks)),
        )

        result = self._parse_json_response(response)
        analyses = result.get("analyses") if isinstance(result, dict) else None
        if not isinstance(analyses, list):
            raise ValueError("Batch analysis response missing analyses list")

        by_id = {
            str(analysis.get("id")): analysis
            for analysis in analyses
            if isinstance(analysis, dict) and analysis.get("id")
        }

        for item in items:
            analysis = by_id.get(item.id)
            if not analysis:
                item.ai_score = 0.0
                item.ai_reason = "Batch analysis missing this item"
                item.ai_summary = item.title
                item.ai_tags = []
                continue
            item.ai_score = float(analysis.get("score", 0))
            item.ai_reason = analysis.get("reason", "")
            item.ai_summary = analysis.get("summary", item.title)
            item.ai_tags = analysis.get("tags", [])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_item(self, item: ContentItem) -> None:
        """Analyze a single content item.

        Args:
            item: Content item to analyze (modified in-place)
        """
        # Prepare content section
        content_section = ""
        if item.content:
            # Split off comments if present
            content_text = item.content
            if "--- Top Comments ---" in content_text:
                main, comments_part = content_text.split("--- Top Comments ---", 1)
                content_section = f"Content: {main.strip()[:800]}"
            else:
                content_section = f"Content: {content_text[:1000]}"

        # Prepare discussion section (comments, engagement)
        discussion_parts = []
        if item.content and "--- Top Comments ---" in item.content:
            comments_part = item.content.split("--- Top Comments ---", 1)[1]
            discussion_parts.append(f"Community Comments:\n{comments_part[:1500]}")

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("bookmarks"):
            engagement_items.append(f"{meta['bookmarks']} bookmarks")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")
        if meta.get("community_note"):
            discussion_parts.append(f"Community Note: {meta['community_note']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""

        # Generate user prompt
        user_prompt = CONTENT_ANALYSIS_USER.format(
            title=item.title,
            source=f"{item.source_type.value}",
            author=item.author or "Unknown",
            url=str(item.url),
            content_section=content_section,
            discussion_section=discussion_section
        )

        # Get AI completion
        response = await self.client.complete(
            system=CONTENT_ANALYSIS_SYSTEM,
            user=user_prompt,
        )

        # Parse JSON response with robust fallback
        result = self._parse_json_response(response)
        if result is None:
            print(f"Warning: could not parse analysis response for {item.id}, using defaults")
            item.ai_score = 0.0
            item.ai_reason = "Analysis response parse failed"
            item.ai_summary = item.title
            item.ai_tags = []
            return

        # Update item with analysis results
        item.ai_score = float(result.get("score", 0))
        item.ai_reason = result.get("reason", "")
        item.ai_summary = result.get("summary", item.title)
        item.ai_tags = result.get("tags", [])
