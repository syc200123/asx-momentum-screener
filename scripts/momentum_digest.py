#!/usr/bin/env python3
"""
ASX Momentum Digest — Post-Scan Filter & Slack Reporter
========================================================
Reads the full asx_momentum_data.json from the screener, filters to
actionable candidates across two tracks (Momentum + Reversal), and
outputs results in multiple formats:

  markdown  — Full tables for GitHub commit (docs/data/momentum_digest.md)
  slack     — Concise summary message for Slack (top 5 per track)
  claude    — Structured prompt for Claude Slack channel (ticker lists + data)

Designed to run as a second step in the GitHub Actions pipeline:
  1. scan.py → asx_momentum_data.json (full ~5MB dataset)
  2. momentum_digest.py → filtered candidates in chosen format

Environment variables:
  SCAN_OUTPUT     Path to screener JSON (default: docs/data/asx_momentum_data.json)
  SLACK_WEBHOOK   Slack webhook URL for posting
  MIN_PRICE       Minimum price filter (default: 0.50)
  MIN_MCAP        Minimum market cap in millions (default: 100)
  MIN_VOLUME      Minimum 20-day avg volume (default: 50000)
  MAX_MOMENTUM    Max momentum candidates to report (default: 30)
  MAX_REVERSAL    Max reversal candidates to report (default: 20)
  OUTPUT_FILE     Save digest to file (optional)
  FORMAT          Output format: 'slack', 'markdown', or 'claude' (default: slack)
  PAGES_URL       GitHub Pages URL for linking (default: empty)
"""

import json
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("digest")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCAN_OUTPUT = os.environ.get("SCAN_OUTPUT", "docs/data/asx_momentum_data.json")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK", "")
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.50"))
MIN_MCAP = float(os.environ.get("MIN_MCAP", "100")) * 1_000_000
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "50000"))
MAX_MOMENTUM = int(os.environ.get("MAX_MOMENTUM", "30"))
MAX_REVERSAL = int(os.environ.get("MAX_REVERSAL", "20"))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "")
FORMAT = os.environ.get("FORMAT", "slack")
PAGES_URL = os.environ.get("PAGES_URL", "")

# Flag key reference (from scan.py):
# CU = Continuous Uptrend    EU = Extended Uptrend
# AC = Accelerating          HS = Hot Streak (8+ positive weeks)
# CS = Cold Streak (6+ neg)  CW = Consistent Winner (70%+ positive weeks)
# NU = New Uptrend           LU = Lost Uptrend


# ---------------------------------------------------------------------------
# Load & Parse
# ---------------------------------------------------------------------------

