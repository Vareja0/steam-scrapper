# steam-scrapper

Steam Community Market scraper. Collects daily price history + a current snapshot
for the items of a Steam app and writes a tidy CSV where **one row = one item at
one moment in time, with its price**, plus a set of engineered features meant to
feed a price-forecasting model.

Only the Python standard library + [`requests`](https://pypi.org/project/requests/)
are required.

## Install

```bash
pip install -r requirements.txt
```

`PySocks` (commented in `requirements.txt`) is only needed if `--proxies-file`
contains `socks5://` entries.

## Data sources

| Endpoint | Gives | Needs |
|---|---|---|
| `market/search/render` | item discovery + identity (classid, colors, commodity/tradable flags, listing count) | nothing |
| `market/priceoverview` | current lowest / median price, 24h volume | nothing |
| `ISteamEconomy/GetAssetClassInfo` (Web API) | full item tags (quality, rarity, exterior, ...) | free API key |
| `market/pricehistory` | full daily price history | `steamLoginSecure` login cookie |

- Free Web API key: <https://steamcommunity.com/dev/apikey> — pass via `--api-key`
  or env `STEAM_API_KEY`. Without it the tag columns stay empty.
- Login cookie: the `steamLoginSecure` cookie value from a logged-in browser
  session — pass via `--cookie` or env `STEAM_LOGIN_SECURE`. **Without it there is
  no price history**, so rows are current-snapshot only.

Steam's market listing pages were migrated to a client-rendered SPA and no longer
embed price history, item tags, or the order-book item id server-side, so those
are not scraped from there anymore.

## Quickstart

```bash
# Team Fortress 2 (appid 440): discover 40 items and scrape them
python steam_market_scraper.py --appid 440 --discover 40 -o tf2_prices.csv

# Explicit items
python steam_market_scraper.py --appid 730 \
    --item "AK-47 | Redline (Field-Tested)" \
    --item "Glock-18 | Water Elemental (Minimal Wear)" -o cs2.csv

# Full daily history needs the login cookie
python steam_market_scraper.py --appid 440 --discover-all \
    --cookie "$STEAM_LOGIN_SECURE" -o tf2_all.csv
```

### The command from the earlier run

```bash
python3 steam_market_scraper.py --appid 440 --wide --wide-days 60 -o tf2_last60d.csv --discover 100
```

- `--appid 440` — Team Fortress 2.
- `--discover 100` — auto-pick the 100 most-traded items and scrape those.
- `--wide --wide-days 60` — write **one row per item** with a `price_<date>`
  column for each of the trailing 60 days (covers the 7/30/60-day windows),
  instead of one row per item-day.
- `-o tf2_last60d.csv` — output file.

Note: this run passed **no `--cookie`**, so `market/pricehistory` was not
available and the `price_<date>` columns are mostly empty — only the
current-snapshot fields (`snap_lowest_price`, `snap_median_price`,
`snap_volume_24h`, `total_listings`) are populated. Add
`--cookie "$STEAM_LOGIN_SECURE"` to fill in the history.

## Flags

### Selecting items

| Flag | Default | Meaning |
|---|---|---|
| `--appid APPID` | `440` | Steam app id. `440` = TF2, `730` = CS2, `570` = Dota 2. |
| `--item NAME` | — | A `market_hash_name` to scrape. Repeatable. |
| `--items-file PATH` | — | File with one `market_hash_name` per line (`#` lines ignored). |
| `--discover N` | `0` | Auto-discover the `N` most-traded items for the app. |
| `--discover-all` | off | Discover **every** marketable item for the app (can be thousands; slow). |

Sources combine — you can pass `--item` and `--discover` together; duplicates are
dropped.

### Output shape

| Flag | Default | Meaning |
|---|---|---|
| `-o`, `--output PATH` | `steam_market.csv` | Output CSV path. |
| `--append` | off | Append to the CSV instead of overwriting (keeps the existing header). Not compatible with `--wide`. |
| `--resume` | off | Skip items whose `market_hash_name` is already in the output CSV and append the rest, so a run killed by a rate-limit ban picks up where it stopped. Implies `--append`. Not compatible with `--wide`. |
| `--wide` | off | One row per item with a `price_<date>` column per observation date, instead of one row per item-day. Not compatible with `--append` / `--resume`. |
| `--wide-days N` | `0` | With `--wide`, keep only the last `N` days of `price_<date>` columns. Setting it implies `--wide`. |
| `--no-fill-gaps` | off | Do not forward-fill days with no sales (default fills them and marks `is_filled=1`). |

### Credentials

| Flag | Env | Meaning |
|---|---|---|
| `--cookie VALUE` | `STEAM_LOGIN_SECURE` | `steamLoginSecure` cookie value. Required for price history. |
| `--api-key KEY` | `STEAM_API_KEY` | Steam Web API key. Required for item tags (quality/rarity/exterior/...). |

### Network / rate limiting

| Flag | Default | Meaning |
|---|---|---|
| `--currency CODE` | `1` | Steam currency code: `1` USD, `2` GBP, `3` EUR, `5` RUB, `7` BRL, `20` PLN, `23` UAH. |
| `--delay SECONDS` | `3.0` | Minimum seconds between requests **to the same exit IP**. Lower risks HTTP 429. |
| `--proxies-file PATH` | — | File with one proxy URL per line (`http://[user:pass@]host:port` or `socks5://...`; `#` lines ignored). |

## Rate limits, proxies, resuming

Steam limits the market endpoints per IP (roughly ~20 requests/minute) and will
temporarily ban an address (HTTP 429) that pushes past it — from minutes to
hours. The scraper makes ~3 rate-limited requests per item, so large runs get
throttled.

Mitigations, weakest to strongest:

1. **Raise `--delay`** (e.g. `5`–`8`). Fewer 429s means less time lost to
   backoff, so the total run is often faster.
2. **`--resume`.** Re-run the exact same command after a ban; items already in
   the CSV are skipped. Pair it with a shell loop for unattended runs:
   ```bash
   until python steam_market_scraper.py --appid 440 --discover-all \
       --proxies-file proxies.txt --resume -o tf2_prices.csv; do sleep 300; done
   ```
3. **`--proxies-file`.** The proxy pool is rotated once per request (even spread)
   and again whenever an exit trips a 429/5xx. Each exit IP keeps its own
   rate-limit budget and its own `--delay` clock, so a pool of N proxies is
   roughly N× the throughput. Residential proxies work best; Steam often blocks
   datacenter ranges.

On a 429 the scraper honours the response's `Retry-After` header when present
(capped at 300s), otherwise backs off `30 × attempt` seconds.

## Output columns

- **Identity / meta:** `scrape_timestamp_utc`, `appid`, `market_hash_name`,
  `item_name`, `item_type`, `currency`, `classid`, `instanceid`, `commodity`,
  `tradable`, `marketable`, `name_color`, `background_color`, `icon_url`,
  `quality`, `rarity`, `type`, `hero`, `klass`, `exterior`, `collection`,
  `tags_json`.
- **Observation:** `observation_date`, `price`, `volume`, `is_filled`.
- **Engineered features:** price lags / moving averages / EMA / stdev / returns /
  z-score / min-max over 7 & 30-day windows, `ath`, `atl`, distance-from-ath/atl,
  volume lags & moving averages, calendar features (`dow`, `month`,
  `day_of_year`, `is_weekend`), and forward-looking targets
  (`target_price_next_1d`, `target_price_next_7d`, `target_return_next_7d`).
- **Snapshot:** `snap_lowest_price`, `snap_median_price`, `snap_volume_24h`,
  `total_listings`.

In `--wide` mode the per-day columns collapse to one `price_<YYYY-MM-DD>` column
per date and the meta + snapshot columns are kept once per item.
