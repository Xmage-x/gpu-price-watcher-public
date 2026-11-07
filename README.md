# GPU Price Watcher

A Python-based GPU marketplace price watcher using Playwright, SQLite, Docker, and AstrBot integrations.

This is a personal software development and automation project focused on browser automation, structured data collection, price tracking, and notification workflows. It runs scheduled checks against public second-hand marketplace search pages for Tesla T10, Tesla T40, and NVIDIA A100 compute cards, keeps the candidates in SQLite, and sends alerts to a WeChat bot session through AstrBot when price or availability rules match.

## Project Overview

The project demonstrates three common automation approaches and wires them into one
pipeline: data collection → structured storage → rule matching → notification.

1. **Browser watcher** (`browser_market_watcher.py`) — the main implementation. It
   renders marketplace search pages with Playwright and extracts candidate listings
   from the visible DOM, the in-page state objects, and captured network responses.
   This works for pages that need a logged-in, JavaScript-rendered browser.
2. **HTTP fetcher** (`gpu_price_watcher.py`) — a dependency-free fallback for plain
   product pages. It extracts prices with CSS selectors, JSON-LD paths, or custom
   regex, and runs under a systemd timer.
3. **AstrBot plugin** (`astrbot_plugin_gpu_price_watch/`) — wraps the watcher into a
   chat-bot plugin: it triggers a check every 15 minutes and proactively pushes the
   alert to a bound WeChat session.

## Features

- **Rendered search-page polling** with Playwright and a persistent logon profile
  (`user_data_dir`), so cookies live in a local directory instead of the repo.
- **Candidate extraction from visible DOM anchors** (title, link, price). Pinduoduo
  pages get an extra extractor that walks the in-page state objects, captured JSON
  network responses, and a raw-HTML fallback.
- **Deduplication** by normalizing item URLs (`item.htm?id=`, `item?id=`,
  `goods_id=`) into a SHA-256 `item_key`.
- **Keyword filtering**: `include_keywords` must all match; `exclude_keywords`
  filters out repairs, accessories, rentals, and other junk results.
- **Alert rules**: price below `target_price`, newly seen listing
  (`alert_on_new_listing`), reappearing out-of-stock model
  (`alert_on_available`), and an anomaly floor (`min_alert_price`) that ignores
  obviously bogus "too good to be true" prices.
- **Polite pacing**: per-page delay between visits, and when the Pinduoduo page
  reports the "system busy" message, the watcher pauses that platform for an hour
  (default `pdd_backoff_seconds: 3600`) instead of retrying aggressively.
- **Notification channels**: stdout (browser watcher), and webhook / AstrBot HTTP
  API (HTTP fetcher), plus proactive WeChat push via the AstrBot plugin.
- **Remote QR-login tooling**: an Xvfb + TigerVNC + noVNC setup driven by shell
  scripts, a small TCP forwarder, and periodic status screenshots, for signing in
  to marketplaces on a headless Linux box.

## Architecture

```
            ┌────────────── Linux host (Docker) ───────────────┐
            │                                                  │
  cron-like │  astrbot_plugin_gpu_price_watch                  │
  trigger → │    │ runs the watcher every 15 min (flock guard) │
            │    ▼                                             │
            │  browser_market_watcher.py (Playwright, xvfb-run)│
            │    ├─ browser_profile/      persistent logon     │
            │    ├─ browser_market_prices.sqlite3   data        │
            │    └─ match → proactive push to WeChat session   │
            │                                                  │
            │  optional: Xvfb + TigerVNC + noVNC → manual       │
            │            QR-code login from another machine    │
            └──────────────────────────────────────────────────┘

    Standalone alternative: gpu_price_watcher.py (HTTP fetch + systemd timer)
```

Data flow: `search page → DOM / page-state extraction → candidate listings
(title, URL, price) → SQLite dedup → rule engine → AstrBot push / stdout / webhook`.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.11+ (`datetime.UTC`) |
| Page automation | Playwright (Chromium), persistent contexts |
| Storage | SQLite via the stdlib `sqlite3` module |
| Chat integration | AstrBot plugin API / generic webhook |
| Deployment | Docker (AstrBot container), systemd timer, shell scripts |
| Headless login | Xvfb, TigerVNC, noVNC, websockify |

## Quick Start

