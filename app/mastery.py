"""Mastery tracking + gamification layer.

- Box levels 0-5 per (user, question): correct -> +1 (cap 5), wrong -> 0.
  Mastered = box >= 4.
- Deck mastery % = mastered / total questions in the deck.
- Deck level from mastery %: 0% New, 1-25 Silver, 26-50 So-so, 51-75 Adept,
  76-99 Expert, 100% Elite.
- Daily streak: counts consecutive calendar days (server timezone) with >= 5
  answered questions. One missed day per rolling 7-day window is forgiven
  silently (repair, at most once per 7 days, does not accumulate).
- Badges: awarded once per user when the condition is first met.
"""
from datetime import datetime, timedelta, timezone

from . import db
from .db import now_utc

STREAK_MIN_QUESTIONS_PER_DAY = 5
REPAIR_WINDOW_DAYS = 7
MASTERED_BOX = 4

DECK_LEVELS = [
    (100, "elite"),
    (76, "expert"),
    (51, "adept"),
    (26, "so_so"),
    (1, "silver"),
    (0, "new"),
]

BADGES = {
    "first_deck_mastered": "Master a deck completely (100% mastery)",
    "perfect_session": "Answer 10+ questions in one session, all correct",
    "century_mastered": "Master 100 questions in total",
}


def record_attempt(user_id: int, question_id: int, deck_id: int, correct: bool,
                   time_ms: int = 0, session_id: int | None = None) -> dict:
    """Log exactly one attempt and move the box level. Returns the new box."""
    db.execute(
        "INSERT INTO attempts(user_id, question_id, deck_id, correct, time_ms, session_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, question_id, deck_id, 1 if correct else 0, int(time_ms or 0), session_id))
    row = db.query_one("SELECT box_level FROM mastery WHERE user_id = ? AND question_id = ?",
                       (user_id, question_id))
    box = row["box_level"] if row else 0
    if correct:
        box = min(5, box + 1)
    else:
        box = 0
    if row:
        db.execute("UPDATE mastery SET box_level = ?, last_answered_at = ? WHERE user_id = ? AND question_id = ?",
                   (box, now_utc(), user_id, question_id))
    else:
        db.execute("INSERT INTO mastery(user_id, question_id, box_level, last_answered_at) VALUES (?, ?, ?, ?)",
                   (user_id, question_id, box, now_utc()))
    return {"box": box}


def box_of(user_id: int, question_id: int) -> int:
    row = db.query_one("SELECT box_level FROM mastery WHERE user_id = ? AND question_id = ?",
                       (user_id, question_id))
    return row["box_level"] if row else 0


def last_answered(user_id: int, question_id: int) -> str | None:
    row = db.query_one("SELECT last_answered_at FROM mastery WHERE user_id = ? AND question_id = ?",
                       (user_id, question_id))
    return row["last_answered_at"] if row else None


def deck_mastery(user_id: int, deck_id: int) -> dict:
    """mastered count, total, percentage for a deck."""
    row = db.query_one(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN COALESCE(m.box_level, 0) >= ? THEN 1 ELSE 0 END), 0) AS mastered"
        " FROM questions q LEFT JOIN mastery m ON m.question_id = q.id AND m.user_id = ?"
        " WHERE q.deck_id = ?",
        (MASTERED_BOX, user_id, deck_id))
    total = row["total"] or 0
    mastered = row["mastered"] or 0
    pct = round(100.0 * mastered / total) if total else 0
    return {"total": total, "mastered": mastered, "pct": pct}


def deck_level(pct: int) -> str:
    for threshold, name in DECK_LEVELS:
        if pct >= threshold:
            return name
    return "new"


def total_mastered(user_id: int) -> int:
    return db.query_one(
        "SELECT COUNT(*) c FROM mastery WHERE user_id = ? AND box_level >= ?",
        (user_id, MASTERED_BOX))["c"]


def _server_today() -> str:
    """Calendar day in the server timezone (streak days are counted there)."""
    return datetime.now().astimezone().date().isoformat()


