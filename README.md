# Quipeat

Turn any document, link, pasted text, or just a topic name into a quiz deck
(20–200 questions), then drill it until you master it. Study serves the
least-mastered questions first, shows the correct answer + explanation after
every answer, and tracks per-question mastery boxes. Beyond studying: inspect
decks read-only, edit/flag questions, export decks as shareable pack files,
browse a pack library, and race friends in 1v1 VS duels. Gamification: XP,
deck levels, combos, streaks (with a weekly forgiveness), badges, profile.
English and Polish UI; quiz language is chosen per deck and passed to the LLM.

## Run

```bash
cp .env.example .env        # fill SECRET_KEY and MASTER_KEY (see below)
docker compose up -d --build
```

Prebuilt image on GHCR (built on merge to `main`): swap `build: .` for
`image: ghcr.io/nobodycs/quipeat:latest` in `docker-compose.yml`.

The container listens on 8000 internally. The host port is set with
`QUIPEAT_LAN_BIND` (default `127.0.0.1:37411:8000`), e.g. point your reverse
proxy at host port 37411.

### Required env

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"                      # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # MASTER_KEY
```

Optional: `ADMIN_IDENT` (email or display name of the admin user),
`MISTRAL_API_KEY` + `BUILTIN_MODEL` (built-in LLM for admin/privileged users),
`PASSKEYS_ENABLED=true` + `RP_ID` when serving over HTTPS to enable passkeys,
`LLM_MOCK=true` for a keyless mock LLM (dev/tests).

## Use

Register → (first time: a 6-step tutorial) → Settings: connect an LLM key
(OpenRouter or Mistral; encrypted at rest) or, if privileged, use the built-in
one → **New**: upload PDF/DOCX/TXT/MD, paste a URL, paste text, or just name a
topic (the app writes a research document on it first), pick the quiz language
and deck size (20/50/100/200) → questions generate in the background, each
verified against its source chunk → **Study**.

Deck page: **Inspect** (read-only, tap to reveal answers), **Review questions**
(edit / delete / regenerate one / flag), **Regenerate** (replace all or add
more), **Export pack** (download a shareable `.pack.json`). **Library**: shipped
packs you can clone into your decks in one click + import a pack file.
**Duels**: challenge another user on one of your decks — same 10 questions for
both, live polling scores, 60 s idle = forfeit, winner gets bonus XP.
**Profile** (`/profile`): streak, biggest combo, badges. **Stats**: charts +
duel W/L. Language switch (🇬🇧/🇵🇱) is in the top header; replay the tutorial any
time via the ⓘ button in the header or Settings.

## How question quality stays consistent across models

- Strict JSON Schema on every LLM call + pydantic validation + retry-with-error.
- Structure-aware chunking (paragraphs/headings, never mid-sentence); questions
  must be answerable from the attached chunk.
- Wording rules (self-contained stems, length caps, no double negatives /
  all-of-the-above / meta references) in `app/prompts/generate_v2.md`, and a
  critic pass (`verify_v2.md`) re-checks each question for factual support AND
  clarity; weak ones are dropped and the critic notes are stored.
- Normalized dedup (prompts + option sets), versioned prompts in `app/prompts/`,
  and `model` + `prompt_version` stored on every question.
- Eval harness with quality assertions: `LLM_MOCK=true python scripts/eval_generation.py`
- Pack builder: `python scripts/build_pack.py --topic "..." --count 100 --lang en`

See `docs/generation_improvements.md` (audit + proposal) and
`docs/queue_status.md` (duels + packs delivery status).

## Stack

FastAPI + SQLite (WAL), Jinja2 + htmx + Tailwind (vendored htmx/fonts, no CDN),
a bounded thread pool for generation, FSRS-inspired scheduling + mastery boxes.
Tests: `pytest -q` (LLM_MOCK=true runs everything keyless).
