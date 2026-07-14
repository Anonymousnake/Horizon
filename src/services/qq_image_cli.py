"""Render the latest Horizon summary as a QQ-friendly image and send it."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import shorten

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont


WIDTH = 1080
MARGIN = 54
CARD_PAD = 30
BG = "#f4f6fb"
CARD = "#ffffff"
TEXT = "#182033"
MUTED = "#667085"
BLUE = "#2f6fed"
GREEN = "#14a36c"
GOLD = "#f5a623"
LINE = "#e6eaf2"


@dataclass
class BriefItem:
    category: str
    title: str
    score: str
    url: str
    summary: str
    source: str
    tags: list[str]


@dataclass
class RenderedCard:
    """One QQ image plus the ordered source links rendered on that image."""

    card_id: str
    category: str
    items: list[BriefItem]
    output_path: Path


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)


def _load_fonts() -> dict[str, ImageFont.ImageFont]:
    cjk = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    cjk_bold = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ]
    return {
        "title": _font(cjk_bold, 42),
        "subtitle": _font(cjk, 24),
        "category": _font(cjk_bold, 32),
        "item_title": _font(cjk_bold, 30),
        "body": _font(cjk, 25),
        "meta": _font(cjk, 21),
        "tag": _font(cjk, 20),
    }


def _clean(text: str) -> str:
    text = re.sub(r"<a id=.*?</a>", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_>#]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _latest_summary(data_dir: Path) -> Path:
    summaries = sorted((data_dir / "summaries").glob("horizon-*-zh.md"), key=lambda p: p.stat().st_mtime)
    if not summaries:
        raise FileNotFoundError("No zh summary found in data/summaries")
    return summaries[-1]


def _parse_summary(path: Path) -> tuple[str, str, list[BriefItem]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), "Horizon 每日速递")
    subtitle = next((_clean(line) for line in lines if line.startswith(">")), "")

    items: list[BriefItem] = []
    current_category = "今日要闻"
    sections: list[tuple[str, str]] = []
    section_lines: list[str] = []
    in_details = False
    in_item = False
    for line in lines:
        if line == "---":
            in_details = True
            if in_item and section_lines:
                sections.append((current_category, "\n".join(section_lines)))
            section_lines = []
            in_item = False
            continue
        if not in_details:
            continue
        if line.startswith("## ") and not line.startswith("## ["):
            current_category = _clean(line.lstrip("# "))
            continue
        if line.startswith("## ["):
            if in_item and section_lines:
                sections.append((current_category, "\n".join(section_lines)))
            section_lines = [line]
            in_item = True
            continue
        if in_item:
            section_lines.append(line)
    if in_item and section_lines:
        sections.append((current_category, "\n".join(section_lines)))

    for category, section in sections:
        m = re.search(r"## \[([^\]]+)\]\(([^)]+)\)\s*⭐️?\s*([\d.]+/\d+)", section)
        if not m:
            continue
        raw_title, url, score = m.groups()
        body_lines = [line.strip() for line in section.splitlines()]
        summary = ""
        source = ""
        tags: list[str] = []
        for idx, line in enumerate(body_lines):
            if idx <= 1 or not line or line.startswith("<") or line.startswith("##"):
                continue
            if line.startswith("rss ·") or line.startswith("rss "):
                source = _clean(line)
                continue
            if line.startswith("**标签**"):
                tags = [part.strip(" #`，,") for part in re.split(r"[,，]", line.split(":", 1)[-1]) if part.strip()]
                continue
            if not summary:
                summary = _clean(line)
        items.append(
            BriefItem(
                category=category,
                title=_clean(raw_title),
                score=score,
                url=url,
                summary=summary,
                source=source,
                tags=tags[:4],
            )
        )
    return title, subtitle, items


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int | None = None) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        trial = current + char
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = char
        if max_lines and len(lines) >= max_lines:
            lines[-1] = shorten(lines[-1], width=max(8, len(lines[-1]) - 1), placeholder="…")
            return lines
    if current:
        lines.append(current)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = shorten(lines[-1], width=max(8, len(lines[-1]) - 1), placeholder="…")
    return lines


def _line_height(font: ImageFont.ImageFont) -> int:
    bbox = font.getbbox("国")
    return bbox[3] - bbox[1] + 10


def _measure_item(draw: ImageDraw.ImageDraw, item: BriefItem, fonts: dict[str, ImageFont.ImageFont], width: int) -> int:
    inner = width - CARD_PAD * 2
    title_lines = _wrap(draw, item.title, fonts["item_title"], inner - 92, max_lines=2)
    summary_lines = _wrap(draw, item.summary, fonts["body"], inner, max_lines=3)
    h = CARD_PAD + len(title_lines) * _line_height(fonts["item_title"]) + 16
    h += len(summary_lines) * _line_height(fonts["body"]) + 16
    h += _line_height(fonts["meta"])
    if item.tags:
        h += 34
    return h + CARD_PAD


def _render_items_image(title: str, subtitle: str, items: list[BriefItem], output_path: Path) -> Path:
    fonts = _load_fonts()
    probe = Image.new("RGB", (WIDTH, 100), BG)
    draw = ImageDraw.Draw(probe)
    card_width = WIDTH - MARGIN * 2

    height = MARGIN + 86 + 34 + 28
    last_category = None
    for item in items:
        if item.category != last_category:
            height += 58
            last_category = item.category
        height += _measure_item(draw, item, fonts, card_width) + 20
    height += MARGIN

    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    y = MARGIN
    draw.text((MARGIN, y), title, font=fonts["title"], fill=TEXT)
    y += 62
    draw.text((MARGIN, y), subtitle, font=fonts["subtitle"], fill=MUTED)
    y += 58

    last_category = None
    for index, item in enumerate(items, start=1):
        if item.category != last_category:
            draw.text((MARGIN, y), item.category, font=fonts["category"], fill=TEXT)
            count = sum(1 for candidate in items if candidate.category == item.category)
            count_text = f"{count} 条"
            count_w = draw.textlength(count_text, font=fonts["meta"])
            draw.text((WIDTH - MARGIN - count_w, y + 8), count_text, font=fonts["meta"], fill=MUTED)
            y += 50
            last_category = item.category
        card_h = _measure_item(draw, item, fonts, card_width)
        x = MARGIN
        draw.rounded_rectangle((x, y, x + card_width, y + card_h), radius=22, fill=CARD)
        cx = x + CARD_PAD
        cy = y + CARD_PAD
        badge = f"{index}"
        draw.rounded_rectangle((cx, cy + 3, cx + 42, cy + 45), radius=14, fill=BLUE)
        draw.text((cx + 14, cy + 8), badge, font=fonts["body"], fill="white")
        score_text = f"⭐ {item.score}"
        score_w = draw.textlength(score_text, font=fonts["meta"])
        draw.text((x + card_width - CARD_PAD - score_w, cy + 10), score_text, font=fonts["meta"], fill=GOLD)

        title_x = cx + 58
        title_width = card_width - CARD_PAD * 2 - 58 - int(score_w) - 18
        for line in _wrap(draw, item.title, fonts["item_title"], title_width, max_lines=2):
            draw.text((title_x, cy), line, font=fonts["item_title"], fill=TEXT)
            cy += _line_height(fonts["item_title"])
        cy += 10
        for line in _wrap(draw, item.summary, fonts["body"], card_width - CARD_PAD * 2, max_lines=3):
            draw.text((cx, cy), line, font=fonts["body"], fill=TEXT)
            cy += _line_height(fonts["body"])
        cy += 10
        draw.text((cx, cy), item.source, font=fonts["meta"], fill=MUTED)
        cy += _line_height(fonts["meta"])
        if item.tags:
            tag_x = cx
            for tag in item.tags:
                label = f"#{tag}"
                tw = draw.textlength(label, font=fonts["tag"]) + 24
                draw.rounded_rectangle((tag_x, cy, tag_x + tw, cy + 30), radius=13, fill="#eef4ff")
                draw.text((tag_x + 12, cy + 3), label, font=fonts["tag"], fill=GREEN)
                tag_x += tw + 10
        y += card_h + 20

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=92)
    return output_path


def render_image(summary_path: Path, output_path: Path) -> Path:
    title, subtitle, items = _parse_summary(summary_path)
    return _render_items_image(title, subtitle, items, output_path)


def _summary_date_key(summary_path: Path) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", summary_path.name)
    return match.group(0) if match else datetime.now().strftime("%Y-%m-%d")


def _card_id(summary_path: Path, index: int, category: str, items: list[BriefItem]) -> str:
    material = "\n".join(f"{item.title}\t{item.url}" for item in items)
    digest = hashlib.sha256(f"{category}\n{material}".encode("utf-8")).hexdigest()[:8]
    return f"{_summary_date_key(summary_path).replace('-', '')}-{index:02d}-{digest}"


def render_category_cards(summary_path: Path, output_path: Path) -> list[RenderedCard]:
    title, subtitle, items = _parse_summary(summary_path)
    grouped: dict[str, list[BriefItem]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)
    if len(grouped) <= 1:
        category = next(iter(grouped), "daily-brief")
        return [
            RenderedCard(
                card_id=_card_id(summary_path, 1, category, items),
                category=category,
                items=items,
                output_path=_render_items_image(title, subtitle, items, output_path),
            ),
        ]

    cards: list[RenderedCard] = []
    for index, (category, category_items) in enumerate(grouped.items(), start=1):
        safe_category = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", category).strip("-")
        category_output = output_path.with_name(
            f"{output_path.stem}-{index:02d}-{safe_category}{output_path.suffix}"
        )
        category_subtitle = f"{subtitle} · {category} {len(category_items)} 条"
        cards.append(
            RenderedCard(
                card_id=_card_id(summary_path, index, category, category_items),
                category=category,
                items=category_items,
                output_path=_render_items_image(title, category_subtitle, category_items, category_output),
            ),
        )
    return cards


def render_category_images(summary_path: Path, output_path: Path) -> list[Path]:
    """Backward-compatible path-only API used by external callers."""
    return [card.output_path for card in render_category_cards(summary_path, output_path)]


def _group_id_from_umo(umo: str) -> str:
    parts = umo.rsplit(":", 1)
    return parts[-1].strip() if len(parts) == 2 and ":GroupMessage:" in umo else ""


def _card_manifest_path(summary_path: Path) -> Path:
    return summary_path.parent / f"horizon-link-cards-{_summary_date_key(summary_path)}.json"


def _write_link_card_manifest(summary_path: Path, cards: list[RenderedCard], umo: str) -> Path:
    """Persist the exact source URLs shown on each sent image for reply lookups."""
    manifest_path = _card_manifest_path(summary_path)
    previous_cards: list[dict[str, object]] = []
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("cards"), list):
                previous_cards = [card for card in old["cards"] if isinstance(card, dict)]
        except (OSError, json.JSONDecodeError):
            pass

    group_id = _group_id_from_umo(umo)
    current_cards = [
        {
            "card_id": card.card_id,
            "group_id": group_id,
            "category": card.category,
            "summary_file": summary_path.name,
            "image_file": card.output_path.name,
            "items": [
                {"index": index, "title": item.title, "url": item.url}
                for index, item in enumerate(card.items, start=1)
            ],
        }
        for card in cards
    ]
    current_ids = {str(card["card_id"]) for card in current_cards}
    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cards": [card for card in previous_cards if str(card.get("card_id") or "") not in current_ids] + current_cards,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    _cleanup_old_link_card_manifests(manifest_path.parent)
    return manifest_path


def _cleanup_old_link_card_manifests(directory: Path) -> None:
    retention_days = max(1, int(os.getenv("HORIZON_LINK_CARD_RETENTION_DAYS", "14")))
    cutoff = datetime.now() - timedelta(days=retention_days)
    for path in directory.glob("horizon-link-cards-*.json"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError:
            continue


def _caption_for_card(card: RenderedCard) -> str:
    return (
        f"Horizon {card.category}\n"
        "回复本条发送序号获取原文链接，例如：1 3 5\n"
        f"[HZN:{card.card_id}]"
    )


async def _send_image(base_url: str, api_key: str, umo: str, image_path: Path, text: str = "Horizon 每日速递") -> None:
    headers = {"X-API-Key": api_key}
    async with httpx.AsyncClient(timeout=60) as client:
        with image_path.open("rb") as fh:
            files = {"file": (image_path.name, fh, "image/png")}
            upload = await client.post(f"{base_url.rstrip('/')}/api/v1/file", headers=headers, files=files)
        upload.raise_for_status()
        data = upload.json()
        attachment_id = data.get("data", {}).get("attachment_id")
        if not attachment_id:
            raise RuntimeError(f"Upload did not return attachment_id: {data}")
        payload = {
            "umo": umo,
            "message": [
                {"type": "plain", "text": text},
                {"type": "image", "attachment_id": attachment_id},
            ],
        }
        resp = await client.post(
            f"{base_url.rstrip('/')}/api/v1/im/message",
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        resp.raise_for_status()
        data = resp.json()
        print(json.dumps(data, ensure_ascii=False))
        if data.get("status") != "ok":
            raise RuntimeError(data.get("message") or resp.text)


async def _send_plain(base_url: str, api_key: str, umo: str, text: str) -> None:
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    payload = {"umo": umo, "message": text}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/api/v1/im/message",
            headers=headers,
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        resp.raise_for_status()
        data = resp.json()
        print(json.dumps(data, ensure_ascii=False))
        if data.get("status") != "ok":
            raise RuntimeError(data.get("message") or resp.text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render and send latest Horizon zh summary as an image.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--summary")
    parser.add_argument("--output", default="data/summaries/horizon-latest-zh.png")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--base-url", default=os.getenv("ASTRBOT_BASE_URL", "http://127.0.0.1:6185"))
    parser.add_argument("--umo", default=os.getenv("HORIZON_NOTIFY_UMO", "napcat_onebot_v11:GroupMessage:951944306"))
    args = parser.parse_args()

    load_dotenv()
    summary_path = Path(args.summary) if args.summary else _latest_summary(Path(args.data_dir))
    output_path = Path(args.output)
    cards = render_category_cards(summary_path, output_path) if args.send else []
    output_paths = [card.output_path for card in cards] if args.send else [render_image(summary_path, output_path)]
    for rendered_path in output_paths:
        print(rendered_path)

    if args.send:
        api_key = os.getenv("ASTRBOT_API_KEY")
        if not api_key:
            raise RuntimeError("ASTRBOT_API_KEY is required when --send is used")
        manifest_path = _write_link_card_manifest(summary_path, cards, args.umo)
        print(f"link_card_manifest={manifest_path}")
        if len(cards) > 1:
            asyncio.run(_send_plain(args.base_url, api_key, args.umo, f"Horizon 每日速递：{len(cards)} 个分类"))
        for card in cards:
            try:
                asyncio.run(_send_image(args.base_url, api_key, args.umo, card.output_path, _caption_for_card(card)))
            except Exception as exc:
                print(f"send_failed {card.output_path}: {exc}")


if __name__ == "__main__":
    main()
