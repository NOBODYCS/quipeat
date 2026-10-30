from datetime import date

from . import db
from .fsrs import level_for

BADGES = {
    "first_steps": "Review your first card",
    "getting_warm": "10 cards reviewed",
    "century": "100 cards reviewed",
    "streak_3": "3-day streak",
    "streak_7": "7-day streak",
    "perfect_session": "A session with 100% correct",
}


def award_xp(user_id: int, xp: int) -> dict:
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    new_xp = user["xp_total"] + xp
    new_level = level_for(new_xp)
    db.execute("UPDATE users SET xp_total = ?, level = ? WHERE id = ?", (new_xp, new_level, user_id))
    return {"xp": new_xp, "level": new_level, "leveled_up": new_level > user["level"]}


def touch_activity(user_id: int) -> dict:
    today = date.today().isoformat()
    user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    last = user["last_active_day"]
    streak = user["streak_days"]
    freeze_used = False
    if last != today:
        gap = (date.today() - date.fromisoformat(last)).days if last else 99
        if gap == 1:
            streak += 1
        elif gap == 2 and user["freeze_available"]:
            freeze_used = True  # freeze protects the streak across one missed day
            db.execute("UPDATE users SET freeze_available = 0 WHERE id = ?", (user_id,))
        else:
            streak = 1
        db.execute("UPDATE users SET streak_days = ?, last_active_day = ? WHERE id = ?",
                   (streak, today, user_id))
        if streak > 0 and streak % 7 == 0:
            db.execute("UPDATE users SET freeze_available = 1 WHERE id = ?", (user_id,))
    else:
        streak = user["streak_days"]
    new_badges = []
    for badge in ("streak_3", "streak_7"):
        threshold = int(badge.split("_")[1])
        if streak >= threshold and not db.query_one(
                "SELECT 1 FROM user_badges WHERE user_id = ? AND badge = ?", (user_id, badge)):
            db.execute("INSERT INTO user_badges(user_id, badge) VALUES (?, ?)", (user_id, badge))
            new_badges.append(badge)
    return {"streak": streak, "new_badges": new_badges, "freeze_used": freeze_used}


def check_badges(user_id: int, total_reviews: int, session_perfect: bool = False) -> list[str]:
    new = []
    checks = [
        ("first_steps", total_reviews >= 1),
        ("getting_warm", total_reviews >= 10),
        ("century", total_reviews >= 100),
        ("perfect_session", session_perfect),
    ]
    for badge, cond in checks:
        if cond and not db.query_one("SELECT 1 FROM user_badges WHERE user_id = ? AND badge = ?",
                                     (user_id, badge)):
            db.execute("INSERT INTO user_badges(user_id, badge) VALUES (?, ?)", (user_id, badge))
            new.append(badge)
    return new
