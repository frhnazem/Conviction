"""Five process-habit detectors. One suggestion. Optional focus theme."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

HABIT_LOOKBACK_DAYS = 30
HABIT_MIN_MISSING = 3
HABIT_MIN_RATE = 0.5

FOCUS_THEMES = ("auto", "fomo", "rushed", "invalidation", "revenge")
KNOWN_PATTERN_IDS = (
    "fomo_no_invalidation",
    "rushed_buy_no_invalidation",
    "ignore_invalidation_then_loss",
    "revenge_after_loss",
    "buy_missing_invalidation",
)

RULE_TEXT = {
    "fomo_no_invalidation": "No Buy without invalidation when emotion is FOMO.",
    "rushed_buy_no_invalidation": "No Buy without invalidation when emotion is Rushed.",
    "ignore_invalidation_then_loss": "When invalidation is hit, do not ignore it — exit or reduce instead of holding.",
    "revenge_after_loss": "After a Loss, no new Buy for 24 hours.",
    "buy_missing_invalidation": "Require an invalidation level before every Buy.",
}

PATTERN_LABELS = {
    "fomo_no_invalidation": "FOMO buys with no invalidation",
    "rushed_buy_no_invalidation": "Rushed buys with no invalidation",
    "ignore_invalidation_then_loss": "Ignored invalidation then loss",
    "revenge_after_loss": "Buys within 24 hours after a loss",
    "buy_missing_invalidation": "Buys with no invalidation",
}

THEME_FAMILY = {
    "fomo": ["fomo_no_invalidation"],
    "rushed": ["rushed_buy_no_invalidation"],
    "invalidation": ["ignore_invalidation_then_loss", "buy_missing_invalidation"],
    "revenge": ["revenge_after_loss"],
}

EMPTY = {
    "pattern_id": None,
    "habit_evidence": "Not enough matching logs to suggest a habit rule yet.",
    "suggested_rule": None,
}


def is_focus_theme(value: str | None) -> bool:
    return (value or "") in FOCUS_THEMES


def _parse_ts(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, datetime):
        ts = raw
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    text = str(raw or "")
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return fallback


def _has_invalidation(row: dict) -> bool:
    return bool(str(row.get("invalidation") or "").strip())


def _emotion(row: dict) -> str:
    return str(row.get("emotion") or "").strip().lower()


def _is_fomo(row: dict) -> bool:
    return _emotion(row) == "fomo"


def _is_rushed(row: dict) -> bool:
    e = _emotion(row)
    return e == "rushed" or "rush" in e


def _lookback(rows: list[dict], now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=HABIT_LOOKBACK_DAYS)
    recent = [r for r in rows if _parse_ts(r.get("created_at"), now) >= cutoff]
    return recent if recent else list(rows)


def _outcome_evidence(subset: list[dict], label: str) -> str | None:
    with_result = [
        r
        for r in subset
        if str(r.get("trade_result") or "").strip().lower() in ("win", "loss", "breakeven", "skipped")
    ]
    if len(with_result) < 3:
        return None
    losses = sum(1 for r in with_result if str(r.get("trade_result") or "").lower() == "loss")
    wins = sum(1 for r in with_result if str(r.get("trade_result") or "").lower() == "win")
    n = len(with_result)
    pct = round((losses / n) * 100)
    extra = f", {wins} Win" if wins else ""
    return f"Of {n} {label} and a recorded result, {losses} were Loss ({pct}%){extra}."


def _count_revenge_buys(rows: list[dict], now: datetime) -> int:
    dated = sorted(((_parse_ts(r.get("created_at"), now), r) for r in rows), key=lambda x: x[0])
    loss_times: list[datetime] = []
    count = 0
    for ts, r in dated:
        if str(r.get("trade_result") or "").strip().lower() == "loss":
            loss_times.append(ts)
        if str(r.get("action") or "").lower() == "buy":
            if any(timedelta(0) < ts - lt <= timedelta(hours=24) for lt in loss_times):
                count += 1
    return count


def qualify_all(rows: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    recent = _lookback(rows, now)
    buys = [r for r in recent if str(r.get("action") or "").lower() == "buy"]
    out: list[dict] = []

    fomo_buys = [r for r in buys if _is_fomo(r)]
    if fomo_buys:
        missing_rows = [r for r in fomo_buys if not _has_invalidation(r)]
        missing = len(missing_rows)
        rate = missing / len(fomo_buys)
        if missing >= HABIT_MIN_MISSING and rate >= HABIT_MIN_RATE:
            out.append(
                {
                    "pattern_id": "fomo_no_invalidation",
                    "habit_evidence": _outcome_evidence(missing_rows, "FOMO Buys that had no invalidation")
                    or (
                        f"Of {len(fomo_buys)} Buy logs tagged FOMO (last {HABIT_LOOKBACK_DAYS} days), "
                        f"{missing} had no invalidation ({round(rate * 100)}%)."
                    ),
                    "suggested_rule": RULE_TEXT["fomo_no_invalidation"],
                }
            )

    rushed_buys = [r for r in buys if _is_rushed(r)]
    if rushed_buys:
        missing_rows = [r for r in rushed_buys if not _has_invalidation(r)]
        missing = len(missing_rows)
        rate = missing / len(rushed_buys)
        if missing >= HABIT_MIN_MISSING and rate >= HABIT_MIN_RATE:
            out.append(
                {
                    "pattern_id": "rushed_buy_no_invalidation",
                    "habit_evidence": _outcome_evidence(missing_rows, "Rushed Buys that had no invalidation")
                    or (
                        f"Of {len(rushed_buys)} Buy logs tagged Rushed (last {HABIT_LOOKBACK_DAYS} days), "
                        f"{missing} had no invalidation ({round(rate * 100)}%)."
                    ),
                    "suggested_rule": RULE_TEXT["rushed_buy_no_invalidation"],
                }
            )

    ignore_loss = [
        r
        for r in recent
        if str(r.get("invalidation_result") or "").strip().lower() == "ignored"
        and str(r.get("trade_result") or "").strip().lower() == "loss"
    ]
    if len(ignore_loss) >= HABIT_MIN_MISSING:
        out.append(
            {
                "pattern_id": "ignore_invalidation_then_loss",
                "habit_evidence": (
                    f"You marked invalidation as ignored and result as Loss on {len(ignore_loss)} logs "
                    f"(last {HABIT_LOOKBACK_DAYS} days)."
                ),
                "suggested_rule": RULE_TEXT["ignore_invalidation_then_loss"],
            }
        )

    revenge_n = _count_revenge_buys(recent, now)
    if revenge_n >= HABIT_MIN_MISSING:
        out.append(
            {
                "pattern_id": "revenge_after_loss",
                "habit_evidence": (
                    f"You logged a Buy within 24 hours after a Loss on {revenge_n} occasions "
                    f"(last {HABIT_LOOKBACK_DAYS} days)."
                ),
                "suggested_rule": RULE_TEXT["revenge_after_loss"],
            }
        )

    if buys:
        missing_rows = [r for r in buys if not _has_invalidation(r)]
        missing = len(missing_rows)
        rate = missing / len(buys)
        if missing >= HABIT_MIN_MISSING and rate >= HABIT_MIN_RATE:
            out.append(
                {
                    "pattern_id": "buy_missing_invalidation",
                    "habit_evidence": _outcome_evidence(missing_rows, "Buys that had no invalidation")
                    or (
                        f"Of {len(buys)} Buy logs (last {HABIT_LOOKBACK_DAYS} days), "
                        f"{missing} had no invalidation ({round(rate * 100)}%)."
                    ),
                    "suggested_rule": RULE_TEXT["buy_missing_invalidation"],
                }
            )

    return out


def detect_habit_rule(rows: list[dict], now: datetime | None = None, theme: str = "auto") -> dict:
    """Return the single suggested pattern. Theme prefers its family only if it qualifies."""
    now = now or datetime.now(timezone.utc)
    candidates = qualify_all(rows, now)
    if not candidates:
        return dict(EMPTY)
    if theme and theme != "auto":
        family = THEME_FAMILY.get(theme) or []
        preferred = next((c for c in candidates if c["pattern_id"] in family), None)
        if preferred:
            return preferred
    return candidates[0]


def count_pattern_matches(
    rows: list[dict],
    pattern_id: str,
    now: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Count matches in [start, end). Revenge uses all rows for loss context."""
    now = now or datetime.now(timezone.utc)
    start = start or (now - timedelta(days=HABIT_LOOKBACK_DAYS))
    end = end or now

    def in_window(row: dict) -> bool:
        ts = _parse_ts(row.get("created_at"), now)
        return start <= ts < end

    window_rows = [r for r in rows if in_window(r)]
    if pattern_id == "fomo_no_invalidation":
        return sum(
            1
            for r in window_rows
            if str(r.get("action") or "").lower() == "buy" and _is_fomo(r) and not _has_invalidation(r)
        )
    if pattern_id == "rushed_buy_no_invalidation":
        return sum(
            1
            for r in window_rows
            if str(r.get("action") or "").lower() == "buy" and _is_rushed(r) and not _has_invalidation(r)
        )
    if pattern_id == "ignore_invalidation_then_loss":
        return sum(
            1
            for r in window_rows
            if str(r.get("invalidation_result") or "").strip().lower() == "ignored"
            and str(r.get("trade_result") or "").strip().lower() == "loss"
        )
    if pattern_id == "buy_missing_invalidation":
        return sum(
            1
            for r in window_rows
            if str(r.get("action") or "").lower() == "buy" and not _has_invalidation(r)
        )
    if pattern_id == "revenge_after_loss":
        dated = sorted(((_parse_ts(r.get("created_at"), now), r) for r in rows), key=lambda x: x[0])
        loss_times: list[datetime] = []
        count = 0
        for ts, r in dated:
            if str(r.get("trade_result") or "").strip().lower() == "loss":
                loss_times.append(ts)
            if str(r.get("action") or "").lower() == "buy" and start <= ts < end:
                if any(timedelta(0) < ts - lt <= timedelta(hours=24) for lt in loss_times):
                    count += 1
        return count
    return 0


def had_loss_in_last_24h(rows: list[dict], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    window = timedelta(hours=24)
    for r in rows:
        if str(r.get("trade_result") or "").lower() != "loss":
            continue
        ts = _parse_ts(r.get("created_at"), now)
        delta = now - ts
        if timedelta(0) <= delta <= window:
            return True
    return False
