# One-hour job queue — status

Queue: Job A (1v1 VS duels) then Job B (topic pack library). Worked strictly
top-down, one unit per commit (`feat(duels): …` / `feat(packs): …`).

Verified current state before coding: main @ b123e7a; generation overhaul,
gamification/mastery layer, deck inspect view all present; htmx polling
pattern exists (`hx-trigger="every 2s"` in `_job_status.html`); auth via
session cookie + `require_user`/`owned_deck`; gamify.award_xp exists; tests
67 green with LLM_MOCK=true.

## Job A — 1v1 VS duels — DONE (f161acc)

- [x] Schema: duels + duel_answers tables (idempotent in db.init_db)
- [x] Challenge flow: pick user + own deck -> pending challenge for opponent
- [x] Accept -> both play same 10 questions in seed-shuffled order (seed on duel row)
- [x] Live polling fragment (every 2s hx-trigger) showing opponent score + question index
- [x] Forfeit: 60s inactivity after start / after opponent finishes -> other player wins
- [x] Results screen: winner, scores, accuracy, XP bonus via gamify.award_xp (flat 50)
- [x] No FSRS/reviews impact (tested); deck privacy (opponent sees title + duel questions only, tested)
- [x] Stats page: duel wins/losses
- [x] Tests green: lifecycle, deterministic seed sequence, forfeit, no-FSRS, privacy; en/pl strings

## Job B — topic pack library — DONE (488bfdf)

- [x] Pack format JSON (format: Literal[1], title, lang, questions[]) + pydantic validation
  (4 distinct options, non-empty explanation, no placeholder/meta prompts)
- [x] Export deck -> /decks/{id}/export.pack.json; import pack file -> new owned deck,
  origin='pack' column (idempotent ALTER)
- [x] /library page lists packs/*.pack.json; "Add to my decks" clones with fresh FSRS
  state, status=ready, immediately playable; path traversal rejected
- [x] scripts/build_pack.py --topic --count --lang --out via the app's own pipeline
  (research -> chunk_v2 -> generate_v2 -> batch verify -> dedup); tested with LLM_MOCK
- [x] Starter packs: NO API key in this environment, so packs/ contains two
  LLM_MOCK format samples (photosynthesis_basics.pack.json, uklad_sloneczny.pack.json),
  each marked with a "note" field. REAL 100-question packs still to generate with a key:
  1. "Photosynthesis basics" (en), 2. "Układ Słoneczny" (pl)
- [x] Tests green: export/import round trip, clone state reset, validation rejects, build script

## Remaining real packs to generate (starter packs were mock-built format samples)

- [ ] packs/photosynthesis_basics.pack.json — real 100-question build, en
- [ ] packs/uklad_sloneczny.pack.json — real 100-question build, pl
Run: python scripts/build_pack.py --topic "..." --count 100 --lang en|pl --out packs/...

## User-facing feature map (post-queue, for docs/onboarding)

- Decks: create from file/URL/text/topic, size target 20–200, language en/pl;
  detail page with job progress, inspect (read-only reveal), review/edit/flag,
  regen replace/add, export pack, rename, delete.
- Study: least-mastered-first order, instant feedback (correct answer +
  explanation), FSRS scheduling, mastery boxes, combo.
- Library: shipped packs + clone + import pack file.
- Duels: 1v1 on a deck, seeded 10 questions, polling live scores, forfeit 60s,
  results + XP bonus, W/L on stats.
- Profile: streak (5/day bar, 1 forgiven missed day per 7d), biggest combo,
  badges; Settings toggle hides streak display.
- Onboarding tutorial: 7 steps covering all of the above (en/pl).
