# Generation flow v3 — repair, target-budgeted extraction, multi-source decks

Scope: the generation pipeline and the `/new` flow only. Verified against `main`
@ 7ad6f6c. This doc is the Phase B proposal for the mission below; Phase A was the
pre-flight confirmation (file:line evidence in each item).

## Confirmed findings (Phase A)

| ID | Finding | Evidence on main |
|----|---------|------------------|
| F1 | Multi-pass loop is broken: in `_generate_deck_questions_inner` the drafting `structured_call` sits OUTSIDE the `for pass_no in range(max_passes)` loop. The loop only builds `user_msg`/`aspect`; every chunk gets exactly ONE generation call built from the LAST pass's aspect. `MAX_PASSES_PER_CHUNK` (3) never actually runs. Consequence: topic decks stall at ~chunks × MAX_QUESTIONS_PER_CHUNK instead of chunks × 6 × 3. | `app/generation.py:467-486` (loop + `user_msg` build) vs `app/generation.py:487-494` (`structured_call` after the loop body, `continue` on failure exits the chunk loop) |
| F2 | Topic-mode research is a single LLM call capped at `max_tokens=8192` (~6k words → ~9-12 chunks). Even with multi-pass repaired, the raw ceiling is ~12 × 6 × 3 = 216 questions, and realistically ~chunks × 6. Target 200 in topic mode is unreachable in practice. | `app/generation.py:341-352` (`research` call, `max_tokens=8192`, `ResearchDoc` ≤ 40 sections) |
| F3 | URL crawl budget comes from `LEVELS[level]["max_pages"]` (4/8/20), not from the deck's target; `settings.crawl_max_pages` (60) is unused for it. | `app/generation.py:374-377` (`cfg["max_pages"]`), `app/config.py:31` (`crawl_max_pages`), `app/config.py:41-76` (`LEVELS`) |
| F4 | `verify_v2` checks factual grounding and clarity; nothing rejects questions that are factually sound yet irrelevant to the stated learning goal. The goal is not even passed into the verify call. | `app/prompts/verify_v2.md`, `app/generation.py:520-535` (`verify_msg` has no goal) |
| F5 | `decks.source_id` is 1:1 with sources; a deck cannot combine material. (Correction accepted in pre-flight: pack decks DO have a source, `kind='pack'` — `app/packs.py:76-83` — so the migration treats them like any other 1:1 deck, no special-casing.) | `app/schema.sql:78`, `app/packs.py:69-90` |
| F6 | `LEVELS` still carries `target_questions` / `questions_per_chunk` keys that the target-size redesign made dead config — consumed only by `tests/test_smoke.py:120-125`. | `app/config.py:41-76`, grep for consumers |

Already working (do not change behavior): goal field + keyword/semantic triage
(`_topic_terms`, `_topic_score`, `_semantic_triage`, `app/generation.py:243-306`);
crawl4ai + httpx/trafilatura fallback with challenge detection (`app/ingest.py:103-220`);
`chunk_text_v2` (`app/ingest.py:262-314`); batch verify v2 with `MIN_VERIFY_SCORE=0.7`
and mandatory explanations (`app/generation.py:19,100-115,520-560`); text-tracked
dedup (`_normalize_options`, `_is_duplicate`) + cross-run seeding for mode=add
(`app/generation.py:164-230,455-467`); job stage/kept/dropped counters and retry
hardening (HEAD 7ad6f6c).

## Design per Phase C item

### P0 — repair the generation loop
1. Move the drafting `structured_call` and candidate collection INSIDE
   `for pass_no in range(max_passes)`; each pass builds its own `user_msg` with its
   own aspect instruction. Verify + insert per pass, as today. Early-stop when
   target reached must `break` out of both the pass loop and the chunk loop
   (use a flag; the existing `reached_target` break only exits the candidate loop).
   - Impact: large targets become reachable; topic decks stop stalling at chunks × 6.
   - Risk: more LLM calls when target > chunks × 6 — that is the intent, and the
     early-stop bounds it. Mock provider makes it testable.
   - Tests: mock call-count test (target > chunks × MAX_QUESTIONS_PER_CHUNK ⇒
     drafting calls per chunk == MAX_PASSES_PER_CHUNK; total inserted can exceed
     chunks × 6); full suite.
2. Honest shortfall math in the final job message: ceiling =
   chunks × questions_per_chunk × passes actually run. en + pl strings.
   - Test: assert message contains the computed ceiling after a short run.

### P1 — one unified /new flow, material budgeted to target
3. `/new` becomes: deck title (optional), learning goal (required), materials
   (required: one or more files and/or links), quiz language, target size
   (20/50/100/200). Remove the "Just a topic" tab and the paste-text panel;
   quick notes stay possible via `.txt`/`.md` upload. Refuse creation with a clear
   i18n message when no material is attached. Multiple files (`list[UploadFile]`)
   and multiple URLs (one per line) both accepted.
   - Files: `app/main.py:new_source`, `app/templates/new_source.html`.
   - Tests: TestClient — no material ⇒ form error, no deck row; 1 file + 2 URLs ⇒
     3 sources attached, one deck.
4. Legacy topic/text decks remain playable/studyable; deck detail shows a small
   "generated from model knowledge (legacy)" note for `kind in ('topic','text')`.
   - Files: `app/main.py:deck_detail`, `app/templates/deck_detail.html`, i18n.