```bash
git clone https://github.com/Xmage-x/gpu-price-watcher.git
cd gpu-price-watcher

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# copy an example config and run one manual check
cp browser_market_config.tesla.json browser_market_config.json
python browser_market_watcher.py --config browser_market_config.json --verbose
```

The first run is a smoke test over public pages in a fresh browser profile. To
watch a marketplace that requires your own login, log in once so the profile
persists (see [Remote login](#remote-login-for-protected-marketplaces)).

## Configuration

The watcher reads a JSON config. A compact example:

```json
{
  "database": "browser_market_prices.sqlite3",
  "user_data_dir": "browser_profile",
  "headless": false,
  "products": [
    {
      "name": "Tesla T10",
      "target_price": 1250,
      "min_alert_price": 900,
      "alert_on_new_listing": true,
      "include_keywords": ["T10"],
      "exclude_keywords": ["repair", "rental"],
      "sources": [
        { "platform": "Goofish search", "query": "Tesla T10 显卡",
          "url": "https://www.goofish.com/search?q=..." },
        { "platform": "Taobao search",  "url": "https://s.taobao.com/search?q=..." },
        { "platform": "Pinduoduo search", "url": "https://mobile.yangkeduo.com/search_result.html?search_key=..." }
      ]
    }
  ]
}
```

Key fields:

| Field | Meaning |
| --- | --- |
| `user_data_dir` | Persistent Chromium profile directory (relative to the config file) |
| `headless` | Headless browser; set `false` for the first manual login |
| `target_price` | Alert when price ≤ this value |
| `min_alert_price` | Ignore suspicious low prices below this floor |
| `include_keywords` / `exclude_keywords` | Keyword whitelist / blacklist per product and per source |
| `alert_on_new_listing` | Alert when a previously unseen item appears |
| `alert_on_available` | Alert when a stocked-out model reappears |
| `pdd_backoff_seconds` | Cooldown for the Pinduoduo "system busy" page, default 3600 |

`browser_market_config.tesla.json` in the repo is a complete working example for
Tesla T10 / T40 / A100 with tuned keyword filters. HTTP-fetcher configs are in
`config.example.json` (generic) and `config.tesla.example.json` (Tesla models).

The AstrBot plugin reads two environment variables (see `.env.example`):

```dotenv
# Pre-bind the reminder target without running /gpuwatch_bind
GPU_WATCH_PUSH_ORIGIN=weixin_personal:FriendMessage:<YOUR_SESSION_ID>@im.wechat
# Background check interval in seconds (default 900)
GPU_WATCH_INTERVAL_SECONDS=900
```

## AstrBot Integration

Install the plugin into the AstrBot container:

```bash
docker cp browser_market_watcher.py astrbot:/AstrBot/data/gpu-price-watcher-browser/
docker cp browser_market_config.tesla.json astrbot:/AstrBot/data/gpu-price-watcher-browser/browser_market_config.json
docker cp astrbot_plugin_gpu_price_watch astrbot:/AstrBot/data/plugins/

docker exec astrbot python -m py_compile /AstrBot/data/plugins/astrbot_plugin_gpu_price_watch/main.py
docker restart astrbot
```

In-chat commands:

| Command | Action |
| --- | --- |
| `/gpuwatch_bind` | Bind the current (real WeChat) chat as the alert target. WebChat backend sessions are deliberately rejected so the web UI never shows "sent" while WeChat received nothing. |
| `/gpuwatch_test` | Send a test message to verify the push path. |
| `/gpuwatch_now` | Run one check immediately and return the summary. |
| `/gpuwatch_status` | Show the bound target, last run time, and last push error. |

The plugin shells out to `browser_market_watcher.py` under `xvfb-run` with a
`flock` guard so overlapping checks never pile up on a small host.

## Docker / Linux Deployment

- **AstrBot container**: the `docker cp` sequence above plus `docker restart
  astrbot` is the whole deployment. `run_browser_market_watcher.sh` runs one
  check inside the container manually.
- **systemd timer** (HTTP fetcher variant):

  ```bash
  sudo cp systemd/gpu-price-watcher.service systemd/gpu-price-watcher.timer /etc/systemd/system/
  sudo systemctl enable --now gpu-price-watcher.timer
  ```

  The timer runs `gpu_price_watcher.py` every 10 minutes with jitter.

### Remote login for protected marketplaces

For first-time QR-code logins on a headless box, `start_browser_desktop.sh`
brings up an Xvfb display, a TigerVNC server, and noVNC, then keeps the login
page open so the session can be authorized from a browser. `tcp_forward.py` — a
small `selectors`-multiplexing forwarder under 60 lines — bridges the container's
noVNC port to a host bind address, and `start_screenshot_server.sh` serves the
periodic login-status screenshots. `BIND_HOST` defaults to `127.0.0.1`; only
bind it to a private address you control, never a public interface.

## Data Storage

`browser_market_watcher.py` maintains three SQLite tables:

```sql
CREATE TABLE market_items (          -- deduplicated catalog
    item_key TEXT PRIMARY KEY,       -- sha256(normalized URL)[:24]
    product_name, platform, query, title, url,
    first_seen_at TEXT, last_seen_at TEXT,
    first_price TEXT, last_price TEXT,
    text TEXT
);

CREATE TABLE market_observations (   -- raw check history
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT, item_key TEXT,
    product_name, platform, query, title, url, price, text
);

CREATE TABLE market_alerts (         -- sent-alert de-duplication
    alert_key TEXT PRIMARY KEY,      -- item_key:reason
    alerted_at TEXT, item_key TEXT, reason TEXT
);
```

The HTTP fetcher uses the analogous `observations`, `latest`, and `alerts`
tables.

Handy query:

```bash
sqlite3 browser_market_prices.sqlite3 \
  "SELECT platform, title, last_price FROM market_items ORDER BY last_price LIMIT 20;"
```

Runtime artifacts are local-only and gitignored: `browser_profile/`,
`browser_market_prices.sqlite3`, `browser_market_backoff.json`, `*.log`,
`vnc_password.txt`, `x11vnc.pass`, `config.json`, `.env`.

## Security / Privacy

This repository ships with **no credentials**:

1. Session IDs, API keys, and bot accounts appear only as placeholders such as
   `abk_xxx`, `YOUR_SESSION_ID`, and `example.com` URLs.
2. Everything that can contain private state is gitignored — the Chromium
   profile (`browser_profile/`), the SQLite data files, logs, the auto-generated
   VNC password, and both live-config filenames.
3. The WeChat push target is bound at runtime via `/gpuwatch_bind` or the
   `GPU_WATCH_PUSH_ORIGIN` environment variable; no session ID is hard-coded.
4. `browser_profile/` holds marketplace login sessions. Treat it like a
   password: keep it on your own machine, never copy or upload it.
5. The SQLite stores only what the watcher needs — titles, links, prices, and
   timestamps. It does not collect seller contact info.
6. Access is polite by design: delays between pages, one-hour cooldown when a
   page reports "system busy", and no circumvention of captchas or login walls.
7. `BIND_HOST` in the login-desktop scripts defaults to `127.0.0.1`; the VNC
   password is a random 16-character value generated on first launch.

The project is for personal use and learning. Follow the target marketplaces'
terms of service and local regulations, keep request rates reasonable, and keep
login sessions within your own accounts.

## Project Structure

```text
gpu-price-watcher/
├── browser_market_watcher.py        # Playwright search-page watcher (main)
├── browser_market_config.tesla.json # complete example config (T10/T40/A100)
├── browser_desktop.py               # keep a visible browser open for manual login
├── browser_login_helper.py          # login helper + periodic status screenshots
├── pdd_diag.py                      # debug script for Pinduoduo page rendering
├── tcp_forward.py                   # ~60-line TCP forwarder (noVNC bridge)
├── gpu_price_watcher.py             # stdlib HTTP fetcher (fallback variant)
├── config.example.json              # HTTP-fetcher example config
├── config.tesla.example.json        # HTTP-fetcher example config (Tesla)
├── run_browser_market_watcher.sh    # run one check inside the AstrBot container
├── start_browser_desktop.sh         # one-shot Xvfb + TigerVNC + noVNC desktop
├── start_browser_login.sh           # background login helper
├── start_screenshot_server.sh       # temporary HTTP server for login screenshots
├── requirements.txt
├── .env.example
├── systemd/                         # systemd timer for the HTTP fetcher
│   ├── gpu-price-watcher.service
│   └── gpu-price-watcher.timer
└── astrbot_plugin_gpu_price_watch/  # AstrBot plugin (scheduler + push)
    ├── main.py
    ├── metadata.yaml
    └── README.md
```

## License

MIT — see [LICENSE](LICENSE).
