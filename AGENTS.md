# AGENTS.md — Agent guide for AgentZero

> **For the agent reading this:** this is the living orientation doc for the project,
> meant for whatever agent picks up the work next. Read it first, then continue.
> **Keep it updated** — when you add a capability, change a convention, or finish a
> pending item, edit this file in the same change so the next agent inherits an
> accurate picture. Do not let it drift.

---

## What this is

AgentZero is a personal-assistant Telegram bot for one user (Blaise). It started as
a work tracker and grew into a general assistant. It runs 24/7 on a VPS and is **live
in production**.

Capabilities: projects/tasks, ad-hoc reminders, freeform memory, voice notes
(Whisper transcription), image input (vision), a proactive autonomy heartbeat, a
daily morning digest, and an MCP client layer for external platforms (Gmail/Calendar
read-only — server-side setup pending; see CONNECT_GOOGLE.md).

## Core architecture

NL write path: **Telegram → FastAPI webhook → load chat history + store snapshot →
LLM with tool definitions → executor validates/applies tool calls → bot confirms.**

The LLM **never writes to the DB directly.** It proposes tool calls; the deterministic
`executor.py` validates and applies them, logging every write to the `events`
collection so `/undo` can reverse it. Tools are defined in a neutral JSON-Schema
format and each LLM provider adapter translates them internally.

### File map (`agentzero/`)
| File | Responsibility |
|---|---|
| `main.py` | FastAPI app, lifespan, webhook + polling, command handlers, NL orchestration (`_handle_nl`) |
| `config.py` | All env vars. **Canonical names only** — no legacy fallbacks. |
| `db.py` | Motor async MongoDB client + `create_indexes()` |
| `models.py` | TypedDict schemas (docs are plain dicts at runtime) |
| `tools.py` | Neutral JSON-Schema tool definitions |
| `llm.py` | `LLMProvider` Protocol + `OpenAIProvider` (default) + `AnthropicProvider`. **Nothing else imports openai/anthropic.** |
| `executor.py` | Deterministic tool execution, fuzzy matching, events log, `undo_last` |
| `prompts.py` | `build_system_prompt()` (injects date/time, store snapshot, reminders, memory) + `PERSONALITY` constant |
| `scheduler.py` | APScheduler: one-off reminders, heartbeat interval, morning-digest cron |
| `autonomy.py` | Proactive heartbeat — gathers candidates, LLM decides send-or-SILENT |
| `digest.py` | Morning digest — daily rundown, always sends |
| `mcp_client.py` | Generic MCP client — connect, namespace (`server__tool`), route calls |
| `audio.py` | Whisper voice transcription (always OpenAI) |
| `telegram_io.py` | `send()` with 4096-char splitting |
| `collectors/` | Phase-4 stubs (external task collectors) — interface only |

### Data model (MongoDB collections)
`projects`, `tasks`, `events` (undo log), `chat_history` (last ~10 msgs/chat),
`reminders`, `memory` (freeform facts), `system_state` (last proactive-nudge time),
`disambiguation` (unused stub).

### Tools the LLM can call
Local: `create_project`, `add_task`, `mark_done`, `update_task`, `snooze`,
`get_status`, `set_reminder`, `list_reminders`, `cancel_reminder`, `remember`,
`forget`. MCP tools are added at runtime, namespaced `google__…` etc.

### Bot commands (fallbacks / manual triggers)
`/start` `/status [work|personal]` `/undo` `/done <task>` `/add <project> | <task>`
`/snooze <task> until <YYYY-MM-DD>` `/checkin` (force heartbeat) `/brief` (force digest).

## Conventions & gotchas (read before editing)

- **Do NOT run git commit/push.** Blaise handles all git operations himself. You may
  edit files and run tests, never commit.
- **Env var names are canonical and final:** `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_ID`
  (NOT the old `TELEGRAM_TOKEN`/`ALLOWED_USER_ID` — fallbacks were removed). `.env.example`
  is authoritative and must match `config.py` exactly.
- **Personality is for the BOT, not for you.** The `PERSONALITY` constant (witty,
  dry, sarcastic) governs LLM-generated bot text (chitchat replies, autonomy briefs,
  digests, reminder firing). Your own responses to Blaise stay normal/clear.
- **Tool results are narrated in voice.** The executor returns flat deterministic
  strings (e.g. `Created project "X"`) — those are the FACTS, but `_handle_nl` runs
  every tool-using turn's results through a second `llm.chat()` pass so the user gets
  one natural reply, not a list of robotic confirmations (collapses repeated
  successes/errors too). Same pass covers MCP results. Falls back to the raw joined
  strings if that LLM call fails. Cost: one extra LLM call per tool-using turn — kept
  deliberately because Blaise wants natural language over the micro-optimization.
- **"Most recent event" queries sort by `_id` desc, not `created_at`** — in-process
  timestamps collide at ms resolution; ObjectId is monotonic. Use the cursor pattern
  `.find().sort(...).limit(1).to_list(1)`, NOT `find_one(sort=...)` (mongomock ignores it).
- `undo_last` uses `replace_one(upsert=True)` so it restores deletes (e.g. `forget`)
  as well as updates.
- Scheduled/LLM features (reminders, digest) always have a **plain-text fallback** if
  the LLM call fails — a missed reminder is worse than a missed joke.
- `datetime.utcnow()` is used widely and emits deprecation warnings on 3.12+. Harmless
  for now; migrate to `datetime.now(timezone.utc)` if you touch that code.

## Run / test / deploy

```bash
# Tests (mongomock-motor, no real DB or API needed)
source venv/bin/activate && pytest -q          # currently 43 tests, keep green

# Run locally (polling mode)
uvicorn agentzero.main:app --port 8080
```

- **Local dev:** `.env` has `TELEGRAM_MODE=polling`. **Production VPS:** separate `.env`
  with `TELEGRAM_MODE=webhook`. `.env` is gitignored — never collides.
- **Deploy:** push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) SSHes to
  the VPS, pulls, restarts `agentzero`. No tests run in CI (by choice). See `DEPLOY.md`.
- Prod: Ubuntu VPS, `/var/www/production/AgentZero`, Apache reverse proxy (TLS) →
  uvicorn `127.0.0.1:8080`, systemd `agentzero.service`, MongoDB Atlas.
- **Debugging "bot not responding":** the webhook returns 200 regardless of handler
  success, so Telegram metrics look fine even when every message fails internally.
  `journalctl -u agentzero` is the source of truth.

## Status & what's next

**Done & live:** projects/tasks, reminders, memory, voice, images, autonomy heartbeat,
morning digest, MCP client layer (code).

**In progress — Gmail/Calendar via MCP:** client layer is built and tested. The
server-side setup is pending on Blaise: Google Cloud OAuth + running the
`workspace-mcp` server. Full walkthrough in `CONNECT_GOOGLE.md`; systemd unit in
`deploy/workspace-mcp.service`. Once running, set `MCP_ENABLED=true` and `GOOGLE_MCP_URL`.

**Pending from the original spec (not yet built):** disambiguation flow (Phase 2 —
`disambiguation` collection exists but unused), collectors wiring (Phase 4 — stubs only),
and per-scope twice-daily digests (the single morning digest partly covers this).

## Docs in this repo
- `AGENTS.md` (this file) — agent orientation, keep updated
- `DEPLOY.md` — VPS deployment walkthrough
- `CONNECT_GOOGLE.md` — Gmail/Calendar MCP setup
- `deploy/` — systemd units + Apache vhost
- `.env.example` — authoritative env var reference
```
