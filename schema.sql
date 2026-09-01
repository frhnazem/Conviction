-- Reference schema. Runtime uses db.init_db() (SQLite or Postgres).

-- users
-- id, email UNIQUE, password_hash, focus_theme DEFAULT 'auto', created_at

-- decisions
-- id, user_id, created_at, raw_text, pair, bias, invalidation, target, size_note,
-- action, emotion, parse_confidence, status, invalidation_result, trade_result

-- user_rules
-- id, user_id, pattern_id, rule_text, status, created_at
-- UNIQUE (user_id, pattern_id)

-- demo_feedback
-- id, user_id, logged_more_than_once, rule_made_sense, warning_help_or_annoy,
-- confusing, use_next_week, ideas, created_at

-- product_memory
-- id, record_type, title, body, source, created_at
