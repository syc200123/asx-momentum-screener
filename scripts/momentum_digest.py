#!/usr/bin/env python3
"""
ASX Momentum Digest — Post-Scan Filter & Slack Reporter
========================================================
Reads the full asx_momentum_data.json from the screener, filters to
actionable candidates across two tracks (Momentum + Reversal), and
outputs a compact digest suitable for Slack posting and Claude analysis.

Designed to run as a second step in the GitHub Actions pipeline:
  1. scan.py → asx_momentum_data.json (full ~5MB dataset)
  2. momentum_digest.py → filtered candidates posted to Slack

Two candidate tracks:
  MOMENTUM — Stocks in continuous uptrend or accelerating momentum
  REVERSAL — Crashed stocks showing early recovery signals

Environment variables:
  SCAN_OUTPUT     Path to screener JSON (default: docs/data/asx_momentum_data.json)
  SLACK_WEBHOOK   Slack webhook URL for posting digest
  SLACK_CHANNEL   Override channel (optional)
  MIN_PRICE       Minimum price filter (default: 0.50)
  MIN_MCAP        Minimum market cap in millions (default: 100)
  MIN_VOLUME      Minimum 20-day avg volume (default: 50000)
  MAX_MOMENTUM    Max momentum candidates to report (default: 30)
  MAX_REVERSAL    Max reversal candidates to report (default: 20)
  OUTPUT_FILE     Save digest to file (optional, for local testing)
  FORMAT          Output format: 'slack' or 'markdown' (default: slack)
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
MIN_MCAP = float(os.environ.get("MIN_MCAP", "100")) * 1_000_000  # Convert to raw
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "50000"))
MAX_MOMENTUM = int(os.environ.get("MAX_MOMENTUM", "30"))
MAX_REVERSAL = int(os.environ.get("MAX_REVERSAL", "20"))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "")
FORMAT = os.environ.get("FORMAT", "slack")

# Flag key reference (from scan.py):
# CU = Continuous Uptrend (all core segments positive)
# EU = Extended Uptrend (CU + 2yr and 3yr positive)
# AC = Accelerating (each shorter period faster than longer)
# HS = Hot Streak (8+ consecutive positive weeks)
# CS = Cold Streak (6+ consecutive negative weeks)
# CW = Consistent Winner (70%+ positive weeks in last 52)
# NU = New Uptrend (gained CU since last snapshot)
# LU = Lost Uptrend (lost CU since last snapshot)


# ---------------------------------------------------------------------------
# Load & Parse
# ---------------------------------------------------------------------------

def load_scan(path: str) -> dict:
    """Load the screener JSON output."""
    p = Path(path)
    if not p.exists():
        log.error(f"Scan output not found: {path}")
        sys.exit(1)

    with open(p) as f:
        data = json.load(f)

    log.info(
        f"Loaded {len(data.get('stocks', []))} stocks "
        f"from scan at {data.get('ts', 'unknown')}"
    )
    return data


def passes_quality_filter(stock: dict) -> bool:
    """Apply minimum quality thresholds."""
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

    Criteria (any of):
    - CU flag (continuous uptrend across all core segments)
    - Total score >= 5 with AC (accelerating) flag
    - NU flag (newly entered uptrend since last scan)

    Ranked by: total score, then acceleration, then 3-month segment return.
    """
    candidates = []

    for s in stocks:
        if not passes_quality_filter(s):
            continue

        flags = set(s.get("f", []))
        ts = s.get("ts", 0)
        seg = s.get("seg", {})

        # Must meet at least one momentum criterion
        is_cu = "CU" in flags
        is_strong_accel = ts >= 5 and "AC" in flags
        is_new_uptrend = "NU" in flags

        if not (is_cu or is_strong_accel or is_new_uptrend):
            continue

        # Compute a composite rank score for sorting
        rank = ts * 10  # Base: total score (0-80)
        if is_cu:
            rank += 20
        if "EU" in flags:
            rank += 10  # Extended uptrend bonus
        if "AC" in flags:
            rank += 15  # Accelerating bonus
        if "HS" in flags:
            rank += 10  # Hot streak bonus
        if "CW" in flags:
            rank += 5   # Consistent winner bonus
        if is_new_uptrend:
            rank += 25  # New uptrend gets attention

        # Add 3-month segment return as tiebreaker
        seg_3m = seg.get("3mo")
        if seg_3m is not None:
            rank += min(seg_3m * 100, 50)  # Cap contribution at 50

        s["_rank"] = rank
        s["_track"] = "MOMENTUM"
        candidates.append(s)

    candidates.sort(key=lambda x: x["_rank"], reverse=True)
    return candidates[:MAX_MOMENTUM]


