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
import random
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
        if pd.isna(r) or np.isinf(r):
            continue
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
# Stock analysis (operates on pre-downloaded data)
# ---------------------------------------------------------------------------

def analyse_stock(ticker: str, close: pd.Series, volume: pd.Series | None) -> dict | None:
    """Analyse a single stock from pre-downloaded price data."""
    if close.empty or len(close) < 5:
        return None

    close = close.dropna()
    if len(close) < 5:
        return None

    price = close.iloc[-1]
    if price < MIN_PRICE:
        return None

    now = close.index[-1]

    # Standard periods — collect boundary prices
    boundary_prices = {"now": float(price)}
    for label, cal, td in STANDARD_PERIODS:
        mask = close.index <= (now - pd.Timedelta(days=cal))
        if not mask.any():
            boundary_prices[label] = None
            continue
        bp = close[mask].iloc[-1]
        if pd.isna(bp) or np.isinf(bp) or bp <= 0:
            boundary_prices[label] = None
        else:
            boundary_prices[label] = float(bp)

    # Cumulative returns
    periods = {}
    daily_avg = {}
    for label, cal, td in STANDARD_PERIODS:
        bp = boundary_prices.get(label)
        if bp is None:
            periods[label] = None
            daily_avg[label] = None
            continue
        ret = (price - bp) / bp
        if pd.isna(ret) or np.isinf(ret):
            periods[label] = None
            daily_avg[label] = None
            continue
        periods[label] = round(float(ret), 5)
        if td > 0 and ret > -1:
            da = (1 + ret) ** (1 / td) - 1
            daily_avg[label] = round(float(da), 6) if not (pd.isna(da) or np.isinf(da)) else 0
        else:
            daily_avg[label] = 0

    # Segment returns
    SEGMENTS = [
        ("1wk",  "now",  "1wk",  5),
        ("1mo",  "1wk",  "1mo",  16),
        ("3mo",  "1mo",  "3mo",  42),
        ("6mo",  "3mo",  "6mo",  63),
        ("1yr",  "6mo",  "1yr",  126),
        ("2yr",  "1yr",  "2yr",  252),
        ("3yr",  "2yr",  "3yr",  252),
        ("5yr",  "3yr",  "5yr",  504),
    ]

    segments = {}
    seg_daily = {}
    for seg_label, end_key, start_key, seg_td in SEGMENTS:
        end_price = boundary_prices.get(end_key)
        start_price = boundary_prices.get(start_key)
        if end_price is None or start_price is None or start_price <= 0:
            segments[seg_label] = None
            seg_daily[seg_label] = None
            continue
        seg_ret = (end_price - start_price) / start_price
        if pd.isna(seg_ret) or np.isinf(seg_ret):
            segments[seg_label] = None
            seg_daily[seg_label] = None
            continue
        segments[seg_label] = round(float(seg_ret), 5)
        if seg_td > 0 and seg_ret > -1:
            sda = (1 + seg_ret) ** (1 / seg_td) - 1
            seg_daily[seg_label] = round(float(sda), 6) if not (pd.isna(sda) or np.isinf(sda)) else 0
        else:
            seg_daily[seg_label] = 0

    # Scores
    all_seg = ["1wk", "1mo", "3mo", "6mo", "1yr", "2yr", "3yr", "5yr"]
    core_seg = ["1wk", "1mo", "3mo", "6mo", "1yr"]
    mscore = sum(1 for s in core_seg if segments.get(s) is not None and segments[s] > 0)
    tscore = sum(1 for s in all_seg if segments.get(s) is not None and segments[s] > 0)

    # Flags
    flags = []
    if all(segments.get(s) is not None and segments[s] > 0 for s in core_seg):
        flags.append("CU")
    if "CU" in flags and all(segments.get(s) is not None and segments[s] > 0 for s in ["2yr", "3yr"]):
        flags.append("EU")

    accel = [seg_daily.get(s) for s in ["1yr", "6mo", "3mo", "1mo", "1wk"]]
    ac = [d for d in accel if d is not None]
    if len(ac) >= 3 and all(ac[i] < ac[i+1] for i in range(len(ac)-1)):
        flags.append("AC")

    # Price levels
    y1 = close.tail(252) if len(close) >= 252 else close
    h52 = float(y1.max())
    l52 = float(y1.min())
    h5y = float(close.max())

    # Volume
    v20 = 0
    if volume is not None and len(volume) >= 20:
        v20 = int(volume.tail(20).mean())

    # Granular breakdowns
    wr = period_returns(close, 'W')
    mr = period_returns(close, 'ME')
    qr = period_returns(close, 'QE')

    if wr:
        ws52 = streaks(wr[:52])
        if ws52["cs"] >= 8:
            flags.append("HS")
        if ws52["cs"] <= -6:
            flags.append("CS")
        if ws52["pr"] >= 0.70:
            flags.append("CW")

    wstats = windowed_stats(wr, [13, 26, 52, 104, 260])
    mstats = windowed_stats(mr, [3, 6, 12, 24, 36, 60])
    qstats = windowed_stats(qr, [4, 8, 12, 20])

    result = {
        "t": ticker, "n": ticker, "pr": round(float(price), 4),
        "s": "Unknown", "mc": 0,
        "p": periods, "da": daily_avg,
        "seg": segments, "sda": seg_daily,
        "ms": mscore, "ts": tscore, "pp": tscore,
        "f": flags,
        "h52": round(h52, 4), "l52": round(l52, 4),
        "h5y": round(h5y, 4),
        "pfh": round((price - h52) / h52, 4) if h52 > 0 else 0,
        "pf5h": round((price - h5y) / h5y, 4) if h5y > 0 else 0,
        "v20": v20,
        "ws": wstats, "mos": mstats, "qs": qstats,
    }

    include_granular = tscore >= GRANULAR_THRESHOLD or "CU" in flags
    if include_granular:
        result["wr"] = wr
        result["mr"] = mr
        result["qr"] = qr

    return result


