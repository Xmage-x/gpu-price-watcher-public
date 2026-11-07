#!/usr/bin/env python3
"""Rendered marketplace watcher for dynamic search pages.

This companion watcher uses Playwright to render marketplace search pages, then
extracts candidate listing text, links, and prices from the visible DOM. It is
intended for monitoring search result pages for new listings, not bypassing
captchas, login walls, or platform risk controls.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse


PRICE_RE = re.compile(r"(?:￥|¥|CNY\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
PDD_GOODS_RE = re.compile(
    r'"goodsID"\s*:\s*(?P<id>\d+).*?'
    r'"goodsName"\s*:\s*"(?P<name>(?:\\.|[^"\\])*)".*?'
    r'"priceInfo"\s*:\s*\{(?P<price_info>.*?)\}',
    re.S,
)
PDD_PRICE_RE = re.compile(r'"price"\s*:\s*"?(?P<price>[0-9]+(?:\.[0-9]+)?)"?')


@dataclasses.dataclass
class Candidate:
    product_name: str
    platform: str
    query: str
    title: str
    url: str
    price: Decimal | None
    text: str
    item_key: str


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def parse_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    text = value.replace(",", "").replace("，", "").replace("￥", "").replace("¥", "")
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def compact(text: str, limit: int = 260) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        config = json.load(fh)
    if "products" not in config:
        raise ValueError("config must contain products")
    return config


def load_backoff(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): float(value) for key, value in data.items()}


def save_backoff(path: Path, data: dict[str, float]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_items (
            item_key TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            query TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            first_price TEXT,
            last_price TEXT,
            text TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            item_key TEXT NOT NULL,
            product_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            query TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            price TEXT,
            text TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_alerts (
            alert_key TEXT PRIMARY KEY,
            alerted_at TEXT NOT NULL,
            item_key TEXT NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def price_from_text(text: str) -> Decimal | None:
    prices = [parse_decimal(match.group(1)) for match in PRICE_RE.finditer(text)]
    prices = [price for price in prices if price is not None and Decimal("10") <= price <= Decimal("999999")]
    if not prices:
        return None
    return min(prices)


def canonical_item_url(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "taobao.com" in host and path.endswith("/item.htm") and params.get("id"):
        return f"https://item.taobao.com/item.htm?id={params['id'][0]}"
    if "goofish.com" in host and path.endswith("/item") and params.get("id"):
        return f"https://www.goofish.com/item?id={params['id'][0]}"
    if "yangkeduo.com" in host and params.get("goods_id"):
        return f"https://mobile.yangkeduo.com/goods.html?goods_id={params['goods_id'][0]}"
    return url.split("#", 1)[0]


def is_market_item_url(url: str, platform: str) -> bool:
    if not url or url.startswith("javascript:"):
        return False
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    platform_text = platform.lower()
    if "闲鱼" in platform or "goofish" in platform_text:
        return "goofish.com" in host and path.endswith("/item") and bool(params.get("id"))
    if "淘宝" in platform or "taobao" in platform_text:
        return "taobao.com" in host and path.endswith("/item.htm") and bool(params.get("id"))
    if "拼多多" in platform or "yangkeduo" in platform_text:
        return ("yangkeduo.com" in host or "pinduoduo.com" in host) and bool(params.get("goods_id"))
    return True


def item_key(url: str, title: str, platform: str) -> str:
    stable = canonical_item_url(url) if url and not url.startswith("javascript:") else f"{platform}:{title}"
    return hashlib.sha256(stable.encode("utf-8", errors="ignore")).hexdigest()[:24]


def include_ok(text: str, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    low = text.lower()
    if include_keywords and not all(keyword.lower() in low for keyword in include_keywords):
        return False
    if any(keyword.lower() in low for keyword in exclude_keywords):
        return False
    return True


def save_candidate(conn: sqlite3.Connection, candidate: Candidate) -> bool:
    checked_at = now_utc()
    existing = conn.execute(
        "SELECT last_price FROM market_items WHERE item_key = ?",
        (candidate.item_key,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO market_observations
            (checked_at, item_key, product_name, platform, query, title, url, price, text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checked_at,
            candidate.item_key,
            candidate.product_name,
            candidate.platform,
            candidate.query,
            candidate.title,
            candidate.url,
            str(candidate.price) if candidate.price is not None else None,
            candidate.text,
        ),
    )
    conn.execute(
        """
        INSERT INTO market_items
            (item_key, product_name, platform, query, title, url, first_seen_at, last_seen_at, first_price, last_price, text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            last_price = excluded.last_price,
            text = excluded.text,
            title = excluded.title,
            url = excluded.url
        """,
        (
            candidate.item_key,
            candidate.product_name,
            candidate.platform,
            candidate.query,
            candidate.title,
            candidate.url,
            checked_at,
            checked_at,
            str(candidate.price) if candidate.price is not None else None,
            str(candidate.price) if candidate.price is not None else None,
            candidate.text,
        ),
    )
    conn.commit()
    return existing is None


def already_alerted(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM market_alerts WHERE alert_key = ?", (key,)).fetchone() is not None


def mark_alerted(conn: sqlite3.Connection, key: str, candidate: Candidate, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO market_alerts (alert_key, alerted_at, item_key, reason) VALUES (?, ?, ?, ?)",
        (key, now_utc(), candidate.item_key, reason),
    )
    conn.commit()


def alert_reasons(product: dict[str, Any], candidate: Candidate, is_new: bool) -> list[str]:
    reasons: list[str] = []
    min_alert = parse_decimal(str(product.get("min_alert_price"))) if product.get("min_alert_price") is not None else None
    if min_alert is not None and candidate.price is not None and candidate.price < min_alert:
        return reasons
    if product.get("alert_on_new_listing") and is_new:
        if product.get("new_listing_requires_price", True) and candidate.price is None:
            pass
        else:
            max_new = parse_decimal(str(product.get("max_new_listing_price"))) if product.get("max_new_listing_price") is not None else None
            if max_new is None or candidate.price is None or candidate.price <= max_new:
                reasons.append("发现新上架候选")
    if product.get("alert_on_available") and is_new:
        reasons.append("缺货型号出现新候选")
    target = parse_decimal(str(product.get("target_price"))) if product.get("target_price") is not None else None
    if target is not None and candidate.price is not None and candidate.price <= target:
        reasons.append(f"价格 {candidate.price} CNY 低于目标价 {target} CNY")
    return reasons


def send_stdout(alerts: list[tuple[Candidate, str]]) -> None:
    if not alerts:
        return
    print("显卡搜索结果提醒")
    for candidate, reason in alerts:
        price = f"{candidate.price} CNY" if candidate.price is not None else "未知价格"
        print()
        print(candidate.product_name)
        print(f"平台：{candidate.platform}")
        print(f"查询：{candidate.query}")
        print(f"价格：{price}")
        print(f"原因：{reason}")
        print(f"标题：{candidate.title}")
        print(f"链接：{candidate.url}")


def collect_from_page(page: Any, product: dict[str, Any], source: dict[str, Any]) -> list[Candidate]:
    platform = str(source["platform"])
    if "拼多多" in platform or "yangkeduo.com" in str(source.get("url", "")):
        return collect_from_pdd_page(page, product, source)

    return collect_from_anchor_page(page, product, source)


def collect_from_anchor_page(page: Any, product: dict[str, Any], source: dict[str, Any]) -> list[Candidate]:
    platform = str(source["platform"])
    query = str(source.get("query") or source.get("url") or "")
    base_url = str(source["url"])
    include_keywords = [str(k) for k in product.get("include_keywords", []) + source.get("include_keywords", [])]
    exclude_keywords = [str(k) for k in product.get("exclude_keywords", []) + source.get("exclude_keywords", [])]

    anchors = page.locator("a").evaluate_all(
        """
        els => els.slice(0, 500).map(a => {
          const rect = a.getBoundingClientRect();
          const parent = a.closest('li, [class*=item], [class*=card], [class*=goods], [class*=product], div') || a;
          return {
            href: a.href || a.getAttribute('href') || '',
            text: (parent.innerText || a.innerText || '').slice(0, 700),
            anchorText: (a.innerText || '').slice(0, 260),
            visible: rect.width > 0 && rect.height > 0
          };
        })
        """
    )
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for anchor in anchors:
        text = compact(str(anchor.get("text") or anchor.get("anchorText") or ""))
        if not text or not anchor.get("visible"):
            continue
        if not include_ok(text, include_keywords, exclude_keywords):
            continue
        href = str(anchor.get("href") or "")
        if href:
            href = urljoin(base_url, href)
        if not is_market_item_url(href, platform):
            continue
        title = compact(str(anchor.get("anchorText") or text), 120) or text[:120]
        price = price_from_text(text)
        key = item_key(href, title, platform)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            Candidate(
                product_name=str(product["name"]),
                platform=platform,
                query=query,
                title=title,
                url=href,
                price=price,
                text=text,
                item_key=key,
            )
        )
    return candidates


def pdd_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("price", "normalPrice", "normal_price", "groupPrice", "group_price"):
            price = pdd_price(value.get(key))
            if price is not None:
                return price
        return None
    return parse_decimal(str(value))


def pdd_title(item: dict[str, Any]) -> str:
    for key in ("goodsName", "goods_name", "shortName", "short_name", "title", "name"):
        value = item.get(key)
        if value:
            return compact(str(value), 160)
    return ""


def pdd_url(item: dict[str, Any]) -> str:
    goods_id = item.get("goodsID") or item.get("goods_id") or item.get("goodsId")
    link = str(item.get("linkURL") or item.get("link_url") or item.get("url") or "")
    if link:
        link = urljoin("https://mobile.yangkeduo.com/", link)
        parsed = urlparse(link)
        params = parse_qs(parsed.query)
        if params.get("goods_id"):
            return f"https://mobile.yangkeduo.com/goods.html?goods_id={params['goods_id'][0]}"
    if goods_id:
        return f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"
    return ""


def pdd_text(item: dict[str, Any]) -> str:
    parts = [
        pdd_title(item),
        str(item.get("salesTip") or item.get("sales_tip") or ""),
        str(item.get("mallName") or item.get("mall_name") or item.get("mallNameTag") or ""),
    ]
    price = pdd_price(item.get("priceInfo") or item)
    if price is not None:
        parts.append(f"¥ {price}")
    return compact(" ".join(part for part in parts if part), 700)


def pdd_items_from_page_state(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
            const roots = [
                window.rawData,
                window.__INITIAL_STATE__,
                window.__SSR_DATA__,
                window.__STORE__,
                window.__PDD_DATA__,
                window.leo && window.leo.data,
                window.leo,
            ].filter(Boolean);
            const out = [];
            const seen = new Set();
            const visited = new Set();
            function pick(obj) {
                const goodsID = obj.goodsID || obj.goods_id || obj.goodsId;
                const linkURL = obj.linkURL || obj.link_url || obj.url;
                const goodsName = obj.goodsName || obj.goods_name || obj.shortName || obj.short_name || obj.title || obj.name;
                if (!(goodsID || linkURL) || !goodsName) return;
                const key = String(goodsID || linkURL);
                if (seen.has(key)) return;
                seen.add(key);
                out.push({
                    goodsID,
                    linkURL,
                    goodsName,
                    shortName: obj.shortName || obj.short_name,
                    priceInfo: obj.priceInfo || obj.price_info || obj,
                    salesTip: obj.salesTip || obj.sales_tip || obj.sales || '',
                    mallName: obj.mallName || obj.mall_name || '',
                });
            }
            function walk(value, depth) {
                if (!value || depth > 8 || out.length >= 120) return;
                if (typeof value !== 'object') return;
                if (visited.has(value)) return;
                visited.add(value);
                if (Array.isArray(value)) {
                    for (const item of value) walk(item, depth + 1);
                    return;
                }
                pick(value);
                for (const key of Object.keys(value)) {
                    if (/dom|node|element|parent|window|document/i.test(key)) continue;
                    walk(value[key], depth + 1);
                }
            }
            for (const root of roots) walk(root, 0);
            return out;
        }
        """
    )