# ---------------------------------------------------------------------------
# Track 2: Reversal Candidates
# ---------------------------------------------------------------------------

def filter_reversal(stocks: list[dict]) -> list[dict]:
    """
    Identify crashed stocks showing early recovery signals.

    Criteria (all required):
    - Price >= 40% below 52-week high (pfh <= -0.40)
    - At least one positive recent segment (1wk or 1mo)
    - Not in cold streak (CS flag absent)
    - Minimum market cap and volume filters apply

    Additional signals captured:
    - Positive 1-week segment (immediate momentum)
    - Positive 1-month segment (sustained bounce)
    - Score improving (dts > 0 from previous scan)

    These are candidates for the Reversal Signal Detector skill to
    analyse in detail — NOT buy signals.
    """
    candidates = []

    for s in stocks:
        if not passes_quality_filter(s):
            continue

        flags = set(s.get("f", []))
        seg = s.get("seg", {})
        pfh = s.get("pfh", 0)  # Percent from 52-week high (negative = below)

        # Must be significantly below 52-week high
        if pfh is None or pfh > -0.40:
            continue

        # Must NOT be in cold streak (still actively declining)
        if "CS" in flags:
            continue

        # Must show at least one positive recent segment
        seg_1wk = seg.get("1wk")
        seg_1mo = seg.get("1mo")
        has_recent_positive = (
            (seg_1wk is not None and seg_1wk > 0) or
            (seg_1mo is not None and seg_1mo > 0)
        )
        if not has_recent_positive:
            continue

        # Compute reversal strength score for ranking
        rank = 0

        # How far below high (deeper crash = more upside if reversing)
        crash_depth = abs(pfh)
        rank += min(crash_depth * 100, 60)  # Cap at 60 pts

        # Recent momentum signals
        if seg_1wk is not None and seg_1wk > 0:
            rank += 15 + min(seg_1wk * 200, 20)  # Positive week
        if seg_1mo is not None and seg_1mo > 0:
            rank += 20 + min(seg_1mo * 100, 20)  # Positive month (stronger signal)

        # Score improvement from previous scan
        dts = s.get("dts", 0)
        if dts and dts > 0:
            rank += dts * 5  # Each score point gained

        # Weekly streak data — positive current streak is good
        ws52 = s.get("ws", {}).get("52", {})
        cs = ws52.get("cs", 0)
        if cs > 0:
            rank += min(cs * 3, 15)  # Positive streak, cap at 15

        # Larger market cap = more institutional interest in recovery
        mc = s.get("mc", 0)
        if mc and mc > 1_000_000_000:  # > $1B
            rank += 10
        elif mc and mc > 500_000_000:  # > $500M
            rank += 5

        s["_rank"] = rank
        s["_track"] = "REVERSAL"
        candidates.append(s)

    candidates.sort(key=lambda x: x["_rank"], reverse=True)
    return candidates[:MAX_REVERSAL]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_pct(val, places=1) -> str:
    """Format a decimal as percentage string."""
    if val is None:
        return "  —  "
    return f"{val:+.{places}%}"


def fmt_mcap(val) -> str:
    """Format market cap as human-readable string."""
    if not val:
        return "—"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.0f}M"
    return f"${val:,.0f}"


def fmt_vol(val) -> str:
    """Format volume as human-readable string."""
    if not val:
        return "—"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.0f}K"
    return str(val)


def flag_display(flags: list) -> str:
    """Convert flag codes to readable labels."""
    labels = {
        "CU": "Uptrend",
        "EU": "Ext.Uptrend",
        "AC": "Accelerating",
        "HS": "HotStreak",
        "CS": "ColdStreak",
        "CW": "ConsistentWin",
        "NU": "NewUptrend",
        "LU": "LostUptrend",
    }
    return ", ".join(labels.get(f, f) for f in flags if f in labels)


