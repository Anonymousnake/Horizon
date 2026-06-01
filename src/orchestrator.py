"""Main orchestrator coordinating the entire workflow."""

import asyncio
import re
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from urllib.parse import urlparse
import httpx
from rich.console import Console

from .models import Config, ContentItem
from .storage.manager import StorageManager
from .services.email import EmailManager
from .services.webhook import WebhookNotifier
from .scrapers.github import GitHubScraper
from .scrapers.hackernews import HackerNewsScraper
from .scrapers.rss import RSSScraper
from .scrapers.reddit import RedditScraper
from .scrapers.telegram import TelegramScraper
from .scrapers.twitter import TwitterScraper
from .scrapers.openbb import OpenBBScraper
from .scrapers.ossinsight import OSSInsightScraper
from .ai.client import create_ai_client
from .ai.analyzer import ContentAnalyzer
from .ai.summarizer import DailySummarizer
from .ai.enricher import ContentEnricher
from .ai.tokens import get_usage_snapshot


class HorizonOrchestrator:
    """Orchestrates the complete workflow for content aggregation and analysis."""

    def __init__(self, config: Config, storage: StorageManager):
        """Initialize orchestrator.

        Args:
            config: Application configuration
            storage: Storage manager
        """
        self.config = config
        self.storage = storage
        self.console = Console()
        self.email_manager = EmailManager(config.email, console=self.console) if config.email else None
        self.webhook_notifier = (
            WebhookNotifier(config.webhook, console=self.console)
            if config.webhook and config.webhook.enabled
            else None
        )

    async def run(self, force_hours: int = None) -> None:
        """Execute the complete workflow.

        Args:
            force_hours: Optional override for time window in hours
        """
        self.console.print("[bold cyan]🌅 Horizon - Starting aggregation...[/bold cyan]\n")

        # Check email subscriptions if configured
        if (
            self.email_manager
            and self.config.email
            and self.config.email.enabled
            and self.config.email.imap_enabled
        ):
            self.console.print("📧 Checking for new email subscriptions...")
            self.email_manager.check_subscriptions(self.storage)

        try:
            # 1. Determine time window
            since = self._determine_time_window(force_hours)
            self.console.print(f"📅 Fetching content since: {since.strftime('%Y-%m-%d %H:%M:%S')}\n")

            # 2. Fetch content from all sources
            all_items = await self.fetch_all_sources(since)
            self.console.print(f"📥 Fetched {len(all_items)} items from all sources\n")

            if not all_items:
                self.console.print("[yellow]No new content found. Exiting.[/yellow]")
                return

            # 3. Merge cross-source duplicates (same URL from different sources)
            merged_items = self.merge_cross_source_duplicates(all_items)
            if len(merged_items) < len(all_items):
                self.console.print(
                    f"🔗 Merged {len(all_items) - len(merged_items)} cross-source duplicates "
                    f"→ {len(merged_items)} unique items\n"
                )

            # 4. Analyze with AI
            analyzed_items = await self._analyze_content(merged_items)
            self.console.print(f"🤖 Analyzed {len(analyzed_items)} items with AI\n")

            # 5. Filter by score threshold
            threshold = self.config.filtering.ai_score_threshold
            candidate_items = [
                item for item in analyzed_items
                if item.ai_score is not None
            ]
            candidate_items.sort(key=lambda x: x.ai_score or 0, reverse=True)

            important_items = self.select_categorized_items(
                candidates=candidate_items,
                threshold=threshold,
                max_per_category=10,
            )

            self.console.print(
                f"⭐️ {len(important_items)} categorized items selected "
                f"(threshold ≥ {threshold}, backfill ≥ {max(0, threshold - 1)})\n"
            )
            target_item_count = len(important_items)

            # 5.5 Fast local deduplication: drop obvious same-event duplicates.
            deduped_items = self.merge_similar_headline_duplicates(important_items)
            if len(deduped_items) < len(important_items):
                self.console.print(
                    f"🧹 Removed {len(important_items) - len(deduped_items)} local headline duplicates "
                    f"→ {len(deduped_items)} unique items\n"
                )
            important_items = deduped_items

            # 5.6 Backfill after deduplication so a briefing does not shrink too much.
            backfilled_items = self.backfill_unique_items(
                selected=important_items,
                candidates=candidate_items,
                target_count=target_item_count,
                min_score=max(0, threshold - 1),
            )
            if len(backfilled_items) > len(important_items):
                self.console.print(
                    f"➕ Backfilled {len(backfilled_items) - len(important_items)} unique items "
                    f"→ {len(backfilled_items)} total items\n"
                )
            important_items = backfilled_items

            # 5.7 Optional semantic deduplication: drop items covering the same topic
            deduped_items = await self.merge_topic_duplicates(important_items)
            if len(deduped_items) < len(important_items):
                self.console.print(
                    f"🧹 Removed {len(important_items) - len(deduped_items)} topic duplicates "
                    f"→ {len(deduped_items)} unique items\n"
                )
            important_items = deduped_items

            # 5.8 Optional second-stage Twitter reply expansion + targeted re-analysis
            await self._expand_twitter_discussion(important_items)

            # Show per-sub-source selection breakdown
            selected_counts: Dict[str, int] = defaultdict(int)
            for item in important_items:
                key = f"{item.source_type.value}/{self._sub_source_label(item)}"
                selected_counts[key] += 1
            for source_key, count in sorted(selected_counts.items()):
                self.console.print(f"      • {source_key}: {count}")
            self.console.print("")

            # 6. Search related stories + enrich with background knowledge (2nd AI pass)
            await self._enrich_important_items(important_items)

            # 7. Generate and save daily summaries for each configured language
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for lang in self.config.ai.languages:
                summarizer = DailySummarizer()
                summary = await summarizer.generate_summary(important_items, today, len(all_items), language=lang)

                # Save to data/summaries/
                summary_path = self.storage.save_daily_summary(today, summary, language=lang)
                self.console.print(f"💾 Saved {lang.upper()} summary to: {summary_path}\n")

                # Copy to docs/ for GitHub Pages
                try:
                    from pathlib import Path

                    post_filename = f"{today}-summary-{lang}.md"
                    posts_dir = Path("docs/_posts")
                    posts_dir.mkdir(parents=True, exist_ok=True)

                    dest_path = posts_dir / post_filename

                    # Add Jekyll front matter
                    front_matter = (
                        "---\n"
                        "layout: default\n"
                        f"title: \"Horizon Summary: {today} ({lang.upper()})\"\n"
                        f"date: {today}\n"
                        f"lang: {lang}\n"
                        "---\n\n"
                    )

                    # Strip leading H1 header to avoid duplication with Jekyll title
                    summary_content = summary
                    first_line = summary_content.strip().split("\n")[0]
                    if first_line.startswith("# "):
                        parts = summary_content.split("\n", 1)
                        if len(parts) > 1:
                            summary_content = parts[1].strip()

                    with open(dest_path, "w", encoding="utf-8") as f:
                        f.write(front_matter + summary_content)

                    self.console.print(f"📄 Copied {lang.upper()} summary to GitHub Pages: {dest_path}\n")
                except Exception as e:
                    self.console.print(f"[yellow]⚠️  Failed to copy {lang.upper()} summary to docs/: {e}[/yellow]\n")

                # Send email if configured
                if self.email_manager and self.config.email and self.config.email.enabled:
                    self.console.print(f"📧 Sending {lang.upper()} email summary...")
                    subscribers = self.storage.load_subscribers()
                    subject = f"Horizon Summary ({lang.upper()}) - {today}"
                    self.email_manager.send_daily_summary(summary, subject, subscribers)

                # Send webhook notification if configured
                if self.webhook_notifier:
                    await self.webhook_notifier.send_daily_summary(
                        summary=summary,
                        important_items=important_items,
                        all_items_count=len(all_items),
                        date=today,
                        lang=lang,
                        summarizer=summarizer,
                    )

            self.console.print("[bold green]✅ Horizon completed successfully![/bold green]")
            usage = get_usage_snapshot()
            if usage.total_tokens > 0:
                self.console.print(
                    f"\n🧮 Token usage this run: "
                    f"{usage.total_tokens} tokens "
                    f"(input: {usage.total_input_tokens}, output: {usage.total_output_tokens})"
                )
                for provider, u in sorted(usage.per_provider.items()):
                    if u.total <= 0:
                        continue
                    self.console.print(
                        f"   • {provider}: {u.total} tokens "
                        f"(in: {u.input_tokens}, out: {u.output_tokens})"
                    )

        except Exception as e:
            self.console.print(f"[bold red]❌ Error: {e}[/bold red]")

            # Send webhook failure notification if configured
            if self.webhook_notifier:
                await self.webhook_notifier.send_failure(
                    date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    error_message=str(e),
                )

            raise

    def _determine_time_window(self, force_hours: int = None) -> datetime:
        if force_hours:
            since = datetime.now(timezone.utc) - timedelta(hours=force_hours)
        else:
            hours = self.config.filtering.time_window_hours
            since = datetime.now(timezone.utc) - timedelta(hours=hours)
        return since

    async def fetch_all_sources(self, since: datetime) -> List[ContentItem]:
        """Fetch content from all configured sources.

        This is a stable stage entry point for integrations such as MCP.

        Args:
            since: Fetch items published after this time

        Returns:
            List[ContentItem]: All fetched items
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = []

            # GitHub sources
            if self.config.sources.github:
                github_scraper = GitHubScraper(self.config.sources.github, client)
                tasks.append(self._fetch_with_progress("GitHub", github_scraper, since))

            # Hacker News
            if self.config.sources.hackernews.enabled:
                hn_scraper = HackerNewsScraper(self.config.sources.hackernews, client)
                tasks.append(self._fetch_with_progress("Hacker News", hn_scraper, since))

            # RSS feeds
            if self.config.sources.rss:
                rss_scraper = RSSScraper(self.config.sources.rss, client)
                tasks.append(self._fetch_with_progress("RSS Feeds", rss_scraper, since))

            # Reddit
            if self.config.sources.reddit.enabled:
                reddit_scraper = RedditScraper(self.config.sources.reddit, client)
                tasks.append(self._fetch_with_progress("Reddit", reddit_scraper, since))

            # Telegram
            if self.config.sources.telegram.enabled:
                telegram_scraper = TelegramScraper(self.config.sources.telegram, client)
                tasks.append(self._fetch_with_progress("Telegram", telegram_scraper, since))

            # Twitter
            if self.config.sources.twitter and self.config.sources.twitter.enabled:
                twitter_scraper = TwitterScraper(self.config.sources.twitter, client)
                tasks.append(self._fetch_with_progress("Twitter", twitter_scraper, since))

            # OpenBB (financial news / filings via the OpenBB Platform SDK)
            if self.config.sources.openbb and self.config.sources.openbb.enabled:
                openbb_scraper = OpenBBScraper(self.config.sources.openbb, client)
                tasks.append(self._fetch_with_progress("OpenBB", openbb_scraper, since))

            # OSS Insight trending repos
            if self.config.sources.ossinsight and self.config.sources.ossinsight.enabled:
                oss_scraper = OSSInsightScraper(self.config.sources.ossinsight, client)
                tasks.append(self._fetch_with_progress("OSS Insight", oss_scraper, since))

            # Fetch all concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Flatten results
            all_items = []
            for result in results:
                if isinstance(result, Exception):
                    self.console.print(f"[red]Error fetching source: {result}[/red]")
                elif isinstance(result, list):
                    all_items.extend(result)

            return all_items

    async def _fetch_with_progress(self, name: str, scraper, since: datetime) -> List[ContentItem]:
        """Fetch from a scraper with progress indication.

        Args:
            name: Source name for display
            scraper: Scraper instance
            since: Fetch items after this time

        Returns:
            List[ContentItem]: Fetched items
        """
        self.console.print(f"🔍 Fetching from {name}...")
        items = await scraper.fetch(since)
        self.console.print(f"   Found {len(items)} items from {name}")

        # Show per-sub-source breakdown when there are multiple sub-sources
        sub_counts: Dict[str, int] = defaultdict(int)
        for item in items:
            sub_counts[self._sub_source_label(item)] += 1
        if len(sub_counts) > 1:
            for sub, count in sorted(sub_counts.items()):
                self.console.print(f"      • {sub}: {count}")

        return items

    @staticmethod
    def _sub_source_label(item: ContentItem) -> str:
        """Return a human-readable sub-source label for an item."""
        meta = item.metadata
        if meta.get("subreddit"):
            return f"r/{meta['subreddit']}"
        if meta.get("feed_name"):
            return meta["feed_name"]
        if meta.get("channel"):
            return f"@{meta['channel']}"
        if meta.get("period") and meta.get("repo"):
            return f"ossinsight:{meta.get('primary_language', 'all')}"
        if meta.get("repo"):
            return meta["repo"]
        if meta.get("watchlist"):
            return meta["watchlist"]
        return item.author or "unknown"

    def merge_cross_source_duplicates(self, items: List[ContentItem]) -> List[ContentItem]:
        """Merge items that point to the same URL from different sources.

        This is a stable stage helper for integrations such as MCP.

        Keeps the item with the richest content and combines metadata.

        Args:
            items: Items to deduplicate

        Returns:
            List[ContentItem]: Deduplicated items
        """
        def normalize_url(url: str) -> str:
            parsed = urlparse(str(url))
            # Strip www prefix, trailing slashes, and fragments
            host = parsed.hostname or ""
            if host.startswith("www."):
                host = host[4:]
            path = parsed.path.rstrip("/")
            return f"{host}{path}"

        # Group by normalized URL
        url_groups: Dict[str, List[ContentItem]] = {}
        for item in items:
            key = normalize_url(str(item.url))
            url_groups.setdefault(key, []).append(item)

        merged = []
        for key, group in url_groups.items():
            if len(group) == 1:
                merged.append(group[0])
                continue

            # Pick the item with the richest content as primary
            primary = max(group, key=lambda x: len(x.content or ""))

            # Merge metadata and source info from other items
            all_sources = set()
            for item in group:
                all_sources.add(item.source_type.value)
                # Merge metadata (engagement, discussion, etc.)
                for mk, mv in item.metadata.items():
                    if mk not in primary.metadata or not primary.metadata[mk]:
                        primary.metadata[mk] = mv

                # Append content (e.g., comments from another source)
                if item is not primary and item.content:
                    if primary.content and item.content not in primary.content:
                        primary.content = (primary.content or "") + f"\n\n--- From {item.source_type.value} ---\n" + item.content

            primary.metadata["merged_sources"] = list(all_sources)
            merged.append(primary)

        return merged

    def merge_similar_headline_duplicates(self, items: List[ContentItem]) -> List[ContentItem]:
        """Merge obvious same-event duplicates without an additional AI call.

        RSS packs often contain the same event from multiple outlets. URL
        deduplication cannot catch that, and running semantic dedup for every
        briefing is expensive. This pass is deliberately conservative: it only
        merges items with highly similar translated/original headlines, or
        strong headline/summary token overlap plus shared numbers or tags.
        """
        if len(items) <= 1:
            return items

        merged: List[ContentItem] = []
        for item in items:
            duplicate_of: ContentItem | None = None
            for primary in merged:
                if self._looks_like_same_event(primary, item):
                    duplicate_of = primary
                    break

            if duplicate_of is None:
                merged.append(item)
                continue

            self._merge_duplicate_item(duplicate_of, item)
            self.console.print(
                f"   [dim]local dedup: keep {duplicate_of.metadata.get('title_zh') or duplicate_of.title}[/dim]\n"
                f"   [dim]             drop {item.metadata.get('title_zh') or item.title}[/dim]"
            )

        return merged

    def backfill_unique_items(
        self,
        selected: List[ContentItem],
        candidates: List[ContentItem],
        target_count: int,
        min_score: float,
    ) -> List[ContentItem]:
        """Append lower-scored unique candidates after deduplication."""
        if len(selected) >= target_count:
            return selected

        result = list(selected)
        selected_ids = {item.id for item in result}
        for candidate in candidates:
            if len(result) >= target_count:
                break
            if candidate.id in selected_ids:
                continue
            if candidate.ai_score is None or candidate.ai_score < min_score:
                continue
            if any(self._looks_like_same_event(existing, candidate) for existing in result):
                continue
            result.append(candidate)
            selected_ids.add(candidate.id)

        return result

    def select_categorized_items(
        self,
        candidates: List[ContentItem],
        threshold: float,
        max_per_category: int,
    ) -> List[ContentItem]:
        """Select a categorized briefing, capped per category."""
        categories = [
            "今日要闻",
            "财经商业",
            "AI科技",
            "工程安全",
            "社区趋势",
            "游戏文化",
        ]
        selected_by_category: dict[str, List[ContentItem]] = {
            category: [] for category in categories
        }
        selected: List[ContentItem] = []
        min_score = max(0, threshold - 1)

        for item in candidates:
            if item.ai_score is None or item.ai_score < min_score:
                continue
            category = self._briefing_category(item)
            if len(selected_by_category[category]) >= max_per_category:
                continue
            if any(self._looks_like_same_event(existing, item) for existing in selected):
                continue
            item.metadata["briefing_category"] = category
            selected_by_category[category].append(item)
            selected.append(item)

        flattened: List[ContentItem] = []
        for category in categories:
            flattened.extend(selected_by_category[category])
        return flattened

    @staticmethod
    def _briefing_category(item: ContentItem) -> str:
        text = " ".join(
            [
                item.title or "",
                item.ai_summary or "",
                " ".join(item.ai_tags),
                str(item.metadata.get("title_zh") or ""),
                str(item.metadata.get("detailed_summary_zh") or ""),
                str(item.metadata.get("category") or ""),
                str(item.metadata.get("feed_name") or ""),
            ]
        ).lower()

        def has(*needles: str) -> bool:
            return any(needle.lower() in text for needle in needles)

        if has("游戏", "gaming", "game", "ign", "pc gamer", "eurogamer", "gcores", "机核", "触乐"):
            return "游戏文化"
        if has("热榜", "社区", "v2ex", "hacker news", "product hunt", "trending", "linuxdo", "知乎", "微博", "百度热搜", "掘金"):
            return "社区趋势"
        if has("security", "安全", "cisa", "hacker news", "freebuf", "krebs", "schneier", "漏洞", "攻击"):
            return "工程安全"
        if has("github", "cloudflare", "aws", "vercel", "supabase", "react", "vue", "svelte", "astro", "next.js", "typescript", "rust", "python", "node.js", "开源", "工程博客", "编程", "developer", "developers"):
            return "工程安全"
        if has("ai", "人工智能", "智能体", "agent", "openai", "deepmind", "hugging face", "gemini", "llm", "模型", "算力", "数据中心"):
            return "AI科技"
        if has("财经", "金融", "商业", "finance", "markets", "market", "ipo", "etf", "估值", "投资", "融资", "财新", "华尔街", "coin", "crypto", "比亚迪", "softbank", "软银"):
            return "财经商业"
        return "今日要闻"

    @staticmethod
    def _headline_variants(item: ContentItem) -> List[str]:
        variants = [
            str(item.metadata.get("title_zh") or ""),
            item.title or "",
        ]
        return [text for text in variants if text.strip()]

    @staticmethod
    def _normalize_headline(text: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text).lower()

    @staticmethod
    def _tokenize_event_text(text: str) -> set[str]:
        text = text.lower()
        tokens = set(re.findall(r"[a-z0-9]{2,}", text))
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            tokens.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
        return tokens

    @classmethod
    def _event_tokens(cls, item: ContentItem) -> set[str]:
        pieces = cls._headline_variants(item)
        pieces.extend(
            [
                item.ai_summary or "",
                str(item.metadata.get("detailed_summary_zh") or ""),
                " ".join(item.ai_tags),
            ]
        )
        return cls._tokenize_event_text(" ".join(pieces))

    @classmethod
    def _signature_tokens(cls, item: ContentItem) -> set[str]:
        text = " ".join(
            cls._headline_variants(item)
            + [
                item.ai_summary or "",
                str(item.metadata.get("detailed_summary_zh") or ""),
                " ".join(item.ai_tags),
            ]
        ).lower()

        signatures = set()
        synonym_patterns = {
            "softbank": [r"softbank", r"软银"],
            "france": [r"france", r"french", r"法国"],
            "ai": [r"\bai\b", r"artificial intelligence", r"人工智能", r"智能"],
            "datacenter": [r"data centers?", r"数据中心", r"计算集群", r"算力集群", r"基础设施", r"设施"],
            "invest": [r"invest", r"investment", r"投资", r"斥资", r"投入", r"承诺"],
            "spacex": [r"spacex"],
            "ipo": [r"\bipo\b", r"上市"],
            "xrp": [r"\bxrp\b"],
            "defi": [r"\bdefi\b"],
            "california": [r"california", r"加州"],
            "gaming": [r"gaming", r"games?", r"游戏"],
            "byd": [r"\bbyd\b", r"比亚迪"],
            "anthropic": [r"anthropic"],
            "google": [r"google", r"谷歌"],
        }
        for token, patterns in synonym_patterns.items():
            if any(re.search(pattern, text) for pattern in patterns):
                signatures.add(token)

        signatures.update(tag.lower() for tag in item.ai_tags if len(tag) >= 3)
        signatures.update(re.findall(r"\d+(?:\.\d+)?", text))
        return signatures

    @classmethod
    def _headline_tokens(cls, item: ContentItem) -> set[str]:
        return cls._tokenize_event_text(" ".join(cls._headline_variants(item)))

    @classmethod
    def _title_similarity(cls, left: ContentItem, right: ContentItem) -> float:
        best = 0.0
        for left_title in cls._headline_variants(left):
            left_norm = cls._normalize_headline(left_title)
            if len(left_norm) < 6:
                continue
            for right_title in cls._headline_variants(right):
                right_norm = cls._normalize_headline(right_title)
                if len(right_norm) < 6:
                    continue
                best = max(best, SequenceMatcher(None, left_norm, right_norm).ratio())
        return best

    @classmethod
    def _token_similarity(cls, left: ContentItem, right: ContentItem) -> float:
        left_tokens = cls._event_tokens(left)
        right_tokens = cls._event_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        overlap = len(left_tokens & right_tokens)
        return 2 * overlap / (len(left_tokens) + len(right_tokens))

    @classmethod
    def _headline_token_similarity(cls, left: ContentItem, right: ContentItem) -> float:
        left_tokens = cls._headline_tokens(left)
        right_tokens = cls._headline_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        overlap = len(left_tokens & right_tokens)
        return 2 * overlap / (len(left_tokens) + len(right_tokens))

    @classmethod
    def _looks_like_same_event(cls, left: ContentItem, right: ContentItem) -> bool:
        title_score = cls._title_similarity(left, right)
        if title_score >= 0.72:
            return True

        left_text = " ".join(cls._headline_variants(left))
        right_text = " ".join(cls._headline_variants(right))
        shared_numbers = set(re.findall(r"\d+(?:\.\d+)?", left_text)) & set(
            re.findall(r"\d+(?:\.\d+)?", right_text)
        )
        shared_tags = {tag.lower() for tag in left.ai_tags} & {
            tag.lower() for tag in right.ai_tags
        }

        if title_score >= 0.66 and shared_numbers and shared_tags:
            return True

        headline_token_score = cls._headline_token_similarity(left, right)
        if headline_token_score >= 0.54 and (shared_numbers or shared_tags):
            return True

        token_score = cls._token_similarity(left, right)
        if token_score >= 0.56 and bool(shared_numbers or shared_tags):
            return True

        shared_signatures = cls._signature_tokens(left) & cls._signature_tokens(right)
        generic_signatures = {"ai", "gaming", "news", "technology", "tech"}
        strong_signatures = shared_signatures - generic_signatures
        return len(strong_signatures) >= 3 and bool(shared_tags or shared_numbers)

    @staticmethod
    def _merge_duplicate_item(primary: ContentItem, duplicate: ContentItem) -> None:
        sources = set(primary.metadata.get("merged_sources", []))
        sources.add(primary.source_type.value)
        sources.add(duplicate.source_type.value)
        primary.metadata["merged_sources"] = sorted(sources)

        feed_names = set(primary.metadata.get("merged_feed_names", []))
        for item in (primary, duplicate):
            if feed_name := item.metadata.get("feed_name"):
                feed_names.add(str(feed_name))
        if feed_names:
            primary.metadata["merged_feed_names"] = sorted(feed_names)

        for key, value in duplicate.metadata.items():
            if key not in primary.metadata or not primary.metadata[key]:
                primary.metadata[key] = value

        if duplicate.content and duplicate.content not in (primary.content or ""):
            label = duplicate.metadata.get("feed_name") or duplicate.source_type.value
            primary.content = (primary.content or "") + f"\n\n--- From {label} ---\n{duplicate.content}"

    async def merge_topic_duplicates(self, items: List[ContentItem]) -> List[ContentItem]:
        """Merge items covering the same topic using AI semantic deduplication.

        This is a stable stage helper for integrations such as MCP.

        Sends all item titles, tags, and summaries to AI in a single call.
        Items must already be sorted by ai_score descending so that the first
        item in each duplicate group is always the highest-scored one.
        Content (comments) from duplicate items is merged into the primary.

        Falls back to returning items unchanged if the AI call fails.
        """
        if not self.config.filtering.enable_topic_dedup:
            return items

        if len(items) <= 1:
            return items

        from .ai.prompts import TOPIC_DEDUP_SYSTEM, TOPIC_DEDUP_USER
        from .ai.utils import parse_json_response

        # Build the item list for the prompt
        lines = []
        for i, item in enumerate(items):
            tags = ", ".join(item.ai_tags) if item.ai_tags else "—"
            summary = item.ai_summary or "—"
            lines.append(f"[{i}] {item.title}\n    Tags: {tags}\n    Summary: {summary}")
        items_text = "\n\n".join(lines)

        try:
            ai_client = create_ai_client(self.config.ai)
            response = await ai_client.complete(
                system=TOPIC_DEDUP_SYSTEM,
                user=TOPIC_DEDUP_USER.format(items=items_text),
            )
            result = parse_json_response(response)
            if result is None:
                self.console.print("[yellow]  dedup: could not parse AI response, skipping[/yellow]")
                return items

            duplicate_groups = result.get("duplicates", [])
        except Exception as e:
            self.console.print(f"[yellow]  dedup: AI call failed ({e}), skipping[/yellow]")
            return items

        if not duplicate_groups:
            return items

        # Build a set of indices to drop (all non-primary duplicates)
        drop_indices: set[int] = set()
        for group in duplicate_groups:
            if not isinstance(group, list) or len(group) < 2:
                continue
            primary_idx = group[0]
            if primary_idx < 0 or primary_idx >= len(items):
                continue
            primary = items[primary_idx]
            for dup_idx in group[1:]:
                if not isinstance(dup_idx, int) or dup_idx < 0 or dup_idx >= len(items):
                    continue
                if dup_idx == primary_idx:
                    continue
                dup = items[dup_idx]
                # Merge comments/content from the duplicate into the primary
                if dup.content:
                    if not primary.content or dup.content not in primary.content:
                        label = dup.source_type.value
                        primary.content = (primary.content or "") + f"\n\n--- From {label} ---\n{dup.content}"
                self.console.print(
                    f"   [dim]dedup: keep [{primary_idx}] {primary.title}[/dim]\n"
                    f"   [dim]       drop [{dup_idx}] {dup.title}[/dim]"
                )
                drop_indices.add(dup_idx)

        return [item for i, item in enumerate(items) if i not in drop_indices]

    async def _expand_twitter_discussion(self, items: List[ContentItem]) -> None:
        """Second-stage: fetch reply text for important Twitter items and re-analyze.

        Only runs when sources.twitter.fetch_reply_text is True.
        Bounded by max_tweets_to_expand to control cost.
        """
        tw_cfg = self.config.sources.twitter
        if not tw_cfg or not tw_cfg.enabled or not tw_cfg.fetch_reply_text:
            return

        from .models import SourceType

        twitter_items = [
            item for item in items
            if item.source_type == SourceType.TWITTER
        ][:tw_cfg.max_tweets_to_expand]

        if not twitter_items:
            return

        self.console.print(
            f"💬 Fetching reply text for {len(twitter_items)} Twitter items..."
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            scraper = TwitterScraper(tw_cfg, client)
            expanded = []
            for item in twitter_items:
                try:
                    reply_lines = await scraper.fetch_replies_for_item(item)
                    if TwitterScraper.append_discussion_content(item, reply_lines):
                        expanded.append(item)
                        self.console.print(
                            f"   💬 {len(reply_lines)} replies added to: {item.title[:60]}"
                        )
                except Exception as exc:
                    self.console.print(
                        f"   [yellow]⚠️  Reply fetch failed for {item.id}: {exc}[/yellow]"
                    )

        if not expanded:
            return

        self.console.print(
            f"   Re-analyzing {len(expanded)} Twitter items with reply context...\n"
        )
        ai_client = create_ai_client(self.config.ai)
        analyzer = ContentAnalyzer(ai_client)
        await analyzer.analyze_batch(expanded)

    async def _enrich_important_items(self, items: List[ContentItem]) -> None:
        """Enrich items with background knowledge (2nd AI pass).

        For each item that passed the score threshold, call AI to generate
        background knowledge based on the item's actual content.

        Args:
            items: Important items to enrich (modified in-place)
        """
        if not items:
            return
        if not self.config.filtering.enable_enrichment:
            self.console.print("Skipping enrichment to reduce AI calls\n")
            return

        self.console.print("📚 Enriching with background knowledge...")
        ai_client = create_ai_client(self.config.ai)
        enricher = ContentEnricher(ai_client)
        await enricher.enrich_batch(items)
        self.console.print(f"   Enriched {len(items)} items\n")

    async def _analyze_content(self, items: List[ContentItem]) -> List[ContentItem]:
        """Analyze content items with AI.

        Args:
            items: Items to analyze

        Returns:
            List[ContentItem]: Analyzed items
        """
        self.console.print("🤖 Analyzing content with AI...")

        ai_client = create_ai_client(self.config.ai)
        analyzer = ContentAnalyzer(ai_client)

        return await analyzer.analyze_batch(items)

    async def _generate_summary(
        self,
        items: List[ContentItem],
        date: str,
        total_fetched: int,
        language: str = "en",
    ) -> str:
        """Generate daily summary.

        Args:
            items: Important items to include (already enriched with background/related)
            date: Date string
            total_fetched: Total items fetched
            language: Output language ("en" or "zh")

        Returns:
            str: Markdown summary
        """
        self.console.print("📝 Generating daily summary...")

        summarizer = DailySummarizer()

        return await summarizer.generate_summary(items, date, total_fetched, language=language)
