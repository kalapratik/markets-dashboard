#!/usr/bin/env python3
"""AI / neocloud credit proxies for the EOD desk.

Basisline bond-implied 5Y CDS (keyless) and Atlantis cash-bond z-spreads
(free tier, 100 calls/hour per IP, no key). A missing Atlantis key is not an
error. History we have already stored is kept and appended, because the free
Atlantis tier does not return a z-spread path.

This module never raises out of refresh_credit(). A source failure leaves the
last good credit file in place when nothing new was priced.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "site" / "data" / "credit.json"
UA = "markets-dashboard (github.com/kalapratik/markets-dashboard)"
BASIS = "https://basisline.xyz"
ATLANTIS = "https://atlantisdatasolutions.com"
HISTORY_CAP = 90
SPARK = 63


def http_json(url: str, headers: dict, timeout: int = 40) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def basis_headers() -> dict:
    return {"User-Agent": UA, "Accept": "application/json"}


def atlantis_headers() -> dict:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    key = os.environ.get("ATLANTIS_API_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key
    return headers


def load_previous() -> dict:
    if not OUTPUT.exists():
        return {}
    try:
        return json.loads(OUTPUT.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def parse_history(raw) -> list:
    points = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            day, value = item[0], item[1]
        elif isinstance(item, dict):
            day, value = item.get("d") or item.get("date"), item.get("v") if "v" in item else item.get("level")
        else:
            continue
        if day and value is not None:
            points.append((str(day), float(value)))
    points.sort()
    return points[-HISTORY_CAP:]


def upsert(history: list, day: str, value: float) -> list:
    kept = [(d, v) for d, v in history if d != day]
    kept.append((day, float(value)))
    kept.sort()
    return kept[-HISTORY_CAP:]


def session_change(history: list, level_day: str, level: float):
    """1D only when the previous stored print is the prior session, not a weekly sample."""
    prior = [(day, value) for day, value in history if day < level_day]
    if not prior:
        return None
    prev_day, prev_value = prior[-1]
    gap = (date.fromisoformat(level_day) - date.fromisoformat(prev_day)).days
    if gap > 5:
        return None
    return round(level - prev_value, 1)


def prior_weekdays(end: date, count: int) -> list:
    days = []
    cursor = end - timedelta(days=1)
    while len(days) < count and cursor.year >= 2024:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def spark_from(history: list) -> list:
    return [round(value, 1) for _, value in history[-SPARK:]]


def basis_row(spec: dict, previous: dict | None) -> dict:
    headers = basis_headers()
    slug = spec["slug"]
    latest = http_json(f"{BASIS}/v1/curves/{urllib.parse.quote(slug)}/latest.json", headers)
    level = (latest.get("par_cds_bp") or {}).get("5.0")
    as_of = latest.get("as_of")
    if level is None or not as_of:
        raise RuntimeError(f"basisline {slug}: no 5y par CDS")
    history = parse_history((previous or {}).get("history"))
    try:
        series = http_json(f"{BASIS}/v1/history/{urllib.parse.quote(slug)}.json", headers)
        for point in series.get("series") or []:
            value = point.get("par_cds_5y_bp")
            day = point.get("date")
            if day and value is not None:
                history = upsert(history, day, float(value))
    except Exception as exc:  # noqa: BLE001 - latest print still stands
        print(f"credit {spec['id']} history skipped: {exc}", flush=True)
    history = upsert(history, as_of, float(level))
    delta = latest.get("delta_1d_bp")
    row = {
        "id": spec["id"],
        "name": spec["name"],
        "metric": spec.get("metric") or "Bond-implied 5Y CDS",
        "level": round(float(level), 1),
        "levelDate": as_of,
        "d1": round(float(delta), 1) if delta is not None else d1_from(history),
        "spark": spark_from(history),
        "price": None,
        "ytm": None,
        "gSpread": None,
        "source": "basisline",
        "history": [[d, round(v, 1)] for d, v in history],
    }
    status = latest.get("status") or ""
    if "volatile" in status:
        row["flag"] = "VOL"
    return row


def zspread_url(cusip: str, day: date | None) -> str:
    url = f"{ATLANTIS}/v1/bond/{urllib.parse.quote(cusip)}/zspread"
    if day is not None:
        url += "?date=" + day.isoformat()
    return url


def read_zspread(payload: dict):
    block = payload.get("zspread") or {}
    value = block.get("value_bp")
    day = block.get("date")
    price = block.get("price")
    if value is None or not day or block.get("status") not in (None, "available"):
        raise RuntimeError(payload.get("message") or "zspread unavailable")
    return str(day), float(value), None if price is None else float(price)


def atlantis_bond(spec: dict, previous: dict | None) -> dict:
    headers = atlantis_headers()
    candidates = [{"cusip": spec["cusip"], "coupon": spec["coupon"], "maturity": spec["maturity"]}]
    candidates.extend(spec.get("fallbacks") or [])
    last_err = None
    chosen = None
    latest = None
    for candidate in candidates:
        try:
            latest = http_json(zspread_url(candidate["cusip"], None), headers)
            chosen = candidate
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.15)
    if chosen is None or latest is None:
        raise RuntimeError(f"atlantis zspread: {last_err}")
    level_day, level, price = read_zspread(latest)
    history = parse_history((previous or {}).get("history"))
    level_date = date.fromisoformat(level_day)
    # The free tier serves recent dates and rejects the older path. Walk backward
    # from the prior session and stop at that wall. The first hit inside 5 days is 1D.
    restricted = 0
    want = 4 if len(history) >= 8 else 12
    for day in prior_weekdays(level_date, want):
        if any(existing == day.isoformat() for existing, _ in history):
            if session_change(history, level_day, level) is not None and len(history) >= 8:
                break
            continue
        try:
            payload = http_json(zspread_url(chosen["cusip"], day), headers)
            got_day, got_value, _ = read_zspread(payload)
            history = upsert(history, got_day, got_value)
            restricted = 0
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "free plan" in text or "not included" in text:
                restricted += 1
                if restricted >= 2:
                    break
            else:
                print(f"credit {spec['id']} {day.isoformat()} skipped: {exc}", flush=True)
        time.sleep(0.12)
    history = upsert(history, level_day, level)
    ytm = None
    g_spread = None
    try:
        analytics = http_json(
            f"{ATLANTIS}/v1/bond/{urllib.parse.quote(chosen['cusip'])}/analytics?date={level_day}",
            headers,
        )
        spread = analytics.get("spread") or {}
        if spread.get("bond_ytm_percent") is not None:
            ytm = round(float(spread["bond_ytm_percent"]), 2)
        if spread.get("spread_risk_free_bp") is not None:
            g_spread = round(float(spread["spread_risk_free_bp"]), 1)
    except Exception as exc:  # noqa: BLE001
        print(f"credit {spec['id']} analytics skipped: {exc}", flush=True)
    metric = f"Z-spread {chosen['coupon']} {chosen['maturity']}"
    return {
        "id": spec["id"],
        "name": spec["name"],
        "metric": metric,
        "level": round(level, 1),
        "levelDate": level_day,
        "d1": session_change(history, level_day, level),
        "spark": spark_from(history),
        "price": None if price is None else round(price, 2),
        "ytm": ytm,
        "gSpread": g_spread,
        "cusip": chosen["cusip"],
        "source": "atlantis",
        "history": [[d, round(v, 1)] for d, v in history],
    }


def placeholder(spec: dict, error: str) -> dict:
    return {
        "id": spec["id"],
        "name": spec["name"],
        "metric": spec.get("metric") or "—",
        "level": None,
        "levelDate": None,
        "d1": None,
        "spark": [],
        "price": None,
        "ytm": None,
        "gSpread": None,
        "source": spec.get("source"),
        "history": [],
        "error": error[:240],
    }


def to_json(obj, level: int = 0) -> str:
    pad = "  " * level
    if isinstance(obj, dict):
        lines = [f"{pad}  {json.dumps(key)}: {to_json(value, level + 1)}" for key, value in obj.items()]
        return "{\n" + ",\n".join(lines) + f"\n{pad}}}"
    if isinstance(obj, list):
        if all(isinstance(item, (int, float, str)) or item is None for item in obj):
            return json.dumps(obj)
        if obj and all(isinstance(item, list) and len(item) == 2 for item in obj):
            return json.dumps(obj)
        inner = ",\n".join("  " * (level + 1) + to_json(item, level + 1) for item in obj)
        return "[\n" + inner + f"\n{pad}]"
    return json.dumps(obj)


def refresh_credit(cfg: dict) -> None:
    specs = cfg.get("credit") or []
    if not specs:
        print("credit: no rows configured", flush=True)
        return
    previous = load_previous()
    prev_rows = {row.get("id"): row for row in previous.get("rows") or []}
    rows = []
    failures = []
    for spec in specs:
        try:
            if spec.get("source") == "basisline":
                row = basis_row(spec, prev_rows.get(spec["id"]))
            elif spec.get("source") == "atlantis":
                row = atlantis_bond(spec, prev_rows.get(spec["id"]))
            else:
                raise RuntimeError(f"unknown credit source {spec.get('source')}")
            rows.append(row)
            print(
                f"OK  credit {row['id']:<8} {row['levelDate']}  {row['level']}  d1={row['d1']}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - one name must not blank the box or the desk
            message = str(exc)[:240]
            failures.append({"id": spec.get("id"), "error": message})
            print(f"FAIL credit {spec.get('id')} {message}", flush=True)
            kept = prev_rows.get(spec.get("id"))
            rows.append(kept if kept and kept.get("level") is not None else placeholder(spec, message))
        time.sleep(0.1)
    if not any(row.get("level") is not None for row in rows):
        if previous.get("rows"):
            print("credit: no prints, left the last credit file", flush=True)
            return
    dates = [row["levelDate"] for row in rows if row.get("levelDate")]
    payload = {
        "asOf": max(dates) if dates else previous.get("asOf"),
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "title": "AI / NEOCLOUD CREDIT",
        "caveat": "Proxies from bond prices, not traded Markit CDS.",
        "attribution": "Bond data: Atlantis · Basisline credit",
        "rows": rows,
        "failures": failures,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(to_json(payload) + "\n")
    tmp.replace(OUTPUT)
    print(f"wrote {OUTPUT.relative_to(ROOT)} rows={len(rows)} fail={len(failures)}", flush=True)