def pdd_items_from_html(content: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in PDD_GOODS_RE.finditer(content):
        goods_id = match.group("id")
        if goods_id in seen:
            continue
        seen.add(goods_id)
        try:
            name = json.loads(f'"{match.group("name")}"')
        except json.JSONDecodeError:
            name = html.unescape(match.group("name"))
        price_match = PDD_PRICE_RE.search(match.group("price_info"))
        price_info = {"price": price_match.group("price")} if price_match else {}
        items.append(
            {
                "goodsID": goods_id,
                "goodsName": name,
                "priceInfo": price_info,
                "linkURL": f"goods.html?goods_id={goods_id}",
            }
        )
    return items


def pdd_items_from_any(value: Any, limit: int = 160) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    visited: set[int] = set()

    def pick(obj: dict[str, Any]) -> None:
        goods_id = obj.get("goodsID") or obj.get("goods_id") or obj.get("goodsId")
        link = obj.get("linkURL") or obj.get("link_url") or obj.get("url")
        title = pdd_title(obj)
        if not (goods_id or link) or not title:
            return
        key = str(goods_id or link)
        if key in seen:
            return
        seen.add(key)
        items.append(
            {
                "goodsID": goods_id,
                "linkURL": link,
                "goodsName": title,
                "shortName": obj.get("shortName") or obj.get("short_name"),
                "priceInfo": obj.get("priceInfo") or obj.get("price_info") or obj,
                "salesTip": obj.get("salesTip") or obj.get("sales_tip") or obj.get("sales") or "",
                "mallName": obj.get("mallName") or obj.get("mall_name") or obj.get("shopName") or "",
            }
        )

    def walk(obj: Any, depth: int) -> None:
        if obj is None or depth > 10 or len(items) >= limit:
            return
        if isinstance(obj, dict):
            ident = id(obj)
            if ident in visited:
                return
            visited.add(ident)
            pick(obj)
            for child in obj.values():
                walk(child, depth + 1)
        elif isinstance(obj, list):
            for child in obj:
                walk(child, depth + 1)

    walk(value, 0)
    return items


def collect_from_pdd_page(page: Any, product: dict[str, Any], source: dict[str, Any]) -> list[Candidate]:
    platform = str(source["platform"])
    query = str(source.get("query") or source.get("url") or "")
    include_keywords = [str(k) for k in product.get("include_keywords", []) + source.get("include_keywords", [])]
    exclude_keywords = [str(k) for k in product.get("exclude_keywords", []) + source.get("exclude_keywords", [])]

    raw_items = list(getattr(page, "_pdd_network_items", []) or [])
    raw_items.extend(pdd_items_from_page_state(page))
    if not raw_items:
        raw_items = pdd_items_from_html(page.content())

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for item in raw_items:
        text = pdd_text(item)
        title = pdd_title(item) or compact(text, 120)
        url = pdd_url(item)
        if not text or not include_ok(text, include_keywords, exclude_keywords):
            continue
        price = pdd_price(item.get("priceInfo") or item) or price_from_text(text)
        key = item_key(url, title, platform)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            Candidate(
                product_name=str(product["name"]),
                platform=platform,
                query=query,
                title=title,
                url=url,
                price=price,
                text=text,
                item_key=key,
            )
        )
    return candidates


