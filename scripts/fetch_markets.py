#!/usr/bin/env python3
"""Fetch prior-session closes and write site/data/markets.json.

Sources, in order, all keyless:
  - Yahoo Finance chart API for listed prices, FX, futures, crypto, VIX
  - US Treasury daily par yield curve for 2Y / 10Y / 30Y
  - FRED public CSV if Treasury is down (DGS2 / DGS10 / DGS30)
  - CoinGecko daily market chart if a crypto Yahoo symbol fails

The as-of date is the US cash session of the anchor (SPY). Bars dated after
that session are dropped so an overnight futures or crypto print is not mixed
with the prior equity close. While the US cash session is still open, today's
partial US bar is dropped too.

No third-party packages. Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "universe.json"
OUTPUT = ROOT / "site" / "data" / "markets.json"
NY = ZoneInfo("America/New_York")
UA = (
    "Mozilla/5.0 (compatible; markets-dashboard/1.0; "
    "+https://github.com/kalapratik/markets-dashboard)"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/csv,*/*"}


class FetchError(Exception):
    pass


def http_bytes(opener, url: str, timeout: int = 30, attempts: int = 3) -> bytes:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and i + 1 < attempts:
                time.sleep(1.2 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(1.2 * (i + 1))
                continue
            raise
    raise FetchError(str(last))


def make_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def clean(value, nd: int = 6):
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, nd)


def session_of(group: dict, inst: dict) -> str:
    return inst.get("session") or group.get("session") or "other"


def iter_instruments(cfg: dict):
    for group in cfg["groups"]:
        for inst in group["instruments"]:
            yield group, inst


def ny_session_closed(now: datetime) -> bool:
    if now.weekday() >= 5:
        return True
    close = now.replace(hour=16, minute=15, second=0, microsecond=0)
    return now >= close


def trim_partial_cash(bars: list, today: date, closed: bool) -> list:
    if closed or not bars:
        return bars
    if bars[-1][0] >= today:
        return [bar for bar in bars if bar[0] < today]
    return bars


def bars_from_yahoo(payload: dict) -> list:
    chart = payload.get("chart") or {}
    result = chart.get("result")
    if not result:
        err = chart.get("error") or {}
        raise FetchError(err.get("description") or "empty yahoo chart")
    res = result[0]
    timestamps = res.get("timestamp") or []
    indicators = res.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    closes = quote.get("close") or []
    adj_block = indicators.get("adjclose") or []
    adj = (adj_block[0].get("adjclose") if adj_block else None) or []
    by_date = {}
    for i, ts in enumerate(timestamps):
        px = None
        if i < len(adj) and adj[i] is not None:
            px = adj[i]
        elif i < len(closes) and closes[i] is not None:
            px = closes[i]
        if px is None:
            continue
        by_date[datetime.fromtimestamp(ts, timezone.utc).date()] = float(px)
    bars = sorted(by_date.items())
    if len(bars) < 2:
        raise FetchError("yahoo returned fewer than 2 closes")
    return bars


def yahoo_crumb(opener) -> str:
    crumb_url = "https://query1.finance.yahoo.com/v1/test/getcrumb"

    def read_crumb() -> str:
        raw = http_bytes(opener, crumb_url, timeout=15, attempts=2)
        crumb = raw.decode("utf-8", "replace").strip()
        if not crumb or "<" in crumb or len(crumb) > 64:
            raise FetchError("yahoo crumb missing")
        return crumb

    try:
        return read_crumb()
    except urllib.error.HTTPError:
        pass
    try:
        http_bytes(opener, "https://fc.yahoo.com", timeout=15, attempts=1)
    except urllib.error.HTTPError:
        # 404 still plants the cookie Yahoo expects before issuing a crumb.
        pass
    return read_crumb()


def fetch_yahoo(opener, symbol: str, crumb_box: dict) -> list:
    start = int(time.time()) - 500 * 86400
    end = int(time.time()) + 86400
    quoted = urllib.parse.quote(symbol, safe="")
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    last_err = None
    for host in hosts:
        url = (
            f"https://{host}/v8/finance/chart/{quoted}"
            f"?period1={start}&period2={end}&interval=1d"
            "&includeAdjustedClose=true&events=div%7Csplit"
        )
        if crumb_box.get("value"):
            url += "&crumb=" + urllib.parse.quote(crumb_box["value"])
        try:
            body = http_bytes(opener, url, timeout=25, attempts=2)
            return bars_from_yahoo(json.loads(body.decode("utf-8")))
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (401, 403) and not crumb_box.get("value"):
                crumb_box["value"] = yahoo_crumb(opener)
                url += "&crumb=" + urllib.parse.quote(crumb_box["value"])
                body = http_bytes(opener, url, timeout=25, attempts=2)
                return bars_from_yahoo(json.loads(body.decode("utf-8")))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, FetchError) as exc:
            last_err = exc
    raise FetchError(f"yahoo {symbol}: {last_err}")


def parse_treasury(text: str, fields: list) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not any(name.strip() == "Date" for name in reader.fieldnames):
        raise FetchError("treasury CSV missing Date column")
    found = {f: {} for f in fields}
    for row in reader:
        normalized = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        raw_date = normalized.get("Date", "")
        if not raw_date:
            continue
        session = datetime.strptime(raw_date, "%m/%d/%Y").date()
        for field in fields:
            raw = normalized.get(field, "")
            if raw in ("", "."):
                continue
            found[field][session] = float(raw)
    return found


def fetch_treasury(opener, fields: list) -> dict:
    merged = {field: {} for field in fields}
    errors = []
    years_ok = 0
    for year in (date.today().year, date.today().year - 1):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{year}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        )
        try:
            text = http_bytes(opener, url, timeout=40, attempts=2).decode("utf-8-sig")
            part = parse_treasury(text, fields)
            for field in fields:
                merged[field].update(part[field])
            years_ok += 1
        except Exception as exc:  # noqa: BLE001 - keep the other year / FRED path alive
            errors.append(f"{year}: {exc}")
    if years_ok == 0:
        raise FetchError("treasury unavailable (" + "; ".join(errors) + ")")
    bars = {field: sorted(points.items()) for field, points in merged.items()}
    for field, series in bars.items():
        if len(series) < 2:
            errors.append(f"{field}: short history")
    if errors:
        print("treasury notes: " + "; ".join(errors), file=sys.stderr)
    return bars


def fetch_fred(opener, series: str) -> list:
    start = (date.today() - timedelta(days=520)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series)}&cosd={start}"
    text = http_bytes(opener, url, timeout=40, attempts=2).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    bars = []
    for row in reader:
        raw = (row.get(series) or "").strip()
        if raw in ("", "."):
            continue
        session = datetime.strptime(row["observation_date"], "%Y-%m-%d").date()
        bars.append((session, float(raw)))
    if len(bars) < 2:
        raise FetchError(f"fred {series}: short history")
    return bars


def fetch_currency_pair(base: str, quote: str) -> list:
    """Weekday spot from the keyless fawazahmed0 currency-api.

    Yahoo's USDCNH chart is a single live quote, so the offshore yuan uses this
    daily file. Weekends are omitted so 1D/1W count weekday sessions.
    """
    start = date.today() - timedelta(days=460)
    days = []
    cursor = start
    today = date.today()
    while cursor <= today:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)

    def pull(batch: list) -> tuple[dict, list]:
        found = {}
        failed = []

        def one(session: date):
            url = (
                "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@"
                f"{session.isoformat()}/v1/currencies/{base}.min.json"
            )
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            px = (payload.get(base) or {}).get(quote)
            if px is None:
                raise FetchError("missing quote")
            return session, float(px)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(one, session): session for session in batch}
            for future in as_completed(futures):
                session = futures[future]
                try:
                    day, px = future.result()
                    found[day] = px
                except Exception:  # noqa: BLE001 - one missing day should not kill the pair
                    failed.append(session)
        return found, failed

    points, failed = pull(days)
    if failed:
        recovered, failed = pull(failed)
        points.update(recovered)
    bars = sorted(points.items())
    if len(bars) < 60:
        raise FetchError(f"currency-api {base}/{quote}: only {len(bars)} days")
    if failed:
        print(f"currency-api notes: {len(failed)} days missing", file=sys.stderr)
    return bars


def fetch_coingecko(opener, coin_id: str) -> list:
    url = (
        "https://api.coingecko.com/api/v3/coins/"
        f"{urllib.parse.quote(coin_id)}/market_chart?vs_currency=usd&days=500&interval=daily"
    )
    try:
        body = http_bytes(opener, url, timeout=30, attempts=2)
    except urllib.error.HTTPError:
        # The keyless API sometimes rejects interval=daily. Daily-bucket the raw series.
        url = (
            "https://api.coingecko.com/api/v3/coins/"
            f"{urllib.parse.quote(coin_id)}/market_chart?vs_currency=usd&days=365"
        )
        body = http_bytes(opener, url, timeout=30, attempts=2)
    payload = json.loads(body.decode("utf-8"))
    by_date = {}
    for ts_ms, px in payload.get("prices") or []:
        if px is None:
            continue
        by_date[datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date()] = float(px)
    bars = sorted(by_date.items())
    if len(bars) < 2:
        raise FetchError(f"coingecko {coin_id}: short history")
    return bars


def change(last: float, previous, kind: str):
    if previous is None:
        return None
    if kind == "yield":
        return (last - previous) * 100.0
    if previous == 0:
        return None
    return (last / previous - 1.0) * 100.0


def metrics(bars: list, as_of: date, kind: str) -> dict:
    usable = [(d, v) for d, v in bars if d <= as_of]
    if not usable:
        raise FetchError("no bars on or before as-of")
    last_d, last = usable[-1]

    def ago(sessions: int):
        index = len(usable) - 1 - sessions
        if index < 0:
            return None
        return usable[index][1]

    year_end = date(as_of.year - 1, 12, 31)
    ytd_base = None
    for session, value in usable:
        if session <= year_end:
            ytd_base = value
        else:
            break
    window_start = as_of - timedelta(days=365)
    window = [value for session, value in usable if session >= window_start] or [last]
    return {
        "last": last,
        "lastDate": last_d.isoformat(),
        "chg": {
            "d1": change(last, ago(1), kind),
            "w1": change(last, ago(5), kind),
            "m1": change(last, ago(21), kind),
            "ytd": change(last, ytd_base, kind),
        },
        "low52": min(window),
        "high52": max(window),
        "spark": [value for _, value in usable[-63:]],
    }


def determine_as_of(cfg: dict, series: dict, today: date, closed: bool) -> date:
    anchor = cfg.get("anchor")
    if anchor and series.get(anchor):
        bars = trim_partial_cash(series[anchor], today, closed) if anchor_is_cash(cfg, anchor) else series[anchor]
        if bars:
            return bars[-1][0]
    cash_dates = []
    for group, inst in iter_instruments(cfg):
        if session_of(group, inst) != "us_cash":
            continue
        bars = trim_partial_cash(series.get(inst["id"]) or [], today, closed)
        if bars:
            cash_dates.append(bars[-1][0])
    if cash_dates:
        return max(cash_dates)
    dates = [bars[-1][0] for bars in series.values() if bars]
    if not dates:
        raise FetchError("no dated bars")
    return max(dates)


def anchor_is_cash(cfg: dict, anchor: str) -> bool:
    for group, inst in iter_instruments(cfg):
        if inst["id"] == anchor:
            return session_of(group, inst) == "us_cash"
    return False


def row_from(inst: dict, source: str, stats: dict, symbol: str | None = None) -> dict:
    kind = inst.get("kind") or "price"
    row = {
        "id": inst["id"],
        "name": inst["name"],
        "kind": kind,
        "decimals": int(inst.get("decimals", 2)),
        "source": source,
        "last": clean(stats["last"]),
        "lastDate": stats["lastDate"],
        "chg": {key: clean(value, 4) for key, value in stats["chg"].items()},
        "low52": clean(stats["low52"]),
        "high52": clean(stats["high52"]),
        "spark": [clean(value) for value in stats["spark"]],
    }
    if symbol:
        row["symbol"] = symbol
    return row


def to_json(obj, level: int = 0) -> str:
    pad = "  " * level
    if isinstance(obj, dict):
        lines = [f"{pad}  {json.dumps(key)}: {to_json(value, level + 1)}" for key, value in obj.items()]
        return "{\n" + ",\n".join(lines) + f"\n{pad}}}"
    if isinstance(obj, list):
        if all(isinstance(item, (int, float)) or item is None for item in obj):
            return json.dumps(obj)
        inner = ",\n".join("  " * (level + 1) + to_json(item, level + 1) for item in obj)
        return "[\n" + inner + f"\n{pad}]"
    return json.dumps(obj)


def load_universe() -> dict:
    cfg = json.loads(UNIVERSE.read_text())
    seen = set()
    count = 0
    for _, inst in iter_instruments(cfg):
        count += 1
        if inst["id"] in seen:
            raise SystemExit(f"duplicate instrument id {inst['id']}")
        seen.add(inst["id"])
        source = inst.get("source") or "yahoo"
        if source == "yahoo" and not inst.get("symbol"):
            raise SystemExit(f"{inst['id']} needs a Yahoo symbol")
        if source == "treasury" and not inst.get("field"):
            raise SystemExit(f"{inst['id']} needs a Treasury field")
        if source == "currency-api" and not inst.get("quote"):
            raise SystemExit(f"{inst['id']} needs a currency-api quote")
    if count == 0:
        raise SystemExit("universe has no instruments")
    return cfg


def build(cfg: dict) -> dict:
    opener = make_opener()
    crumb_box = {}
    series = {}
    sources = {}
    failures = []
    now_ny = datetime.now(NY)
    closed = ny_session_closed(now_ny)

    treasury_fields = []
    for _, inst in iter_instruments(cfg):
        if (inst.get("source") or "yahoo") == "treasury":
            treasury_fields.append(inst["field"])
    treasury = {}
    treasury_error = None
    if treasury_fields:
        try:
            treasury = fetch_treasury(opener, treasury_fields)
        except Exception as exc:  # noqa: BLE001
            treasury_error = str(exc)
            print(f"treasury failed, will try FRED: {exc}", file=sys.stderr)

    for group, inst in iter_instruments(cfg):
        iid = inst["id"]
        source_name = inst.get("source") or "yahoo"
        try:
            symbol_used = inst.get("symbol") or inst.get("field") or ""
            if source_name == "treasury":
                bars = list(treasury.get(inst["field"]) or [])
                used = "treasury"
                if len(bars) < 2:
                    if not inst.get("fred"):
                        detail = treasury_error or f"no Treasury column {inst['field']}"
                        raise FetchError(detail)
                    bars = fetch_fred(opener, inst["fred"])
                    used = "fred"
                    symbol_used = inst["fred"]
            elif source_name == "currency-api":
                try:
                    bars = fetch_currency_pair(inst.get("base", "usd"), inst["quote"])
                    used = "currency-api"
                    symbol_used = f"{inst.get('base', 'usd')}/{inst['quote']}"
                except Exception as api_exc:
                    if not inst.get("fallbackSymbol"):
                        raise FetchError(str(api_exc)) from api_exc
                    print(f"{iid} currency-api failed ({api_exc}); trying {inst['fallbackSymbol']}", file=sys.stderr)
                    bars = fetch_yahoo(opener, inst["fallbackSymbol"], crumb_box)
                    used = "yahoo"
                    symbol_used = inst["fallbackSymbol"]
            else:
                symbols = [inst["symbol"]]
                if inst.get("fallbackSymbol"):
                    symbols.append(inst["fallbackSymbol"])
                bars = None
                used = "yahoo"
                last_exc = None
                for index, sym in enumerate(symbols):
                    try:
                        candidate = fetch_yahoo(opener, sym, crumb_box)
                        if len(candidate) < 30 and index < len(symbols) - 1:
                            raise FetchError(f"{sym}: only {len(candidate)} closes")
                        bars = candidate
                        symbol_used = sym
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        print(f"{iid} {sym} failed ({exc})", file=sys.stderr)
                if bars is None:
                    if inst.get("coingecko"):
                        print(f"{iid} trying CoinGecko", file=sys.stderr)
                        bars = fetch_coingecko(opener, inst["coingecko"])
                        used = "coingecko"
                        symbol_used = inst["coingecko"]
                    else:
                        raise FetchError(str(last_exc))
            if session_of(group, inst) == "us_cash":
                bars = trim_partial_cash(bars, now_ny.date(), closed)
            if len(bars) < 2:
                raise FetchError("not enough bars after session filter")
            series[iid] = bars
            sources[iid] = (used, symbol_used)
            last = bars[-1]
            print(f"OK  {iid:<8} {used:<12} {last[0].isoformat()}  {last[1]:.4f}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "id": iid,
                    "symbol": inst.get("symbol") or inst.get("field") or "",
                    "error": str(exc)[:240],
                }
            )
            print(f"FAIL {iid:<8} {exc}", file=sys.stderr, flush=True)
        time.sleep(0.08)

    ok = len(series)
    total = ok + len(failures)
    if ok == 0 or ok / total < 0.6:
        print(
            f"refusing to write snapshot: {ok}/{total} instruments succeeded",
            file=sys.stderr,
        )
        raise SystemExit(1)

    as_of = determine_as_of(cfg, series, now_ny.date(), closed)
    groups_out = []
    for group in cfg["groups"]:
        rows = []
        for inst in group["instruments"]:
            bars = series.get(inst["id"])
            if not bars:
                continue
            kind = inst.get("kind") or "price"
            try:
                stats = metrics(bars, as_of, kind)
            except FetchError as exc:
                failures.append({"id": inst["id"], "symbol": inst.get("symbol") or "", "error": str(exc)})
                continue
            used, symbol_used = sources[inst["id"]]
            rows.append(row_from(inst, used, stats, symbol_used))
        if not rows:
            continue
        payload = {"id": group["id"], "label": group["label"], "column": int(group.get("column", 1)), "rows": rows}
        if group.get("note"):
            payload["note"] = group["note"]
        groups_out.append(payload)

    return {
        "asOf": as_of.isoformat(),
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "anchor": cfg.get("anchor"),
        "cashSessionClosed": closed,
        "okCount": sum(len(group["rows"]) for group in groups_out),
        "groups": groups_out,
        "failures": failures,
    }


def write_output(payload: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(to_json(payload) + "\n")
    tmp.replace(OUTPUT)


def self_test() -> None:
    bars = [
        (date(2025, 12, 30), 100.0),
        (date(2025, 12, 31), 110.0),
        (date(2026, 1, 2), 120.0),
    ]
    # 21 sessions of climb so 1M is defined, then a last print.
    start = date(2026, 2, 2)
    for i in range(30):
        bars.append((start + timedelta(days=i), 130.0 + i))
    as_of = bars[-1][0]
    stats = metrics(bars, as_of, "price")
    assert stats["last"] == bars[-1][1]
    assert abs(stats["chg"]["d1"] - ((bars[-1][1] / bars[-2][1] - 1) * 100)) < 1e-9
    assert abs(stats["chg"]["w1"] - ((bars[-1][1] / bars[-6][1] - 1) * 100)) < 1e-9
    assert abs(stats["chg"]["ytd"] - ((bars[-1][1] / 110.0 - 1) * 100)) < 1e-9
    assert stats["low52"] <= stats["last"] <= stats["high52"]
    yld = metrics([(date(2026, 11, 24), 4.50), (date(2026, 11, 25), 4.61)], date(2026, 11, 25), "yield")
    assert abs(yld["chg"]["d1"] - 11.0) < 1e-9
    trimmed = trim_partial_cash(bars, bars[-1][0], closed=False)
    assert trimmed[-1][0] < bars[-1][0]
    kept = trim_partial_cash(bars, bars[-1][0], closed=True)
    assert kept[-1][0] == bars[-1][0]
    filtered = metrics(bars + [(as_of + timedelta(days=1), 999.0)], as_of, "price")
    assert filtered["last"] == bars[-1][1]
    print("self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch EOD market data into site/data/markets.json")
    parser.add_argument("--self-test", action="store_true", help="run offline metric checks and exit")
    parser.add_argument("--credit-only", action="store_true", help="refresh site/data/credit.json and leave markets.json")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    cfg = load_universe()
    if args.credit_only:
        from fetch_credit import refresh_credit
        refresh_credit(cfg)
        return
    payload = build(cfg)
    write_output(payload)
    print(
        f"wrote {OUTPUT.relative_to(ROOT)} asOf={payload['asOf']} "
        f"ok={payload['okCount']} fail={len(payload['failures'])}"
    )
    try:
        from fetch_credit import refresh_credit
        refresh_credit(cfg)
    except Exception as exc:  # noqa: BLE001 - credit must not fail the equity snapshot
        print(f"credit refresh failed, left the last credit file: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
