# Quipeat

Turn any document, link, or topic into a quiz deck, then drill it with spaced
repetition. Gamification: XP, levels, streaks, badges. English and Polish UI;
quiz language is chosen per deck and passed to the LLM.

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

Register → (first time: short tutorial) → Settings: connect an LLM key
(OpenRouter or Mistral; encrypted at rest) or, if privileged, use the built-in
one → New: upload PDF/DOCX/TXT/MD, paste a URL, or paste text, pick the quiz
language → questions generate in the background → Study.

Language switch (🇬🇧/🇵🇱) is in the top header.

## How question quality stays consistent across models

- Strict JSON Schema on every LLM call + pydantic validation + retry-with-error.
- Sources are chunked; questions must be answerable from the attached chunk.
- A critic pass re-checks each question against its chunk; weak ones are dropped.
- Dedup, versioned prompts in `app/prompts/`, and `model` + `prompt_version`
  stored on every question.
- Eval harness: `LLM_MOCK=true python scripts/eval_generation.py`

## Stack

FastAPI + SQLite (WAL), Jinja2 + htmx + Tailwind, background threads for
generation, FSRS-inspired scheduling. Tests: `pytest -q`.
