# Quiz generation overhaul — findings verification & improvement proposal

Scope: the generation pipeline only (ingest → chunk → generate → verify → dedup →
store → regen → review UI). Auth/settings/admin/passkeys and FSRS/gamification are
out of scope. Verified against `main` @ 25b4c5e.

## Phase A — verified findings

| ID | Finding | Verdict | Severity | Evidence |
|----|---------|---------|----------|----------|
| F1 | Placeholder options "Distractor N" reach DB/UI | **Confirmed** | P0 | `app/generation.py:146-156` pads to 4 with `f"Distractor {n}"`. Reproduced: `options=["A","","B",""]` → `['A','B','Distractor 3','Distractor 4']`. Nothing downstream rejects them; they are inserted and shown in study UI. |
| F2 | Silent answer corruption from stale `correct_index` | **Confirmed** | P0 | Same function: after dedup `options[:4]` with `correct_index=0` clamp. Reproduced: `options=["A","A","B","C"], correct_index=2` → options `['A','B','C','Distractor 4']`, `correct_index` stays 2 → now points at "C", not the claimed-correct "B". Wrong answer can be graded correct. |
| F3 | Regen duplicates | **Refuted on current main** | — | `regen_deck` (app/main.py) now does `DELETE FROM questions WHERE deck_id=?` + re-chunk, and dedup is in-memory per run only. Verified live: regen yields same count, not doubled. **But**: there is no "Add more" mode and no cross-run dedup, so a user who wants more questions has no safe path — the underlying gap (F3's spirit) remains. Phase C adds Replace/Add-more explicitly. |
| F4 | No topic mode | **Confirmed** | P1 | `/new` requires file/URL/text; a bare topic string goes to `topic_text` and is chunked as source text, so the mock/LLM is asked to write questions *about the string itself*. No `kind='topic'` path exists. |
| F5 | Fixed density / no user control of deck size | **Partially refuted** | P1 | `LEVELS` (app/config.py) already offers quick/standard/thorough with target 50/100/250 q and a `<select>` on /new. But targets are not the requested 20/50/100/200, thorough's 250 exceeds the 200 cap, and per-chunk density is derived, not chosen. Phase C replaces levels with an explicit target picker. |
| F6 | Chunking ignores structure, can cut sentences | **Confirmed** | P1 | `chunk_text` (app/ingest.py:229-243) splits on whitespace at fixed word windows; reproduced mid-sentence cut with unpunctuated text (`'...powerhouse of the cell'` / `'the cell it produces...'`). Paragraphs/headings ignored. |
| F7 | verify_v1.md has no clarity checks | **Confirmed** | P0 | `app/prompts/verify_v1.md` checks supported/unambiguous/distractors only. No length, double-negative, meta-reference, "all of the above", parallelism checks. |
| F8 | generate_v1.md lacks wording constraints | **Confirmed** | P0 | `app/prompts/generate_v1.md` has 4-option/exactly-one-correct/plausible-distractor rules but no length caps, no self-contained stem, no double-negative/meta bans, no parallelism rule. |
| F9 | Empty explanations reach the user | **Confirmed** | P0 | `QuestionDraft.explanation: str = ""` (generation.py:92); insert path never checks emptiness. |
| F10 | Flagged questions stay in study queue | **Confirmed** | P1 | All due queries (`app/main.py:346,427,466,482,494`) have no `flagged=0` filter; `flag_question` (main.py:611) only sets the flag. |
| F11 | Verify notes discarded; no counts in job UI | **Confirmed** | P1 | `VerifyItem.notes` parsed (generation.py:114) but never stored; `questions` table has no notes column; `_job_status.html` shows only done/total + message. |
| F12 | Dedup misses paraphrases, ignores option sets | **Confirmed** | P1 | `_similarity` (generation.py:159-165) is raw Jaccard on word sets, threshold 0.85, prompts only, in-memory per run (`seen_prompts`). No normalization (case/punct already lowercased via regex but no stemming/diacritics folding), no option-set comparison, no cross-run check. |
| F13 | Eval harness asserts only min_questions=1 on one sample | **Confirmed** | P2 | `scripts/eval_generation.py:24-25` — one sample (`photosynthesis.txt`), `min_questions: 1`, no quality assertions. |

### Extra findings (affect the end goal)

| ID | Finding | Severity | Evidence |
|----|---------|----------|----------|
| X1 | Verify-failure fallback scores 0.5 (below 0.7 → dropped): fail-closed today, good — but it means a flaky LLM call silently drops all candidates of a chunk. Kept as-is (fail-closed invariant); surfaced via counts (P1-10) instead. | info | generation.py:409-412 |
| X2 | `MIN_VERIFY_SCORE` drop path stores no reason; job message only says "rejected by verify" in logs, not in UI. Covered by P1-10/P2-11. | P2 | generation.py:417-420 |
| X3 | Triage prompt (`triage_v1.md`) also stays v1; PROMPT_VERSION bump must remap generate/verify only or all three consistently. Decision: bump all to v2 with new files, keep v1 files. | info | generation.py:21,82-84 |
| X4 | Research-doc step for topic mode needs a large structured output (~2-4k words). `structured_call` takes `max_tokens` per call and providers accept it; no design change needed — pass a bigger `max_tokens` for the research call only. | info | llm/structured.py:26, providers.py |
| X5 | "Add more" regen currently impossible to dedup safely across runs because `seen_prompts` is per-run and there is no DB-side existing-question check. Phase C loads existing prompts/options into the dedup set at job start (for Add more only). | P1 | generation.py:379 |

## Phase B — design (priority order)

### P0 — correctness and silent corruption

1. **`_normalize_options` remap (F1+F2).** Track the correct option by its text: `correct_text = q.options[q.correct_index]` before dedup; after dedup find its new index (case-insensitive); if absent → return `None` (drop the question). Never pad with placeholders; if `< 4` distinct non-empty options → `None` (drop; the generate call already retried once via structured_call, and per-question option regeneration is out of scope for P0-1 — P2-12 adds single-question regen). Impact: eliminates placeholder questions and wrong-answer corruption. Effort: small. Risk: low. Test: unit-test every remap case incl. correct-removed-as-duplicate.
2. **Regen modes (F3-spirit).** `/decks/{id}/regen?mode=replace|add` (default replace). Replace = current behavior (delete all questions, full regen). Add = keep existing, load their normalized prompts+option sets into dedup set at job start. UI: two buttons on deck page. Impact: safe path to grow a deck. Effort: small. Risk: low. Test: replace leaves 0 old questions; add inserts nothing matching existing normalized prompt/option set; edited questions untouched.
3. **Dedup upgrade (F12).** `_normalize_text()`: lowercase, strip punctuation, fold diacritics (unicodedata NFKD), drop stopwords (existing `_STOPWORDS`), sort-unique tokens → canonical key. Similarity = Jaccard on normalized token sets, threshold 0.8. Also compare sorted normalized option sets (exact match ⇒ duplicate). Cross-run for Add mode. Impact: catches paraphrases + copied option sets. Effort: medium. Risk: low (threshold conservative). Test: paraphrase + diacritics + option-set cases.
4. **`generate_v2.md` wording rules (F8).** New prompt file; `PROMPT_VERSION="v2"`. Rules: stem answerable before options are seen; one fact per question; no double negatives; no "all/none of the above", no "both A and B"; no meta references ("the text", "the author", "according to the passage"); distractors = plausible confusions from the source, not random strings; options parallel in grammar, similar length, correct option not systematically longest; caps: stem ≤ 40 words, option ≤ 12 words, explanation 1–3 sentences citing the source; mixed difficulty; explanation mandatory. Impact: directly attacks ambiguity/verbosity. Effort: small. Risk: low (prompt-only). Test: prompt file loads (existing test), mock still passes.
5. **`verify_v2.md` clarity checks (F7) + store notes (F11).** Extend critic: factual checks (kept) + clarity checks (length caps, convoluted phrasing, double negatives, meta references, option parallelism, exactly one defensible answer). New nullable column `questions.verify_notes TEXT` via idempotent ALTER in `db.init_db` (never edit schema.sql destructively). Store `notes` for every verified item (kept and dropped). Impact: drops unclear questions fail-closed with an inspectable reason. Effort: medium. Risk: low. Test: overlong stem / double negative / meta reference / "all of the above" → verdict fail + notes stored.
6. **Mandatory explanations (F9).** `explanation: str = Field(min_length=10)` in `QuestionDraft`; generation.py drops empty-explanation drafts explicitly (belt and braces). Impact: no empty explanations in DB. Effort: tiny. Risk: low. Test: draft with empty explanation never inserted.

### P1 — scale and topic mode

7. **Deck size target (F5).** Replace level select on /new with target select: 20 / 50 / 100 / 200 (default 50, cap 200). Store `decks.target_questions` (idempotent ALTER, default 50). Pipeline: `questions_per_chunk = ceil(target / n_chunks)` clamped to [1, 6]; if `n_chunks * 6 < target`, run up to 3 passes per chunk with different "aspect" instructions; job `message` reports final count + shortfall reason ("source too small: N chunks × max 6 q/chunk × 3 passes = M < target"). Keep `LEVELS` for difficulty hint/back-compat but drive counts from target. Impact: user controls size up to 200. Effort: medium. Risk: medium (cost) — bounded by 3-pass cap and executor. Test: small source either reaches target or reports shortfall; target 200 capped in UI.
8. **Topic mode (F4).** New radio "Just a topic" on /new → `kind='topic'`. Job first calls LLM with new `research_v2.md` prompt → structured `ResearchDoc(sections: [{heading, body}])` with `max_tokens=8192`, rendered to markdown, stored as `sources.raw_text` (kind='topic'), then normal chunking+generation runs on it. Mock provider returns a deterministic multi-section doc. Deck page shows a disclosure chip "grounded in generated research". Impact: bare topic → real domain questions. Effort: medium. Risk: low (provider abstraction already supports larger max_tokens — no design change; X4). Test: mock topic deck has source kind=topic, research text stored, question count driven by target.
9. **Structure-aware chunking (F6).** `chunk_text_v2`: split into blocks on blank lines (paragraphs) and markdown/heading lines (`#`, all-caps short lines); split blocks into sentences via regex on `.!?…` + newline; pack sentences into chunks ≤ CHUNK_TOKENS words, never splitting a sentence (a sentence longer than the limit becomes its own chunk); keep 60-word overlap by carrying whole trailing sentences; drop boilerplate chunks (< 8 words or no sentence terminator and < 25 words). `chunk_text` stays for back-compat; new code uses v2. Impact: no mid-sentence cuts for paragraph-structured input; less boilerplate. Effort: medium. Risk: low. Test: paragraph-structured input → every chunk ends at sentence boundary; boilerplate dropped; overlap preserved.
10. **Progress + counts (F11, X2).** jobs table already has message/done/total; add columns via ALTER: `stage TEXT` (chunking/generating/verifying/done), `generated INT`, `kept INT`, `dropped INT`. `_job_status.html` shows stage + "generated X · kept Y · dropped Z". Impact: user sees what happened. Effort: small. Risk: low. Test: after mock run, kept+dropped == generated, stage=done.

### P2 — post-generation QC

11. **Result summary.** When job done, deck page shows "N created, M dropped (reasons in job log)" + one-click link to `/decks/{id}/questions`. Implemented via job counts + link; no new page. Effort: small.
12. **Question review.** On questions page: per-question Edit (inline form: prompt, 4 options, correct radio, explanation), Delete, Regenerate-one (runs generate+verify for that chunk, replaces the question). New column `edited INT DEFAULT 0`; edits set it; dedup skips edited questions in Add mode. Effort: large. Risk: medium (UI + routes). Test: edit sets flag; Add-more leaves edited rows untouched; regen-one replaces.
13. **Flagged handling (F10).** All due/study queries add `AND flagged = 0`; deck page lists flagged questions with edit/delete/unflag. Effort: small. Risk: low. Test: flagged question never served by /study; appears on deck page.

### Deliberately not doing

- **Stemming/lemmatization or embeddings for dedup** — would need a dependency (nltk/spacy) or API; normalized-Jaccard at 0.8 catches the practical cases. Revisit if false-negatives show up.
- **Per-question option regeneration inside P0-1** — structured_call already retries the whole call; P2-12's regen-one covers the residual case. Keeps P0 minimal.
- **LLM-based paraphrase detection in verify** — the critic already flags near-duplicates implicitly via unambiguity; adding a second model call per question doubles cost for marginal gain.
- **Deleting LEVELS entirely** — quick/standard/thorough carry the difficulty hint; keep as the "depth" axis, target is the "size" axis. Two axes are fine.
- **Changing FSRS/gamification/study rating** — out of scope per invariants.
- **Real-model eval in CI** — needs an API key; harness stays mock-green, real run is manual (documented in final verification).

### Dependencies
None new. All work uses existing pydantic/httpx/stdlib. (Justification: dedup stays dependency-free per "not doing" above.)

## Phase C — commit plan (one improvement per commit, `fix feat:` prefix)

P0-1 normalize → P0-2 regen modes → P0-3 dedup → P0-4 generate_v2 → P0-5 verify_v2+notes → P0-6 explanations → P1-7 target → P1-8 topic mode → P1-9 chunking v2 → P1-10 progress/counts → P2-11 summary → P2-12 review UI → P2-13 flagged.
Each commit: code + tests; `pytest -q` green after each.
