#!/usr/bin/env python3
"""GPU price watcher for small always-on Linux hosts.

The watcher reads product/listing rules from a JSON config, extracts prices from
public product pages, stores observations in SQLite, and sends alerts through
stdout, a generic webhook, or AstrBot's HTTP API.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_OUT_OF_STOCK_KEYWORDS = [
    "无货",
    "暂时缺货",
    "缺货",
    "售罄",
    "已下架",
    "到货通知",
    "Sold Out",
    "Out of stock",
    "Currently unavailable",
]

COMMON_PRICE_PATTERNS = [
    r'"price"\s*:\s*"?(?P<price>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"?',
    r'"salePrice"\s*:\s*"?(?P<price>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"?',
    r'"finalPrice"\s*:\s*"?(?P<price>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"?',
    r'(?:￥|¥|CNY\s*)\s*(?P<price>[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
    r'(?:USD|\$)\s*(?P<price>[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
]


@dataclasses.dataclass
class ListingResult:
    product_name: str
    platform: str
    url: str
    price: Decimal | None
    currency: str
    available: bool
    title: str
    error: str | None = None


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value)
    text = html.unescape(text)
    text = text.replace(",", "").replace("，", "").replace("￥", "").replace("¥", "")
    text = text.replace("$", "").replace("CNY", "").replace("USD", "")
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def compact_text(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit].strip()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        config = json.load(fh)
    if "products" not in config or not isinstance(config["products"], list):
        raise ValueError("config must contain a products list")
    return config


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            product_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            url TEXT NOT NULL,
            price TEXT,
            currency TEXT NOT NULL,
            available INTEGER NOT NULL,
            title TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS latest (
            product_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            url TEXT NOT NULL,
            price TEXT,
            currency TEXT NOT NULL,
            available INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            title TEXT,
            error TEXT,
            PRIMARY KEY (product_name, platform, url)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            product_name TEXT PRIMARY KEY,
            last_alert_at TEXT NOT NULL,
            last_alert_price TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def fetch_url(url: str, timeout: int, user_agent: str, headers: dict[str, str] | None = None) -> str:
    request_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
    return raw.decode(charset, errors="replace")


def find_by_selector(document: str, selector: str, attribute: str | None) -> str | None:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return None
    soup = BeautifulSoup(document, "html.parser")
    node = soup.select_one(selector)
    if not node:
        return None
    if attribute:
        value = node.get(attribute)
        return str(value) if value is not None else None
    return node.get_text(" ", strip=True)


def find_by_json_path(document: str, path: str) -> str | None:
    """Read a simple JSON-LD path like offers.price from script blocks."""
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    parts = [part for part in path.split(".") if part]
    for script in scripts:
        try:
            payload = json.loads(html.unescape(script.strip()))
        except json.JSONDecodeError:
            continue
        for candidate in flatten_json_candidates(payload):
            value: Any = candidate
            for part in parts:
                if isinstance(value, list):
                    value = value[0] if value else None
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                if value is None:
                    break
            if value is not None:
                return str(value)
    return None


def flatten_json_candidates(value: Any) -> list[Any]:
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            out.extend(flatten_json_candidates(item))
        return out
    if isinstance(value, dict) and "@graph" in value:
        return flatten_json_candidates(value["@graph"]) + [value]
    return [value]


def extract_price(document: str, listing: dict[str, Any]) -> Decimal | None:
    selector = listing.get("selector")
    if selector:
        selected = find_by_selector(document, selector, listing.get("attribute"))
        price = parse_decimal(selected)
        if price is not None:
            return price

    json_path = listing.get("json_path")
    if json_path:
        value = find_by_json_path(document, json_path)
        price = parse_decimal(value)
        if price is not None:
            return price

    custom_regex = listing.get("regex")
    if custom_regex:
        match = re.search(custom_regex, document, flags=re.IGNORECASE | re.DOTALL)
        if match:
            if match.groupdict().get("price") is not None:
                return parse_decimal(match.group("price"))
            if match.groups():
                return parse_decimal(match.group(1))
            return parse_decimal(match.group(0))

    for pattern in COMMON_PRICE_PATTERNS:
        match = re.search(pattern, document, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return parse_decimal(match.group("price"))
    return None


def extract_title(document: str, listing: dict[str, Any]) -> str:
    title_selector = listing.get("title_selector")
    if title_selector:
        selected = find_by_selector(document, title_selector, listing.get("title_attribute"))
        if selected:
            return compact_text(selected, 160)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", document, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        return compact_text(title_match.group(1), 160)
    return ""


def detect_availability(document: str, product: dict[str, Any], listing: dict[str, Any], title: str) -> bool:
    text = compact_text(document, limit=500_000)
    haystack = f"{title} {text}"

    include_keywords = product.get("include_keywords", []) + listing.get("include_keywords", [])
    for keyword in include_keywords:
        if keyword and str(keyword).lower() not in haystack.lower():
            return False

    exclude_keywords = (
        DEFAULT_OUT_OF_STOCK_KEYWORDS
        + product.get("exclude_keywords", [])
        + listing.get("exclude_keywords", [])
    )
    for keyword in exclude_keywords:
        if keyword and str(keyword).lower() in haystack.lower():
            return False

    in_stock_regex = listing.get("in_stock_regex")
    if in_stock_regex and not re.search(in_stock_regex, document, flags=re.IGNORECASE | re.DOTALL):
        return False

    out_of_stock_regex = listing.get("out_of_stock_regex")
    if out_of_stock_regex and re.search(out_of_stock_regex, document, flags=re.IGNORECASE | re.DOTALL):
        return False

    return True


def check_listing(
    product: dict[str, Any],
    listing: dict[str, Any],
    config: dict[str, Any],
) -> ListingResult:
    product_name = str(product["name"])
    platform = str(listing.get("platform") or urllib.parse.urlparse(listing["url"]).netloc)
    url = str(listing["url"])
    currency = str(listing.get("currency") or product.get("currency") or "CNY")
    try:
        document = fetch_url(
            url,
            timeout=int(config.get("timeout_seconds", 20)),
            user_agent=str(config.get("user_agent") or DEFAULT_USER_AGENT),
            headers=listing.get("headers") or config.get("headers") or {},
        )
        title = extract_title(document, listing)
        price = extract_price(document, listing)
        available = detect_availability(document, product, listing, title)
        return ListingResult(product_name, platform, url, price, currency, available, title)
    except Exception as exc:  # noqa: BLE001 - errors are persisted per listing
        return ListingResult(product_name, platform, url, None, currency, False, "", str(exc))


def save_result(conn: sqlite3.Connection, result: ListingResult) -> None:
    checked_at = now_utc().isoformat()
    conn.execute(
        """
        INSERT INTO observations
            (checked_at, product_name, platform, url, price, currency, available, title, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checked_at,
            result.product_name,
            result.platform,
            result.url,
            str(result.price) if result.price is not None else None,
            result.currency,
            1 if result.available else 0,
            result.title,
            result.error,
        ),
    )
    conn.execute(
        """
        INSERT INTO latest
            (product_name, platform, url, price, currency, available, checked_at, title, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_name, platform, url) DO UPDATE SET
            price = excluded.price,
            currency = excluded.currency,
            available = excluded.available,
            checked_at = excluded.checked_at,
            title = excluded.title,
            error = excluded.error
        """,
        (
            result.product_name,
            result.platform,
            result.url,
            str(result.price) if result.price is not None else None,
            result.currency,
            1 if result.available else 0,
            checked_at,
            result.title,
            result.error,
        ),
    )
    conn.commit()