def load_scan(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        log.error(f"Scan output not found: {path}")
        sys.exit(1)
    with open(p) as f:
        data = json.load(f)
    log.info(f"Loaded {len(data.get('stocks', []))} stocks from scan at {data.get('ts', 'unknown')}")
    return data


def passes_quality_filter(stock: dict) -> bool:
    if stock.get("pr", 0) < MIN_PRICE:
        return False
    if stock.get("mc", 0) and stock["mc"] < MIN_MCAP:
        return False
    if stock.get("v20", 0) < MIN_VOLUME:
        return False
    return True


# ---------------------------------------------------------------------------
# Track 1: Momentum Candidates
# ---------------------------------------------------------------------------

def filter_momentum(stocks: list[dict]) -> list[dict]:
    """
    Identify stocks with strong upward momentum.
    Criteria: CU flag, or score >= 5 with AC flag, or NU flag.
    """
    candidates = []

    for s in stocks:
        if not passes_quality_filter(s):
            continue

        flags = set(s.get("f", []))
        ts = s.get("ts", 0)
        seg = s.get("seg", {})

        is_cu = "CU" in flags
        is_strong_accel = ts >= 5 and "AC" in flags
        is_new_uptrend = "NU" in flags

        if not (is_cu or is_strong_accel or is_new_uptrend):
            continue

        rank = ts * 10
        if is_cu:
            rank += 20
        if "EU" in flags:
            rank += 10
        if "AC" in flags:
            rank += 15
        if "HS" in flags:
            rank += 10
        if "CW" in flags:
            rank += 5
        if is_new_uptrend:
            rank += 25

        seg_3m = seg.get("3mo")
        if seg_3m is not None:
            rank += min(seg_3m * 100, 50)

        s["_rank"] = rank
        candidates.append(s)

    candidates.sort(key=lambda x: x["_rank"], reverse=True)
    return candidates[:MAX_MOMENTUM]


# ---------------------------------------------------------------------------
# Track 2: Reversal Candidates
# ---------------------------------------------------------------------------

def filter_reversal(stocks: list[dict]) -> list[dict]:
    """
    Identify crashed stocks showing early recovery signals.
    Criteria: 40%+ below 52-week high, positive recent segment, not in cold streak.
    """
    candidates = []

    for s in stocks:
        if not passes_quality_filter(s):
            continue

        flags = set(s.get("f", []))
        seg = s.get("seg", {})
        pfh = s.get("pfh", 0)

        if pfh is None or pfh > -0.40:
            continue
        if "CS" in flags:
            continue

        seg_1wk = seg.get("1wk")
        seg_1mo = seg.get("1mo")
        has_recent_positive = (
            (seg_1wk is not None and seg_1wk > 0) or
            (seg_1mo is not None and seg_1mo > 0)
        )
        if not has_recent_positive:
            continue

        rank = 0
        crash_depth = abs(pfh)
        rank += min(crash_depth * 100, 60)

        if seg_1wk is not None and seg_1wk > 0:
            rank += 15 + min(seg_1wk * 200, 20)
        if seg_1mo is not None and seg_1mo > 0:
            rank += 20 + min(seg_1mo * 100, 20)

        dts = s.get("dts", 0)
        if dts and dts > 0:
            rank += dts * 5

        ws52 = s.get("ws", {}).get("52", {})
        cs = ws52.get("cs", 0)
        if cs > 0:
            rank += min(cs * 3, 15)

        mc = s.get("mc", 0)
        if mc and mc > 1_000_000_000:
            rank += 10
        elif mc and mc > 500_000_000:
            rank += 5

        s["_rank"] = rank
        candidates.append(s)

    candidates.sort(key=lambda x: x["_rank"], reverse=True)
    return candidates[:MAX_REVERSAL]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_pct(val, places=1) -> str:
    if val is None:
        return "—"
    return f"{val:+.{places}%}"


def fmt_mcap(val) -> str:
    if not val:
        return "—"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.0f}M"
    return f"${val:,.0f}"


def fmt_vol(val) -> str:
    if not val:
        return "—"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.0f}K"
    return str(val)


def flag_emojis(flags: list) -> str:
    """Convert flags to compact emoji indicators for Slack."""
    parts = []
    if "CU" in flags:
        parts.append(":chart_with_upwards_trend:")
    if "EU" in flags:
        parts.append(":rocket:")
    if "AC" in flags:
        parts.append(":zap:")
    if "HS" in flags:
        parts.append(":fire:")
    if "CW" in flags:
        parts.append(":white_check_mark:")
    if "NU" in flags:
        parts.append(":new:")
    return "".join(parts) if parts else ""


def flag_labels(flags: list) -> str:
    """Convert flags to readable text labels."""
    labels = {
        "CU": "Uptrend", "EU": "Ext.Uptrend", "AC": "Accelerating",
        "HS": "HotStreak", "CS": "ColdStreak", "CW": "ConsistentWin",
        "NU": "NewUptrend", "LU": "LostUptrend",
    }
    return ", ".join(labels.get(f, f) for f in flags if f in labels)


# ---------------------------------------------------------------------------
# Format: Slack (concise summary message)
# ---------------------------------------------------------------------------

