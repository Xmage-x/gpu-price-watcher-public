#!/usr/bin/env python3
"""Diagnose what the Pinduoduo search page renders under the saved browser profile."""

from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "https://mobile.yangkeduo.com/search_result.html?search_key=Tesla%20T10%20%E6%98%BE%E5%8D%A1"
PROFILE = Path("/AstrBot/data/gpu-price-watcher-browser/browser_profile")


def main() -> int:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)
        body = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
        anchors = page.locator("a").evaluate_all(
            """
            els => els.slice(0, 80).map(a => ({
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || '').slice(0, 120)
            }))
            """
        )
        print(f"TITLE={page.title()[:120]}")
        print(f"URL={page.url[:240]}")
        print("BODY=" + body[:1000].replace("\n", " | "))
        print(f"ANCHORS={len(anchors)}")
        for anchor in anchors[:20]:
            text = str(anchor.get("text") or "").replace("\n", " ")[:80]
            href = str(anchor.get("href") or "")[:160]
            print(f"A={text} => {href}")
        snippets = page.evaluate(
            """
            () => {
                const textRe = /Tesla|T10|T40|A100|计算卡/i;
                const nodes = [...document.querySelectorAll('div, section, li')];
                return nodes
                    .map((el) => {
                        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!textRe.test(text) || text.length < 8) return null;
                        const attrs = {};
                        for (const attr of el.attributes || []) {
                            if (/id|goods|item|url|link|href|data/i.test(attr.name + attr.value)) {
                                attrs[attr.name] = attr.value;
                            }
                        }
                        return {
                            tag: el.tagName,
                            cls: el.className || '',
                            attrs,
                            text: text.slice(0, 260),
                        };
                    })
                    .filter(Boolean)
                    .slice(0, 25);
            }
            """
        )
        print(f"SNIPPETS={len(snippets)}")
        for item in snippets:
            print(f"E={item}")
        content = page.content()
        for needle in ("goods_id", "goodsID", "goodsId", "mall_id", "search_result"):
            print(f"COUNT[{needle}]={content.count(needle)}")
        for needle in ("goods_id", "goodsID"):
            index = content.find(needle)
            if index >= 0:
                print(f"NEAR[{needle}]=" + content[max(0, index - 260) : index + 500].replace("\n", " ")[:900])
        clicked = page.evaluate(
            """
            () => {
                const cards = [...document.querySelectorAll('div[data-uniqid]')];
                const card = cards.find(el => /Tesla T10 16G|T10\\s*16G|计算卡/i.test(el.innerText || ''));
                if (!card) return {ok: false, count: cards.length};
                card.scrollIntoView({block: 'center'});
                card.click();
                return {ok: true, count: cards.length, text: (card.innerText || '').slice(0, 240)};
            }
            """
        )
        print(f"CLICK={clicked}")
        if clicked.get("ok"):
            page.wait_for_timeout(5000)
            print(f"AFTER_CLICK_URL={page.url[:240]}")
            print(f"AFTER_CLICK_TITLE={page.title()[:120]}")
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
