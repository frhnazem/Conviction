"""Conviction — decision log for crypto traders. Flask / Railway."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
from extract import extract_with_rules, sanitize_pair
from habits import (
    FOCUS_THEMES,
    KNOWN_PATTERN_IDS,
    detect_habit_rule,
    had_loss_in_last_24h,
    is_focus_theme,
)
from review import NO_HABIT_MESSAGE, build_progress, build_weekly_review, outcome_prompt, pending_outcomes

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-change-me"

ALLOWED_ACTIONS = ("buy", "hold", "skip", "reduce")
ALLOWED_INV_RESULT = ("respected", "hit", "ignored")
ALLOWED_TRADE_RESULT = ("win", "loss", "breakeven", "skipped")
CHOICE_1235 = ("yes", "maybe", "no", "skip")
CHOICE_WARN = ("help", "annoy", "neither", "skip")

_db_ready = False


@app.before_request
def _boot():
    global _db_ready
    if request.endpoint == "static":
        return
    if not _db_ready:
        db.init_db()
        _db_ready = True


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    rows = db.query("SELECT id, email, focus_theme FROM users WHERE id = ?", (uid,))
    return rows[0] if rows else None


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapped


def _clean_choice(raw, allowed):
    v = (raw or "").strip().lower()
    if not v or v == "skip":
        return "skip"
    return v if v in allowed else None


def _form_state(src) -> dict:
    src = src or {}
    return {
        "raw_text": src.get("raw_text") or "",
        "pair": src.get("pair") or "",
        "bias": src.get("bias") or "",
        "invalidation": src.get("invalidation") or "",
        "target": src.get("target") or "",
        "size_note": src.get("size_note") or "",
        "action": src.get("action") or "",
        "emotion": src.get("emotion") or "",
        "emotion_other": src.get("emotion_other") or "",
        "parse_confidence": src.get("parse_confidence") or src.get("confidence_in_parse") or "",
        "notes_for_user": src.get("notes_for_user") or "",
    }


def _warning_message(user, form) -> str | None:
    action = (form.get("action") or "").lower()
    if action != "buy":
        return None
    accepted = db.query(
        "SELECT pattern_id, rule_text FROM user_rules WHERE user_id = ? AND status = 'accepted'",
        (user["id"],),
    )
    if not accepted:
        return None
    pid = accepted[0]["pattern_id"]
    emo = (form.get("emotion") or "").strip().lower()
    inv = (form.get("invalidation") or "").strip()
    logs = db.query(
        "SELECT * FROM decisions WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    )
    if pid == "revenge_after_loss" and had_loss_in_last_24h(logs):
        return "Your rule: After a Loss, no new Buy for 24 hours. A Loss was logged in the last 24 hours."
    if not inv:
        if pid == "fomo_no_invalidation" and emo == "fomo":
            return "Your rule: No Buy without invalidation when emotion is FOMO. Invalidation is empty."
        if pid == "rushed_buy_no_invalidation" and (emo == "rushed" or "rush" in emo):
            return "Your rule: No Buy without invalidation when emotion is Rushed. Invalidation is empty."
        if pid == "buy_missing_invalidation":
            return "Your rule: Require an invalidation level before every Buy. Invalidation is empty."
    return None


def _nav(page: str):
    return {"page": page, "user": current_user()}


@app.route("/")
def landing():
    return render_template("landing.html", **_nav("home"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("capture"))
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or "@" not in email:
            error = "Enter a valid email."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif db.query("SELECT id FROM users WHERE email = ?", (email,)):
            error = "That email is already registered."
        else:
            db.execute(
                "INSERT INTO users (email, password_hash, focus_theme) VALUES (?, ?, 'auto')",
                (email, generate_password_hash(password)),
            )
            user = db.query("SELECT id FROM users WHERE email = ?", (email,))[0]
            session["user_id"] = user["id"]
            return redirect(url_for("capture"))
    return render_template("register.html", error=error, **_nav("home"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("capture"))
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        rows = db.query("SELECT id, password_hash FROM users WHERE email = ?", (email,))
        if not rows or not check_password_hash(rows[0]["password_hash"], password):
            error = "Email or password is wrong."
        else:
            session["user_id"] = rows[0]["id"]
            nxt = request.args.get("next") or url_for("capture")
            if not str(nxt).startswith("/"):
                nxt = url_for("capture")
            return redirect(nxt)
    return render_template("login.html", error=error, **_nav("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


@app.route("/app", methods=["GET", "POST"])
@login_required
def capture():
    user = current_user()
    error = ""
    warn = ""
    parsed = None
    logged_ok = False
    show_parsed = False
    count_rows = db.query("SELECT COUNT(*) AS n FROM decisions WHERE user_id = ?", (user["id"],))
    log_count = int(count_rows[0]["n"])
    at_limit = log_count >= db.FREE_TIER_LIMIT

    if request.method == "POST" and request.form.get("save_theme"):
        theme = request.form.get("focus_theme") or "auto"
        if is_focus_theme(theme):
            db.execute("UPDATE users SET focus_theme = ? WHERE id = ?", (theme, user["id"]))
            user["focus_theme"] = theme
        return redirect(url_for("capture"))

    if request.method == "POST" and request.form.get("extract"):
        raw = (request.form.get("raw_text") or "").strip()
        if not raw:
            error = "Paste or type a note first."
        else:
            parsed = extract_with_rules(raw)
            parsed["raw_text"] = raw
            show_parsed = True

    logging = request.method == "POST" and (request.form.get("log") or request.form.get("log_anyway"))
    if logging:
        raw = (request.form.get("raw_text") or "").strip()
        action = (request.form.get("action") or "").lower()
        show_parsed = True
        if at_limit:
            error = db.FREE_TIER_MESSAGE
        elif not raw:
            error = "Raw note is required."
        elif action not in ALLOWED_ACTIONS:
            error = "Choose an action."
        else:
            force = request.form.get("log_anyway") == "1"
            if not force:
                warn = _warning_message(user, request.form) or ""
            if not warn:
                pair = sanitize_pair(request.form.get("pair"), raw)
                bias = request.form.get("bias") or None
                if bias not in ("long", "short", "flat"):
                    bias = None
                emotion = (request.form.get("emotion") or "").strip() or None
                if emotion == "Other":
                    emotion = (request.form.get("emotion_other") or "").strip() or "Other"
                db.execute(
                    """
                    INSERT INTO decisions (
                      user_id, raw_text, pair, bias, invalidation, target, size_note,
                      action, emotion, parse_confidence, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                    """,
                    (
                        user["id"],
                        raw,
                        pair,
                        bias,
                        (request.form.get("invalidation") or "").strip() or None,
                        (request.form.get("target") or "").strip() or None,
                        (request.form.get("size_note") or "").strip() or None,
                        action,
                        emotion,
                        request.form.get("parse_confidence") or None,
                    ),
                )
                logged_ok = True
                show_parsed = False
                log_count += 1
                at_limit = log_count >= db.FREE_TIER_LIMIT

    if request.method == "POST" and request.form.get("edit"):
        show_parsed = True

    if request.method == "POST" and request.form.get("save_outcome"):
        did = request.form.get("decision_id")
        db.execute(
            """
            UPDATE decisions
            SET invalidation_result = ?, trade_result = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                _norm_inv(request.form.get("invalidation_result")),
                _norm_trade(request.form.get("trade_result")),
                did,
                user["id"],
            ),
        )
        return redirect(url_for("capture"))

    recent = db.query(
        "SELECT * FROM decisions WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 8",
        (user["id"],),
    )
    now = datetime.now(timezone.utc)
    recent = [_with_prompt(r, now) for r in recent]
    pending = pending_outcomes(
        db.query(
            "SELECT * FROM decisions WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
            (user["id"],),
        ),
        now,
        limit=8,
    )
    recent_ids = {r["id"] for r in recent}
    pending_extra = [p for p in pending if p["id"] not in recent_ids]
    if logged_ok:
        form = _form_state({})
    elif parsed:
        form = _form_state(parsed)
    else:
        form = _form_state(request.form)
        if request.method == "POST" and (request.form.get("log") or request.form.get("extract") or request.form.get("edit") or request.form.get("log_anyway")):
            show_parsed = show_parsed or bool(form["raw_text"])
    return render_template(
        "app.html",
        form=form,
        show_parsed=show_parsed,
        warn=warn,
        error=error,
        logged_ok=logged_ok,
        recent=recent,
        log_count=log_count,
        limit=db.FREE_TIER_LIMIT,
        at_limit=at_limit,
        themes=FOCUS_THEMES,
        theme=user.get("focus_theme") or "auto",
        pending_extra=pending_extra,
        **_nav("app"),
    )


