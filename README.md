# Quipeat

Learn by repetition: feed it a document, link, or topic and it drafts a large
bank of multiple-choice questions with an LLM, then drills you with spaced
repetition and gamification (XP, levels, streaks, badges).

## Run (LAN only)

Prebuilt image (built and pushed to GHCR automatically on merge to `main` by
`.github/workflows/docker-ghcr.yml`):

```bash
cp .env.example .env   # then edit SECRET_KEY and MASTER_KEY
# generate both keys:
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# in docker-compose.yml: comment `build: .`, uncomment `image: ghcr.io/nobodycs/quipeat:latest`
docker compose up -d
```

Or build from source instead: keep `build: .` and run `docker compose up -d --build`.

The container always listens on **8000 internally**. To use a different *host* port
(e.g. an obscure one for nginx), set in `.env`:

```
QUIPEAT_LAN_BIND=127.0.0.1:37411:8000
```

→ app reachable on the Docker host at `http://127.0.0.1:37411` (or `0.0.0.0:37411:8000`
for LAN). nginx then proxies `your.name` → `http://<docker-host>:37411`.

In Portainer: create a stack from the repo (branch `main`, path `docker-compose.yml`)
and set the env vars in the stack's environment section; or paste a compose file with
`image: ghcr.io/nobodycs/quipeat:latest`.

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
