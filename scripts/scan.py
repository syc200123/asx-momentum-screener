#!/usr/bin/env python3
"""
ASX Momentum Screener — CI Scanner (v2)
========================================
Designed to run headless in GitHub Actions. Scans all ASX stocks,
computes momentum scores with granular breakdowns, and outputs
size-optimised JSON for the GitHub Pages frontend.

Full granular data (weekly/monthly/quarterly returns) is included only
for stocks scoring 3+ or flagged as uptrend. All other stocks get
summary stats only. This keeps the output under ~5MB for fast page loads.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    import requests
except ImportError:
    print("pip install yfinance requests pandas numpy")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.10"))
WORKERS = int(os.environ.get("WORKERS", "20"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs/data")
HISTORY_PERIOD = "5y"
GRANULAR_THRESHOLD = 3  # Include full granular data for stocks scoring >= this

STANDARD_PERIODS = [
    ("1wk", 7, 5), ("1mo", 30, 21), ("3mo", 91, 63), ("6mo", 182, 126),
    ("1yr", 365, 252), ("2yr", 730, 504), ("3yr", 1095, 756), ("5yr", 1825, 1260),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scan")


# ---------------------------------------------------------------------------
# Ticker list
# ---------------------------------------------------------------------------

def fetch_asx_tickers() -> list[str]:
    log.info("Fetching ASX ticker list...")

    for url in [
        "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file?access_token=83ff96335c2d45a094df02a206a39ff4",
        "https://www.asx.com.au/asx/research/ASXListedCompanies.csv",
    ]:
        try:
            import io
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            if resp.status_code != 200:
                continue
            text = resp.text
            if "ASXListedCompanies" in url:
                lines = text.strip().split('\n')
                text = '\n'.join(lines[2:]) if len(lines) > 2 else text
            df = pd.read_csv(io.StringIO(text))
            code_col = [c for c in df.columns if 'code' in c.lower() or 'symbol' in c.lower()]
            if code_col:
                tickers = df[code_col[0]].dropna().str.strip().tolist()
                tickers = [t for t in tickers if t and len(t) <= 5 and t.isalpha()]
                log.info(f"Got {len(tickers)} tickers from {url.split('/')[2]}")
                return tickers
        except Exception as e:
            log.warning(f"Failed: {url} — {e}")

    cache = Path("asx_tickers_cache.json")
    if cache.exists():
        return json.loads(cache.read_text())

    log.error("Could not fetch ASX ticker list")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Granular analysis
# ---------------------------------------------------------------------------

def period_returns(close: pd.Series, freq: str) -> list[dict]:
    if close.empty or len(close) < 2:
        return []
    resampled = close.resample(freq).last().dropna()
    if len(resampled) < 2:
        return []
    results = []
    for i in range(1, len(resampled)):
        p = resampled.iloc[i - 1]
        c = resampled.iloc[i]
        r = (c - p) / p if p != 0 else 0
        results.append({
            "pe": resampled.index[i].strftime("%Y-%m-%d"),
            "r": round(float(r), 5),
            "p": bool(r > 0),
        })
    results.reverse()
    return results


def streaks(rets: list[dict]) -> dict:
    if not rets:
        return {"cs": 0, "lps": 0, "lns": 0, "tp": 0, "tt": 0, "pr": 0, "apr": 0, "anr": 0}

    total = len(rets)
    pos = [r for r in rets if r["p"]]
    neg = [r for r in rets if not r["p"]]

    cs = 0
    if rets:
        ip = rets[0]["p"]
        for r in rets:
            if r["p"] == ip:
                cs += 1
            else:
                break
        if not ip:
            cs = -cs

    lps = lns = cp = cn = 0
    for r in reversed(rets):
        if r["p"]:
            cp += 1; cn = 0; lps = max(lps, cp)
        else:
            cn += 1; cp = 0; lns = max(lns, cn)

    return {
        "cs": cs, "lps": lps, "lns": lns,
        "tp": len(pos), "tt": total,
        "pr": round(len(pos) / total, 3) if total else 0,
        "apr": round(sum(r["r"] for r in pos) / len(pos), 5) if pos else 0,
        "anr": round(sum(r["r"] for r in neg) / len(neg), 5) if neg else 0,
    }


def windowed_stats(rets: list[dict], windows: list[int]) -> dict:
    return {str(w): streaks(rets[:w] if w <= len(rets) else rets) for w in windows}


# ---------------------------------------------------------------------------
# Stock scanner
# ---------------------------------------------------------------------------

def scan_stock(ticker: str) -> dict | None:
    try:
        stock = yf.Ticker(f"{ticker}.AX")
        hist = stock.history(period=HISTORY_PERIOD, auto_adjust=True)

        if hist.empty or len(hist) < 5:
            return None

        price = hist['Close'].iloc[-1]
        if price < MIN_PRICE:
            return None

        now = hist.index[-1]
        close = hist['Close']

        # Standard periods
        periods = {}
        daily_avg = {}
        for label, cal, td in STANDARD_PERIODS:
            mask = hist.index <= (now - pd.Timedelta(days=cal))
            if not mask.any():
                periods[label] = None
                daily_avg[label] = None
                continue
            past = close[mask].iloc[-1]
            ret = (price - past) / past
            periods[label] = round(float(ret), 5)
            # Geometric daily average (CAGR-style)
            daily_avg[label] = round(float((1 + ret) ** (1 / td) - 1), 6) if td > 0 and ret > -1 else 0

        # Scores
        all_p = ["1wk", "1mo", "3mo", "6mo", "1yr", "2yr", "3yr", "5yr"]
        core_p = ["1wk", "1mo", "3mo", "6mo", "1yr"]
        mscore = sum(1 for p in core_p if daily_avg.get(p) is not None and daily_avg[p] >= 0.002)
        tscore = sum(1 for p in all_p if daily_avg.get(p) is not None and daily_avg[p] >= 0.002)
        pos_count = sum(1 for p in all_p if periods.get(p) is not None and periods[p] > 0)

        # Flags
        flags = []
        if all(periods.get(p) and periods[p] > 0 for p in ["1wk", "3mo", "6mo", "1yr"]):
            flags.append("CU")
        if "CU" in flags and all(periods.get(p) and periods[p] > 0 for p in ["2yr", "3yr"]):
            flags.append("EU")

        accel = [daily_avg.get(p) for p in ["1yr", "6mo", "3mo", "1mo", "1wk"]]
        ac = [d for d in accel if d is not None]
        if len(ac) >= 3 and all(ac[i] < ac[i+1] for i in range(len(ac)-1)):
            flags.append("AC")

        # Price levels
        y1 = close.tail(252) if len(close) >= 252 else close
        h52 = float(y1.max())
        l52 = float(y1.min())
        h5y = float(close.max())

        # Volume
        v20 = int(hist['Volume'].tail(20).mean()) if 'Volume' in hist.columns and len(hist) >= 20 else 0

        # Granular breakdowns
        wr = period_returns(close, 'W')
        mr = period_returns(close, 'ME')
        qr = period_returns(close, 'QE')

        # Weekly streak flags
        if wr:
            ws52 = streaks(wr[:52])
            if ws52["cs"] >= 8:
                flags.append("HS")
            if ws52["cs"] <= -6:
                flags.append("CS")
            if ws52["pr"] >= 0.70:
                flags.append("CW")

        # Windowed stats (always included)
        wstats = windowed_stats(wr, [13, 26, 52, 104, 260])
        mstats = windowed_stats(mr, [3, 6, 12, 24, 36, 60])
        qstats = windowed_stats(qr, [4, 8, 12, 20])

        # Info
        name = ticker
        sector = "Unknown"
        mcap = 0
        try:
            info = stock.info
            name = info.get("shortName", ticker)
            sector = info.get("sector", "Unknown")
            mcap = info.get("marketCap", 0)
        except Exception:
            pass

        result = {
            "t": ticker, "n": name, "pr": round(float(price), 4),
            "s": sector, "mc": mcap,
            "p": periods, "da": daily_avg,
            "ms": mscore, "ts": tscore, "pp": pos_count,
            "f": flags,
            "h52": round(h52, 4), "l52": round(l52, 4),
            "h5y": round(h5y, 4),
            "pfh": round((price - h52) / h52, 4) if h52 > 0 else 0,
            "pf5h": round((price - h5y) / h5y, 4) if h5y > 0 else 0,
            "v20": v20,
            "ws": wstats, "mos": mstats, "qs": qstats,
        }

        # Include granular returns only for qualifying stocks
        include_granular = tscore >= GRANULAR_THRESHOLD or "CU" in flags
        if include_granular:
            result["wr"] = wr
            result["mr"] = mr
            result["qr"] = qr

        return result

    except Exception as e:
        log.debug(f"{ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Batch scan
# ---------------------------------------------------------------------------

def run_scan(tickers: list[str]) -> list[dict]:
    total = len(tickers)
    log.info(f"Scanning {total} tickers ({WORKERS} workers, ${MIN_PRICE:.2f} min, {HISTORY_PERIOD} history)")

    results = []
    done = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(scan_stock, t): t for t in tickers}
        for fut in as_completed(futures):
            done += 1
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass
            if done % 200 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                log.info(f"{done}/{total} ({len(results)} ok) [{rate:.0f}/s, ~{(total-done)/rate:.0f}s left]")

    elapsed = time.time() - t0
    log.info(f"Done in {elapsed:.0f}s — {len(results)} stocks passed")

    results.sort(key=lambda x: (x["ts"], x["pp"], x["p"].get("3mo", 0) or 0), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Snapshot comparison
# ---------------------------------------------------------------------------

def load_previous(output_path: str) -> dict | None:
    try:
        with open(output_path) as f:
            prev = json.load(f)
        return {s["t"]: s for s in prev.get("stocks", [])}
    except Exception:
        return None


def add_comparison(results: list[dict], prev: dict | None) -> list[dict]:
    if not prev:
        return results
    for stock in results:
        t = stock["t"]
        if t in prev:
            p = prev[t]
            stock["dpr"] = round((stock["pr"] - p["pr"]) / p["pr"], 4) if p.get("pr") else None
            stock["dts"] = stock["ts"] - p.get("ts", 0)
            pf = set(p.get("f", []))
            cf = set(stock.get("f", []))
            if "CU" in cf and "CU" not in pf:
                stock["f"].append("NU")
            if "CU" not in cf and "CU" in pf:
                stock["f"].append("LU")
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_output(results: list[dict], meta: dict) -> dict:
    flag_counts = {}
    for r in results:
        for f in r.get("f", []):
            flag_counts[f] = flag_counts.get(f, 0) + 1

    return {
        "ts": datetime.now().isoformat(),
        "meta": meta,
        "summary": {
            "scanned": meta["total"],
            "passed": len(results),
            "flags": flag_counts,
            "score_dist": {str(i): sum(1 for r in results if r["ts"] == i) for i in range(9)},
        },
        "stocks": results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tickers = fetch_asx_tickers()
    Path("asx_tickers_cache.json").write_text(json.dumps(tickers))

    meta = {
        "total": len(tickers),
        "min_price": MIN_PRICE,
        "history": HISTORY_PERIOD,
        "start": datetime.now().isoformat(),
    }

    output_path = os.path.join(OUTPUT_DIR, "asx_momentum_data.json")

    prev = load_previous(output_path)

    results = run_scan(tickers)
    results = add_comparison(results, prev)

    meta["end"] = datetime.now().isoformat()
    meta["passed"] = len(results)

    output = build_output(results, meta)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Custom encoder to handle numpy types
    class NumpySafeEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    with open(output_path, "w") as f:
        json.dump(output, f, separators=(',', ':'), cls=NumpySafeEncoder)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log.info(f"Written {output_path} ({size_mb:.1f} MB)")

    # Dated snapshot
    snapshot_dir = os.path.join(OUTPUT_DIR, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    snap_name = f"snapshot_{datetime.now().strftime('%Y%m%d')}.json"
    with open(os.path.join(snapshot_dir, snap_name), "w") as f:
        json.dump(output, f, separators=(',', ':'), cls=NumpySafeEncoder)
    log.info(f"Snapshot: {snap_name}")

    # Summary
    s = output["summary"]
    print(f"\n{'='*60}")
    print(f"  ASX MOMENTUM SCREENER — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*60}")
    print(f"  Scanned: {s['scanned']}  Passed: {s['passed']}")
    print(f"  Flags: {s['flags']}")
    if results:
        print(f"\n  Top 15:")
        for r in results[:15]:
            p = r["p"]
            ws = r.get("ws", {}).get("52", {})
            print(
                f"  {r['t']:<7} ${r['pr']:>7.2f}  scr={r['ts']}  "
                f"1w={p.get('1wk',0) or 0:+.1%}  3m={p.get('3mo',0) or 0:+.1%}  "
                f"1y={p.get('1yr',0) or 0:+.1%}  3y={p.get('3yr',0) or 0:+.1%}  "
                f"wk_str={ws.get('cs',0):+d}  flags={','.join(r['f'])}"
            )
    print()


if __name__ == "__main__":
    main()