"""
Analysis and scoring logic for Velocity tracker
"""

import math
from typing import Optional, Tuple, List, Dict
from .database import daily_history, weekly_history

def roc(values: list[float], n: int) -> Optional[float]:
    """
    Rate of change: (v[0] - v[n]) / v[n]

    Args:
        values: List of values (most recent first)
        n: Number of periods to look back

    Returns:
        Rate of change or None if not enough data or division by zero
    """
    if len(values) < n + 1 or values[n] == 0:
        return None
    return (values[0] - values[n]) / values[n]


def max_drawdown(prices: list[float]) -> Optional[float]:
    """
    Calculate maximum drawdown: (current - peak) / peak

    Args:
        prices: List of prices (most recent first)

    Returns:
        Drawdown as negative number (or zero) or None if no data
    """
    if not prices:
        return None
    peak = max(prices)
    return (prices[0] - peak) / peak if peak else None


def momentum_consistency(prices: list[float], lag: int = 4) -> Optional[float]:
    """
    Fraction of periods where price > price lag periods ago

    Args:
        prices: List of prices (most recent first)
        lag: Number of periods to look back

    Returns:
        Ratio of up periods or None if not enough data
    """
    if len(prices) < lag + 1:
        return None
    hits = sum(1 for i in range(len(prices) - lag) if prices[i] > prices[i + lag])
    return hits / (len(prices) - lag)


def weight_percentile(conn, ticker: str, today: str) -> Optional[float]:
    """
    Calculate weight percentile among all holdings for a given date.

    Args:
        conn: Database connection
        ticker: Ticker symbol
        today: Date in YYYY-MM-DD format

    Returns:
        Percentile (0-100) or None if insufficient data
    """
    all_weights = [r["weight_pct"] for r in conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND weight_pct IS NOT NULL", (today,)
    )]

    if len(all_weights) < 2:
        return None

    my_weight = conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND ticker=?", (today, ticker)
    ).fetchone()

    if not my_weight:
        return None

    below = sum(1 for w in all_weights if w < my_weight["weight_pct"])
    return (below / len(all_weights)) * 100


def weight_zscore(conn, ticker: str, today: str) -> Optional[float]:
    """
    Calculate weight z-score: (weight - mean) / stddev

    Args:
        conn: Database connection
        ticker: Ticker symbol
        today: Date in YYYY-MM-DD format

    Returns:
        Z-score or None if insufficient data or zero stddev
    """
    all_weights = [r["weight_pct"] for r in conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND weight_pct IS NOT NULL", (today,)
    )]

    if len(all_weights) < 2:
        return None

    mean = sum(all_weights) / len(all_weights)
    variance = sum((w - mean) ** 2 for w in all_weights) / len(all_weights)
    if variance == 0:
        return None

    std_dev = math.sqrt(variance)
    my_weight = conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND ticker=?", (today, ticker)
    ).fetchone()

    return ((my_weight["weight_pct"] - mean) / std_dev) if my_weight else None


def confirmation_score(metrics: dict) -> Tuple[int, List[str], List[bool]]:
    """
    Calculate confirmation score (0-5) based on five signals.

    Args:
        metrics: Dictionary containing calculated metrics

    Returns:
        Tuple of (score, signals_list, signal_bits). ``signal_bits`` is a
        length-5 list of booleans in the SAME fixed order the signals are
        appended: a bit is True only in the ✅ (score-incrementing) branch,
        False for both ❌ (failing) and ⬜ (insufficient data). The bit order
        MUST match the signal append order — transitions diff bits positionally.
    """
    score = 0
    signals = []
    bits = []

    # Signal 1: Price ROC (20d) > 0
    roc20 = metrics.get("price_roc_20d")
    if roc20 is not None:
        if roc20 > 0:
            score += 1
            bits.append(True)
            signals.append(f"✅ Price ROC 20d: +{roc20*100:.1f}% — still rising")
        else:
            bits.append(False)
            signals.append(f"❌ Price ROC 20d: {roc20*100:.1f}% — fading")
    else:
        bits.append(False)
        signals.append("⬜ Price ROC 20d: building history")

    # Signal 2: Weight growing since entry
    wroc = metrics.get("weight_roc_since_entry")
    if wroc is not None:
        if wroc > 0:
            score += 1
            bits.append(True)
            signals.append(f"✅ Weight since entry: +{wroc*100:.1f}% — ETF adding conviction")
        else:
            bits.append(False)
            signals.append(f"❌ Weight since entry: {wroc*100:.1f}% — not growing")
    else:
        bits.append(False)
        signals.append("⬜ Weight change: first appearance")

    # Signal 3: Weight > 50th percentile
    pct = metrics.get("weight_percentile")
    if pct is not None:
        if pct >= 50:
            score += 1
            bits.append(True)
            signals.append(f"✅ Weight rank: {pct:.0f}th pctile — meaningful position")
        else:
            bits.append(False)
            signals.append(f"❌ Weight rank: {pct:.0f}th pctile — small/token position")
    else:
        bits.append(False)
        signals.append("⬜ Weight rank: unknown")

    # Signal 4: Price above entry price
    entry_roc = metrics.get("price_roc_since_entry")
    if entry_roc is not None:
        if entry_roc >= 0:
            score += 1
            bits.append(True)
            signals.append(f"✅ vs Entry price: +{entry_roc*100:.1f}% — holding gains")
        else:
            bits.append(False)
            signals.append(f"❌ vs Entry price: {entry_roc*100:.1f}% — fading post-inclusion")
    else:
        bits.append(False)
        signals.append("⬜ vs Entry price: first appearance today")

    # Signal 5: Within 20% of 52-week high
    dd = metrics.get("drawdown_from_52w_high")
    if dd is not None:
        if dd >= -0.20:  # Within 20% of high (drawdown >= -20%)
            score += 1
            bits.append(True)
            signals.append(f"✅ 52w drawdown: {dd*100:.1f}% — not overextended")
        else:
            bits.append(False)
            signals.append(f"❌ 52w drawdown: {dd*100:.1f}% — extended, reversion risk")
    else:
        bits.append(False)
        signals.append("⬜ 52w drawdown: building weekly history")

    return score, signals, bits


