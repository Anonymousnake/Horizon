#!/usr/bin/env python3
"""Merge layered RSS sources into Horizon data/config.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def load_layered_sources(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources: list[dict] = []
    for layer in data.get("layers", []):
        layer_name = layer.get("name") or layer.get("id") or "未分层"
        for source in layer.get("sources", []):
            category = source.get("category") or "未分类"
            sources.append(
                {
                    "name": source["name"],
                    "url": source["url"],
                    "enabled": source.get("enabled", True),
                    "category": f"{layer_name}-{category}",
                }
            )
    return sources


def merge_sources(config_path: Path, layer_path: Path) -> tuple[int, int, int]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rss_sources = config.setdefault("sources", {}).setdefault("rss", [])

    seen = {normalize_url(item["url"]) for item in rss_sources if item.get("url")}
    added = 0
    skipped = 0

    for source in load_layered_sources(layer_path):
        key = normalize_url(source["url"])
        if key in seen:
            skipped += 1
            continue
        rss_sources.append(source)
        seen.add(key)
        added += 1

    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(rss_sources), added, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/config.json")
    parser.add_argument("--layers", default="data/horizon-rss-layers.json")
    args = parser.parse_args()

    total, added, skipped = merge_sources(Path(args.config), Path(args.layers))
    print(f"rss_total={total} added={added} skipped_existing={skipped}")


if __name__ == "__main__":
    main()
