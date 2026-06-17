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
(Whisper transcription), image input (vision), web search + page fetch (research
inside the chat), read-only Yahoo Mail (IMAP, opt-in), automatic job-application tracking
(scans the inbox for confirmations/replies), expense tracking (logs payment receipts from
Yahoo + Gmail), a proactive autonomy heartbeat, a daily morning digest, and an MCP client layer
for external platforms — Gmail + Google Calendar read access is LIVE (see "Google … LIVE" below).

## Google (Gmail + Calendar) — LIVE (read-only) as of 2026-06-14

Gmail + Calendar read access is connected and working in production. How it's wired:

- **MCP server:** `workspace-mcp` (taylorwilsdon/google_workspace_mcp) installed at
  `/opt/workspace-mcp` (own venv; separate from the repo). systemd service `workspace-mcp`,
  runs as `www-data`: `--transport streamable-http --read-only --tool-tier core` on
  **127.0.0.1:8003** (loopback only). Exposes **24 read-only tools**; write tools (gmail
  send/modify/draft/trash) are NOT exposed — read-only enforced at the tool layer.
- **AgentZero wiring:** `/var/www/production/AgentZero/.env` has `MCP_ENABLED=true` and
  `GOOGLE_MCP_URL=http://127.0.0.1:8003/mcp`. The bot loads the `google__*` tools at startup.
- **OAuth callback exposure:** Apache vhost `agent-mcp.artfricastudio.com` (TLS via certbot)
  proxies **ONLY `/oauth2callback`** → 127.0.0.1:8003. **`/mcp` is NOT publicly exposed**
  (verified 404). NEVER proxy `/mcp` — it has no auth and can read Gmail.
- **Credential:** `/var/www/.google_workspace_mcp/credentials/menniablaise@gmail.com.json`
  (refresh_token). NOTE: the token carries BROAD read+write scopes (the only consent flow
  the server offers requires non-read-only mode); read-only is enforced by the `--read-only`
  flag, not the token. This was a conscious, owner-approved trade-off.
- Port note: 8001 is taken by another service (`bitovi-api`) — that's why 8003.

**Re-auth runbook (if the token is ever revoked/expired):** `--read-only` HIDES the
`start_google_auth` tool, so consent needs full mode temporarily:
1. (Safety) set AgentZero `MCP_ENABLED=false`, restart agentzero.
2. Change the `workspace-mcp` unit ExecStart to `--transport streamable-http --tool-tier complete`
   (drop `--read-only`; `complete` is required — `full` is invalid), daemon-reload, restart.
3. Mint a URL: via AgentZero venv python (`mcp` SDK) connect to `http://127.0.0.1:8003/mcp`,
   call `start_google_auth(service_name="gmail", user_google_email="menniablaise@gmail.com")`.
   The URL's redirect_uri must be `https://agent-mcp.artfricastudio.com/oauth2callback`.
4. **HUMAN:** owner opens the URL in any browser, approves. The callback hits the Apache
   proxy → running server → token written. (Auth code/state TTL is ~10 min; mint fresh if it lapses.)
5. Restore ExecStart to `--transport streamable-http --read-only --tool-tier core`, daemon-reload,
   restart. Re-enable AgentZero `MCP_ENABLED=true`, restart agentzero.
An agent can do everything EXCEPT step 4 (browser approval) — hand that to the owner.

## Job application tracking (`applications.py`)

