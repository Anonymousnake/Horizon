"""Render the latest Horizon summary as a QQ-friendly image and send it."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
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
    title: str
    score: str
    url: str
    summary: str
    source: str
    tags: list[str]


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
    sections = re.split(r"\n---\n", text)
    for section in sections:
        if "\n## [" not in section and not section.startswith("## ["):
            continue
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


def render_image(summary_path: Path, output_path: Path) -> Path:
    title, subtitle, items = _parse_summary(summary_path)
    fonts = _load_fonts()
    probe = Image.new("RGB", (WIDTH, 100), BG)
    draw = ImageDraw.Draw(probe)
    card_width = WIDTH - MARGIN * 2

    height = MARGIN + 86 + 34 + 28
    for item in items:
        height += _measure_item(draw, item, fonts, card_width) + 20
    height += MARGIN

    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    y = MARGIN
    draw.text((MARGIN, y), title, font=fonts["title"], fill=TEXT)
    y += 62
    draw.text((MARGIN, y), subtitle, font=fonts["subtitle"], fill=MUTED)
    y += 58

    for index, item in enumerate(items, start=1):
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


async def _send_image(base_url: str, api_key: str, umo: str, image_path: Path) -> None:
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
                {"type": "plain", "text": "Horizon 每日速递"},
                {"type": "image", "attachment_id": attachment_id},
            ],
        }
        resp = await client.post(
            f"{base_url.rstrip('/')}/api/v1/im/message",
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        resp.raise_for_status()
        print(resp.text)


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
    output_path = render_image(summary_path, Path(args.output))
    print(output_path)

    if args.send:
        api_key = os.getenv("ASTRBOT_API_KEY")
        if not api_key:
            raise RuntimeError("ASTRBOT_API_KEY is required when --send is used")
        asyncio.run(_send_image(args.base_url, api_key, args.umo, output_path))


if __name__ == "__main__":
    main()
