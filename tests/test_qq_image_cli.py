from __future__ import annotations

import json
from pathlib import Path

from src.services.qq_image_cli import BriefItem
from src.services.qq_image_cli import RenderedCard
from src.services.qq_image_cli import _caption_for_card
from src.services.qq_image_cli import _card_id
from src.services.qq_image_cli import _write_link_card_manifest


def _item(title: str, url: str) -> BriefItem:
    return BriefItem(
        category="今日要闻",
        title=title,
        score="8.0/10",
        url=url,
        summary="summary",
        source="source",
        tags=[],
    )


def test_link_card_manifest_keeps_exact_ordered_urls_and_previous_cards(tmp_path: Path) -> None:
    summary = tmp_path / "horizon-2026-07-14-zh.md"
    summary.write_text("# test\n", encoding="utf-8")
    first_items = [_item("one", "https://example.com/one"), _item("two", "https://example.com/two")]
    first = RenderedCard(
        card_id=_card_id(summary, 1, "今日要闻", first_items),
        category="今日要闻",
        items=first_items,
        output_path=tmp_path / "first.png",
    )
    _write_link_card_manifest(summary, [first], "napcat_onebot_v11:GroupMessage:951944306")

    second_items = [_item("three", "https://example.com/three")]
    second = RenderedCard(
        card_id=_card_id(summary, 2, "科技", second_items),
        category="科技",
        items=second_items,
        output_path=tmp_path / "second.png",
    )
    manifest_path = _write_link_card_manifest(summary, [second], "napcat_onebot_v11:GroupMessage:951944306")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert [card["card_id"] for card in payload["cards"]] == [first.card_id, second.card_id]
    assert payload["cards"][0]["group_id"] == "951944306"
    assert payload["cards"][0]["items"] == [
        {"index": 1, "title": "one", "url": "https://example.com/one"},
        {"index": 2, "title": "two", "url": "https://example.com/two"},
    ]


def test_horizon_card_caption_contains_a_stable_reply_marker(tmp_path: Path) -> None:
    summary = tmp_path / "horizon-2026-07-14-zh.md"
    item = _item("one", "https://example.com/one")
    card = RenderedCard(
        card_id=_card_id(summary, 1, "今日要闻", [item]),
        category="今日要闻",
        items=[item],
        output_path=tmp_path / "card.png",
    )

    caption = _caption_for_card(card)

    assert "回复本条发送序号" in caption
    assert f"[HZN:{card.card_id}]" in caption
