# One-hour job queue — status

Queue: Job A (1v1 VS duels) then Job B (topic pack library). Worked strictly
top-down, one unit per commit (`feat(duels): …` / `feat(packs): …`).

Verified current state before coding: main @ b123e7a; generation overhaul,
gamification/mastery layer, deck inspect view all present; htmx polling
pattern exists (`hx-trigger="every 2s"` in `_job_status.html`); auth via
session cookie + `require_user`/`owned_deck`; gamify.award_xp exists; tests
60 green with LLM_MOCK=true.

## Job A — 1v1 VS duels — DONE (commit TBD)

- [x] Schema: duels + duel_answers tables (idempotent in db.init_db)
- [x] Challenge flow: pick user + own deck -> pending challenge for opponent
- [x] Accept -> both play same 10 questions in seed-shuffled order (seed on duel row)
- [x] Live polling fragment (every 2s hx-trigger) showing opponent score + question index
- [x] Forfeit: 60s inactivity after start / after opponent finishes -> other player wins
- [x] Results screen: winner, scores, accuracy, XP bonus via gamify.award_xp (flat 50)
- [x] No FSRS/reviews impact (tested); deck privacy (opponent sees title + duel questions only, tested)
- [x] Stats page: duel wins/losses
- [x] Tests green: lifecycle, deterministic seed sequence, forfeit, no-FSRS, privacy; en/pl strings

## Job B — topic pack library

- [ ] Pack format JSON (format field, title, lang, questions[]) + validation
- [ ] Export deck -> pack file; import pack -> new owned deck, origin='pack' column
- [ ] Library page listing packs/ shipped packs; "Add to my decks" clones with fresh FSRS state
- [ ] scripts/build_pack.py --topic --count --lang via the app's generation pipeline
- [ ] Starter packs (real key if available, else LLM_MOCK format samples marked as such)
- [ ] Tests: export/import round trip, clone state reset, pack validation; en/pl strings

## Remaining real packs to generate (if starter packs were mock-built)

- (fill in)