@app.post("/logs/<int:decision_id>/delete")
@login_required
def delete_log(decision_id: int):
    user = current_user()
    db.execute("DELETE FROM decisions WHERE id = ? AND user_id = ?", (decision_id, user["id"]))
    flash("Log deleted. It is gone from weekly review, rules, and warnings.")
    return redirect(request.referrer or url_for("capture"))


def _safe_next(raw) -> str:
    nxt = (raw or "").strip()
    if nxt in ("/app", "/weekly"):
        return nxt
    return url_for("capture")


def _norm_inv(raw) -> str | None:
    v = (raw or "").strip().lower()
    if v == "held":
        v = "respected"
    if v in ALLOWED_INV_RESULT:
        return v
    return None


def _norm_trade(raw) -> str | None:
    v = (raw or "").strip().lower()
    if v in ALLOWED_TRADE_RESULT:
        return v
    return None


def _with_prompt(row: dict, now: datetime) -> dict:
    item = dict(row)
    item.update(outcome_prompt(item, now))
    inv = str(item.get("invalidation_result") or "").strip().lower()
    if inv == "held":
        item["invalidation_result"] = "respected"
    return item


@app.post("/logs/<int:decision_id>/outcome")
@login_required
def set_outcome(decision_id: int):
    user = current_user()
    inv = _norm_inv(request.form.get("invalidation_result"))
    trade = _norm_trade(request.form.get("trade_result"))
    if inv:
        db.execute(
            "UPDATE decisions SET invalidation_result = ? WHERE id = ? AND user_id = ?",
            (inv, decision_id, user["id"]),
        )
    if trade:
        db.execute(
            "UPDATE decisions SET trade_result = ? WHERE id = ? AND user_id = ?",
            (trade, decision_id, user["id"]),
        )
    return redirect(_safe_next(request.form.get("next")))