BUY_THRESHOLD = 4  # score must reach this or above to signal a buy

# Fixed order MUST match the bit append order in confirmation_score():
#   [price_roc_20d, weight_roc_since_entry, weight_percentile,
#    price_roc_since_entry, drawdown_from_52w_high]
# Each label describes the signal flipping ON (❌/⬜ → ✅), used to build
# human-readable buy reasons from a positionally diffed bit vector.
SIGNAL_LABELS = [
    "Price ROC 20d turned positive",   # signal 1
    "Weight growing since entry",      # signal 2
    "Weight rank entered top half",    # signal 3
    "Price above entry",               # signal 4
    "Within 20% of 52w high",           # signal 5
]


def _flipped_signal_labels(
    prior_bits: list[bool] | None,
    current_bits: list[bool] | None,
) -> list[str]:
    """Labels for signals that went False→True between prior and current bits.

    A signal present in ``current_bits`` but missing (or False) in ``prior_bits``
    counts as flipped. A prior ticker absent from the bit map is treated as
    all-False, so a brand-new ticker's labels reflect every signal currently ON
    (informative for a "new entrant" reason).
    """
    if not current_bits:
        return []
    if prior_bits is None:
        prior_bits = [False] * len(current_bits)
    # Guard against mismatched lengths — only diff the overlapping tail.
    n = min(len(current_bits), len(prior_bits))
    return [
        SIGNAL_LABELS[i]
        for i in range(n)
        if current_bits[i] and not prior_bits[i]
    ]


def format_buy_reason(
    prior: int,
    current: int,
    flipped: list[str],
    is_new: bool = False,
) -> str:
    """Build a human-readable reason string for a buy trade.

    - Genuine threshold crossing (ticker scored before): ``score 3→4 · <flips>``
    - New entrant to scoring (ticker first scored today, prior state exists):
      ``New entrant · score 0→4 · <signals currently ON>`` — ``flipped`` holds
      the on-signals in this case.
    - Whole-system bootstrap (no prior trading state at all): the caller passes
      ``flipped=[]`` (no prior bits to diff), yielding ``Initial buy · score 4/5``
      — honest that we have no prior signal history to compare against.
    """
    if is_new:
        if flipped:
            base = f"New entrant · score 0→{current}"
        else:
            base = f"Initial buy · score {current}/5"
    else:
        base = f"score {prior}→{current}"
    if flipped:
        base += " · " + " + ".join(flipped)
    return base


