#!/usr/bin/env python3
"""Open marketplace pages with a persistent Playwright profile for manual login.

Run this under Xvfb/noVNC or headless screenshot mode. The helper does not bypass
captchas or login controls; it only keeps a normal browser session open and saves
periodic screenshots so the user can complete QR-based login when available.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def pick_url(config: dict[str, Any], platform: str | None, url: str | None) -> str:
    if url:
        return url
    for product in config.get("products", []):
        for source in product.get("sources", []):
            if platform is None or source.get("platform") == platform:
                return str(source["url"])
    raise ValueError("no matching URL found in config")


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep a persistent browser session open for marketplace login.")
    parser.add_argument("--config", default="browser_market_config.json")
    parser.add_argument("--platform", help="Platform name from config, for example: 淘宝搜索")
    parser.add_argument("--url", help="Direct URL to open instead of picking from config.")
    parser.add_argument("--seconds", type=int, default=240)
    parser.add_argument("--screenshot", default="login_status.png")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    target_url = pick_url(config, args.platform, args.url)

    user_data_dir = Path(str(config.get("user_data_dir", "browser_profile")))
    if not user_data_dir.is_absolute():
        user_data_dir = config_path.parent / user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = Path(args.screenshot)
    if not screenshot_path.is_absolute():
        screenshot_path = config_path.parent / screenshot_path

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=args.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1365, "height": 900},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        deadline = time.time() + args.seconds
        print(f"opened {target_url}")
        print(f"profile {user_data_dir}")
        print(f"screenshot {screenshot_path}")
        while time.time() < deadline:
            try:
                page.screenshot(path=str(screenshot_path), full_page=False)
                title = page.title()
                body = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())
                print(f"title={title!r} url={page.url!r} text={body[:160]!r}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"snapshot_error={type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