def format_slack(data: dict, momentum: list[dict], reversal: list[dict]) -> str:
    """
    Build a concise Slack message. Uses Slack mrkdwn format.
    Shows stats + top 5 per track as one-liners + link to full data.
    """
    summary = data.get("summary", {})
    flags = summary.get("flags", {})
    scan_ts = data.get("ts", "unknown")[:10]

    parts = []

    # ── Header ──
    parts.append(f":bar_chart: *ASX Momentum Digest — {scan_ts}*")
    parts.append("")
    parts.append(
        f":mag: Scanned *{summary.get('scanned', '?')}* stocks  "
        f":arrow_up: *{flags.get('CU', 0)}* uptrends  "
        f":zap: *{flags.get('AC', 0)}* accelerating  "
        f":new: *{flags.get('NU', 0)}* new  "
        f":small_red_triangle_down: *{flags.get('LU', 0)}* lost"
    )

    # ── Track 1: Momentum (top 5) ──
    parts.append("")
    parts.append(f"*:chart_with_upwards_trend: Momentum — Top {min(5, len(momentum))} of {len(momentum)} candidates*")

    for i, s in enumerate(momentum[:5]):
        seg = s.get("seg", {})
        fl = flag_emojis(s.get("f", []))
        seg_parts = []
        for period in ["1wk", "1mo", "3mo", "6mo", "1yr"]:
            v = seg.get(period)
            if v is not None:
                seg_parts.append(fmt_pct(v))
        seg_str = " → ".join(seg_parts) if seg_parts else "—"

        parts.append(
            f"  *{i+1}. {s['t']}* — ${s['pr']:.2f}  "
            f"Score {s['ts']}/8  {fl}  "
            f"_{s.get('s', '?')}_  {fmt_mcap(s.get('mc'))}"
        )
        parts.append(f"      Segments: {seg_str}")

    if len(momentum) > 5:
        remaining = [s["t"] for s in momentum[5:15]]
        parts.append(f"  _+{len(momentum) - 5} more: {', '.join(remaining)}{'...' if len(momentum) > 15 else ''}_")

    # ── Track 2: Reversal (top 5) ──
    parts.append("")
    parts.append(f"*:leftwards_arrow_with_hook: Reversal — Top {min(5, len(reversal))} of {len(reversal)} candidates*")
    parts.append("_40%+ below 52wk high with positive recent segments. Run Reversal Signal Detector before entry._")

    for i, s in enumerate(reversal[:5]):
        seg = s.get("seg", {})
        ws52 = s.get("ws", {}).get("52", {})
        wk_streak = ws52.get("cs", 0)
        streak_str = f"+{wk_streak}wk streak" if wk_streak > 0 else ""

        parts.append(
            f"  *{i+1}. {s['t']}* — ${s['pr']:.2f}  "
            f"*{fmt_pct(s.get('pfh'))}* from 52wH  "
            f"_{s.get('s', '?')}_  {fmt_mcap(s.get('mc'))}"
        )
        recovery_parts = []
        if seg.get("1wk") is not None:
            recovery_parts.append(f"1wk {fmt_pct(seg['1wk'])}")
        if seg.get("1mo") is not None:
            recovery_parts.append(f"1mo {fmt_pct(seg['1mo'])}")
        if streak_str:
            recovery_parts.append(streak_str)
        if recovery_parts:
            parts.append(f"      Recovery: {', '.join(recovery_parts)}")

    if len(reversal) > 5:
        remaining = [s["t"] for s in reversal[5:15]]
        parts.append(f"  _+{len(reversal) - 5} more: {', '.join(remaining)}{'...' if len(reversal) > 15 else ''}_")

    # ── Footer ──
    parts.append("")
    if PAGES_URL:
        parts.append(f":link: <{PAGES_URL}|Full interactive screener>")
    parts.append(
        f"_Filters: ≥${MIN_PRICE:.2f}, ≥${MIN_MCAP/1_000_000:.0f}M mcap, "
        f"≥{MIN_VOLUME:,} vol. Not financial advice._"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Format: Claude trigger (structured prompt for Slack)
# ---------------------------------------------------------------------------

def format_claude(data: dict, momentum: list[dict], reversal: list[dict]) -> str:
    """
    Build a structured prompt for Claude in Slack.
    Includes enough data for Claude to run skills, but kept concise.
    """
    scan_ts = data.get("ts", "unknown")[:10]

    parts = []
    parts.append(f"*ASX Momentum Screener — Candidates for Review ({scan_ts})*")
    parts.append("")

    # ── Momentum: full list as compact data ──
    parts.append(f"*Track 1: Momentum Candidates ({len(momentum)})*")
    parts.append("Run a quick fundamental check on the top 5. Flag any with red flags or strong buy signals.")
    parts.append("")

    for s in momentum[:10]:
        seg = s.get("seg", {})
        fl = flag_labels(s.get("f", []))
        parts.append(
            f"• *{s['t']}* ${s['pr']:.2f} | Score {s['ts']}/8 | {fl} | "
            f"{s.get('s', '?')} | {fmt_mcap(s.get('mc'))} | "
            f"3M {fmt_pct(seg.get('3mo'))} | 1Y {fmt_pct(seg.get('1yr'))} | "
            f"From 52wH {fmt_pct(s.get('pfh'))}"
        )

    if len(momentum) > 10:
        extras = ", ".join(s["t"] for s in momentum[10:])
        parts.append(f"_Also: {extras}_")

    # ── Reversal: full list as compact data ──
    parts.append("")
    parts.append(f"*Track 2: Reversal Candidates ({len(reversal)})*")
    parts.append("Run the Reversal Signal Detector on the top 3-5. Confirm genuine reversal vs dead-cat bounce.")
    parts.append("")

    for s in reversal[:10]:
        seg = s.get("seg", {})
        ws52 = s.get("ws", {}).get("52", {})
        parts.append(
            f"• *{s['t']}* ${s['pr']:.2f} | *{fmt_pct(s.get('pfh'))}* from 52wH | "
            f"{s.get('s', '?')} | {fmt_mcap(s.get('mc'))} | "
            f"1wk {fmt_pct(seg.get('1wk'))} | 1mo {fmt_pct(seg.get('1mo'))} | "
            f"Wk streak {ws52.get('cs', 0):+d}"
        )

    if len(reversal) > 10:
        extras = ", ".join(s["t"] for s in reversal[10:])
        parts.append(f"_Also: {extras}_")

    # ── Instructions for Claude ──
    parts.append("")
    parts.append("*Sector context:* Cross-reference against latest sector rotation signals. Prioritise stocks in sectors with current tailwinds.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Format: Markdown (full tables for GitHub commit)
# ---------------------------------------------------------------------------

def format_markdown(data: dict, momentum: list[dict], reversal: list[dict]) -> str:
    """Build full markdown digest with tables for GitHub commit."""
    summary = data.get("summary", {})
    flags = summary.get("flags", {})
    scan_ts = data.get("ts", "unknown")

    parts = []

    parts.append(f"# ASX Momentum Digest — {scan_ts[:10]}")
    parts.append("")
    parts.append(
        f"**Scanned:** {summary.get('scanned', '?')} stocks | "
        f"**Passed filters:** {summary.get('passed', '?')} | "
        f"**Uptrends (CU):** {flags.get('CU', 0)} | "
        f"**Accelerating:** {flags.get('AC', 0)} | "
        f"**New Uptrends:** {flags.get('NU', 0)} | "
        f"**Lost Uptrends:** {flags.get('LU', 0)}"
    )

    # Track 1: Momentum
    parts.append("")
    parts.append(f"## Track 1: Momentum Candidates ({len(momentum)})")
    parts.append("")
    parts.append(
        "Stocks in continuous uptrend (CU), accelerating (AC), "
        "or newly entering uptrend (NU). Ranked by composite momentum score."
    )
    parts.append("")

    parts.append(
        "| # | Ticker | Name | Price | Score | Flags | "
        "1W | 1M | 3M | 6M | 1Y | "
        "From 52wH | MCap | Vol(20d) | Sector |"
    )
    parts.append("|" + "|".join(["---"] * 15) + "|")

    for i, s in enumerate(momentum):
        seg = s.get("seg", {})
        fl = flag_labels(s.get("f", []))
        parts.append(
            f"| {i+1} | **{s['t']}** | {s.get('n', s['t'])[:25]} | "
            f"${s['pr']:.2f} | {s['ts']} | {fl} | "
            f"{fmt_pct(seg.get('1wk'))} | {fmt_pct(seg.get('1mo'))} | "
            f"{fmt_pct(seg.get('3mo'))} | {fmt_pct(seg.get('6mo'))} | "
            f"{fmt_pct(seg.get('1yr'))} | "
            f"{fmt_pct(s.get('pfh'))} | {fmt_mcap(s.get('mc'))} | "
            f"{fmt_vol(s.get('v20'))} | {s.get('s', '—')} |"
        )

    # Sector breakdown
    parts.append("")
    parts.append(_sector_summary_md(momentum))

    # Track 2: Reversal
    parts.append("")
    parts.append(f"## Track 2: Reversal Candidates ({len(reversal)})")
    parts.append("")
    parts.append(
        "Stocks 40%+ below 52-week high but showing positive recent segments. "
        "Not buy signals — candidates for Reversal Signal Detector analysis."
    )
    parts.append("")

    parts.append(
        "| # | Ticker | Name | Price | From 52wH | From 5yH | "
        "1W | 1M | Score | Wk Streak | MCap | Vol(20d) | Sector |"
    )
    parts.append("|" + "|".join(["---"] * 13) + "|")

    for i, s in enumerate(reversal):
        seg = s.get("seg", {})
        ws52 = s.get("ws", {}).get("52", {})
        parts.append(
            f"| {i+1} | **{s['t']}** | {s.get('n', s['t'])[:25]} | "
            f"${s['pr']:.2f} | {fmt_pct(s.get('pfh'))} | "
            f"{fmt_pct(s.get('pf5h'))} | "
            f"{fmt_pct(seg.get('1wk'))} | {fmt_pct(seg.get('1mo'))} | "
            f"{s['ts']} | {ws52.get('cs', 0):+d} | "
            f"{fmt_mcap(s.get('mc'))} | {fmt_vol(s.get('v20'))} | "
            f"{s.get('s', '—')} |"
        )

    parts.append("")
    parts.append(_sector_summary_md(reversal))

    # Footer
    parts.append("")
    parts.append("---")
    parts.append(
        f"*Filters: min price ${MIN_PRICE:.2f}, min mcap ${MIN_MCAP/1_000_000:.0f}M, "
        f"min vol {MIN_VOLUME:,}. General information only. Not personal financial advice.*"
    )

    return "\n".join(parts)


def _sector_summary_md(candidates: list[dict]) -> str:
    sectors = {}
    for s in candidates:
        sec = s.get("s", "Unknown")
        if sec not in sectors:
            sectors[sec] = []
        sectors[sec].append(s["t"])

    if not sectors:
        return ""

    lines = ["**Sector breakdown:**"]
    for sec, tickers in sorted(sectors.items(), key=lambda x: len(x[1]), reverse=True):
        t_str = ", ".join(tickers[:10])
        suffix = f" +{len(tickers) - 10} more" if len(tickers) > 10 else ""
        lines.append(f"- {sec}: {len(tickers)} ({t_str}{suffix})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def post_to_slack(message: str, webhook: str) -> bool:
    if not requests:
        log.error("requests library not available")
        return False

    payload = {"text": message}
    try:
        resp = requests.post(webhook, json=payload, timeout=30)
        if resp.status_code != 200:
            log.error(f"Slack post failed ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        log.error(f"Slack post error: {e}")
        return False

    log.info("Posted to Slack")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Building momentum digest...")

    data = load_scan(SCAN_OUTPUT)
    stocks = data.get("stocks", [])

    if not stocks:
        log.error("No stocks in scan output")
        sys.exit(1)

    momentum = filter_momentum(stocks)
    reversal = filter_reversal(stocks)

    log.info(f"Momentum candidates: {len(momentum)}")
    log.info(f"Reversal candidates: {len(reversal)}")

    # Build output in requested format
    if FORMAT == "markdown":
        output = format_markdown(data, momentum, reversal)
    elif FORMAT == "claude":
        output = format_claude(data, momentum, reversal)
    else:
        output = format_slack(data, momentum, reversal)

    # Save to file if requested
    if OUTPUT_FILE:
        Path(OUTPUT_FILE).write_text(output)
        log.info(f"Saved digest to {OUTPUT_FILE}")

    # Post to Slack if webhook set
    if SLACK_WEBHOOK:
        post_to_slack(output, SLACK_WEBHOOK)
    elif not OUTPUT_FILE:
        print(output)

    # Print summary to stdout regardless
    print(f"\n{'=' * 60}")
    print(f"  MOMENTUM DIGEST — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'=' * 60}")
    print(f"  Format: {FORMAT}")
    print(f"  Momentum candidates: {len(momentum)}")
    print(f"  Reversal candidates: {len(reversal)}")
    if momentum:
        print(f"\n  Top 5 momentum:")
        for s in momentum[:5]:
            print(f"    {s['t']:<7} ${s['pr']:>7.2f}  score={s['ts']}  flags={','.join(s.get('f', []))}")
    if reversal:
        print(f"\n  Top 5 reversal:")
        for s in reversal[:5]:
            print(f"    {s['t']:<7} ${s['pr']:>7.2f}  from_high={fmt_pct(s.get('pfh'))}  1wk={fmt_pct(s.get('seg', {}).get('1wk'))}")
    print()


if __name__ == "__main__":
    main()