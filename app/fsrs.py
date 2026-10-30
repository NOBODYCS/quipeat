"""FSRS-inspired scheduling. Simplified SM-2/FSRS hybrid: each question keeps
interval, ease factor, reps, lapses. Ratings: again/hard/good/easy."""
from datetime import datetime, timedelta, timezone

W = (1.0, 2.5, 5.0, 10.0)  # intervals in days for again/hard/good/easy on first pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def grade(card: dict, rating: str) -> dict:
    """Return updated scheduling fields for a question row."""
    ef = float(card.get("ef") or 2.5)
    interval = float(card.get("interval_days") or 0)
    reps = int(card.get("reps") or 0)
    lapses = int(card.get("lapses") or 0)

    if rating == "again":
        lapses += 1
        reps = 0
        ef = max(1.3, ef - 0.2)
        interval = 1 / 1440  # 1 minute
    else:
        reps += 1
        if rating == "hard":
            ef = max(1.3, ef - 0.15)
            factor = 1.2
        elif rating == "good":
            factor = ef
        else:  # easy
            ef = ef + 0.15
            factor = ef * 1.3
        if reps == 1:
            interval = W[{"hard": 0, "good": 1, "easy": 2}[rating]] if rating != "hard" else 1
        elif reps == 2:
            interval = 2 if rating != "hard" else 1
        else:
            interval = max(interval * factor, 1) if rating != "hard" else max(interval * 1.2, 1)
        if rating == "easy":
            interval = max(interval, W[3])

    due = _now() + timedelta(days=interval)
    return {
        "due_at": due.isoformat(),
        "interval_days": round(interval, 6),
        "ef": round(ef, 4),
        "reps": reps,
        "lapses": lapses,
    }


def xp_for(rating: str, correct: bool, difficulty: str) -> int:
    base = {"again": 1, "hard": 5, "good": 10, "easy": 15}[rating] if correct else 1
    mult = {"easy": 1.0, "medium": 1.5, "hard": 2.0}.get(difficulty, 1.0)
    return int(round(base * mult))


def level_for(xp: int) -> int:
    # level n requires ~ n^1.6 * 100 xp
    level = 1
    while xp >= int((level + 1) ** 1.6 * 100):
        level += 1
    return level


def level_progress(xp: int) -> tuple[int, int, int]:
    """(level, xp_into_level, xp_needed_for_next_level)"""
    level = level_for(xp)
    cur = int(level ** 1.6 * 100)
    nxt = int((level + 1) ** 1.6 * 100)
    return level, xp - cur, nxt - cur