5. The research pipeline is NOT deleted: `scripts/build_pack.py` and
   `app/packs.py` keep using kind='topic' via `generate_deck_questions`. Only the
   `/new` entry point is gated; the topic branch in the generation job stays.
   - Test: `LLM_MOCK=true python scripts/build_pack.py --topic … --count 20` still works.
6. Crawl budget derives from the target:
   `max_pages = clamp(ceil(target * WORDS_PER_QUESTION / WORDS_PER_PAGE), 1, settings.crawl_max_pages)`
   with named constants `WORDS_PER_QUESTION = 110`, `WORDS_PER_PAGE = 800`
   (app/config.py). Replaces `LEVELS[level]["max_pages"]` for URL sources.
   - Tests assert ordering (target 200 ⇒ more pages than target 20; both ≤ cap),
     not exact numbers.
7. Job message reports pages read, pages skipped, total material size (today only
   in logs). `extract_text_from_url` returns text; crawl stats are logged — surface
   them via the job `message`/events. Files: `app/generation.py` (url branch),
   `app/ingest.py` (log counts), `app/templates/_job_status.html`.
8. Shortfall messages name uncovered goal areas: compute goal terms
   (`_topic_terms(goal)`) vs kept chunks (`_topic_score` per kept chunk); terms with
   zero coverage are listed with a "attach material about X" suggestion. en + pl.

### P2 — multi-source decks
9. Schema: `deck_sources(deck_id, source_id, PRIMARY KEY(deck_id, source_id))` join
   table; idempotent migration in `db.init_db` backfilling from `decks.source_id`.
   `decks.source_id` is KEPT (pack decks and all existing queries rely on it);
   `questions.chunk_id` stays valid because chunks belong to sources.
   - Test: build a DB from the pre-migration schema, run `init_db` twice, assert
     backfill and idempotency; pack import still works.
10. Generation iterates ALL sources of the deck: chunks queried across the
    `deck_sources` join, triage + sampling across the combined pool, per-source
    pass accounting in logs/job, one combined job progress.
    - File: `_generate_deck_questions_inner`.
11. Cross-source dedup: existing `_is_duplicate` (normalized prompt + option set)
    applies across all sources within a run and across mode=add runs (already seeded).
12. Deck page lists sources (files by name, links as links) with a remove action
    (`POST /decks/{id}/sources/{source_id}/remove` — deletes that source's
    questions for the deck, asks confirmation when deck is ready). "Add more"
    becomes "Add material": attach another file/link, then run a mode=add job
    against all sources.
    - Files: `app/main.py` (new routes), `app/templates/deck_detail.html`.
    - Tests: 1 file + 2 links generate from all three; no cross-source dups above
      threshold; removing one source leaves the rest intact.

### P3 — goal-relevance gate
13. New `app/prompts/generate_v3.md` + `verify_v3.md` (v2 files stay on disk);
    `PROMPT_VERSION = "v3"`. `verify_v3` additionally rejects questions that pass
    factual checks but do not serve the stated learning goal; the goal is passed
    into the batch verify call. `generate_v3` keeps v2 wording rules; only
    goal-related lines change.
14. Relevance is a field on the existing batch verify response
    (`VerifyItem.relevant: bool` / fail reason), NOT a separate per-question call.
    - Mock test: off-goal question scores 0, on-goal passes.
15. Cleanup: remove dead `LEVELS` keys `target_questions` / `questions_per_chunk`
    after confirming no consumers; update `tests/test_smoke.py:120-125` (which is the
    only consumer) to not reference deleted config. `difficulty_hint`, level
    handling and `max_pages` stay until P1-6 replaces the latter.

## Deliberately not doing

- No refactor of the verified-good list above (goal triage, crawl4ai pipeline,
  chunk_v2, batch verify, text-tracked dedup, job counters, retry hardening).
- No UI redesign of `/new` beyond the changes listed (no new tabs/panels besides
  multi-file/multi-URL material inputs).
- No new dependencies (deep crawl stays crawl4ai/httpx+trafilatura; no new packages).
- No deletion of the topic research pipeline — packs rely on it; only the `/new`
  entry point is gated.
- No deletion of `decks.source_id` (pack decks and existing queries use it).
- No per-question relevance LLM call — relevance rides the existing batch verify.

## Acceptance criteria (from the mission)

- Target 200 with a large enough source produces ~200 questions; multi-pass
  verified by call-count test; no early stall at chunks × 6.
- `/new` has no topic and no paste-text entry; creating without material is refused
  clearly; legacy topic/text decks play with the legacy note; pack library and
  `build_pack.py` pass unchanged.
- URL crawl depth scales with target (target 200 > target 20, both ≤ crawl_max_pages).
- Job message shows pages read/skipped + material size; shortfall names uncovered
  goal areas, en + pl.
- 1 file + 2 links generate from all three; no cross-source dups above threshold;
  sources listed and removable; existing 1:1 and pack decks migrate cleanly.
- "Add material" on a ready deck dedups against all existing questions.
- Verify drops goal-irrelevant but factually sound questions (mock test).
- Full suite + eval green with `LLM_MOCK=true`; new eval samples: multi-source,
  goal-conflict, relevance rejection. All new strings en + pl.
