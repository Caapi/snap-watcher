#!/usr/bin/env python3
"""Eurostar Snap watcher.

Checks Snap fares for the routes and dates in config.json, appends every
result to data/prices.csv (so you can see how prices move), and emails you
when a Snap fare appears or gets cheaper.

Standard library only, no pip installs needed.

Usage:
    python watcher.py              # normal run (what GitHub Actions does)
    python watcher.py --dry-run    # check and log, but print alerts instead of emailing
    python watcher.py --test-email # send a test email and exit

Email settings come from environment variables (GitHub secrets):
    SMTP_USER       Gmail address that sends the alerts
    SMTP_PASSWORD   Gmail app password (16 characters, not your normal password)
    ALERT_TO        address that receives the alerts
    SMTP_HOST       optional, default smtp.gmail.com
    SMTP_PORT       optional, default 465 (SSL)
"""

import argparse
import csv
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
PRICES_CSV = DATA_DIR / "prices.csv"
STATE_JSON = DATA_DIR / "state.json"
LATEST_MD = DATA_DIR / "latest.md"

GATEWAY = "https://site-api.eurostar.com/gateway"
SNAP_PRODUCT_FAMILIES = ["LASTMIN", "RED_US"]  # what snap.eurostar.com itself asks for
LONDON_TZ = ZoneInfo("Europe/London")
FAILURES_BEFORE_ALERT = 3
REQUEST_GAP_SECONDS = 1.0  # be polite between requests

QUERY = (
    "query cheapestFaresSearch($numberOfPassenger: Int, $productFamiliesSearch: [String!], "
    "$currency: Currency!, $fares: [CheapestFaresList!]!) { "
    "cheapestFaresSearch(numberOfPassenger: $numberOfPassenger, "
    "productFamiliesSearch: $productFamiliesSearch, currency: $currency, "
    "cheapestFaresLists: $fares) { cheapestFares { date price } } }"
)

CSV_HEADER = ["checked_at_utc", "route", "date", "snap_price", "standard_price", "status"]


class FetchError(Exception):
    """The API didn't give us usable data (blocked, down, or changed)."""


# ---------------------------------------------------------------- fetching

def fetch_fares(origin, destination, start, end, passengers, currency, product_families):
    """Return {date: price} for one route. Dates with no fare are simply absent."""
    variables = {
        "fares": [{
            "origin": origin,
            "destination": destination,
            "startDate": start,
            "endDate": end,
            "journeyType": "SINGLE",
            "direction": "OUTBOUND",
        }],
        "currency": currency,
        "numberOfPassenger": passengers,
    }
    if product_families:
        variables["productFamiliesSearch"] = product_families

    body = json.dumps({
        "operationName": "cheapestFaresSearch",
        "variables": variables,
        "query": QUERY,
    }).encode()
    req = urllib.request.Request(
        GATEWAY,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "origin": "https://snap.eurostar.com",
            "referer": "https://snap.eurostar.com/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} (likely blocked by bot protection)" if e.code in (403, 405, 429)
                         else f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise FetchError(f"network error: {e}") from e

    if status != 200:
        # AWS WAF challenges come back as 202 with an HTML/JS page
        raise FetchError(f"HTTP {status} (likely a bot-protection challenge)")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"non-JSON response, starts: {raw[:120]!r}") from e
    if payload.get("errors"):
        raise FetchError(f"API error: {payload['errors'][0].get('message', '?')}")
    try:
        lists = payload["data"]["cheapestFaresSearch"]
        fares = lists[0]["cheapestFares"] if lists else []
    except (KeyError, TypeError, IndexError) as e:
        raise FetchError(f"unexpected response shape: {raw[:200]!r}") from e
    return {f["date"]: f["price"] for f in fares if f and f.get("price") is not None}


# ---------------------------------------------------------------- state & logging

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def append_rows(rows):
    DATA_DIR.mkdir(exist_ok=True)
    new_file = not PRICES_CSV.exists()
    with PRICES_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        w.writerows(rows)


def fmt_price(p, currency="GBP"):
    if p is None:
        return "sold out"
    sym = {"GBP": "£", "EUR": "€"}.get(currency, currency + " ")
    return f"{sym}{p:g}"


def fmt_date(iso):
    d = dt.date.fromisoformat(iso)
    return d.strftime("%a %-d %b")


def write_latest(results, checked_at, currency):
    lines = [
        "# Latest Snap check",
        "",
        f"Checked {checked_at.astimezone(LONDON_TZ):%a %-d %b %Y, %H:%M} (London time).",
        "",
        "| Route | Date | Snap | Standard |",
        "|---|---|---|---|",
    ]
    for r in results:
        if r["status"] != "ok":
            snap, std = "error", "error"
        else:
            snap = fmt_price(r["snap"], currency)
            std = fmt_price(r["standard"], currency) if r["standard"] is not None else "–"
        lines.append(f"| {r['route']} | {fmt_date(r['date'])} | {snap} | {std} |")
    lines += ["", "Full history: [prices.csv](prices.csv)", ""]
    LATEST_MD.write_text("\n".join(lines))


# ---------------------------------------------------------------- email

def send_email(subject, text, to_addr, dry_run=False):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if dry_run or not (user and password and to_addr):
        reason = "dry run" if dry_run else "SMTP_USER, SMTP_PASSWORD or ALERT_TO not set"
        print(f"\n--- email not sent ({reason}) ---\nTo: {to_addr}\nSubject: {subject}\n\n{text}\n---")
        return False
    msg = EmailMessage()
    msg["From"] = f"Snap watcher <{user}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
    print(f"Email sent to {to_addr}: {subject}")
    return True


# ---------------------------------------------------------------- main check