def touch_streak(user_id: int) -> dict:
    """Update the streak after an answer. A day counts only if >= 5 questions
    were answered that day. One missed day per rolling 7 days is forgiven."""
    today = _server_today()
    user = db.query_one("SELECT streak_days, last_streak_day, streak_repair_used_at FROM users WHERE id = ?",
                        (user_id,))
    streak = user["streak_days"] or 0
    last = user["last_streak_day"]
    repair_used_at = user["streak_repair_used_at"]

    today_count = db.query_one(
        "SELECT COUNT(*) c FROM attempts WHERE user_id = ? AND date(answered_at) = ?",
        (user_id, today))["c"]

    if last == today:
        # same day: streak only changes when the 5-question bar is reached
        if today_count >= STREAK_MIN_QUESTIONS_PER_DAY and streak == 0:
            streak = 1
        elif today_count >= STREAK_MIN_QUESTIONS_PER_DAY and last and streak > 0:
            pass  # already counting today
        db.execute("UPDATE users SET streak_days = ? WHERE id = ?", (streak, user_id))
        return {"streak": streak, "repaired": False, "day_count": today_count}

    # a new calendar day with activity
    if not last:
        streak = 1 if today_count >= STREAK_MIN_QUESTIONS_PER_DAY else 0
        db.execute("UPDATE users SET streak_days = ?, last_streak_day = ? WHERE id = ?",
                   (streak, today, user_id))
        return {"streak": streak, "repaired": False, "day_count": today_count}

    from datetime import date as _date
    last_d = _date.fromisoformat(last)
    today_d = _date.fromisoformat(today)
    gap = (today_d - last_d).days

    if gap == 1:
        if today_count >= STREAK_MIN_QUESTIONS_PER_DAY:
            streak += 1
        # below the bar: streak unchanged (does not increment, does not break)
        db.execute("UPDATE users SET streak_days = ?, last_streak_day = ? WHERE id = ?",
                   (streak, today, user_id))
        return {"streak": streak, "repaired": False, "day_count": today_count}

    if gap == 2 and today_count >= STREAK_MIN_QUESTIONS_PER_DAY:
        # one missed day in the window: forgive it, at most once per 7 days
        can_repair = True
        if repair_used_at:
            used_d = _date.fromisoformat(repair_used_at)
            can_repair = (today_d - used_d).days >= REPAIR_WINDOW_DAYS
        if can_repair:
            streak += 1
            db.execute(
                "UPDATE users SET streak_days = ?, last_streak_day = ?, streak_repair_used_at = ? WHERE id = ?",
                (streak, today, today, user_id))
            return {"streak": streak, "repaired": True, "day_count": today_count}
        # repair not available: streak breaks
        streak = 1
        db.execute("UPDATE users SET streak_days = ?, last_streak_day = ? WHERE id = ?",
                   (streak, today, user_id))
        return {"streak": streak, "repaired": False, "day_count": today_count}

    # gap > 2 or today below the bar with a big gap: break to 1 if bar met else 0
    streak = 1 if today_count >= STREAK_MIN_QUESTIONS_PER_DAY else 0
    db.execute("UPDATE users SET streak_days = ?, last_streak_day = ? WHERE id = ?",
               (streak, today, user_id))
    return {"streak": streak, "repaired": False, "day_count": today_count}


def award_badges(user_id: int, deck_id: int | None = None, session: dict | None = None) -> list[str]:
    """Award each badge once, at the moment the condition is first met.
    Returns the list of newly awarded badge names."""
    new = []
    conds = {
        "first_deck_mastered": deck_id is not None and deck_mastery(user_id, deck_id)["pct"] >= 100,
        "century_mastered": total_mastered(user_id) >= 100,
        "perfect_session": bool(
            session and session.get("cards_reviewed", 0) >= 10
            and session.get("combo", 0) == session.get("cards_reviewed", 0)
            and session.get("cards_reviewed", 0) > 0),
    }
    for name, cond in conds.items():
        if not cond:
            continue
        if db.query_one("SELECT 1 FROM user_badges WHERE user_id = ? AND badge = ?", (user_id, name)):
            continue
        db.execute("INSERT INTO user_badges(user_id, badge) VALUES (?, ?)", (user_id, name))
        new.append(name)
    return new