def latest_previous_price(conn: sqlite3.Connection, result: ListingResult) -> Decimal | None:
    row = conn.execute(
        """
        SELECT price FROM latest
        WHERE product_name = ? AND platform = ? AND url = ?
        """,
        (result.product_name, result.platform, result.url),
    ).fetchone()
    if not row:
        return None
    return parse_decimal(row[0])


def should_alert(
    conn: sqlite3.Connection,
    product: dict[str, Any],
    best: ListingResult,
    previous_price: Decimal | None,
    cooldown_minutes: int,
) -> tuple[bool, str]:
    if not best.available:
        return False, ""

    reasons: list[str] = []
    if product.get("alert_on_available"):
        reasons.append("检测到可能有货")

    target = parse_decimal(product.get("target_price"))
    if target is not None and best.price is not None and best.price <= target:
        reasons.append(f"低于目标价 {target} {best.currency}")

    drop_pct = parse_decimal(product.get("alert_on_drop_pct"))
    if best.price is not None and previous_price is not None and drop_pct is not None and drop_pct > 0:
        threshold = previous_price * (Decimal("1") - drop_pct / Decimal("100"))
        if best.price <= threshold:
            reasons.append(f"比上次 {previous_price} {best.currency} 下降至少 {drop_pct}%")

    if not reasons:
        return False, ""

    row = conn.execute(
        "SELECT last_alert_at FROM alerts WHERE product_name = ?",
        (best.product_name,),
    ).fetchone()
    if row:
        last_alert_at = dt.datetime.fromisoformat(row[0])
        if now_utc() - last_alert_at < dt.timedelta(minutes=cooldown_minutes):
            return False, ""

    return True, "；".join(reasons)


