"""
eod_journal.py  —  EOD market-data journal (OHLC) with backfill + daily append.

On every run it covers [START_DATE .. today]:
  - First run  : fills every trading day from 1 Apr 2026 to today.
  - Later runs : adds only the newest day; existing OK rows are left
                 untouched (history stays, git diffs stay small).
                 Rows previously stale/missing get a fresh attempt.

Source: Yahoo Finance (EOD only, never intraday). Open/High/Low/Close captured
for each instrument. A mutual fund prices once daily, so the ABSL fund's
O/H/L/C are normally the same number (the NAV).

  NIFTY 50   ^NSEI
  India VIX  ^INDIAVIX
  ABSL Liquid Fund Direct Growth  0P00005V43.BO   (Yahoo NAV; may lag 1 day)

Run:  pip install yfinance pandas
      python eod_journal.py                      # ./trade_journal.csv, from 2026-04-01
      python eod_journal.py --start 2026-04-01 --journal trade_journal.csv
"""

import os, sys, time, argparse, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
START_DEFAULT = os.getenv("START_DATE", "2026-04-01")

INSTRUMENTS = [("nifty", "^NSEI"), ("vix", "^INDIAVIX"), ("absl", "0P00005V43.BO")]
FIELDS = ["open", "high", "low", "close"]
PRICE_COLS = [f"{p}_{f}" for p, _ in INSTRUMENTS for f in FIELDS]
COLS = ["date"] + PRICE_COLS + ["source", "fetch_timestamp", "status", "notes"]


# ─── FETCH HISTORY (OHLC) ──────────────────────────────────────────────────────
def _r(x):
    return round(float(x), 4) if pd.notna(x) else None


def fetch_history(ticker, start, end, retries=3):
    """{date: {open,high,low,close}} for [start, end], or {} on failure."""
    import yfinance as yf
    err = None
    for attempt in range(retries):
        try:
            h = yf.Ticker(ticker).history(
                start=start.isoformat(),
                end=(end + dt.timedelta(days=1)).isoformat(),    # end exclusive
                interval="1d", auto_adjust=False)
            if not h.empty and "Close" in h:
                out = {}
                for ts, row in h.iterrows():
                    if pd.notna(row.get("Close")):
                        out[ts.date()] = {"open": _r(row.get("Open")),
                                          "high": _r(row.get("High")),
                                          "low":  _r(row.get("Low")),
                                          "close": _r(row.get("Close"))}
                if out:
                    return out
        except Exception as e:
            err = e
        time.sleep(1.5 * (attempt + 1))
    print(f"  ! fetch failed for {ticker}: {err}")
    return {}


def asof(items, d):
    """Last (ohlc, date) with date <= d. items sorted ascending."""
    val, vd = None, None
    for k, v in items:
        if k <= d:
            val, vd = v, k
        else:
            break
    return val, vd


# ─── BUILD ONE ROW ─────────────────────────────────────────────────────────────
def build_record(d, hist, now, today, latest_date):
    """hist = {prefix: {"data": {date: ohlc}, "items": sorted items}}"""
    n = hist["nifty"]["data"].get(d)
    v = hist["vix"]["data"].get(d)
    a, a_d = asof(hist["absl"]["items"], d)        # NAV may lag, so as-of

    missing, notes = [], []
    if not n: missing.append("NIFTY")
    if not v: missing.append("India VIX")
    if not a: missing.append("ABSL")

    status = "missing" if missing else "OK"
    if missing:
        notes.append("missing: " + ", ".join(missing))
    if a_d and a_d < d:
        notes.append(f"ABSL NAV as of {a_d} (Yahoo fund NAV lags index by a day)")
    if d == latest_date and d < today and today.weekday() < 5 and status == "OK":
        status = "stale"
        notes.append(f"latest market date {d}, not {today} (holiday or Yahoo not updated yet)")

    row = {"date": d.isoformat()}
    for prefix, data in (("nifty", n), ("vix", v), ("absl", a)):
        for f in FIELDS:
            row[f"{prefix}_{f}"] = data[f] if data else None
    row["source"] = "Yahoo Finance"
    row["fetch_timestamp"] = now.isoformat(timespec="seconds")
    row["status"] = status
    row["notes"] = "; ".join(notes)
    return row


# ─── UPSERT (insert new, refresh stale/missing, keep good rows) ────────────────
def upsert_many(path, records):
    try:
        df = pd.read_csv(path, dtype=str)
    except FileNotFoundError:
        df = pd.DataFrame(columns=COLS)
    for c in COLS:
        if c not in df.columns:
            df[c] = pd.NA
    by_date = {r["date"]: r for r in df.to_dict("records")}

    inserted = updated = skipped = 0
    for rec in records:
        d = rec["date"]
        if d not in by_date:
            by_date[d] = rec; inserted += 1
        elif str(by_date[d].get("status", "")) in ("stale", "missing", "nan", ""):
            by_date[d] = rec; updated += 1
        else:
            skipped += 1

    out = pd.DataFrame(list(by_date.values()))[COLS].sort_values("date").reset_index(drop=True)
    out.to_csv(path, index=False)
    return inserted, updated, skipped, out


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="trade_journal.csv")
    ap.add_argument("--start", default=START_DEFAULT)
    args = ap.parse_args()

    now   = dt.datetime.now(IST)
    today = now.date()
    start = dt.date.fromisoformat(args.start)

    hist = {}
    for prefix, ticker in INSTRUMENTS:
        data = fetch_history(ticker, start, today)
        hist[prefix] = {"data": data, "items": sorted(data.items())}

    if not any(hist[p]["data"] for p, _ in INSTRUMENTS):
        print("FETCH FAILED: no data for any ticker. Check Yahoo access / ticker symbols.")
        sys.exit(2)

    cal = sorted((hist["nifty"]["data"] or hist["vix"]["data"] or hist["absl"]["data"]).keys())
    latest_date = cal[-1]
    records = [build_record(d, hist, now, today, latest_date) for d in cal]

    try:
        inserted, updated, skipped, out = upsert_many(args.journal, records)
    except Exception as e:
        print(f"JOURNAL ERROR: cannot write {args.journal}: {e}")
        sys.exit(3)

    latest = out.iloc[-1]
    issues = out[out["status"] != "OK"]

    print("\n─── EOD journal updated (OHLC) ───────────────────────")
    print(f"  Range        {args.start} -> {today}")
    print(f"  Rows         +{inserted} new, {updated} refreshed, {skipped} unchanged "
          f"({len(out)} total)")
    print(f"  Latest row   {latest['date']}  (showing close)")
    print(f"    NIFTY 50   O {latest['nifty_open']}  H {latest['nifty_high']}  "
          f"L {latest['nifty_low']}  C {latest['nifty_close']}")
    print(f"    India VIX  O {latest['vix_open']}  H {latest['vix_high']}  "
          f"L {latest['vix_low']}  C {latest['vix_close']}")
    print(f"    ABSL NAV   {latest['absl_close']}")
    print(f"    Status     {latest['status']}")
    if latest["notes"]:
        print(f"    Notes      {latest['notes']}")
    if len(issues):
        print(f"  Flagged      {len(issues)} row(s) not OK: "
              + ", ".join(f"{r['date']}({r['status']})" for _, r in issues.iterrows()))
    print(f"  Fetched      {now.isoformat(timespec='seconds')}  (Yahoo Finance)")


if __name__ == "__main__":
    main()