def compute_score_transitions(
    prior_scores: dict[str, int],
    current_scores: dict[str, int],
    threshold: int = BUY_THRESHOLD,
    prior_signals: Optional[dict[str, list[bool]]] = None,
    current_signals: Optional[dict[str, list[bool]]] = None,
) -> dict[str, list[dict]]:
    """
    Compare prior and current score maps, returning threshold-crossing events.

    Rules (from the plan):
    - Buy  : prior < threshold AND current >= threshold  (e.g. 2→4, or new ticker at ≥4)
    - Sell : prior >= threshold AND current < threshold  (e.g. 5→3)
    - Unchanged : everything else

    New tickers absent from prior_scores are treated as prior = 0.
    Tickers that disappeared from current_scores are ignored here (exit logic
    is handled separately via detect_exits / portfolio position checks).

    When ``prior_signals`` and ``current_signals`` are supplied, each buy
    signal's ``flipped`` list names the confirmation signals that went
    False→True to push the score to/below the threshold (used to build buy
    reasons). When either is None — e.g. the whole-system bootstrap run with no
    prior trading state — ``flipped`` is left empty for every buy (the caller
    renders an honest "Initial buy" reason rather than claiming all signals
    flipped).

    Args:
        prior_scores:    {ticker: score} from the last persisted trading state.
        current_scores:  {ticker: score} computed for today's snapshot.
        threshold:       The crossing boundary (default 4).
        prior_signals:   Optional {ticker: [bool*5]} bit map from prior state.
        current_signals: Optional {ticker: [bool*5]} bit map for today.

    Returns:
        {
            "buy_signals":  [{"ticker","prior","current","flipped","is_new"}, ...],
            "sell_signals": [{"ticker","prior","current","flipped"}, ...],
            "unchanged":    [{"ticker","prior","current","flipped"}, ...],
        }
    """
    buy_signals: list[dict] = []
    sell_signals: list[dict] = []
    unchanged: list[dict] = []

    can_diff = prior_signals is not None and current_signals is not None

    for ticker, current in current_scores.items():
        prior = prior_scores.get(ticker, 0)  # absent → treat as 0
        is_new = ticker not in prior_scores
        if can_diff:
            flipped = _flipped_signal_labels(
                prior_signals.get(ticker), current_signals.get(ticker),
            )
        else:
            flipped = []
        entry = {"ticker": ticker, "prior": prior, "current": current, "flipped": flipped}
        if prior < threshold and current >= threshold:
            entry["is_new"] = is_new
            buy_signals.append(entry)
        elif prior >= threshold and current < threshold:
            sell_signals.append(entry)
        else:
            unchanged.append(entry)

    # Sort for deterministic output / logging
    buy_signals.sort(key=lambda x: x["ticker"])
    sell_signals.sort(key=lambda x: x["ticker"])
    unchanged.sort(key=lambda x: x["ticker"])

    return {
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
        "unchanged": unchanged,
    }


def compute_metrics(conn, ticker: str, today: str) -> dict:
    """
    Calculate all metrics for a given ticker and date.

    Args:
        conn: Database connection
        ticker: Ticker symbol
        today: Date in YYYY-MM-DD format

    Returns:
        Dictionary of calculated metrics
    """
    # Get daily and weekly history
    dh = daily_history(conn, ticker, 25)
    wh = weekly_history(conn, ticker, 52)

    # Extract price arrays
    dp = [r["price"] for r in dh if r.get("price") is not None]
    wp = [r["price"] for r in wh if r.get("price") is not None]

    # Get first seen date
    first_seen_row = conn.execute(
        "SELECT MIN(snapshot_date) as d FROM holdings WHERE ticker=?", (ticker,)
    ).fetchone()
    first_seen = first_seen_row["d"] if first_seen_row else None

    # Helper to get value for specific date/column
    def fetch_val(date, col):
        row = conn.execute(
            f"SELECT {col} FROM holdings WHERE ticker=? AND snapshot_date=?",
            (ticker, date)
        ).fetchone()
        return row[col] if row else None

    # Current values (most recent)
    price_now = dp[0] if dp else None
    price_entry = fetch_val(first_seen, "price") if first_seen else None
    weight_now = fetch_val(today, "weight_pct")
    weight_entry = fetch_val(first_seen, "weight_pct") if first_seen else None

    # Calculate returns since entry
    price_roc_entry = None
    weight_roc_entry = None
    if first_seen and first_seen != today:
        if price_now and price_entry and price_entry > 0:
            price_roc_entry = (price_now - price_entry) / price_entry
        if weight_now and weight_entry and weight_entry > 0:
            weight_roc_entry = (weight_now - weight_entry) / weight_entry

    # Get current record for name, sector, currency
    current_record = conn.execute(
        "SELECT currency, name, sector FROM holdings WHERE ticker=? AND snapshot_date=?",
        (ticker, today)
    ).fetchone()

    # Build metrics dictionary
    metrics = {
        "ticker": ticker,
        "name": current_record["name"] if current_record else ticker,
        "sector": current_record["sector"] if current_record else "",
        "currency": current_record["currency"] if current_record else "",
        "first_seen": first_seen,
        "days_in_etf": len(dh),
        "current_price": price_now,
        "price_at_entry": price_entry,
        "current_weight": weight_now,
        "weight_at_entry": weight_entry,
        "price_roc_since_entry": price_roc_entry,
        "weight_roc_since_entry": weight_roc_entry,
        "price_roc_5d": roc(dp, 5) if len(dp) > 5 else None,
        "price_roc_10d": roc(dp, 10) if len(dp) > 10 else None,
        "price_roc_20d": roc(dp, 20) if len(dp) > 20 else None,
        "price_roc_52w": roc(wp, 51) if len(wp) > 51 else None,
        "weight_percentile": weight_percentile(conn, ticker, today),
        "weight_zscore": weight_zscore(conn, ticker, today),
        "drawdown_from_52w_high": max_drawdown(wp) if wp else None,
        "momentum_consistency": momentum_consistency(wp),
        "daily_history": dh[:20],  # Last 20 days for display
        "weekly_history": wh[:52],  # Last 52 weeks for display
    }

    # Calculate confirmation score and signals
    metrics["confirmation_score"], metrics["confirmation_signals"], metrics["confirmation_signal_bits"] = confirmation_score(metrics)

    return metrics