def mark_alerted(conn: sqlite3.Connection, result: ListingResult) -> None:
    conn.execute(
        """
        INSERT INTO alerts (product_name, last_alert_at, last_alert_price)
        VALUES (?, ?, ?)
        ON CONFLICT(product_name) DO UPDATE SET
            last_alert_at = excluded.last_alert_at,
            last_alert_price = excluded.last_alert_price
        """,
        (result.product_name, now_utc().isoformat(), str(result.price)),
    )
    conn.commit()


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 20) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def result_payload(result: ListingResult, reason: str) -> dict[str, Any]:
    payload = dataclasses.asdict(result)
    payload["price"] = str(result.price) if result.price is not None else None
    payload["reason"] = reason
    return payload


def send_notifications(config: dict[str, Any], alerts: list[tuple[ListingResult, str]]) -> None:
    if not alerts:
        return

    lines = ["显卡价格提醒"]
    for result, reason in alerts:
        price_text = f"{result.price} {result.currency}" if result.price is not None else "未知价格"
        lines.extend(
            [
                "",
                f"{result.product_name}",
                f"平台：{result.platform}",
                f"价格：{price_text}",
                f"原因：{reason}",
                f"链接：{result.url}",
            ]
        )
        if result.title:
            lines.append(f"标题：{result.title}")
    message = "\n".join(lines)

    notifications = config.get("notifications") or {}
    if notifications.get("stdout", True):
        print(message)

    webhook = notifications.get("webhook") or {}
    if webhook.get("enabled"):
        post_json(
            webhook["url"],
            {"text": message, "alerts": [result_payload(item[0], item[1]) for item in alerts]},
            headers=webhook.get("headers"),
            timeout=int(webhook.get("timeout_seconds", 20)),
        )

    astrbot = notifications.get("astrbot") or {}
    if astrbot.get("enabled"):
        base_url = str(astrbot["base_url"]).rstrip("/")
        api_key = str(astrbot["api_key"])
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = int(astrbot.get("timeout_seconds", 20))
        if astrbot.get("mode", "im") == "chat":
            post_json(
                f"{base_url}/api/v1/chat",
                {
                    "username": astrbot["username"],
                    "session_id": astrbot.get("session_id", "gpu-price-watcher"),
                    "message": message,
                    "enable_streaming": False,
                },
                headers=headers,
                timeout=timeout,
            )
        else:
            post_json(
                f"{base_url}/api/v1/im/message",
                {"umo": astrbot["umo"], "message": message},
                headers=headers,
                timeout=timeout,
            )


def run_once(config_path: Path, verbose: bool = False) -> int:
    config = load_config(config_path)
    base_dir = config_path.parent
    db_path = Path(config.get("database", "prices.sqlite3"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    conn = init_db(db_path)
    cooldown_minutes = int(config.get("cooldown_minutes", 360))
    alerts: list[tuple[ListingResult, str]] = []

    for product in config["products"]:
        product_results: list[tuple[ListingResult, Decimal | None]] = []
        for listing in product.get("listings", []):
            delay = float(config.get("delay_between_requests_seconds", 2))
            if delay > 0:
                time.sleep(delay)
            result = check_listing(product, listing, config)
            previous_price = latest_previous_price(conn, result)
            save_result(conn, result)
            product_results.append((result, previous_price))
            if verbose:
                price = result.price if result.price is not None else "-"
                status = "available" if result.available else "unavailable"
                print(f"{result.product_name} | {result.platform} | {price} | {status}")
                if result.error:
                    print(f"  error: {result.error}", file=sys.stderr)

        available_results = [
            (result, previous)
            for result, previous in product_results
            if result.available and (result.price is not None or product.get("alert_on_available"))
        ]
        if not available_results:
            continue
        best, previous = min(available_results, key=lambda item: item[0].price or Decimal("Infinity"))
        ok, reason = should_alert(conn, product, best, previous, cooldown_minutes)
        if ok:
            alerts.append((best, reason))
            mark_alerted(conn, best)

    send_notifications(config, alerts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor GPU prices and send alerts.")
    parser.add_argument("--config", default="config.json", help="Path to JSON config.")
    parser.add_argument("--verbose", action="store_true", help="Print every checked listing.")
    parser.add_argument("--debug", action="store_true", help="Show tracebacks for unexpected errors.")
    args = parser.parse_args(argv)

    try:
        return run_once(Path(args.config).resolve(), verbose=args.verbose)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"gpu-price-watcher error: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
