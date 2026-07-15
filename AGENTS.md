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
**First scan only sets a baseline UID — it tracks forward, never trawls history.**

**Sent-folder scanning** (`scan_sent`, also run by `gather_application_update`): scans the SENT
folder of every configured mailbox (multi-account via `imap_mail`; Yahoo `Sent`, Gmail
`[Gmail]/Sent Mail`) and LLM-classifies outgoing mail as `application` (the user applied for a job
by email → start tracking at `applied`, source `<src>:sent`) or `other`. Per-mailbox cursor
`sent_app_cursor_<source>`, baseline-forward on first run. NOTE: the INBOX reply-scan (`scan_inbox`)
is still **Yahoo-only**, so replies to applications the user sent from Gmail won't auto-update yet —
extend `scan_inbox` to `imap_mail` multi-account if that's needed. `send_application_update` proactively reports new tracked apps, status changes,
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
and inserts into `expenses`. **The classifier logs money paid to a third-party merchant only**: for bank/mobile-money alerts
it counts a debit that PAYS a merchant (card/POS purchase, bill, subscription) but excludes credits,
deposits, money received, P2P transfers, refunds, reversals, declined txns, OTPs, balance notices,
AND the user moving their own money between their own accounts/wallets — bank↔mobile-money transfers,
wallet top-ups / "pull" txns (e.g. "CalPay MTN Pull"), and ATM/cash withdrawals (these debit the
account but aren't purchases — this was the real cause of the GHS 4,000 row). `_is_duplicate` also
drops a same merchant+amount+currency same-day repeat (banks sometimes send two alerts per txn). Per-mailbox UID cursors (`receipt_cursor_<source>`); first scan of a
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
**Categories & the charity bug**: `_CATEGORIES` includes `transfers` (P2P money sent to people whose
purpose can't be inferred). `charity` is STRICTLY genuine donations to organisations — the retired
d174136-era statement importer defaulted unknown P2P sends to "charity", polluting 22 rows (fixed by
a description-based recategorisation backfill on 2026-07-12; the scan prompt now forbids it too).
**MoMo provenance on expenses**: momo-sourced rows carry `momo_ref` (= raw statement `F_ID`),
`reference` (the human-typed `REF` shorthand, alias-resolvable), and `counterparty` (`TO NAME` /
alias name), all exposed by `serialize_expense` (null on non-momo rows). NOTE the naming trap:
expense `momo_ref` = statement column `F_ID`; the statement column literally named `REF` is the
typed shorthand, stored on expenses as `reference`.
**MoMo statement import** (`statements.import_momo_statement`, tool `import_momo_statement`): finds the
MoMo PDF in the inbox and saves the FULL statement FAITHFULLY into the `momo_transactions` collection.
Uses `pdfplumber` (dep — must be `pip install`ed in the server venv; the deploy restarts but may not
reinstall requirements) with the DEFAULT ruled-line `extract_tables()` (the PDF has grid lines → clean
16-cell rows; the text/flatten path mangles it — don't use it). Saves EVERY transaction (money in AND
out) verbatim: each doc = `{chat_id, source_file, statement_period, columns (exact 16 names), values
(raw cells, None→"", wrapped cells keep \n), f_id, imported_at}`. **No LLM, no content alteration** —
this is the canonical raw store; a categorised expense view is derived later. Dedup by `f_id` (the
statement's own transaction ID). The columns are exactly: TRANSACTION DATE · FROM ACCT · FROM NAME ·
FROM NO. · TRANS. TYPE · AMOUNT · FEES · E-LEVY · BAL BEFORE · BAL AFTER · TO NO. · TO NAME · TO ACCT ·
F_ID · **REF** (the purpose narration — e.g. `G`=MaryJ, person-name=gift) · OVA. `add_momo_alias`
(+ `_DEFAULT_ALIASES`/`profile.momo_aliases`, `charity` category) are kept for the future derived view.

## Dashboard API (`api.py`)

Read-only JSON for an external spending dashboard, mounted at `/api` on the same FastAPI app
(so it's served through the existing Apache proxy at the bot's domain — Apache must proxy `/api`
too; if it proxies `/` to uvicorn it already does). **Gated by `DASHBOARD_API_KEY`**: every route
needs the `X-API-Key` header to match; if the key is unset the API is fully disabled (404) — never
expose financial data unauthenticated. CORS is restricted by `DASHBOARD_ORIGINS` (GET only).
Routes (all scoped to `ALLOWED_CHAT_ID`): `GET /api/health`, `/api/expenses`
(`period|start|end|category|limit`), `/api/expenses/summary`, `/api/expenses/timeseries`
(`bucket=day|week|month`), `/api/expenses/categories`, and `/api/applications` (optional
`status` filter) → `{count, by_status, cv_on_file, applications:[{id, company, title, status,
status_label, applied_at, last_update_at, source, mailbox, mailbox_url, cv_used, notes,
last_message_body, last_message_snippet, last_message_from, last_message_direction (inbound|outbound),
last_message_at, optional messages[], and `suggested_action` {headline, summary, steps[], priority, generated_at} | null}]}`.
`mailbox_url` is a webmail deep *search* link (IMAP has no stable per-message URL); `cv_used` is
the attached CV filename captured from sent applications; `cv_on_file` is the profile CV.
Message content is captured at scan time onto the application (`_attach_message`: full plain-text body,
quoted history stripped, capped 10k; thread kept to last 15) so the API never hits IMAP live.
`_attach_message` also generates `suggested_action` (`_suggested_action`: LLM next-action from the
latest inbound body — null for outbound/auto-acks/dead-end rejections, null on LLM error, never raises);
the API just serializes the stored value.
`applications.backfill_application_messages()` fills message content for pre-existing apps via
`last_email_uid` (folder/direction derived: `:sent`→that account's Sent/outbound, else Yahoo inbox/inbound).
Board endpoints (`board.py`): `/api/tasks` (`status`/`scope` filters → `{count, by_status, tasks[],
tree[]}`) — the flat `tasks[]` respects the status filter and each row carries its hierarchy relations
(`parent_task_id`/`parent_title` for steps; `is_goal` + `steps_done`/`steps_total` for goals, via
`hierarchy_maps()` over ALL tasks so counts stay truthful under filtering); `tree[]` is the nested
goal→steps view (`task_tree_view`, scope-filtered but ALL statuses so progress is honest — status
filtering is the flat list's job; task rows also carry `remind_at`/`reminded_at` since the merge).
`/api/reminders` is a **compatibility view over timed tasks** (2026-07-14 merge — there is no
reminders collection read anymore): same `{count, by_status, reminders[], recurring[]}` shape,
statuses DERIVED back to the old vocabulary (`open`+`reminded_at` → `awaiting_ack`, plain `open` →
`pending`, `done`/`cancelled` pass through), `status=active` pseudo-filter = still-open timed
tasks. `/api/overview` (`{tasks, reminders, reminders_active, goals:{count, steps_done,
steps_total}, focus, projects}` rollup). `period` is today|week|month|all; explicit
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

## Self-updating user model (`user_model.py`)

Beyond freeform `memory` facts (which it recalls), the bot maintains an *evolving inferred portrait*.
`scheduler._user_model_job` runs daily (`USER_MODEL_HOUR`/`MINUTE`, gated by `USER_MODEL_ENABLED`):
`synthesize_user_model` gathers the operating manual, saved memory, the previous model, and ACTIVITY
signals (open/stalled/recently-done tasks, reminders repeatedly left undone, job-application
follow-through), and the LLM distils a concise WHO / WORKING-ON / GOALS / PATTERNS summary into
`profile.user_model`. `build_system_prompt` injects it as "Your evolving read on the user" (the manual
stays the authoritative layer; this is the bot's working inference). Scope is constrained to
work/goals/productivity — the prompt forbids inferring sensitive personal matters. Surfaced via
`/whoami` (view, synth on first use) and the `refresh_user_model` tool ("update what you know about me").
This is recall→understanding; the prompt tells the model to use it to personalise and prioritise. (Memory
is still injected wholesale — retrieval/top-k is the future upgrade once it grows.)

## Core architecture

NL write path: **Telegram → FastAPI webhook → load chat history + store snapshot →
agentic tool LOOP → bot replies.**

The loop (`llm.run_tool_loop`, used by `main._handle_nl`): the model calls tools, sees
the results, and can call MORE tools before answering — repeating until it produces a
final reply or hits `max_iters` (`_handle_nl` passes 10, for headroom on self-directed research
that chains web_search → web_fetch → act; library default is 6). This is what lets it CHAIN calls
(e.g. search Gmail for ids → fetch each body → summarise, or research a how-to then do it).
The system prompt tells it to RESEARCH (web tools) rather than say "I don't know / I can't", and to
add a proactive tip only when it clears a high relevance bar (else stay silent). `_handle_nl` passes an `execute(name, args)`
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
| `task_tree.py` | Pure helpers for the goal→step task tree (`build_forest`, `active_forest_lines`, progress) — one source of truth for tree rendering across snapshot/status/digest/board |
| `prompts.py` | `build_system_prompt()` (injects date/time, store snapshot, reminders, memory) + `PERSONALITY` constant |
| `scheduler.py` | APScheduler: timed-task pings + follow-up nags, heartbeat interval, digest crons |
| `migrations.py` | Idempotent startup migrations — 2026-07-14 reminders→tasks copy (keeps ObjectIds; legacy collection left as backup) |
| `autonomy.py` | Proactive heartbeat — ranks open tasks by urgency, fenced to today's focus slate, LLM picks ONE to nudge (or SILENT); suppresses only that task; spontaneous jittered spacing |
| `focus.py` | Daily focus — commits the day's 3-4 task slate (carryovers first, LLM-judged fill), fences heartbeat nudges to it, discloses deadline overflow |
| `digest.py` | Morning digest — daily rundown, always sends |
| `mcp_client.py` | Generic MCP client — connect, namespace (`server__tool`), route calls |
| `web.py` | Web search (Tavily/Brave/DuckDuckGo) + page fetch (httpx, dependency-free HTML→text). No DB writes. |
| `yahoo_mail.py` | Yahoo Mail read-only over IMAP (`imaplib`, app password). `yahoo_search`/`yahoo_read`/`fetch_recent`; blocking work in `asyncio.to_thread`. Read-only enforced (readonly select + BODY.PEEK). |
| `applications.py` | Job-application tracking — scans the inbox, LLM-classifies confirmations/replies, upserts the `applications` collection, proactively reports changes + stale follow-ups. |
| `imap_mail.py` | Generic multi-account IMAP batch reader (`mail_accounts()` → Yahoo + Gmail; `fetch_recent(account, …)`). Read-only; reuses yahoo_mail's body/decode helpers. Used by background scanners. |
| `expenses.py` | Expense tracking — scans receipts across mailboxes, LLM-extracts merchant/amount/currency/category into the `expenses` collection, summaries + weekly digest. Also the structured data access (`query_range`/`serialize_expense`/`summary_data`/`timeseries_data`) behind the dashboard API. |
| `statements.py` | MoMo / mobile-money statement import — pulls the PDF from the inbox (`imap_mail.find_pdf_attachment`), extracts text (`pdfplumber`, lazy import), LLM-parses SPENDING only, logs deduped by `momo_ref` (source `momo`). Tool `import_momo_statement`. |
| `api.py` | Read-only dashboard JSON API mounted at `/api` (expenses + applications). Gated by `DASHBOARD_API_KEY` (X-API-Key header); 404 when unset. |
| `user_model.py` | Self-updating user model — daily LLM reflection over memory + activity → an evolving WHO/WORKING-ON/GOALS/PATTERNS summary stored on `profile.user_model`, injected into every prompt. |
| `board.py` | Structured read access to tasks + reminders for the dashboard API (query/serialize/counts, goal→step `hierarchy_maps`/`task_tree_view`, overview rollup). |
| `audio.py` | Whisper voice transcription (always OpenAI) |
| `telegram_io.py` | `send()` with 4096-char splitting |
| `collectors/` | Phase-4 stubs (external task collectors) — interface only |

### Data model (MongoDB collections)
`projects`, `tasks` (optional `parent_task_id` → goal/step tree; optional `remind_at` +
`reminded_at`/`next_nudge_at`/`nudge_count` = a timed ping — "reminders" merged into tasks
2026-07-14; statuses open/done/snoozed/cancelled), `events` (undo log), `chat_history` (last ~10 msgs/chat),
`reminders` (LEGACY — migrated into tasks at startup by `migrations.py`, kept only as a backup,
nothing reads it), `recurring_reminders` (cron-style repeating pings), `daily_focus` (one doc per
local day — today's committed 3-4 task slate; see focus.py), `memory` (freeform facts),
`system_state` (last/next proactive-nudge time, nudge cadence, `last_app_scan_uid`),
`seen_jobs`, `applications` (tracked job applications), `expenses` (logged from receipts),
`momo_transactions` (full MoMo statement, verbatim), `profile`, `disambiguation` (unused stub). `system_state` also holds per-mailbox receipt scan
cursors (`receipt_cursor_<source>`) and the application scan cursor (`last_app_scan_uid`).

### Tools the LLM can call
Local: `create_project`, `add_task` (optional `remind_at` = timed ping; optional `project_name`,
defaults to the Inbox project), `mark_done`, `cancel_task`, `set_task_parent`, `update_task`, `snooze`,
`get_status`, `get_recap`, `set_daily_focus`, `set_recurring_reminder`, `list_reminders`,
`snooze_reminder`, `set_reminder_cadence`, `remember`, `forget`,
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

**Timed pings — tasks and reminders are ONE entity (merged 2026-07-14):** a "reminder" is a
task with `remind_at` set (naive UTC, like every task datetime). `add_task` with `remind_at`
creates it (no `project_name` → the auto-created **Inbox** project; `due_date` defaults to the
ping's local day so due/overdue/focus logic just works); `scheduler.schedule_reminder` registers
the APScheduler job (id `reminder:<task_id>` — same format as pre-merge so jobs scheduled before
a deploy still resolve). Firing does NOT auto-complete: `_fire_reminder` sets `reminded_at` +
`next_nudge_at` (task stays `open` = "fired, awaiting the user's word") and the follow-up loop
(`scheduler._reminder_followup_job`, quiet-hours-aware) keeps re-nudging until the user confirms.
`mark_done` closes it (stamps `completed_at`, clears `next_nudge_at`, unschedules the job);
`cancel_task` drops it (status `cancelled` — undoable, hidden from status/recap, pulled from the
focus slate). The one thing shows up everywhere: status, focus, digests, recap, dashboard.
- **Startup migration** (`migrations.py`, called from `main.lifespan` BEFORE
  `load_pending_reminders`): every legacy `reminders` doc is copied into `tasks` keeping its
  ObjectId (idempotency check = id already present; old inline buttons keep working). Status map:
  pending → open; awaiting_ack / legacy fired → open with `reminded_at` (no `fired_at` → falls
  back to fire time) so nags continue seamlessly; done/cancelled preserved (recap history
  survives, in the Inbox project). Aware datetimes are stripped to naive UTC. The legacy
  collection is left untouched as a backup — nothing reads it.
- **No double-nagging**: `_fire_reminder` and each follow-up nag stamp `last_nudged_at` (arms the
  heartbeat's 24h suppression), and `gather_candidates` skips timed tasks entirely — the exact-time
  ping channel owns them. They're likewise excluded from focus-slate selection
  (`_open_actionables`), the add-task slate notices, and the overbooking count: a "take out the
  chicken at 4" errand must not eat a focus seat. The user can still swap one in via
  `set_daily_focus`.
- **Matching is keyword/token-based** (`_reminder_score`): a partial phrase ("gyacity images from
  brown") matches a long title via substring + word-overlap + fuzzy ratio; `_fuzzy_tasks` uses the
  same scorer, so closing works identically for timed and plain tasks.
- **`cancel_task` covers everything active** — open/snoozed tasks (fired-and-nagging pings
  included) by fuzzy match (ambiguous multi-match → be-specific list), falling back to recurring
  reminders by text score. Cancels log `prev_state` so `/undo` restores.
- **Identical twins close together** (`_close_task` / `_cancel_task`, added 2026-07-15): when
  every match shares the SAME canonical title (duplicates predating the dedup guard — e.g.
  migrated legacy reminders), "done/cancel X" acts on ALL copies instead of asking the user to
  pick between indistinguishable options (an unanswerable "be more specific"). Distinct titles
  still get the ambiguity prompt.
- **Dedup guards are deterministic, in the executor** (belt to the prompt's suspenders): `_add_task`
  refuses a near-identical (`_sim ≥ 0.85`) open/snoozed task with the SAME `parent_task_id` in the
  same project. With `remind_at` in play the guard gets smarter: a near-identical task WITHOUT a
  ping gets this ping ATTACHED (`_attach_ping` — "remind me at 4 to deploy the hotfix" when that
  task exists doesn't make a twin); one with a ping within 5 min is refused as a duplicate; pings
  at genuinely different times ("at 9 and again at 5") coexist as two tasks.
- **Task hierarchy: goals → steps** (`tasks.parent_task_id`, `task_tree.py`). A task with children is a
  "goal"; a task with `parent_task_id` set is a step. The tree is only ever TWO levels deep — filing under
  a step re-points to that step's goal (`_match_goal` flattens; `set_task_parent` refuses to move a task
  that itself has steps). `add_task` takes an optional `parent_task_query`; `set_task_parent` attaches
  ("put X under Y") or detaches (omit parent → standalone). The prompt tells the model to ASK when a new
  task plausibly belongs under an existing goal but wasn't explicitly tied to one, and to NEVER
  auto-generate steps the user didn't mention. Rendering is centralised in `task_tree.active_forest_lines`
  (goals show `(done/total)` with open steps indented) — used by the chat snapshot (`prompts.py`),
  `get_status`, and echoed in the digest (`title (Project ▸ Goal)`) and board (`parent_task_id` field).
  Completion cascades in `_do_close_task`: closing a GOAL closes its open steps (with notice); closing a
  goal's LAST open step nudges to finish the goal but does NOT auto-close it (completion stays the user's
  call). Both the chat path and the by-id button path (`mark_done_by_id`) route through `_do_close_task`.
- **Task matching upgraded** (`_fuzzy_tasks` + `_task_strong`): tasks now match via substring / token-overlap
  (like reminders) instead of bare `_sim ≥ 0.4`. The old whole-string ratio floats ~0.5 for unrelated
  titles sharing a stop-word ("Deploy the website" vs "prep the ENV vars" = 0.514), which made closing a
  goal spuriously ambiguous. `_task_strong` requires a substring or ≥0.6 word-overlap for a STRONG match;
  raw `_sim ≥ 0.6` is only a single-best fuzzy-typo fallback. Returns all strong matches (→ ambiguity
  prompt on genuine near-dupes) else the one best.
- **Exact-title short-circuit** (`_fuzzy_tasks`, first check): a query that IS a task's title
  (case/whitespace/trailing-period insensitive) returns that task alone. Without it, two titles differing
  by one word ("Add Sway to portfolio website" vs "…your portfolio website") are BOTH strong for any
  phrase — including each other's exact titles — so the "be more specific" prompt looped forever with no
  answer that could resolve it (the Telegram infinite-clarification bug). Paired prompt rules: when the
  user picks from a numbered disambiguation list, re-call the tool with that item's EXACT title verbatim
  (never the bare number — positional replies match nothing, there's no pending-choice state); and never
  ask the same clarifying question twice — on a second ambiguous result, surface the near-dupes and how
  they differ instead of re-asking.
- **Completion recap** (`get_recap` / `_get_recap`): "brief me on what I completed this week / the last
  2 days" → the model calls `get_recap(days)` (default 7, clamped 1–90). It lists tasks marked done in the
  window grouped by project (steps annotated `step of "Goal"`, each with its finish day) — since the merge
  that includes confirmed timed pings (migrated history lands under `[personal] Inbox`), and the model
  narrates it. `_do_close_task` stamps `completed_at` on the task AND any cascade-closed steps (naive UTC,
  like all task datetimes); done rows predating 2026-07-14 lack it, so the recap query falls back to
  `updated_at` for them (`completed_at: None` matches missing too) — for a done task that IS the closing
  moment. `_finished_when` still tolerates aware datetimes on migrated rows (normalises to naive; a
  mixed-tz Mongo `$gte` would misbehave under mongomock). Backward-looking only: `get_status` stays the
  "what's still open" view.
- **Closing clears `next_nudge_at` + unschedules the job** (`_do_close_task`, `cancel_task`, and the
  by-id button paths all route through `_unschedule_ping`), and `_fire_reminder` refuses to fire for a
  task that isn't open, was deleted (/undo), or already pinged — together these stop a closed ping from
  being resurrected/re-nudged. (Known edge: /undo of a cancelled PENDING ping restores the doc but not
  the APScheduler job — the next restart's `load_pending_reminders` re-registers it.)
- **One at a time, never a clump:** the follow-up loop wakes every `_FOLLOWUP_WAKE_MINUTES`
  (15) and nags about the SINGLE most-overdue fired-but-unconfirmed timed task per wake; a backlog
  trickles out across cycles instead of dumping 5-8 pings at once. Per-task `next_nudge_at`
  gates when each is next due.
- **Cadence is user-adjustable:** "space them apart / stop nagging so often" → `set_reminder_cadence`
  stores `nudge_interval_minutes` per chat in `system_state` (clamped 30–1440 by
  `clamp_followup_minutes`); `_followup_minutes(chat_id)` reads it, falling back to
  `REMINDER_FOLLOWUP_MINUTES`. Both `_fire_reminder` and the follow-up loop use it.
- **"Remind me later" → `snooze_reminder`:** pushes `next_nudge_at` (already fired) or moves
  `remind_at` and reschedules the job (pending) by N minutes (default 60); omit `query` to push all
  outstanding. A pending push that crosses into a later day drags `due_date` along. Distinct from
  `snooze` (date-based, hides a plain task from digests until then).
- **Recurring reminders stay their own thing** (`set_recurring_reminder`): cron-style repeating
  pings ("every weekday at 8") in the `recurring_reminders` collection, fired by
  `scheduler._fire_recurring` via a `CronTrigger` (job id `recurring:<id>`), re-registered on
  startup by `load_recurring_reminders`. Deliberately NOT merged into tasks — they're schedules,
  not work items (no done state; folding them in would spawn a task per occurrence). They just
  ping each time — no awaiting-ack/follow-up nag. `list_reminders` shows them; `cancel_task`
  falls back to them by text match and deactivates whichever fits.
- **Startup reload**: `load_pending_reminders(chat_id)` re-registers a job for every open,
  not-yet-fired timed task (ones that came due while down fire immediately); fired-unconfirmed
  ones need no job — the follow-up loop owns them.

**Inline buttons (callback queries) — SENDING REMOVED, handlers kept:** as of 2026-07-11 the owner
asked for no more buttons ("I'll just text to reply"), so NO outgoing message attaches a keyboard
anymore — fired reminders, follow-up nudges, and heartbeat nudges all send plain text. Do not re-add
`buttons=` to sends. The receive side is deliberately kept alive, because messages already in the chat
history retain their keyboards forever and a tap on an old one must keep working:
`telegram_io.send(..., buttons=…)` still supports attaching a single-row keyboard, and taps arrive as
`update.callback_query` → `main._handle_callback`,
which parses compact `kind:action:id[:arg]` data (`rem:done`, `rem:snz:<id>:<min>`, `tsk:done`,
`tsk:mute:<id>:<days>`), routes to the executor's by-id actions (`complete_reminder_by_id`,
`snooze_reminder_by_id` — since the merge these resolve the id in the TASKS collection; migrated
reminders kept their ObjectId precisely so these old buttons still work — plus `mark_done_by_id`,
`mute_task_nudge_by_id`), answers the callback (toast),
and edits the message to strip the keyboard so it can't be tapped twice. `mute_task_nudge_by_id`
pushes `last_nudged_at` forward (pauses nudges without hiding the task).

**Proactive nudges (autonomy heartbeat):** the bot decides — on its own judgment — the single
most-urgent open task and pings about THAT ONE only (`run_heartbeat` ranks via `_ranked`, the
LLM picks one or replies `SILENT`). It then suppresses ONLY the task it nudged (`_suppress_after_nudge`
fuzzy-matches the reply to a title, falling back to the top-ranked) so the next heartbeat is free
to raise the next-urgent one — nudges trickle out one at a time. Spacing is spontaneous: after a
nudge it records `next_proactive_at = now + random(NUDGE_MIN_GAP_MINUTES..NUDGE_MAX_GAP_MINUTES)`
rather than a fixed cooldown. `HEARTBEAT_MINUTES` (the check cadence) must be ≤ `NUDGE_MIN_GAP_MINUTES`.
The old `NUDGE_COOLDOWN_HOURS` is gone — replaced by the min/max gap pair. Gap defaults raised
30-150 → 45-180 on 2026-07-14 (owner asked for slightly fewer pings).

**Daily focus (`focus.py`, added 2026-07-14):** each day commits a slate of 3-4 tasks
(`daily_focus` collection, one doc per local date) and **heartbeat nudges are FENCED to that
slate** — the fix for "four different projects pinged back-to-back". Mechanics:
- **Selection** happens at the first `ensure_today_focus` of the day (normally the morning
  digest; an early heartbeat also triggers it). Carryovers — still-open tasks from the previous
  slate — keep their seats first; remaining seats fill from the urgency-ranked backlog (overdue →
  due today → dated → most-stalled undated). The **LLM is consulted only when there's a real
  choice** (candidates > 3): it judges which 3-vs-4 fit the day (weighs effort, deadlines, goals);
  any LLM failure/garbage falls back to deterministic top-3. `allow_llm=False` forces the
  deterministic path — the EXECUTOR always uses it (brain proposes, executor disposes).
- **Actionable units**: a goal with open steps is represented by its steps, never itself.
- **Overflow, never silence**: overdue/due-today tasks that miss the slate land in
  `overflow_ids`; the morning digest discloses them with a swap offer, and they're first in line
  for next-up suggestions. The slate never auto-expands past `MAX_SLATE = 4` — too many same-day
  deadlines is surfaced as a scheduling conflict, not absorbed.
- **Executor integrations**: `_do_close_task` appends a "that clears today's focus" + up-to-2
  deterministic next-up suggestions when the last slate task closes (suggestions are OFFERS —
  nothing joins the slate without the user's word, enforced by a prompt rule); `_add_task`
  appends notices when a new task is due TODAY but the slate is set (offer to swap) or when a
  date accumulates ≥4 open tasks (overbooking warning). `set_daily_focus` tool = the user's
  explicit override: add / remove / swap (resolves both queries BEFORE mutating, so a failed
  swap changes nothing); no args → show the slate.
- **Surfaces**: system-prompt snapshot gets a "Today's focus" section (read-only — building a
  prompt never triggers selection); morning digest leads with the slate; evening digest opens
  with the scoreboard (done/total, what carries over) + an overbooked-tomorrow heads-up (>4 due).
- **Timed tasks are outside the fence AND outside the slate** — a task with `remind_at`
  pings at its exact time regardless of the slate, and is excluded from slate selection
  (`_open_actionables`) so errand pings don't eat focus seats (swap one in explicitly via
  `set_daily_focus` if wanted).
- Testing note: focus's LLM patch point is `agentzero.focus.get_provider`; existing autonomy/
  digest tests stay LLM-free because their small seeds (≤3 candidates) take the deterministic path.

**Mission framing:** the system prompt frames the bot as genuinely invested in the user's
productivity and EARNING — remember goals/clients/deadlines, prioritise by them, and don't
let commitments silently drop. Completion always requires the user's explicit word.

### Bot commands (fallbacks / manual triggers)
`/start` `/status [work|personal]` `/undo` `/done <task>` `/add <project> | <task>`
`/snooze <task> until <YYYY-MM-DD>` `/checkin` (force heartbeat) `/brief` (force morning
digest) `/winddown` (force evening digest) `/jobs` (force job drop) `/applications` (list tracked
job applications) `/expenses` (this month's spending summary) `/whoami` (the bot's evolving read on you).

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
