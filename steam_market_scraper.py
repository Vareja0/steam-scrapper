#!/usr/bin/env python3
"""
Steam Community Market scraper.

Collects historical price data and current snapshot fields for items of a given
Steam app and writes a tidy CSV where **one row = one item at one moment in
time, with its price** plus a set of engineered features meant to feed a price
forecasting model.

Data sources:
  * market/search/render (norender=1)         -> item discovery + identity
                                                  (classid, colors, commodity/
                                                  tradable flags, listing
                                                  count). No key needed.
  * market/priceoverview                      -> current lowest/median price,
                                                  24h volume. No key needed.
  * ISteamEconomy/GetAssetClassInfo (Web API) -> full item tags (quality,
                                                  rarity, exterior, ...).
                                                  Needs a free API key.
  * market/pricehistory                       -> full daily price history.
                                                  Needs a login cookie.

Steam's market listing pages (``market/listings/<appid>/<name>`` and its
``/render/`` variant) were migrated to a client-rendered SPA and no longer
embed price history, item tags, or the order-book item id server-side, so
this scraper no longer scrapes them - the live buy/sell order-book snapshot
that used to come from ``market/itemordershistogram`` has been dropped for
the same reason (its `item_nameid` parameter is no longer exposed anywhere
outside of a fully JS-rendered browser session).

A `steamLoginSecure` cookie (``--cookie`` or env STEAM_LOGIN_SECURE) unlocks
full price history. A Steam Web API key (``--api-key`` or env STEAM_API_KEY,
free at https://steamcommunity.com/dev/apikey) unlocks item tags.

Only the standard library + `requests` are required.

Examples
--------
    # Team Fortress 2 (appid 440): discover 40 items and scrape them
    python steam_market_scraper.py --appid 440 --discover 40 -o tf2_prices.csv

    # Every item of the game, one row each, last 60 days of daily prices
    # (covers the 7/30/60-day windows). Needs --cookie for price history.
    python steam_market_scraper.py --appid 440 --discover-all --wide \
        --wide-days 60 --cookie "$STEAM_LOGIN_SECURE" -o tf2_last60d.csv

    # Explicit items
    python steam_market_scraper.py --appid 730 \
        --item "AK-47 | Redline (Field-Tested)" \
        --item "Glock-18 | Water Elemental (Minimal Wear)" -o cs2.csv

    # From a file (one market_hash_name per line)
    python steam_market_scraper.py --appid 440 --items-file names.txt -o out.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

MARKET = "https://steamcommunity.com/market"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Steam currency codes -> ISO-ish label (subset).
CURRENCIES = {1: "USD", 2: "GBP", 3: "EUR", 5: "RUB", 7: "BRL", 20: "PLN", 23: "UAH"}


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
class SteamClient:
    def __init__(self, currency: int = 1, delay: float = 3.0, cookie: str | None = None,
                 timeout: float = 30.0, max_retries: int = 4):
        self.currency = currency
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        if cookie:
            self.s.cookies.set("steamLoginSecure", cookie, domain="steamcommunity.com")
        self._last = 0.0

    def _throttle(self):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))

    def get(self, url: str, *, params: dict | None = None, referer: str | None = None):
        headers = {"Referer": referer} if referer else {}
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                _warn(f"request error ({exc}); retry {attempt}/{self.max_retries}")
                time.sleep(5 * attempt)
                continue
            finally:
                self._last = time.time()

            if r.status_code == 429:
                back = 30 * attempt
                _warn(f"rate limited (429); sleeping {back}s [{attempt}/{self.max_retries}]")
                time.sleep(back)
                continue
            if r.status_code >= 500:
                _warn(f"server error {r.status_code}; retry {attempt}/{self.max_retries}")
                time.sleep(5 * attempt)
                continue
            return r
        return r  # last response, even if not ok

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        if not r.ok:
            return None
        try:
            return r.json()
        except ValueError:
            return None


def _warn(msg: str):
    print(f"  ! {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
_MONEY_RE = re.compile(r"[-+]?\d[\d.,]*")


def money_to_float(text: str | None) -> float | None:
    """'$1,234.56' / '1.234,56€' / '-- ' -> float or None."""
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    token = m.group(0)
    # Decide decimal separator: the last '.' or ',' that has 1-2 trailing digits.
    if "," in token and "." in token:
        dec = "," if token.rfind(",") > token.rfind(".") else "."
    elif "," in token:
        dec = "," if re.search(r",\d{1,2}$", token) else ""
    elif "." in token:
        dec = "." if re.search(r"\.\d{1,2}$", token) else ""
    else:
        dec = ""
    thousands = {".", ","} - {dec}
    for t in thousands:
        token = token.replace(t, "")
    if dec:
        token = token.replace(dec, ".")
    try:
        return float(token)
    except ValueError:
        return None


def int_or_none(text: str | None) -> int | None:
    if text is None:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


def parse_steam_ts(s: str) -> datetime:
    """'Nov 20 2012 01: +0' -> aware UTC datetime."""
    s = s.split(":")[0].strip()  # 'Nov 20 2012 01'
    return datetime.strptime(s, "%b %d %Y %H").replace(tzinfo=timezone.utc)


_TAG_ALIASES = {
    "quality": ("Quality",),
    "rarity": ("Rarity", "Grade", "Quality"),
    "type": ("Type", "Weapon"),
    "hero": ("Hero",),
    "klass": ("Class", "Slot"),
    "exterior": ("Exterior", "Wear"),
    "collection": ("Collection", "Set", "Tournament"),
}


def _empty_meta() -> dict:
    out = {"item_name": None, "item_type": None, "tags_json": None}
    for key in _TAG_ALIASES:
        out[key] = None
    return out


def meta_from_asset(asset: dict | None) -> dict:
    """Normalise a Steam `asset`/`CEconItem` description object - the same shape
    returned by GetAssetClassInfo, inventory, and (formerly) the listing page."""
    out = _empty_meta()
    if not asset:
        return out
    out["item_name"] = asset.get("market_name") or asset.get("name")
    out["item_type"] = asset.get("type")
    tags = {}
    for tag in asset.get("tags", []) or []:
        cat = tag.get("localized_category_name") or tag.get("category")
        val = tag.get("localized_tag_name") or tag.get("internal_name")
        if cat:
            tags[cat] = val
    out["tags_json"] = json.dumps(tags, ensure_ascii=False) if tags else None
    for field, aliases in _TAG_ALIASES.items():
        for a in aliases:
            if a in tags:
                out[field] = tags[a]
                break
    return out


def merge_meta(primary: dict, fallback: dict) -> dict:
    return {k: (primary.get(k) if primary.get(k) not in (None, "") else fallback.get(k))
            for k in set(primary) | set(fallback)}


# --------------------------------------------------------------------------- #
# Endpoint wrappers
# --------------------------------------------------------------------------- #
def listing_url(appid: int, name: str) -> str:
    return f"{MARKET}/listings/{appid}/{quote(name, safe='')}"


def discover_items(client: SteamClient, appid: int, limit: int | None) -> list[str]:
    """Page through `market/search/render`. `limit=None` means "all of them" -
    it keeps paginating until the API stops returning results (or reports
    its own total_count), rather than stopping at a fixed count.

    Steam frequently serves fewer rows than the requested `count` (its page
    size for this endpoint is often 10 regardless of what you ask for), so a
    short page is *not* treated as the end of the catalogue - only an empty
    page, reaching `total_count`, or several consecutive pages that add no new
    names stop the walk. A failed request (rate limit, network) is reported
    loudly rather than silently ending discovery with a partial list."""
    names: list[str] = []
    seen: set[str] = set()
    start, total_count, stale_pages = 0, None, 0
    while limit is None or len(names) < limit:
        want = 100 if limit is None else min(100, limit - len(names))
        data = client.get_json(
            f"{MARKET}/search/render/",
            params={"appid": appid, "norender": 1, "start": start,
                    "count": want, "currency": client.currency,
                    "sort_column": "quantity", "sort_dir": "desc"},
        )
        if data is None:
            _warn(f"discovery request failed at start={start}; stopping with "
                  f"{len(names)} item(s) so far (raise --delay and retry for the rest)")
            break
        results = data.get("results") or []
        total_count = data.get("total_count", total_count)
        if not results:
            break

        added = 0
        for row in results:
            hn = row.get("hash_name") or row.get("name")
            if hn and hn not in seen:
                seen.add(hn)
                names.append(hn)
                added += 1
        start += len(results)

        if limit is None and total_count:
            print(f"  discovered {len(names)}/{total_count} ...")
        if total_count and start >= total_count:
            break
        stale_pages = stale_pages + 1 if added == 0 else 0
        if stale_pages >= 3:
            break
    return names if limit is None else names[:limit]


def search_asset(client: SteamClient, appid: int, name: str) -> dict | None:
    """Exact-match lookup of one item via `market/search/render` (still a plain
    JSON endpoint). Returns its lightweight asset_description (classid,
    instanceid, commodity/tradable flags, colors, icon) plus the active
    listing count.

    Steam's listing pages (``market/listings/<appid>/<name>`` and its
    ``/render/`` variant) were migrated to a client-rendered SPA that no
    longer embeds this data server-side, so search is now the only
    unauthenticated source for item identity/tags. (Search results also
    carry a `sell_price`, but it's served off a periodically refreshed
    index and can disagree with the live market by a lot, so it's
    deliberately not used here - `market/priceoverview` is the accurate
    current price.)
    """
    data = client.get_json(
        f"{MARKET}/search/render/",
        params={"query": name, "appid": appid, "norender": 1, "count": 10,
                "currency": client.currency},
    ) or {}
    for row in data.get("results") or []:
        ad = row.get("asset_description") or {}
        if ad.get("market_hash_name") == name or row.get("hash_name") == name:
            return {"asset": ad, "sell_listings": int_or_none(row.get("sell_listings"))}
    return None


def fetch_asset_class_info(client: SteamClient, appid: int, classid: str,
                           instanceid: str, api_key: str) -> dict | None:
    """Full item description (incl. tags) via the documented
    ``ISteamEconomy/GetAssetClassInfo`` Web API. Needs a free key from
    https://steamcommunity.com/dev/apikey - the classic listing page's
    embedded `g_rgAssets` (the old tag source) no longer exists."""
    params = {"key": api_key, "appid": appid, "class_count": 1, "classid0": classid}
    if instanceid and instanceid != "0":
        params["instanceid0"] = instanceid
    data = client.get_json(
        "https://api.steampowered.com/ISteamEconomy/GetAssetClassInfo/v1/",
        params=params,
    ) or {}
    return (data.get("result") or {}).get(str(classid))


def fetch_official_history(client: SteamClient, appid: int, name: str) -> list[list]:
    data = client.get_json(
        f"{MARKET}/pricehistory/",
        params={"appid": appid, "market_hash_name": name, "currency": client.currency},
        referer=listing_url(appid, name),
    )
    if data and data.get("success") and data.get("prices"):
        return data["prices"]
    return []


def fetch_overview(client: SteamClient, appid: int, name: str) -> dict:
    data = client.get_json(
        f"{MARKET}/priceoverview/",
        params={"appid": appid, "market_hash_name": name, "currency": client.currency},
        referer=listing_url(appid, name),
    ) or {}
    return {
        "snap_lowest_price": money_to_float(data.get("lowest_price")),
        "snap_median_price": money_to_float(data.get("median_price")),
        "snap_volume_24h": int_or_none(data.get("volume")),
    }


# --------------------------------------------------------------------------- #
# Time series + feature engineering (pure stdlib)
# --------------------------------------------------------------------------- #
def to_daily(raw: list[list]) -> list[dict]:
    """[[date_str, price, volume_str], ...] -> sorted list of daily observations."""
    buckets: dict[datetime, list[tuple[float, int]]] = {}
    for row in raw:
        try:
            ts = parse_steam_ts(row[0])
            price = float(row[1])
            vol = int_or_none(row[2]) or 0
        except (ValueError, IndexError, TypeError):
            continue
        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets.setdefault(day, []).append((price, vol))

    daily = []
    for day in sorted(buckets):
        pairs = buckets[day]
        tot_vol = sum(v for _, v in pairs)
        if tot_vol > 0:
            price = sum(p * v for p, v in pairs) / tot_vol
        else:
            price = statistics.fmean(p for p, _ in pairs)
        daily.append({"date": day, "price": round(price, 4), "volume": tot_vol})
    return daily


def fill_gaps(daily: list[dict]) -> list[dict]:
    if not daily:
        return daily
    out = []
    cur = daily[0]["date"]
    idx = {d["date"]: d for d in daily}
    last_price = daily[0]["price"]
    end = daily[-1]["date"]
    while cur <= end:
        if cur in idx:
            last_price = idx[cur]["price"]
            out.append({**idx[cur], "is_filled": 0})
        else:
            out.append({"date": cur, "price": last_price, "volume": 0, "is_filled": 1})
        cur += timedelta(days=1)
    return out


def _win(seq, i, n):
    return seq[max(0, i - n + 1): i + 1]


def _pct(new, old):
    if old in (None, 0) or new is None:
        return None
    return round((new - old) / old, 6)


def add_features(series: list[dict]) -> list[dict]:
    prices = [r["price"] for r in series]
    vols = [r["volume"] for r in series]
    ath = -math.inf
    atl = math.inf
    ema10 = None
    k = 2 / (10 + 1)
    first_day = series[0]["date"] if series else None

    for i, r in enumerate(series):
        p, v, d = prices[i], vols[i], r["date"]
        ath = max(ath, p)
        atl = min(atl, p)
        ema10 = p if ema10 is None else (p * k + ema10 * (1 - k))

        pw7, pw30 = _win(prices, i, 7), _win(prices, i, 30)
        vw7, vw30 = _win(vols, i, 7), _win(vols, i, 30)

        r["price_lag_1"] = prices[i - 1] if i >= 1 else None
        r["price_lag_7"] = prices[i - 7] if i >= 7 else None
        r["price_lag_30"] = prices[i - 30] if i >= 30 else None
        r["price_ma_7"] = round(statistics.fmean(pw7), 4)
        r["price_ma_30"] = round(statistics.fmean(pw30), 4)
        r["price_ema_10"] = round(ema10, 4)
        r["price_std_7"] = round(statistics.pstdev(pw7), 4) if len(pw7) > 1 else 0.0
        r["price_std_30"] = round(statistics.pstdev(pw30), 4) if len(pw30) > 1 else 0.0
        r["price_return_1d"] = _pct(p, r["price_lag_1"])
        r["price_return_7d"] = _pct(p, r["price_lag_7"])
        r["price_return_30d"] = _pct(p, r["price_lag_30"])
        mu30 = statistics.fmean(pw30)
        sd30 = statistics.pstdev(pw30) if len(pw30) > 1 else 0.0
        r["price_zscore_30"] = round((p - mu30) / sd30, 4) if sd30 else 0.0
        r["price_min_30"] = round(min(pw30), 4)
        r["price_max_30"] = round(max(pw30), 4)

        r["volume_lag_1"] = vols[i - 1] if i >= 1 else None
        r["volume_ma_7"] = round(statistics.fmean(vw7), 4)
        r["volume_ma_30"] = round(statistics.fmean(vw30), 4)
        r["volume_std_7"] = round(statistics.pstdev(vw7), 4) if len(vw7) > 1 else 0.0

        r["ath"] = round(ath, 4)
        r["atl"] = round(atl, 4)
        r["dist_from_ath_pct"] = round((p - ath) / ath, 6) if ath else None
        r["dist_from_atl_pct"] = round((p - atl) / atl, 6) if atl else None
        r["days_since_first_obs"] = (d - first_day).days

        r["dow"] = d.weekday()
        r["month"] = d.month
        r["day_of_year"] = d.timetuple().tm_yday
        r["is_weekend"] = 1 if d.weekday() >= 5 else 0

        r["target_price_next_1d"] = prices[i + 1] if i + 1 < len(prices) else None
        r["target_price_next_7d"] = prices[i + 7] if i + 7 < len(prices) else None
        r["target_return_next_7d"] = (
            _pct(prices[i + 7], p) if i + 7 < len(prices) else None
        )
    return series


# --------------------------------------------------------------------------- #
# CSV assembly
# --------------------------------------------------------------------------- #
META_COLS = ["scrape_timestamp_utc", "appid", "market_hash_name", "item_name",
             "item_type", "currency", "classid", "instanceid", "commodity",
             "tradable", "marketable", "name_color", "background_color",
             "icon_url", "quality", "rarity", "type", "hero",
             "klass", "exterior", "collection", "tags_json"]
TS_COLS = ["observation_date", "price", "volume", "is_filled"]
FEATURE_COLS = ["price_lag_1", "price_lag_7", "price_lag_30", "price_ma_7",
                "price_ma_30", "price_ema_10", "price_std_7", "price_std_30",
                "price_return_1d", "price_return_7d", "price_return_30d",
                "price_zscore_30", "price_min_30", "price_max_30", "volume_lag_1",
                "volume_ma_7", "volume_ma_30", "volume_std_7", "ath", "atl",
                "dist_from_ath_pct", "dist_from_atl_pct", "days_since_first_obs",
                "dow", "month", "day_of_year", "is_weekend",
                "target_price_next_1d", "target_price_next_7d",
                "target_return_next_7d"]
SNAP_COLS = ["snap_lowest_price", "snap_median_price", "snap_volume_24h",
             "total_listings"]
ALL_COLS = META_COLS + TS_COLS + FEATURE_COLS + SNAP_COLS
WIDE_ID_COLS = META_COLS + SNAP_COLS

ICON_CDN = "https://community.cloudflare.steamstatic.com/economy/image/"


def pivot_wide(rows_by_item: list[list[dict]],
               since: str | None = None) -> tuple[list[str], list[dict]]:
    """Long (one row per item-day) -> wide (one row per item, one
    `price_<date>` column per observation date across the whole run).

    `since` (an ISO ``YYYY-MM-DD`` string) keeps only observation dates on or
    after that day, e.g. to emit just the trailing 60 days of daily prices.
    """
    all_dates: set[str] = set()
    wide_rows = []
    for rows in rows_by_item:
        if not rows:
            continue
        by_date = {r["observation_date"]: r["price"] for r in rows
                   if since is None or r["observation_date"] >= since}
        all_dates.update(by_date)
        wide_row = {c: rows[0].get(c) for c in WIDE_ID_COLS}
        wide_row["_prices"] = by_date
        wide_rows.append(wide_row)

    date_cols = [f"price_{d}" for d in sorted(all_dates)]
    for wide_row in wide_rows:
        by_date = wide_row.pop("_prices")
        for d in all_dates:
            wide_row[f"price_{d}"] = by_date.get(d)

    return WIDE_ID_COLS + date_cols, wide_rows


def scrape_item(client: SteamClient, appid: int, name: str, *,
                api_key: str | None, fill: bool) -> list[dict]:
    now = datetime.now(timezone.utc)
    authed = bool(client.s.cookies.get("steamLoginSecure"))

    # Price history is login-gated: Steam removed the unauthenticated
    # `line1` embed from listing pages, so `market/pricehistory/` (itself
    # cookie-gated) is now the only source.
    raw = fetch_official_history(client, appid, name) if authed else []
    if not raw:
        _warn(f"no price history for {name!r} "
              f"({'endpoint returned nothing' if authed else 'needs --cookie / STEAM_LOGIN_SECURE'})")

    # Identity/tags: listing pages no longer embed `g_rgAssets` either, so
    # look the item up via search (classid/instanceid + flags), then pull
    # full tags from the official GetAssetClassInfo Web API if a key is set.
    found = search_asset(client, appid, name)
    meta = _empty_meta()
    ident = {c: None for c in
             ("classid", "instanceid", "commodity", "tradable", "marketable",
              "name_color", "background_color", "icon_url")}
    total_listings = None
    if found:
        ad = found["asset"]
        classid = ad.get("classid")
        instanceid = ad.get("instanceid") or "0"
        ident.update(
            classid=classid,
            instanceid=instanceid,
            commodity=int_or_none(ad.get("commodity")),
            tradable=int_or_none(ad.get("tradable")),
            name_color=ad.get("name_color"),
            background_color=ad.get("background_color"),
            icon_url=(ICON_CDN + ad["icon_url"]) if ad.get("icon_url") else None,
        )
        meta.update(item_name=ad.get("market_name") or ad.get("name"), item_type=ad.get("type"))
        # NB: search/render's own `sell_price` is served off a periodically
        # refreshed search index and can lag the live market by a lot -
        # `priceoverview` below is the authoritative current price, so we
        # only keep the listing count (no equivalent elsewhere) from search.
        total_listings = found.get("sell_listings")

        if api_key and classid:
            asset_info = fetch_asset_class_info(client, appid, classid, instanceid, api_key)
            if asset_info:
                ident["marketable"] = int_or_none(asset_info.get("marketable"))
                meta = merge_meta(meta_from_asset(asset_info), meta)
    elif not api_key:
        _warn("no --api-key / STEAM_API_KEY: item tags will be empty "
              "(get a free key at https://steamcommunity.com/dev/apikey)")

    overview = fetch_overview(client, appid, name)
    snapshot = {
        **overview,
        "total_listings": total_listings,
    }

    base = {
        "scrape_timestamp_utc": now.isoformat(),
        "appid": appid,
        "market_hash_name": name,
        "item_name": meta.get("item_name") or name,
        "item_type": meta.get("item_type"),
        "currency": CURRENCIES.get(client.currency, str(client.currency)),
        "quality": meta.get("quality"),
        "rarity": meta.get("rarity"),
        "type": meta.get("type"),
        "hero": meta.get("hero"),
        "klass": meta.get("klass"),
        "exterior": meta.get("exterior"),
        "collection": meta.get("collection"),
        "tags_json": meta.get("tags_json"),
        **ident,
        **snapshot,
    }

    daily = to_daily(raw)
    if fill:
        daily = fill_gaps(daily)
    else:
        for r in daily:
            r["is_filled"] = 0

    if not daily:
        # No history available -> still emit a single snapshot row.
        price = overview.get("snap_median_price") or overview.get("snap_lowest_price")
        if price is None:
            _warn(f"no data at all for {name!r}; skipped")
            return []
        row = {**base, **{c: None for c in FEATURE_COLS}}
        row["observation_date"] = now.date().isoformat()
        row["price"] = price
        row["volume"] = overview.get("snap_volume_24h")
        row["is_filled"] = 1
        return [row]

    add_features(daily)
    rows = []
    for r in daily:
        row = {**base}
        row["observation_date"] = r["date"].date().isoformat()
        for c in TS_COLS[1:] + FEATURE_COLS:
            row[c] = r.get(c)
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--appid", type=int, default=440,
                    help="Steam app id (default 440 = Team Fortress 2; 730 = CS2, 570 = Dota 2)")
    ap.add_argument("--item", action="append", default=[], dest="items",
                    help="market_hash_name to scrape (repeatable)")
    ap.add_argument("--items-file", help="file with one market_hash_name per line")
    ap.add_argument("--discover", type=int, default=0,
                    help="auto-discover N most-traded items for the app")
    ap.add_argument("--discover-all", action="store_true",
                    help="discover every marketable item for the app, ignoring "
                         "--discover's limit (can be thousands of items and take "
                         "a very long time at the default --delay)")
    ap.add_argument("-o", "--output", default="steam_market.csv")
    ap.add_argument("--append", action="store_true",
                    help="append to the CSV instead of overwriting")
    ap.add_argument("--wide", action="store_true",
                    help="write one row per item with a price_<date> column per "
                         "observation date, instead of one row per item-day "
                         "(not compatible with --append)")
    ap.add_argument("--wide-days", type=int, default=0, metavar="N",
                    help="with --wide, keep only the last N days of price_<date> "
                         "columns (e.g. 60 to cover the 7/30/60-day windows)")
    ap.add_argument("--currency", type=int, default=1,
                    help="Steam currency code (1 USD, 3 EUR, 2 GBP, 7 BRL, ...)")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between requests (default 3; lower risks 429)")
    ap.add_argument("--no-fill-gaps", action="store_true",
                    help="do not forward-fill days with no sales")
    ap.add_argument("--cookie", default=os.environ.get("STEAM_LOGIN_SECURE"),
                    help="steamLoginSecure cookie value (or env STEAM_LOGIN_SECURE), "
                         "needed for price history")
    ap.add_argument("--api-key", default=os.environ.get("STEAM_API_KEY"),
                    help="Steam Web API key (or env STEAM_API_KEY), needed for item "
                         "tags/quality/rarity/exterior - get one free at "
                         "https://steamcommunity.com/dev/apikey")
    args = ap.parse_args(argv)

    names: list[str] = list(args.items)
    if args.items_file:
        with open(args.items_file, encoding="utf-8") as fh:
            names += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]

    client = SteamClient(currency=args.currency, delay=args.delay, cookie=args.cookie)
    if not args.cookie:
        _warn("no steamLoginSecure cookie: historical prices are login-gated by "
              "Steam, so rows will mostly be current-snapshot only. "
              "Pass --cookie or set STEAM_LOGIN_SECURE for full history.")
    if not args.api_key:
        _warn("no Steam Web API key: item tags (quality/rarity/exterior/...) will "
              "be empty. Pass --api-key or set STEAM_API_KEY "
              "(free at https://steamcommunity.com/dev/apikey).")

    if args.discover_all:
        print(f"Discovering ALL marketable items for app {args.appid} ...")
        names += [n for n in discover_items(client, args.appid, None)
                  if n not in names]
    elif args.discover:
        print(f"Discovering up to {args.discover} items for app {args.appid} ...")
        names += [n for n in discover_items(client, args.appid, args.discover)
                  if n not in names]

    if args.wide_days:
        if args.wide_days < 0:
            ap.error("--wide-days must be positive")
        args.wide = True  # --wide-days only makes sense in wide output

    names = list(dict.fromkeys(names))
    if not names:
        ap.error("no items: pass --item, --items-file, --discover, or --discover-all")
    if args.append and args.wide:
        ap.error("--wide is not compatible with --append (each run's date "
                  "columns differ, so wide output can't be appended safely)")

    if args.wide:
        rows_by_item = []
        for i, name in enumerate(names, 1):
            print(f"[{i}/{len(names)}] {name}")
            try:
                rows = scrape_item(client, args.appid, name,
                                   api_key=args.api_key,
                                   fill=not args.no_fill_gaps)
            except Exception as exc:  # keep going on a single bad item
                _warn(f"failed on {name!r}: {exc}")
                continue
            rows_by_item.append(rows)
            print(f"    +{len(rows)} observations")

        since = None
        if args.wide_days:
            since = (datetime.now(timezone.utc).date()
                     - timedelta(days=args.wide_days - 1)).isoformat()
        fieldnames, wide_rows = pivot_wide(rows_by_item, since=since)
        with open(args.output, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(wide_rows)
        print(f"\nDone: {len(wide_rows)} items ({len(fieldnames) - len(WIDE_ID_COLS)} "
              f"price columns) -> {args.output}")
        return

    mode = "a" if args.append else "w"
    exists = os.path.exists(args.output) and os.path.getsize(args.output) > 0
    total = 0
    with open(args.output, mode, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ALL_COLS, extrasaction="ignore")
        if not (args.append and exists):
            writer.writeheader()
        for i, name in enumerate(names, 1):
            print(f"[{i}/{len(names)}] {name}")
            try:
                rows = scrape_item(client, args.appid, name,
                                   api_key=args.api_key,
                                   fill=not args.no_fill_gaps)
            except Exception as exc:  # keep going on a single bad item
                _warn(f"failed on {name!r}: {exc}")
                continue
            writer.writerows(rows)
            fh.flush()
            total += len(rows)
            print(f"    +{len(rows)} rows")

    print(f"\nDone: {total} rows -> {args.output}")


if __name__ == "__main__":
    main()
