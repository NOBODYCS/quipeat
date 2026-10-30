# Quipeat

Learn by repetition: feed it a document, link, or topic and it drafts a large
bank of multiple-choice questions with an LLM, then drills you with spaced
repetition and gamification (XP, levels, streaks, badges).

## Run (LAN only)

```bash
cp .env.example .env   # then edit SECRET_KEY and MASTER_KEY
# generate both keys:
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose up -d --build
# app: http://127.0.0.1:8000 (or your LAN IP: change QUIPEAT_LAN_BIND in .env)
```

No part of this is meant to be exposed to the internet. Access it from PC/phone
over your WireGuard network. If you later go public: serve via HTTPS (Caddy with
Let's Encrypt), close registration (`ALLOW_REGISTRATION=false` when added), and
set `PASSKEYS_ENABLED=true`.

## Using it

1. Register (email + password; the form uses standard `autocomplete` attributes
   so password managers offer to save/fill it).
2. Settings → paste an **OpenRouter** key (one key, any model — pick from the
   dropdown or enter any model id) or a **Mistral Studio** key. Keys are stored
   encrypted at rest (Fernet, `MASTER_KEY`).
3. New source → upload PDF/DOCX/TXT/MD, paste a URL, or paste raw text.
4. Questions generate in the background (progress bar); then Study.

## Question generation quality (how it stays consistent across models)

- **Contract at the API boundary**: every LLM call requests strict JSON Schema
  output (`response_format: json_schema`); responses are parsed and validated
  with pydantic; on failure the validation error is fed back and the call is
  retried (up to 3×). Weak models still converge.
- **Grounded drafting**: sources are chunked (~450 tokens, 60 overlap) and the
  generator may only ask what the attached chunk supports.
- **Critic pass**: a second call re-checks each question against its chunk
  (supported? unambiguous? plausible distractors?) and drops anything below
  a score threshold — this is what prevents hallucinated answers.
- **Dedup** by token-set similarity of prompts.
- **Versioned prompts**: `app/prompts/generate_v1.md`, `verify_v1.md`. Every
  question row records `model` and `prompt_version`, so you can compare models
  or regenerate decks after a prompt bump.
- **Eval harness**: `python scripts/eval_generation.py` runs sample docs through
  the pipeline (mock or real model) and reports counts/scores per model.

## Stack

FastAPI + SQLite (WAL) + Jinja2 templates + htmx + Tailwind (compiled at Docker
build). Background generation via in-process threads with a `jobs` progress
table. FSRS-inspired scheduling (`app/fsrs.py`).

## Dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
npm install && npm run build:css   # only needed when templates change
cp .env.example .env && LLM_MOCK=true uvicorn app.main:app --port 8000
pytest -q                          # smoke tests (mock LLM)
```

`LLM_MOCK=true` runs a deterministic mock LLM so the whole flow works without
any API key (used by tests).
