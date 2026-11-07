#!/usr/bin/env python3
"""Keep a visible Playwright browser open on an X display for manual login."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def pick_urls(config: dict, platforms: list[str], limit: int) -> list[str]:
    urls: list[str] = []
    for product in config.get("products", []):
        for source in product.get("sources", []):
            if not platforms or source.get("platform") in platforms:
                url = str(source["url"])
                if url not in urls:
                    urls.append(url)
                    if limit > 0 and len(urls) >= limit:
                        return urls
    if not urls:
        raise ValueError("no URLs matched the requested platform list")
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="Open persistent marketplace browser tabs for manual login.")
    parser.add_argument("--config", default="browser_market_config.json")
    parser.add_argument("--platform", action="append", default=[], help="Open URLs for this platform. Repeatable.")
    parser.add_argument("--limit", type=int, default=1, help="Maximum number of URLs to open. Use 0 for no limit.")
    parser.add_argument("--seconds", type=int, default=3600)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    urls = pick_urls(config, args.platform, args.limit)

    user_data_dir = Path(str(config.get("user_data_dir", "browser_profile")))
    if not user_data_dir.is_absolute():
        user_data_dir = config_path.parent / user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--disable-restore-session-state",
            ],
            viewport={"width": 1024, "height": 720},
            locale="zh-CN",
        )
        first = context.pages[0] if context.pages else context.new_page()
        for index, url in enumerate(urls):
            page = first if index == 0 else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print(f"opened {url}", flush=True)
            time.sleep(1)
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            time.sleep(5)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