Autonomous loop tying the inbox to outcomes. `scheduler._application_scan_job` runs every
`APPLICATION_SCAN_HOURS` (quiet-hours aware): `scan_inbox` pulls new mail via
`yahoo_mail.fetch_recent` (UID-cursor `last_app_scan_uid` in `system_state`), the LLM classifies
each as **confirmation** (→ start tracking, status `applied`), **update** (→ set status
interview/rejected/offer/replied), or **other**, and `applications` docs are upserted (fuzzy
company/role match, no dupes). An **update for a company with no existing record creates one
from the reply** (e.g. an interview invite with no prior confirmation) — flagged with the
`created` bit so the notification reads "Now tracking … (their reply came in first)". The
classifier is told to categorise by content, not by whether the company is already tracked.
**First scan only sets a baseline UID — it tracks forward, never trawls history.** `send_application_update` proactively reports new tracked apps, status changes,
and **stale follow-ups** (status `applied` past `APPLICATION_STALE_DAYS` → "gone quiet, follow
up?", flagged once via `stale_notified`). Tools: `list_applications`, `track_application`,
`update_application`, `check_job_replies` (force a scan now — uses `gather_application_update`,
which does NOT send, so the tool loop delivers it once). `/applications` lists them. Gated by
`JOB_TRACKING_ENABLED`; auto-scan needs Yahoo, but manual track/update works without it. Statuses:
applied → replied → interview → offer | rejected (closed = archived).

## Expense tracking (`expenses.py` + `imap_mail.py`)

Scans payment receipts across ALL configured IMAP mailboxes (Yahoo + personal Gmail) and logs
them. `imap_mail.mail_accounts()` returns the enabled accounts (Yahoo via `YAHOO_MAIL_*`, Gmail
via `GMAIL_IMAP_*` — Gmail here is IMAP-with-app-password, **separate from the Google MCP**,
because the MCP returns free-text blobs unsuitable for a deterministic scanner). `scheduler._receipt_scan_job`
runs every `RECEIPT_SCAN_HOURS` and is **silent** (a ping per purchase would be spam) — it just
LLM-classifies each mail as `receipt` (extract merchant/amount/currency/category/date) or `other`
and inserts into `expenses`. **The classifier logs money OUT only**: for bank/mobile-money alerts
it counts a debit/card-purchase/bill/subscription but excludes credits, deposits, money received,
incoming/outgoing person-to-person transfers, refunds, reversals, declined txns, OTPs, and balance
notices (this is the fix for bank alerts polluting the data). Per-mailbox UID cursors (`receipt_cursor_<source>`); first scan of a
mailbox sets a baseline (tracks forward). Dedup by `email_id` = `<source>:<uid>`. **Amounts are
grouped per currency, never summed across** (GHS vs USD stay separate). `scheduler._expense_summary_job`
sends a weekly summary (`EXPENSE_SUMMARY_DOW`/`HOUR`). Tools: `list_expenses`, `expense_summary`,
`add_expense` (manual), `check_receipts` (force scan now; pass `days=N` for a historical
backfill — `expenses.backfill_receipts` pulls the last N days via `imap_mail.fetch_since`,
classifies in batches, logs deduped by `email_id`, and does NOT move the forward cursor).
`delete_expense` removes a wrong row (fuzzy by merchant/description, `amount` disambiguates — for
purging a misread bank credit/transfer). `expenses.purge_scanned_expenses(chat_id)` deletes
email-sourced rows but keeps manual ones (used for a clean re-backfill). `/expenses` shows the
month summary. Gated by `EXPENSE_TRACKING_ENABLED`; auto-scan needs an IMAP mailbox, manual
add/delete work without one. The dashboard API stays read-only (no DELETE) — deletions are chat-only.

## Dashboard API (`api.py`)

Read-only JSON for an external spending dashboard, mounted at `/api` on the same FastAPI app
(so it's served through the existing Apache proxy at the bot's domain — Apache must proxy `/api`
too; if it proxies `/` to uvicorn it already does). **Gated by `DASHBOARD_API_KEY`**: every route
needs the `X-API-Key` header to match; if the key is unset the API is fully disabled (404) — never
expose financial data unauthenticated. CORS is restricted by `DASHBOARD_ORIGINS` (GET only).
Routes (all scoped to `ALLOWED_CHAT_ID`): `GET /api/health`, `/api/expenses`
(`period|start|end|category|limit`), `/api/expenses/summary`, `/api/expenses/timeseries`
(`bucket=day|week|month`), `/api/expenses/categories`. `period` is today|week|month|all; explicit
`start`/`end` ISO dates override it. Amounts are grouped per currency (never summed across).

## Yahoo Mail — read-only (IMAP)

Separate from Google (no MCP, no OAuth). `yahoo_mail.py` logs into `imap.mail.yahoo.com:993`
with `YAHOO_MAIL_USER` + `YAHOO_MAIL_APP_PASSWORD` (a Yahoo **app password**, generated at
Yahoo Account Security — NOT the login password). Tools `yahoo_search` (find messages → uids)
and `yahoo_read` (fetch one message body) are added to the toolset only when
`YAHOO_MAIL_ENABLED=true`. Read-only is enforced in code: mailbox opened `readonly=True`,
messages fetched with `BODY.PEEK` (never marked seen/modified/deleted). UIDs are used so a
uid from search stays valid for the follow-up read. To set up: generate the app password, set
the three `YAHOO_MAIL_*` env vars, restart.

## Core architecture

NL write path: **Telegram → FastAPI webhook → load chat history + store snapshot →
agentic tool LOOP → bot replies.**

The loop (`llm.run_tool_loop`, used by `main._handle_nl`): the model calls tools, sees
the results, and can call MORE tools before answering — repeating until it produces a
final reply or hits `max_iters` (6). This is what lets it CHAIN calls (e.g. search Gmail
for ids → fetch each body → summarise). `_handle_nl` passes an `execute(name, args)`
callback that routes local tools to `executor.py` and `google__*` tools to `mcp_client`.
The model's final text IS the reply (narrated in voice); there's no separate narration pass.
`_handle_nl` sends a `ChatAction.TYPING` immediately, and a witty "working on it" filler
(`prompts.THINKING_FILLERS`, picked at random) fires from the `_execute` callback the first
time the model calls a slow internet tool (`_FILLER_TOOLS` = `web_search`/`web_fetch`) — at
most once per turn. Fast local replies (and quick voice answers) get no filler. When the user uses Telegram's reply-to-a-specific-message feature,
`_quoted_context(msg)` pulls the quoted text (and whether it was the bot's own message) and
`_handle_nl` prepends it as `[Replying to …]` to the user turn, so the model knows exactly
what's being referenced. Wired for text, voice, and photo messages.

The LLM **never writes to the DB directly.** It proposes tool calls; the deterministic
`executor.py` validates and applies them, logging every write to the `events`
collection so `/undo` can reverse it. Tools are neutral JSON-Schema; each provider
adapter translates them and manages its own native multi-turn message format inside the loop.

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
| `autonomy.py` | Proactive heartbeat — ranks open tasks by urgency, LLM picks ONE to nudge (or SILENT); suppresses only that task; spontaneous jittered spacing |
| `digest.py` | Morning digest — daily rundown, always sends |
| `mcp_client.py` | Generic MCP client — connect, namespace (`server__tool`), route calls |
| `web.py` | Web search (Tavily/Brave/DuckDuckGo) + page fetch (httpx, dependency-free HTML→text). No DB writes. |
| `yahoo_mail.py` | Yahoo Mail read-only over IMAP (`imaplib`, app password). `yahoo_search`/`yahoo_read`/`fetch_recent`; blocking work in `asyncio.to_thread`. Read-only enforced (readonly select + BODY.PEEK). |
| `applications.py` | Job-application tracking — scans the inbox, LLM-classifies confirmations/replies, upserts the `applications` collection, proactively reports changes + stale follow-ups. |
| `imap_mail.py` | Generic multi-account IMAP batch reader (`mail_accounts()` → Yahoo + Gmail; `fetch_recent(account, …)`). Read-only; reuses yahoo_mail's body/decode helpers. Used by background scanners. |
| `expenses.py` | Expense tracking — scans receipts across mailboxes, LLM-extracts merchant/amount/currency/category into the `expenses` collection, summaries + weekly digest. Also the structured data access (`query_range`/`serialize_expense`/`summary_data`/`timeseries_data`) behind the dashboard API. |
| `api.py` | Read-only dashboard JSON API mounted at `/api` (expenses list/summary/timeseries/categories). Gated by `DASHBOARD_API_KEY` (X-API-Key header); 404 when unset. |
| `audio.py` | Whisper voice transcription (always OpenAI) |
| `telegram_io.py` | `send()` with 4096-char splitting |
| `collectors/` | Phase-4 stubs (external task collectors) — interface only |

### Data model (MongoDB collections)
`projects`, `tasks`, `events` (undo log), `chat_history` (last ~10 msgs/chat),
`reminders`, `recurring_reminders` (cron-style repeating pings), `memory` (freeform facts),
`system_state` (last/next proactive-nudge time, nudge cadence, `last_app_scan_uid`),
`seen_jobs`, `applications` (tracked job applications), `expenses` (logged from receipts),
`profile`, `disambiguation` (unused stub). `system_state` also holds per-mailbox receipt scan
cursors (`receipt_cursor_<source>`) and the application scan cursor (`last_app_scan_uid`).

### Tools the LLM can call
Local: `create_project`, `add_task`, `mark_done`, `update_task`, `snooze`,
`get_status`, `set_reminder`, `set_recurring_reminder`, `list_reminders`, `cancel_reminder`,
`complete_reminder`, `snooze_reminder`, `set_reminder_cadence`, `remember`, `forget`,
`set_job_profile`, `find_jobs`, `web_search`, `web_fetch`, `list_applications`,
`track_application`, `update_application`, `check_job_replies`, `list_expenses`,
`expense_summary`, `add_expense`, `check_receipts`. When `YAHOO_MAIL_ENABLED`, also
`yahoo_search`/`yahoo_read` (read-only Yahoo Mail over IMAP). MCP tools added at runtime, `google__…`.

**Web search/fetch** (`web.py`): `web_search` picks a backend via `WEB_SEARCH_PROVIDER`
(`auto` → Tavily key, else Brave key, else keyless DuckDuckGo). `web_fetch` needs no key
(httpx GET + regex HTML→text, truncated to 6k chars). DuckDuckGo fallback is best-effort and
often blocked from the VPS's datacenter IP — set `TAVILY_API_KEY` (free tier) for reliability.
The prompt tells the model to search-then-fetch for anything current/external rather than guess.

**Job hunter** (`jobs.py`): pulls software/remote postings from free sources (RemoteOK,
Remotive, We Work Remotely RSS — no API keys, no scraping). `find_jobs` fetches new
postings (deduped against the `seen_jobs` collection); the model ranks them against the
user's saved CV/criteria (`profile` collection, injected into the prompt) and surfaces
matches. `set_job_profile` saves CV + criteria. The `profile` doc also holds `manual` (Blaise's
authored operating manual — goals, projects, priorities, how to work with him), injected
into the system prompt by `build_system_prompt` as authoritative context. Daily "job drop" digest (`send_job_digest`,
`scheduler.schedule_job_digest`, `JOB_DIGEST_HOUR`/`MINUTE`, only sends if new matches);
`/jobs` triggers on demand. Add more sources by adding a fetcher to `jobs.py`; paid
search API (Tavily/Brave/SerpAPI) is the planned breadth upgrade.

**Persistent reminders:** a fired reminder does NOT auto-complete — it goes to
`status="awaiting_ack"` and a follow-up loop (`scheduler._reminder_followup_job`,
quiet-hours-aware) keeps re-nudging until the user confirms. `complete_reminder`
(called when the user says "done/sorted") marks it `done` and stops nudges. Reminder
statuses: pending → awaiting_ack → done (or cancelled).
- **One at a time, never a clump:** the follow-up loop wakes every `_FOLLOWUP_WAKE_MINUTES`
  (15) and nudges about the SINGLE most-overdue unconfirmed reminder per wake; a backlog
  trickles out across cycles instead of dumping 5-8 pings at once. Per-reminder `next_nudge_at`
  gates when each is next due.
- **Cadence is user-adjustable:** "space them apart / stop nagging so often" → `set_reminder_cadence`
  stores `nudge_interval_minutes` per chat in `system_state` (clamped 30–1440 by
  `clamp_followup_minutes`); `_followup_minutes(chat_id)` reads it, falling back to
  `REMINDER_FOLLOWUP_MINUTES`. Both `_fire_reminder` and the follow-up loop use it.
- **"Remind me later" → `snooze_reminder`:** pushes `next_nudge_at` (awaiting_ack) or re-schedules
  `fire_at` (pending) by N minutes (default 60); omit `query` to push all outstanding.
- **Recurring reminders** (`set_recurring_reminder`): cron-style repeating pings ("every weekday
  at 8") in the `recurring_reminders` collection, fired by `scheduler._fire_recurring` via a
  `CronTrigger` (job id `recurring:<id>`), re-registered on startup by `load_recurring_reminders`.
  These just ping each occurrence — NO awaiting_ack/follow-up nag. `list_reminders` shows them;
  `cancel_reminder` matches across one-off + recurring and deactivates whichever fits best.

**Inline buttons (callback queries):** fired reminders and follow-up nudges carry
`✅ Done · ⏰ 1h · ⏰ 3h` buttons; proactive heartbeat nudges carry `✅ Done · 🔕 Not now` for the
one task they raised. `telegram_io.send(..., buttons=[(label, callback_data)])` attaches a
single-row keyboard to the final chunk. Taps arrive as `update.callback_query` → `main._handle_callback`,
which parses compact `kind:action:id[:arg]` data (`rem:done`, `rem:snz:<id>:<min>`, `tsk:done`,
`tsk:mute:<id>:<days>`), routes to the executor's by-id actions (`complete_reminder_by_id`,
`snooze_reminder_by_id`, `mark_done_by_id`, `mute_task_nudge_by_id`), answers the callback (toast),
and edits the message to strip the keyboard so it can't be tapped twice. `mute_task_nudge_by_id`
pushes `last_nudged_at` forward (pauses nudges without hiding the task).

**Proactive nudges (autonomy heartbeat):** the bot decides — on its own judgment — the single
most-urgent open task and pings about THAT ONE only (`run_heartbeat` ranks via `_ranked`, the
LLM picks one or replies `SILENT`). It then suppresses ONLY the task it nudged (`_suppress_after_nudge`
fuzzy-matches the reply to a title, falling back to the top-ranked) so the next heartbeat is free
to raise the next-urgent one — nudges trickle out one at a time. Spacing is spontaneous: after a
nudge it records `next_proactive_at = now + random(NUDGE_MIN_GAP_MINUTES..NUDGE_MAX_GAP_MINUTES)`
rather than a fixed cooldown. `HEARTBEAT_MINUTES` (the check cadence) must be ≤ `NUDGE_MIN_GAP_MINUTES`.
The old `NUDGE_COOLDOWN_HOURS` is gone — replaced by the min/max gap pair.

**Mission framing:** the system prompt frames the bot as genuinely invested in the user's
productivity and EARNING — remember goals/clients/deadlines, prioritise by them, and don't
let commitments silently drop. Completion always requires the user's explicit word.

### Bot commands (fallbacks / manual triggers)
`/start` `/status [work|personal]` `/undo` `/done <task>` `/add <project> | <task>`
`/snooze <task> until <YYYY-MM-DD>` `/checkin` (force heartbeat) `/brief` (force morning
digest) `/winddown` (force evening digest) `/jobs` (force job drop) `/applications` (list tracked
job applications) `/expenses` (this month's spending summary).

## Conventions & gotchas (read before editing)

- **Do NOT run git commit/push.** Blaise handles all git operations himself. You may
  edit files and run tests, never commit.
- **Env var names are canonical and final:** `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_ID`
  (NOT the old `TELEGRAM_TOKEN`/`ALLOWED_USER_ID` — fallbacks were removed). `.env.example`
  is authoritative and must match `config.py` exactly.
- **Personality is for the BOT, not for you.** The `PERSONALITY` constant (witty,
  dry, sarcastic) governs LLM-generated bot text (chitchat replies, autonomy briefs,
  digests, reminder firing). Your own responses to Blaise stay normal/clear.
- **Replies are produced by the tool loop, in voice.** The executor returns flat
  deterministic strings (e.g. `Created project "X"`) as the FACTS the model sees; the
  model then writes the natural final reply itself (it has the PERSONALITY system prompt).
  No separate narration pass anymore — the loop's final text is the reply. If the model
  ends with no text, `_handle_nl` falls back to the joined tool results, else "Done."
  Cost: a tool-using turn is now multiple LLM rounds (one per tool step) — accepted for
  the capability (chaining) and natural language.
- **"Most recent event" queries sort by `_id` desc, not `created_at`** — in-process
  timestamps collide at ms resolution; ObjectId is monotonic. Use the cursor pattern
  `.find().sort(...).limit(1).to_list(1)`, NOT `find_one(sort=...)` (mongomock ignores it).
- `undo_last` uses `replace_one(upsert=True)` so it restores deletes (e.g. `forget`)
  as well as updates.
- **Outgoing text is plain text.** Telegram messages are sent with no `parse_mode`
  (free-form LLM output can't be safely escaped into MarkdownV2 without frequent send
  failures). `telegram_io._to_plain()` strips Markdown + LaTeX the model sometimes emits
  (`*…*`, `**…**`, `\[ … \]`, `\text{}`, `\times`, `[t](url)` …) so the user never sees raw
  markup; the system prompt also tells the model to write plain text. Don't add `parse_mode`
  unless you also add robust escaping. NB: `build_system_prompt` is one big f-string — literal
  braces in prompt text must be doubled (`{{}}`) or it's a SyntaxError.
- Scheduled/LLM features (reminders, digest) always have a **plain-text fallback** if
  the LLM call fails — a missed reminder is worse than a missed joke.
- `datetime.utcnow()` is used widely and emits deprecation warnings on 3.12+. Harmless
  for now; migrate to `datetime.now(timezone.utc)` if you touch that code.

## Run / test / deploy

```bash
# Tests (mongomock-motor, no real DB or API needed)
source venv/bin/activate && pytest -q          # currently 66 tests, keep green

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

**Done & live:** projects/tasks, reminders (consolidated nudges + NL snooze/cadence controls),
memory, voice, images, web search/fetch, autonomy heartbeat, morning digest, MCP client layer (code).

**Gmail + Calendar via MCP — DONE & LIVE (2026-06-14):** read-only, working in
production. See the "Google … LIVE" section above for the full wiring and re-auth runbook.

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
