#!/bin/sh
set -eu

docker exec astrbot sh -lc 'cd /AstrBot/data/gpu-price-watcher-browser && xvfb-run -a python browser_market_watcher.py --config browser_market_config.json'
