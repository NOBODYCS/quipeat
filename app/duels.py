"""1v1 VS duels: synchronous race on the challenger's deck, same questions in
the same seed-shuffled order for both players, 10 questions per duel.

State lives in the duels/duel_answers tables (SQLite WAL, single process).
Duel answers never touch questions.due_at or the reviews table — duels are
practice only. Winner gets a flat XP bonus via gamify.award_xp.
"""
import json
import random
import secrets
from datetime import datetime, timedelta

from . import db
from .db import now_utc

DUEL_QUESTIONS = 10
DUEL_XP_BONUS = 50
FORFEIT_AFTER_START_S = 60      # no activity for 60s after start -> forfeit
FORFEIT_AFTER_OPPONENT_DONE_S = 60  # opponent finished, you idle 60s -> forfeit


def new_duel(challenger_id: int, opponent_id: int, deck_id: int) -> str:
    duel_id = secrets.token_hex(12)
    seed = secrets.token_hex(8)
    db.execute(
        "INSERT INTO duels(id, challenger_id, opponent_id, deck_id, seed) VALUES (?, ?, ?, ?, ?)",
        (duel_id, challenger_id, opponent_id, deck_id, seed))
    return duel_id


def duel_questions(deck_id: int, seed: str, n: int = DUEL_QUESTIONS) -> list[int]:
    """Deterministic question id sequence for a duel: shuffle the deck's
    question ids with the duel seed, take n."""
    ids = [r["id"] for r in db.query(
        "SELECT id FROM questions WHERE deck_id = ? AND flagged = 0 ORDER BY id", (deck_id,))]
    rng = random.Random(seed)
    rng.shuffle(ids)
    return ids[:n]


def get_duel(duel_id: str):
    return db.query_one("SELECT * FROM duels WHERE id = ?", (duel_id,))


def is_participant(duel, user_id: int) -> bool:
    return duel and user_id in (duel["challenger_id"], duel["opponent_id"])


def _side(duel, user_id: int) -> str:
    return "challenger" if user_id == duel["challenger_id"] else "opponent"


def record_answer(duel_id: str, user_id: int, question_id: int, choice: int) -> dict:
    """Record one duel answer; update score + index + last-active. Returns the
    answer correctness and whether this finished the player's run."""
    duel = get_duel(duel_id)
    side = _side(duel, user_id)
    q = db.query_one("SELECT id, correct_index FROM questions WHERE id = ? AND deck_id = ?",
                     (question_id, duel["deck_id"]))
    correct = 1 if (q and choice == q["correct_index"]) else 0
    idx_col = f"{side}_idx"
    score_col = f"{side}_score"
    idx = duel[idx_col]
    db.execute(
        "INSERT INTO duel_answers(duel_id, user_id, question_id, q_index, choice, correct)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (duel_id, user_id, question_id, idx, choice, correct))
    db.execute(
        f"UPDATE duels SET {score_col} = {score_col} + ?, {idx_col} = {idx_col} + 1,"
        f" {side}_last_active = ? WHERE id = ?",
        (correct, now_utc(), duel_id))
    finished = (idx + 1) >= DUEL_QUESTIONS
    return {"correct": bool(correct), "finished": finished, "index": idx + 1}


def check_forfeit(duel) -> int | None:
    """Return the winner's user_id if the duel should be forfeited now, else None."""
    if duel["status"] != "active":
        return None
    now = datetime.strptime(now_utc(), "%Y-%m-%d %H:%M:%S")

    def _parse(ts):
        if not ts:
            return None
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")

    started = _parse(duel["started_at"])
    c_act, o_act = _parse(duel["challenger_last_active"]), _parse(duel["opponent_last_active"])
    c_idx, o_idx = duel["challenger_idx"], duel["opponent_idx"]
    c_done, o_done = c_idx >= DUEL_QUESTIONS, o_idx >= DUEL_QUESTIONS

    # rule 1: no activity for 60s after the duel started (both players idle since start)
    if started and not c_act and not o_act and (now - started).total_seconds() > FORFEIT_AFTER_START_S:
        # nobody played at all: draw -> challenger wins by default? No: cancel as draw, no winner.
        return None  # handled by 'abandoned' below
    # rule 2: opponent finished and the other stays inactive 60s
    if c_done and o_act is None and started and (now - started).total_seconds() > FORFEIT_AFTER_START_S:
        return duel["challenger_id"]
    if c_done and o_act and (now - o_act).total_seconds() > FORFEIT_AFTER_OPPONENT_DONE_S:
        return duel["challenger_id"]
    if o_done and c_act is None and started and (now - started).total_seconds() > FORFEIT_AFTER_START_S:
        return duel["opponent_id"]
    if o_done and c_act and (now - c_act).total_seconds() > FORFEIT_AFTER_OPPONENT_DONE_S:
        return duel["opponent_id"]
    # rule 1b: duel started, one player never showed up for 60s while the other played
    if c_act and not o_act and (now - c_act).total_seconds() > FORFEIT_AFTER_START_S and not o_done:
        return duel["challenger_id"]
    if o_act and not c_act and (now - o_act).total_seconds() > FORFEIT_AFTER_START_S and not c_done:
        return duel["opponent_id"]
    return None


def finish_duel(duel_id: str, winner_id: int | None) -> None:
    """Mark finished, award XP to the winner."""
    db.execute("UPDATE duels SET status='finished', winner_id=?, finished_at=? WHERE id=? AND status='active'",
               (winner_id, now_utc(), duel_id))
    if winner_id:
        from . import gamify
        gamify.award_xp(winner_id, DUEL_XP_BONUS)


def duel_state_for(duel, user_id: int) -> dict:
    """What one participant sees: own progress, opponent score + index, status."""
    side = _side(duel, user_id)
    other = "opponent" if side == "challenger" else "challenger"
    ans = db.query("SELECT q_index, correct FROM duel_answers WHERE duel_id = ? AND user_id = ? ORDER BY q_index",
                   (duel["id"], user_id))
    return {
        "duel": duel,
        "side": side,
        "my_idx": duel[f"{side}_idx"],
        "my_score": duel[f"{side}_score"],
        "opp_idx": duel[f"{other}_idx"],
        "opp_score": duel[f"{other}_score"],
        "my_answers": [dict(a) for a in ans],
        "status": duel["status"],
    }


def stats_for(user_id: int) -> dict:
    row = db.query_one(
        "SELECT COUNT(*) c,"
        " COALESCE(SUM(CASE WHEN winner_id = ? THEN 1 ELSE 0 END),0) wins"
        " FROM duels WHERE status='finished' AND (challenger_id = ? OR opponent_id = ?)",
        (user_id, user_id, user_id))
    losses = (row["c"] or 0) - (row["wins"] or 0)
    return {"duels": row["c"] or 0, "wins": row["wins"] or 0, "losses": losses}