@app.route("/weekly", methods=["GET", "POST"])
@login_required
def weekly():
    user = current_user()
    db.ensure_demo_seed(user["id"])
    db.seed_product_memory()
    if request.method == "POST" and request.form.get("rule_action"):
        _save_rule(user, request.form.get("rule_action"), request.form.get("pattern_id"), request.form.get("rule_text"))
        return redirect(url_for("weekly"))

    rows = db.query(
        "SELECT * FROM decisions WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    )
    now = datetime.now(timezone.utc)
    review = build_weekly_review(rows, now)
    theme = user.get("focus_theme") or "auto"
    habit = detect_habit_rule(rows, now, theme)
    accepted = db.query(
        "SELECT * FROM user_rules WHERE user_id = ? AND status = 'accepted' ORDER BY created_at DESC",
        (user["id"],),
    )
    decided = db.query(
        "SELECT pattern_id FROM user_rules WHERE user_id = ?",
        (user["id"],),
    )
    decided_ids = {r["pattern_id"] for r in decided}
    show_suggest = bool(habit.get("pattern_id") and habit.get("suggested_rule") and habit["pattern_id"] not in decided_ids)
    accepted_pid = accepted[0]["pattern_id"] if accepted else None
    progress = build_progress(rows, now, habit.get("pattern_id"), accepted_pid)
    no_habit = (not habit.get("pattern_id") and bool(rows))
    pending = pending_outcomes(rows, now, limit=5)
    review.update(
        {
            "pattern_id": habit.get("pattern_id"),
            "habit_evidence": habit.get("habit_evidence"),
            "suggested_rule": habit.get("suggested_rule"),
            "focus_theme": theme,
            "accepted_rules": accepted,
            "show_suggest": show_suggest,
            "progress": progress,
            "no_habit_message": NO_HABIT_MESSAGE if no_habit else None,
            "pending_outcomes": pending,
        }
    )
    return render_template("weekly.html", review=review, **_nav("weekly"))


def _save_rule(user, action, pattern_id, rule_text):
    pattern_id = (pattern_id or "").strip()
    rule_text = (rule_text or "").strip()
    if pattern_id not in KNOWN_PATTERN_IDS or not rule_text:
        return
    if action not in ("accept", "dismiss"):
        return
    status = "accepted" if action == "accept" else "dismissed"
    if status == "accepted":
        db.execute(
            "UPDATE user_rules SET status = 'dismissed' WHERE user_id = ? AND status = 'accepted' AND pattern_id != ?",
            (user["id"], pattern_id),
        )
    existing = db.query(
        "SELECT id FROM user_rules WHERE user_id = ? AND pattern_id = ?",
        (user["id"], pattern_id),
    )
    if existing:
        db.execute(
            "UPDATE user_rules SET status = ?, rule_text = ?, created_at = ? WHERE user_id = ? AND pattern_id = ?",
            (status, rule_text, datetime.now(timezone.utc).isoformat(), user["id"], pattern_id),
        )
    else:
        db.execute(
            "INSERT INTO user_rules (user_id, pattern_id, rule_text, status) VALUES (?, ?, ?, ?)",
            (user["id"], pattern_id, rule_text, status),
        )


@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    user = current_user()
    db.seed_product_memory()
    thanks = False
    error = ""
    if request.method == "POST" and request.form.get("save_theme") and user:
        theme = request.form.get("focus_theme") or "auto"
        if is_focus_theme(theme):
            db.execute("UPDATE users SET focus_theme = ? WHERE id = ?", (theme, user["id"]))
            user["focus_theme"] = theme
        return redirect(url_for("feedback"))

    if request.method == "POST" and request.form.get("send"):
        logged = _clean_choice(request.form.get("logged_more_than_once"), CHOICE_1235)
        rule = _clean_choice(request.form.get("rule_made_sense"), CHOICE_1235)
        warn = _clean_choice(request.form.get("warning_help_or_annoy"), CHOICE_WARN)
        nxt = _clean_choice(request.form.get("use_next_week"), CHOICE_1235)
        confusing = (request.form.get("confusing") or "")[:2000] or None
        ideas = (request.form.get("ideas") or "")[:4000] or None
        uid = user["id"] if user else None
        db.execute(
            """
            INSERT INTO demo_feedback (
              user_id, logged_more_than_once, rule_made_sense, warning_help_or_annoy,
              confusing, use_next_week, ideas
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, logged, rule, warn, confusing, nxt, ideas),
        )
        thanks = True
    count = int(db.query("SELECT COUNT(*) AS n FROM demo_feedback")[0]["n"])
    theme = (user or {}).get("focus_theme") or "auto"
    return render_template(
        "feedback.html",
        thanks=thanks,
        error=error,
        count=count,
        themes=FOCUS_THEMES,
        theme=theme,
        **_nav("feedback"),
    )


@app.route("/help")
def help_page():
    return render_template("help.html", **_nav("help"))


@app.get("/api/weekly-review")
@login_required
def api_weekly():
    user = current_user()
    db.ensure_demo_seed(user["id"])
    db.seed_product_memory()
    rows = db.query("SELECT * FROM decisions WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
    now = datetime.now(timezone.utc)
    review = build_weekly_review(rows, now)
    habit = detect_habit_rule(rows, now, user.get("focus_theme") or "auto")
    accepted = db.query(
        "SELECT * FROM user_rules WHERE user_id = ? AND status = 'accepted'",
        (user["id"],),
    )
    accepted_pid = accepted[0]["pattern_id"] if accepted else None
    progress = build_progress(rows, now, habit.get("pattern_id"), accepted_pid)
    review.update(
        {
            "pattern_id": habit.get("pattern_id"),
            "habit_evidence": habit.get("habit_evidence"),
            "suggested_rule": habit.get("suggested_rule"),
            "focus_theme": user.get("focus_theme") or "auto",
            "accepted_rules": accepted,
            "progress": progress,
            "no_habit_message": NO_HABIT_MESSAGE if (not habit.get("pattern_id") and rows) else None,
        }
    )
    return jsonify(review)


@app.route("/api/feedback", methods=["GET", "POST"])
def api_feedback():
    db.seed_product_memory()
    user = current_user()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if user and is_focus_theme(str(data.get("theme") or "")):
            db.execute("UPDATE users SET focus_theme = ? WHERE id = ?", (data.get("theme"), user["id"]))
        db.execute(
            """
            INSERT INTO demo_feedback (
              user_id, logged_more_than_once, rule_made_sense, warning_help_or_annoy,
              confusing, use_next_week, ideas
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"] if user else None,
                _clean_choice(data.get("logged_more_than_once"), CHOICE_1235),
                _clean_choice(data.get("rule_made_sense"), CHOICE_1235),
                _clean_choice(data.get("warning_help_or_annoy"), CHOICE_WARN),
                (str(data.get("confusing") or "")[:2000] or None),
                _clean_choice(data.get("use_next_week"), CHOICE_1235),
                (str(data.get("ideas") or "")[:4000] or None),
            ),
        )
        count = int(db.query("SELECT COUNT(*) AS n FROM demo_feedback")[0]["n"])
        return jsonify({"ok": True, "count": count}), 201
    count = int(db.query("SELECT COUNT(*) AS n FROM demo_feedback")[0]["n"])
    catalog = db.query("SELECT record_type, title FROM product_memory ORDER BY id")
    theme = (user or {}).get("focus_theme") or "auto"
    return jsonify({"count": count, "theme": theme, "catalog": catalog})


@app.route("/api/rules", methods=["GET", "POST"])
@login_required
def api_rules():
    user = current_user()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        _save_rule(user, data.get("action"), data.get("pattern_id"), data.get("rule_text"))
        rows = db.query(
            "SELECT * FROM user_rules WHERE user_id = ? AND pattern_id = ?",
            (user["id"], data.get("pattern_id")),
        )
        accepted = db.query(
            "SELECT * FROM user_rules WHERE user_id = ? AND status = 'accepted'",
            (user["id"],),
        )
        return jsonify({"rule": rows[0] if rows else None, "accepted": accepted}), 201
    return jsonify(db.query("SELECT * FROM user_rules WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)))


@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