def run_check(cfg, state, now_utc, fetch=fetch_fares):
    """Fetch everything, return (results, events, state). Pure apart from `fetch`."""
    today_london = now_utc.astimezone(LONDON_TZ).date()
    dates = sorted(d for d in cfg["dates"] if dt.date.fromisoformat(d) > today_london)
    if not dates:
        return [], [], state

    start = dates[0]
    end = (dt.date.fromisoformat(dates[-1]) + dt.timedelta(days=1)).isoformat()
    origin = cfg["origin"]
    last = state.setdefault("last", {})

    results, events, errors = [], [], []
    first_request = True
    for dest in cfg["destinations"]:
        route = f"{origin['name']} → {dest['name']}"
        try:
            if not first_request:
                time.sleep(REQUEST_GAP_SECONDS)
            first_request = False
            snap = fetch(origin["uic"], dest["uic"], start, end,
                         cfg["passengers"], cfg["currency"], SNAP_PRODUCT_FAMILIES)
            standard = {}
            if cfg.get("also_log_standard_fare", True):
                time.sleep(REQUEST_GAP_SECONDS)
                standard = fetch(origin["uic"], dest["uic"], start, end,
                                 cfg["passengers"], cfg["currency"], None)
        except FetchError as e:
            errors.append(f"{route}: {e}")
            for d in dates:
                results.append({"route": route, "date": d, "snap": None,
                                "standard": None, "status": f"error: {e}"})
            continue

        for d in dates:
            price = snap.get(d)
            key = f"{dest['uic']}|{d}"
            prev = last.get(key, "unseen")
            results.append({"route": route, "date": d, "snap": price,
                            "standard": standard.get(d), "status": "ok"})
            if price is not None:
                if prev is None or prev == "unseen":
                    if cfg.get("alert_on_new_availability", True):
                        events.append({"kind": "available", "route": route, "date": d,
                                       "price": price, "standard": standard.get(d)})
                elif price < prev and cfg.get("alert_on_price_drop", True):
                    events.append({"kind": "drop", "route": route, "date": d,
                                   "price": price, "was": prev, "standard": standard.get(d)})
            last[key] = price

    # Track whole-run failures so one blip doesn't email you, but a block does.
    if errors and len(errors) == len(cfg["destinations"]):
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_error"] = errors[0]
    else:
        state["consecutive_failures"] = 0
        state["failure_alerted"] = False
    state["errors_this_run"] = errors
    return results, events, state


def build_alert(events, currency):
    events = sorted(events, key=lambda e: (e["date"], e["price"]))
    best = events[0]
    if len(events) == 1:
        verb = "available" if best["kind"] == "available" else "cheaper"
        subject = (f"Snap {verb}: {best['route'].split(' → ')[1]}, "
                   f"{fmt_date(best['date'])}, {fmt_price(best['price'], currency)}")
    else:
        subject = f"Snap: {len(events)} fares changed, from {fmt_price(best['price'], currency)}"
    lines = []
    for e in events:
        line = f"• {e['route']}, {fmt_date(e['date'])}: {fmt_price(e['price'], currency)}"
        if e["kind"] == "drop":
            line += f" (was {fmt_price(e['was'], currency)})"
        else:
            line += " (newly available)"
        if e.get("standard"):
            line += f", regular fare {fmt_price(e['standard'], currency)}"
        lines.append(line)
    lines += ["", "Book: https://snap.eurostar.com/uk-en",
              "Snap tickets sell out fast and can't be refunded or changed."]
    return subject, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of emailing")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit(f"Can't read {CONFIG_PATH}")
    # Kept out of config.json so the address isn't public in a public repo.
    to_addr = os.environ.get("ALERT_TO") or cfg.get("alert_to") or ""
    if not to_addr and not args.dry_run:
        print("Warning: ALERT_TO isn't set, so alerts will only be printed.")

    if args.test_email:
        send_email("Snap watcher test", "If you're reading this, alerts will reach you.", to_addr)
        return

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    state = load_json(STATE_JSON, {})
    results, events, state = run_check(cfg, state, now)

    if not results:
        print("All watched dates have passed. Nothing to do. You can disable the workflow.")
        return

    checked_at = now.isoformat().replace("+00:00", "Z")
    append_rows([
        [checked_at, r["route"], r["date"],
         "" if r["snap"] is None else r["snap"],
         "" if r["standard"] is None else r["standard"],
         r["status"]]
        for r in results
    ])
    write_latest(results, now, cfg["currency"])

    for r in results:
        std = f"  (standard {fmt_price(r['standard'], cfg['currency'])})" if r["standard"] else ""
        shown = fmt_price(r["snap"], cfg["currency"]) if r["status"] == "ok" else r["status"]
        print(f"{r['route']:<42} {r['date']}  {shown}{std}")

    if events:
        subject, text = build_alert(events, cfg["currency"])
        send_email(subject, text, to_addr, dry_run=args.dry_run)

    if (state.get("consecutive_failures", 0) >= FAILURES_BEFORE_ALERT
            and not state.get("failure_alerted")):
        send_email(
            "Snap watcher can't reach Eurostar",
            f"The last {state['consecutive_failures']} checks all failed.\n"
            f"Latest error: {state.get('last_error')}\n\n"
            "If this says 'blocked' or 'challenge', Eurostar's bot protection is rejecting "
            "GitHub's servers. See the README for the fallback options.\n"
            "You'll get one email per outage, not one every 5 minutes.",
            to_addr, dry_run=args.dry_run,
        )
        state["failure_alerted"] = True

    STATE_JSON.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if state.get("errors_this_run"):
        print("Errors this run:", *state["errors_this_run"], sep="\n  ")


if __name__ == "__main__":
    main()