def format_momentum_table(candidates: list[dict], fmt: str = "slack") -> str:
    """Format momentum candidates as a table."""
    if not candidates:
        return "_No momentum candidates found matching filters._"

    lines = []

    if fmt == "markdown":
        lines.append(
            "| Ticker | Name | Price | Score | Flags | "
            "1W Seg | 1M Seg | 3M Seg | 6M Seg | 1Y Seg | "
            "From 52wH | MCap | Vol(20d) | Sector |"
        )
        lines.append("|" + "|".join(["---"] * 14) + "|")
    else:
        # Slack: use code block for alignment
        lines.append(
            f"{'Tick':<7} {'Price':>8} {'Scr':>3} {'Flags':<20} "
            f"{'1W':>7} {'1M':>7} {'3M':>7} {'6M':>7} {'1Y':>7} "
            f"{'Fr52H':>7} {'MCap':>7} {'Sector':<20}"
        )
        lines.append("─" * 130)

    for s in candidates:
        seg = s.get("seg", {})
        flags = s.get("f", [])

        if fmt == "markdown":
            lines.append(
                f"| **{s['t']}** | {s.get('n', s['t'])[:25]} | "
                f"${s['pr']:.2f} | {s['ts']} | {flag_display(flags)} | "
                f"{fmt_pct(seg.get('1wk'))} | {fmt_pct(seg.get('1mo'))} | "
                f"{fmt_pct(seg.get('3mo'))} | {fmt_pct(seg.get('6mo'))} | "
                f"{fmt_pct(seg.get('1yr'))} | "
                f"{fmt_pct(s.get('pfh'))} | {fmt_mcap(s.get('mc'))} | "
                f"{fmt_vol(s.get('v20'))} | {s.get('s', '—')} |"
            )
        else:
            flag_str = ",".join(f for f in flags if f in {"CU","EU","AC","HS","CW","NU"})
            lines.append(
                f"{s['t']:<7} {s['pr']:>8.2f} {s['ts']:>3} {flag_str:<20} "
                f"{fmt_pct(seg.get('1wk')):>7} {fmt_pct(seg.get('1mo')):>7} "
                f"{fmt_pct(seg.get('3mo')):>7} {fmt_pct(seg.get('6mo')):>7} "
                f"{fmt_pct(seg.get('1yr')):>7} "
                f"{fmt_pct(s.get('pfh')):>7} {fmt_mcap(s.get('mc')):>7} "
                f"{s.get('s', '—')[:20]:<20}"
            )

    return "\n".join(lines)


def format_reversal_table(candidates: list[dict], fmt: str = "slack") -> str:
    """Format reversal candidates as a table."""
    if not candidates:
        return "_No reversal candidates found matching filters._"

    lines = []

    if fmt == "markdown":
        lines.append(
            "| Ticker | Name | Price | From 52wH | From 5yH | "
            "1W Seg | 1M Seg | Score | Wk Streak | "
            "MCap | Vol(20d) | Sector |"
        )
        lines.append("|" + "|".join(["---"] * 12) + "|")
    else:
        lines.append(
            f"{'Tick':<7} {'Price':>8} {'Fr52H':>7} {'Fr5yH':>7} "
            f"{'1W':>7} {'1M':>7} {'Scr':>3} {'WkStr':>5} "
            f"{'MCap':>7} {'Sector':<20}"
        )
        lines.append("─" * 100)

    for s in candidates:
        seg = s.get("seg", {})
        ws52 = s.get("ws", {}).get("52", {})

        if fmt == "markdown":
            lines.append(
                f"| **{s['t']}** | {s.get('n', s['t'])[:25]} | "
                f"${s['pr']:.2f} | {fmt_pct(s.get('pfh'))} | "
                f"{fmt_pct(s.get('pf5h'))} | "
                f"{fmt_pct(seg.get('1wk'))} | {fmt_pct(seg.get('1mo'))} | "
                f"{s['ts']} | {ws52.get('cs', 0):+d} | "
                f"{fmt_mcap(s.get('mc'))} | {fmt_vol(s.get('v20'))} | "
                f"{s.get('s', '—')} |"
            )
        else:
            lines.append(
                f"{s['t']:<7} {s['pr']:>8.2f} {fmt_pct(s.get('pfh')):>7} "
                f"{fmt_pct(s.get('pf5h')):>7} "
                f"{fmt_pct(seg.get('1wk')):>7} {fmt_pct(seg.get('1mo')):>7} "
                f"{s['ts']:>3} {ws52.get('cs', 0):>+5d} "
                f"{fmt_mcap(s.get('mc')):>7} {s.get('s', '—')[:20]:<20}"
            )

    return "\n".join(lines)


