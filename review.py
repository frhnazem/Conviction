"""Weekly counts from the user's own logs. No signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from habits import PATTERN_LABELS, count_pattern_matches

WEEKLY_MIN_LOGS = 3
ACTIONS = ("buy", "hold", "skip", "reduce")
TRADE_ACTIONS = ("buy", "hold", "reduce")
NO_HABIT_MESSAGE = "No habit rule this period — that can mean a steadier stretch."


def _parse_ts(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, datetime):
        ts = raw
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    try:
        ts = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return fallback


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


def _has_invalidation(row: dict) -> bool:
    return bool(str(row.get("invalidation") or "").strip())


def select_period_rows(all_rows: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    if not all_rows:
        return []
    cutoff = now - timedelta(days=7)
    oldest = min(_parse_ts(r.get("created_at"), now) for r in all_rows)
    if oldest >= cutoff:
        return list(all_rows)
    return [r for r in all_rows if _parse_ts(r.get("created_at"), now) >= cutoff]


def build_bullets(total: int, by_action: dict, invalidation_missing_count: int, emotions: dict) -> list[str]:
    if total < WEEKLY_MIN_LOGS:
        return []
    bullets: list[str] = []
    ranked = sorted(
        [(k, n) for k, n in by_action.items() if n > 0],
        key=lambda x: (-x[1], x[0]),
    )
    if ranked:
        name, n = ranked[0]
        bullets.append(f"Most common action: {name} ({n} of {total}).")
    missing_pct = round((invalidation_missing_count / total) * 100)
    bullets.append(
        f"Invalidation was missing on {invalidation_missing_count} of {total} logs ({missing_pct}%)."
    )
    fomo = 0
    for k, n in emotions.items():
        if k.lower() == "fomo":
            fomo = n
            break
    if fomo > 0:
        bullets.append(f"FOMO was tagged on {fomo} of {total} logs.")
    elif by_action.get("skip") != by_action.get("buy"):
        bullets.append(
            f"Skip vs buy this period: skip {by_action.get('skip', 0)}, buy {by_action.get('buy', 0)}."
        )
    else:
        emo = sorted(emotions.items(), key=lambda x: -x[1])
        if emo:
            bullets.append(f"Most common emotion tag: {emo[0][0]} ({emo[0][1]}).")
        elif len(ranked) >= 2:
            bullets.append(f"Next most common action: {ranked[1][0]} ({ranked[1][1]} of {total}).")
    unique: list[str] = []
    for line in bullets:
        if line not in unique:
            unique.append(line)
        if len(unique) == 3:
            break
    return unique[:3]


def build_share_text(review: dict) -> str:
    if review["total_decisions"] < WEEKLY_MIN_LOGS:
        return "Conviction weekly review — not enough logs for a useful review yet."
    emo = ", ".join(
        f"{tag} {n}"
        for tag, n in sorted(review["emotions"].items(), key=lambda x: (-x[1], x[0]))
    )
    ba = review["by_action"]
    lines = [
        "Conviction weekly review",
        f"Period: {review['period_start'][:10]} → {review['period_end'][:10]}",
        f"Logs: {review['total_decisions']}",
        f"Actions: buy {ba['buy']} · hold {ba['hold']} · skip {ba['skip']} · reduce {ba['reduce']}",
        f"Invalidation noted: {review['invalidation_present_count']} / {review['total_decisions']}",
        f"Emotions: {emo or 'none'}",
        *[f"• {b}" for b in review["bullets"]],
    ]
    if review.get("demo_logs_included"):
        lines.append("(Includes demo logs.)")
    return "\n".join(lines)


def build_weekly_review(all_rows: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = select_period_rows(all_rows, now)
    cutoff = now - timedelta(days=7)
    if rows:
        oldest = min(_parse_ts(r.get("created_at"), now) for r in rows)
        period_start = max(oldest, cutoff)
    else:
        period_start = cutoff
    by_action = {a: 0 for a in ACTIONS}
    emotions: dict[str, int] = {}
    present = missing = 0
    for row in rows:
        action = str(row.get("action") or "")
        if action in by_action:
            by_action[action] += 1
        if _has_invalidation(row):
            present += 1
        else:
            missing += 1
        tag = str(row.get("emotion") or "").strip()
        if tag:
            emotions[tag] = emotions.get(tag, 0) + 1
    total = len(rows)
    enough = total >= WEEKLY_MIN_LOGS
    partial = {
        "period_start": _iso(period_start),
        "period_end": _iso(now),
        "total_decisions": total,
        "by_action": by_action,
        "invalidation_present_count": present,
        "invalidation_missing_count": missing,
        "emotions": emotions,
        "bullets": build_bullets(total, by_action, missing, emotions) if enough else [],
        "message": None if enough else "Not enough logs for a useful review yet.",
        "demo_logs_included": any(str(r.get("status") or "") == "demo" for r in rows),
        "pattern_id": None,
        "habit_evidence": "Not enough matching logs to suggest a habit rule yet.",
        "suggested_rule": None,
        "focus_theme": "auto",
        "accepted_rules": [],
        "show_suggest": False,
    }
    partial["share_text"] = build_share_text(partial)
    return partial


def _cmp(label: str, current: int, previous: int, has_previous: bool, *, times: bool = False) -> str:
    if times:
        if has_previous:
            return f"{label}: {current}× this week (was {previous}×)"
        return f"{label}: {current}× this week"
    if has_previous:
        return f"{label}: {current} (was {previous})"
    return f"{label}: {current}"


def _period_stats(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "invalidation_missing": sum(1 for r in rows if not _has_invalidation(r)),
        "fomo": sum(1 for r in rows if str(r.get("emotion") or "").strip().lower() == "fomo"),
        "buys": sum(1 for r in rows if str(r.get("action") or "").lower() == "buy"),
    }


def build_progress(
    all_rows: list[dict],
    now: datetime | None = None,
    pattern_id: str | None = None,
    accepted_pattern_id: str | None = None,
) -> dict:
    """This 7 days vs prior 7. Hide '(was N)' when the prior window has no logs."""
    now = now or datetime.now(timezone.utc)
    cur_start = now - timedelta(days=7)
    prev_start = now - timedelta(days=14)
    current_rows = [r for r in all_rows if _parse_ts(r.get("created_at"), now) >= cur_start]
    previous_rows = [
        r for r in all_rows if prev_start <= _parse_ts(r.get("created_at"), now) < cur_start
    ]
    has_previous = len(previous_rows) > 0

    lines: list[str] = []
    shown: set[str] = set()
    for pid in (pattern_id, accepted_pattern_id):
        if not pid or pid in shown:
            continue
        label = PATTERN_LABELS.get(pid, pid)
        cur = count_pattern_matches(all_rows, pid, now, cur_start, now + timedelta(seconds=1))
        prev = count_pattern_matches(all_rows, pid, now, prev_start, cur_start)
        lines.append(_cmp(label, cur, prev, has_previous))
        shown.add(pid)

    rule_line = None
    if accepted_pattern_id:
        cur = count_pattern_matches(
            all_rows, accepted_pattern_id, now, cur_start, now + timedelta(seconds=1)
        )
        prev = count_pattern_matches(
            all_rows, accepted_pattern_id, now, prev_start, cur_start
        )
        if has_previous or cur > 0:
            rule_line = _cmp("This rule broken", cur, prev, has_previous, times=True)

    cur_s = _period_stats(current_rows)
    prev_s = _period_stats(previous_rows)
    generic = [
        _cmp("Logs", cur_s["total"], prev_s["total"], has_previous),
        _cmp("Invalidation missing", cur_s["invalidation_missing"], prev_s["invalidation_missing"], has_previous),
    ]
    if cur_s["fomo"] or (has_previous and prev_s["fomo"]):
        generic.append(_cmp("FOMO tags", cur_s["fomo"], prev_s["fomo"], has_previous))

    return {
        "has_previous": has_previous,
        "lines": lines,
        "rule_line": rule_line,
        "generic": generic,
    }


def outcome_prompt(row: dict, now: datetime | None = None) -> dict:
    """Flag logs older than 24h that still need a result."""
    now = now or datetime.now(timezone.utc)
    ts = _parse_ts(row.get("created_at"), now)
    old_enough = (now - ts) >= timedelta(hours=24)
    action = str(row.get("action") or "").strip().lower()
    trade_empty = not str(row.get("trade_result") or "").strip()
    inv_filled = bool(str(row.get("invalidation") or "").strip())
    inv_empty = not str(row.get("invalidation_result") or "").strip()
    need_trade = old_enough and action in TRADE_ACTIONS and trade_empty
    need_inv = old_enough and inv_filled and inv_empty
    return {
        "need_trade": need_trade,
        "need_inv": need_inv,
        "prompt": need_trade or need_inv,
    }


def pending_outcomes(rows: list[dict], now: datetime | None = None, limit: int = 8) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    for r in rows:
        flags = outcome_prompt(r, now)
        if flags["prompt"]:
            item = dict(r)
            item.update(flags)
            out.append(item)
        if len(out) >= limit:
            break
    return out