def fetch_info_batch(tickers: list[str], results: dict):
    """Fetch name/sector/market_cap for qualifying stocks (individual calls, but only ~500 not 1654)."""
    log.info(f"Fetching info for {len(tickers)} qualifying stocks...")
    done = 0
    for t in tickers:
        try:
            info = yf.Ticker(f"{t}.AX").info
            if t in results:
                results[t]["n"] = info.get("shortName", t)
                results[t]["s"] = info.get("sector", "Unknown")
                results[t]["mc"] = info.get("marketCap", 0)
        except Exception:
            pass
        done += 1
        if done % 100 == 0:
            log.info(f"Info: {done}/{len(tickers)}")
            time.sleep(1)  # Light throttle


# ---------------------------------------------------------------------------
# Batch download + scan
# ---------------------------------------------------------------------------

def run_scan(tickers: list[str]) -> list[dict]:
    total = len(tickers)
    download_batch = int(os.environ.get("DOWNLOAD_BATCH", "200"))
    log.info(f"Batch downloading {total} tickers in groups of {download_batch} ({HISTORY_PERIOD} history)...")

    t0 = time.time()

    # Convert to Yahoo Finance format
    yf_tickers = [f"{t}.AX" for t in tickers]

    # Download all price data in large batches using yf.download()
    all_close = pd.DataFrame()
    all_volume = pd.DataFrame()

    for batch_start in range(0, len(yf_tickers), download_batch):
        batch = yf_tickers[batch_start:batch_start + download_batch]
        batch_num = batch_start // download_batch + 1
        total_batches = (len(yf_tickers) + download_batch - 1) // download_batch
        log.info(f"Downloading batch {batch_num}/{total_batches} ({len(batch)} tickers)...")

        try:
            data = yf.download(
                batch,
                period=HISTORY_PERIOD,
                auto_adjust=True,
                threads=True,
                progress=False,
            )

            if data.empty:
                log.warning(f"Batch {batch_num} returned empty")
                continue

            # yf.download returns MultiIndex columns (field, ticker) for multiple tickers
            if isinstance(data.columns, pd.MultiIndex):
                if 'Close' in data.columns.get_level_values(0):
                    close_df = data['Close']
                    all_close = pd.concat([all_close, close_df], axis=1)
                if 'Volume' in data.columns.get_level_values(0):
                    vol_df = data['Volume']
                    all_volume = pd.concat([all_volume, vol_df], axis=1)
            else:
                # Single ticker returns flat columns
                if len(batch) == 1 and 'Close' in data.columns:
                    all_close[batch[0]] = data['Close']
                    if 'Volume' in data.columns:
                        all_volume[batch[0]] = data['Volume']

        except Exception as e:
            log.warning(f"Batch {batch_num} failed: {e}")

        # Brief pause between download batches
        if batch_start + download_batch < len(yf_tickers):
            time.sleep(1)

    download_time = time.time() - t0
    downloaded_count = len(all_close.columns)
    log.info(f"Downloaded {downloaded_count}/{total} tickers in {download_time:.0f}s")

    # Analyse each stock from the downloaded data
    log.info("Analysing stocks...")
    results = {}
    filtered = 0

    for col in all_close.columns:
        # col is like "PME.AX"
        ticker = col.replace(".AX", "") if isinstance(col, str) else str(col).replace(".AX", "")
        close_series = all_close[col].dropna()
        vol_series = all_volume[col].dropna() if col in all_volume.columns else None

        try:
            result = analyse_stock(ticker, close_series, vol_series)
            if result:
                results[ticker] = result
            else:
                filtered += 1
        except Exception as e:
            log.debug(f"Analysis failed for {ticker}: {e}")
            filtered += 1

    log.info(f"Analysis complete: {len(results)} passed, {filtered} filtered, {total - downloaded_count} not downloaded")

    # Fetch info (name, sector, market cap) only for stocks that passed
    # This uses individual API calls but only for ~500 stocks, not 1654
    fetch_info = os.environ.get("FETCH_INFO", "true").lower() == "true"
    if fetch_info and results:
        fetch_info_batch(list(results.keys()), results)

    elapsed = time.time() - t0
    log.info(f"Total scan time: {elapsed:.0f}s")

    result_list = list(results.values())
    result_list.sort(key=lambda x: (x["ts"], x["pp"], x.get("seg", {}).get("3mo", 0) or 0), reverse=True)
    return result_list


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

    # Custom encoder to handle numpy types and NaN
    class NumpySafeEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                if np.isnan(obj):
                    return None
                return float(obj)
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    # Also scrub any Python float NaN values before serialising
    import math
    def scrub_nans(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: scrub_nans(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub_nans(v) for v in obj]
        return obj

    output = scrub_nans(output)

    with open(output_path, "w") as f:
        json.dump(output, f, separators=(',', ':'), cls=NumpySafeEncoder)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log.info(f"Written {output_path} ({size_mb:.1f} MB)")

    # Dated snapshot
    snapshot_dir = os.path.join(OUTPUT_DIR, "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    snap_name = f"snapshot_{datetime.now().strftime('%Y%m%d')}.json"
    with open(os.path.join(snapshot_dir, snap_name), "w") as f:
        json.dump(scrub_nans(output), f, separators=(',', ':'), cls=NumpySafeEncoder)
    log.info(f"Snapshot: {snap_name}")

    # Summary
    s = output["summary"]
    print(f"\n{'='*60}")
    print(f"  ASX MOMENTUM SCREENER — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*60}")
    print(f"  Scanned: {s['scanned']}  Passed: {s['passed']}")
    print(f"  Flags: {s['flags']}")
    if results:
        print(f"\n  Top 15 (segment returns — each band independently):")
        for r in results[:15]:
            seg = r.get("seg", {})
            ws = r.get("ws", {}).get("52", {})
            print(
                f"  {r['t']:<7} ${r['pr']:>7.2f}  scr={r['ts']}  "
                f"1w={seg.get('1wk',0) or 0:+.1%}  "
                f"1w→1m={seg.get('1mo',0) or 0:+.1%}  "
                f"1m→3m={seg.get('3mo',0) or 0:+.1%}  "
                f"3m→6m={seg.get('6mo',0) or 0:+.1%}  "
                f"6m→1y={seg.get('1yr',0) or 0:+.1%}  "
                f"wk_str={ws.get('cs',0):+d}  flags={','.join(r['f'])}"
            )
    print()


if __name__ == "__main__":
    main()