def sector_summary(candidates: list[dict]) -> str:
    """Summarise candidates by sector."""
    sectors = {}
    for s in candidates:
        sec = s.get("s", "Unknown")
        if sec not in sectors:
            sectors[sec] = {"count": 0, "tickers": []}
        sectors[sec]["count"] += 1
        sectors[sec]["tickers"].append(s["t"])

    if not sectors:
        return ""

    lines = ["*Sector breakdown:*"]
    for sec, data in sorted(sectors.items(), key=lambda x: x[1]["count"], reverse=True):
        tickers = ", ".join(data["tickers"][:8])
        suffix = f" +{data['count'] - 8} more" if data["count"] > 8 else ""
        lines.append(f"  {sec}: {data['count']} ({tickers}{suffix})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build digest
# ---------------------------------------------------------------------------

def build_digest(
    data: dict,
    momentum: list[dict],
    reversal: list[dict],
    fmt: str = "slack",
) -> str:
    """Build the complete digest message."""
    summary = data.get("summary", {})
    flags = summary.get("flags", {})
    scan_ts = data.get("ts", "unknown")

    parts = []

    # Header
    if fmt == "markdown":
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
    else:
        parts.append(f"*ASX Momentum Digest — {scan_ts[:10]}*")
        parts.append(
            f"Scanned: {summary.get('scanned', '?')} | "
            f"Passed: {summary.get('passed', '?')} | "
            f"Uptrends: {flags.get('CU', 0)} | "
            f"Accel: {flags.get('AC', 0)} | "
            f"New: {flags.get('NU', 0)} | "
            f"Lost: {flags.get('LU', 0)}"
        )

    # Track 1: Momentum
    parts.append("")
    if fmt == "markdown":
        parts.append(f"## Track 1: Momentum Candidates ({len(momentum)})")
        parts.append("")
        parts.append(
            "Stocks in continuous uptrend (CU), accelerating (AC), "
            "or newly entering uptrend (NU). "
            "Ranked by composite momentum score."
        )
    else:
        parts.append(f"*— TRACK 1: MOMENTUM CANDIDATES ({len(momentum)}) —*")

    parts.append("")
    if fmt == "slack":
        parts.append("```")
    parts.append(format_momentum_table(momentum, fmt))
    if fmt == "slack":
        parts.append("```")

    parts.append("")
    parts.append(sector_summary(momentum))

    # Track 2: Reversal
    parts.append("")
    if fmt == "markdown":
        parts.append(f"## Track 2: Reversal Candidates ({len(reversal)})")
        parts.append("")
        parts.append(
            "Stocks 40%+ below 52-week high but showing positive recent "
            "segments (1-week or 1-month). Not buy signals — candidates for "
            "Reversal Signal Detector analysis. Ranked by crash depth × "
            "recovery strength."
        )
    else:
        parts.append(f"*— TRACK 2: REVERSAL CANDIDATES ({len(reversal)}) —*")
        parts.append(
            "40%+ below 52wk high, showing positive recent segments. "
            "Run Reversal Signal Detector before any entry."
        )

    parts.append("")
    if fmt == "slack":
        parts.append("```")
    parts.append(format_reversal_table(reversal, fmt))
    if fmt == "slack":
        parts.append("```")

    parts.append("")
    parts.append(sector_summary(reversal))

    # Footer
    parts.append("")
    if fmt == "markdown":
        parts.append("---")
        parts.append(
            "*Filters: "
            f"min price ${MIN_PRICE:.2f}, "
            f"min mcap ${MIN_MCAP/1_000_000:.0f}M, "
            f"min vol {MIN_VOLUME:,}. "
            "General information only. Not personal financial advice.*"
        )
    else:
        parts.append(
            f"_Filters: ≥${MIN_PRICE:.2f}, ≥${MIN_MCAP/1_000_000:.0f}M mcap, "
            f"≥{MIN_VOLUME:,} vol. Not financial advice._"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def post_to_slack(message: str, webhook: str):
    """Post digest to Slack via webhook."""
    if not requests:
        log.error("requests library not available — cannot post to Slack")
        return False

    # Slack has a 40K character limit per message
    # Split into chunks if needed
    MAX_CHARS = 39_000
    chunks = []

    if len(message) <= MAX_CHARS:
        chunks = [message]
    else:
        # Split at section boundaries
        sections = message.split("\n*— TRACK")
        current = sections[0]
        for section in sections[1:]:
            section = "*— TRACK" + section
            if len(current) + len(section) > MAX_CHARS:
                chunks.append(current)
                current = section
            else:
                current += "\n" + section
        if current:
            chunks.append(current)

    for i, chunk in enumerate(chunks):
        payload = {"text": chunk}
        try:
            resp = requests.post(webhook, json=payload, timeout=30)
            if resp.status_code != 200:
                log.error(f"Slack post failed ({resp.status_code}): {resp.text}")
                return False
            if i < len(chunks) - 1:
                import time
                time.sleep(1)  # Rate limit between chunks
        except Exception as e:
            log.error(f"Slack post error: {e}")
            return False

    log.info(f"Posted digest to Slack ({len(chunks)} message(s))")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Building momentum digest...")

    # Load scan data
    data = load_scan(SCAN_OUTPUT)
    stocks = data.get("stocks", [])

    if not stocks:
        log.error("No stocks in scan output")
        sys.exit(1)

    # Filter both tracks
    momentum = filter_momentum(stocks)
    reversal = filter_reversal(stocks)

    log.info(f"Momentum candidates: {len(momentum)}")
    log.info(f"Reversal candidates: {len(reversal)}")

    # Build digest
    digest = build_digest(data, momentum, reversal, fmt=FORMAT)

    # Output
    if OUTPUT_FILE:
        Path(OUTPUT_FILE).write_text(digest)
        log.info(f"Saved digest to {OUTPUT_FILE}")

    if SLACK_WEBHOOK:
        post_to_slack(digest, SLACK_WEBHOOK)
    elif not OUTPUT_FILE:
        # No webhook and no file — print to stdout
        print(digest)

    # Also save a structured JSON for Claude API consumption
    structured = {
        "generated": datetime.now().isoformat(),
        "scan_timestamp": data.get("ts"),
        "summary": data.get("summary"),
        "filters": {
            "min_price": MIN_PRICE,
            "min_mcap": MIN_MCAP,
            "min_volume": MIN_VOLUME,
        },
        "momentum_candidates": [
            {
                "ticker": s["t"],
                "name": s.get("n", s["t"]),
                "price": s["pr"],
                "score": s["ts"],
                "flags": s.get("f", []),
                "segments": s.get("seg", {}),
                "from_52w_high": s.get("pfh"),
                "from_5y_high": s.get("pf5h"),
                "mcap": s.get("mc"),
                "volume_20d": s.get("v20"),
                "sector": s.get("s", "Unknown"),
                "weekly_streak": s.get("ws", {}).get("52", {}).get("cs", 0),
            }
            for s in momentum
        ],
        "reversal_candidates": [
            {
                "ticker": s["t"],
                "name": s.get("n", s["t"]),
                "price": s["pr"],
                "score": s["ts"],
                "flags": s.get("f", []),
                "segments": s.get("seg", {}),
                "from_52w_high": s.get("pfh"),
                "from_5y_high": s.get("pf5h"),
                "mcap": s.get("mc"),
                "volume_20d": s.get("v20"),
                "sector": s.get("s", "Unknown"),
                "weekly_streak": s.get("ws", {}).get("52", {}).get("cs", 0),
            }
            for s in reversal
        ],
    }

    structured_path = OUTPUT_FILE.replace(".md", "_structured.json") if OUTPUT_FILE else "momentum_digest.json"
    if OUTPUT_FILE or not SLACK_WEBHOOK:
        Path(structured_path).write_text(
            json.dumps(structured, indent=2, default=str)
        )
        log.info(f"Saved structured data to {structured_path}")

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"  MOMENTUM DIGEST — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'=' * 60}")
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