def run_once(config_path: Path, verbose: bool) -> int:
    from playwright.sync_api import sync_playwright

    config = load_config(config_path)
    db_path = Path(config.get("database", "market_prices.sqlite3"))
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path
    conn = init_db(db_path)
    backoff_path = config_path.parent / "browser_market_backoff.json"
    backoff = load_backoff(backoff_path)
    alerts: list[tuple[Candidate, str]] = []

    with sync_playwright() as p:
        context_options = dict(
            viewport={"width": 1365, "height": 900},
            locale="zh-CN",
        )
        if config.get("user_agent"):
            context_options["user_agent"] = str(config["user_agent"])
        launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        user_data_dir = config.get("user_data_dir")
        browser = None
        if user_data_dir:
            profile_path = Path(str(user_data_dir))
            if not profile_path.is_absolute():
                profile_path = config_path.parent / profile_path
            profile_path.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(
                str(profile_path),
                headless=bool(config.get("headless", True)),
                args=launch_args,
                **context_options,
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = p.chromium.launch(headless=bool(config.get("headless", True)), args=launch_args)
            context = browser.new_context(**context_options)
            page = context.new_page()

        setattr(page, "_pdd_network_items", [])

        def capture_pdd_response(response: Any) -> None:
            url = str(getattr(response, "url", ""))
            if "yangkeduo.com" not in url and "pinduoduo.com" not in url:
                return
            content_type = str(response.headers.get("content-type", ""))
            if "json" not in content_type and "/api/" not in url and "/proxy/" not in url:
                return
            try:
                data = response.json()
            except Exception:
                return
            items = pdd_items_from_any(data)
            if not items:
                return
            existing = list(getattr(page, "_pdd_network_items", []) or [])
            existing.extend(items)
            setattr(page, "_pdd_network_items", existing)

        page.on("response", capture_pdd_response)

        for product in config["products"]:
            for source in product.get("sources", []):
                url = str(source["url"])
                platform = str(source["platform"])
                try:
                    is_pdd = "拼多多" in platform or "yangkeduo.com" in url
                    backoff_key = f"{platform}:{url}"
                    now_ts = time.time()
                    if is_pdd and backoff.get(backoff_key, 0) > now_ts:
                        if verbose:
                            print(f"{product['name']} | {platform} | candidates=0 backoff=system_busy")
                        continue
                    if is_pdd:
                        setattr(page, "_pdd_network_items", [])
                        page.set_viewport_size({"width": 390, "height": 844})
                    else:
                        page.set_viewport_size({"width": 1365, "height": 900})
                    page.goto(url, wait_until="domcontentloaded", timeout=int(config.get("navigation_timeout_ms", 30000)))
                    page.wait_for_timeout(int(source.get("settle_ms", config.get("settle_ms", 4500))))
                    if is_pdd:
                        body_text = compact(page.locator("body").inner_text(timeout=5000), 1000)
                        if "系统繁忙" in body_text or "请稍后再试" in body_text:
                            cooldown = int(config.get("pdd_backoff_seconds", 3600))
                            backoff[backoff_key] = time.time() + cooldown
                            save_backoff(backoff_path, backoff)
                            if verbose:
                                print(f"{product['name']} | {platform} | candidates=0 backoff=system_busy")
                            continue
                    for _ in range(int(source.get("scrolls", config.get("scrolls", 2)))):
                        page.mouse.wheel(0, 1200)
                        page.wait_for_timeout(1200)
                    candidates = collect_from_page(page, product, source)
                    if verbose:
                        print(f"{product['name']} | {platform} | candidates={len(candidates)}")
                    for candidate in candidates:
                        is_new = save_candidate(conn, candidate)
                        for reason in alert_reasons(product, candidate, is_new):
                            alert_key = f"{candidate.item_key}:{reason}"
                            if not already_alerted(conn, alert_key):
                                alerts.append((candidate, reason))
                                mark_alerted(conn, alert_key, candidate, reason)
                except Exception as exc:  # noqa: BLE001
                    print(f"{product['name']} | {platform} | error={type(exc).__name__}: {exc}", file=sys.stderr)
                time.sleep(float(config.get("delay_between_pages_seconds", 3)))
        context.close()
        if browser is not None:
            browser.close()

    send_stdout(alerts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rendered marketplace search watcher.")
    parser.add_argument("--config", default="browser_market_config.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return run_once(Path(args.config).resolve(), args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
