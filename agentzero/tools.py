"""
Tool definitions in provider-neutral JSON Schema format.
Each provider adapter in llm.py translates these to its own wire format.
"""
from agentzero.config import YAHOO_MAIL_ENABLED

TOOLS: list[dict] = [
    {
        "name": "create_project",
        "description": "Create a new project to organise tasks under.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "scope": {
                    "type": "string",
                    "enum": ["work", "personal"],
                    "description": "Whether this is a work or personal project",
                },
            },
            "required": ["name", "scope"],
        },
    },
    {
        "name": "add_task",
        "description": "Add a task to an existing project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name of the project (fuzzy matched)",
                },
                "title": {"type": "string", "description": "Task title"},
                "due_date": {
                    "type": "string",
                    "description": "Optional due date in YYYY-MM-DD format",
                },
            },
            "required": ["project_name", "title"],
        },
    },
    {
        "name": "mark_done",
        "description": "Mark an open task as completed.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_query": {
                    "type": "string",
                    "description": "Task title or keyword to find the task (fuzzy matched)",
                }
            },
            "required": ["task_query"],
        },
    },
    {
        "name": "update_task",
        "description": "Update a task's title or due date.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_query": {
                    "type": "string",
                    "description": "Task title or keyword to find the task (fuzzy matched)",
                },
                "new_title": {"type": "string", "description": "New title for the task"},
                "new_due_date": {
                    "type": "string",
                    "description": "New due date in YYYY-MM-DD format",
                },
            },
            "required": ["task_query"],
        },
    },
    {
        "name": "snooze",
        "description": "Snooze a task until a specified date so it is hidden from digests until then.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_query": {
                    "type": "string",
                    "description": "Task title or keyword to find the task (fuzzy matched)",
                },
                "until": {
                    "type": "string",
                    "description": "Date to snooze until in YYYY-MM-DD format",
                },
            },
            "required": ["task_query", "until"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Store a durable fact about the user worth remembering across "
            "conversations — preferences, personal details, important dates, people, "
            "ongoing context, habits. Call this PROACTIVELY whenever the user shares "
            "something lasting, even if they never say 'remember this'. Do not store "
            "transient or task-like items (use add_task / set_reminder for those)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact, phrased concisely as a standalone statement",
                },
                "category": {
                    "type": "string",
                    "description": "Optional grouping, e.g. 'preference', 'personal', 'work', 'people'",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "forget",
        "description": "Remove a remembered fact that's no longer true or that the user asks you to forget.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword/phrase identifying the memory to remove (fuzzy matched)",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "set_reminder",
        "description": (
            "Set a one-off reminder that pings the user at a specific time. "
            "Use for any 'remind me to X in N minutes/hours', 'remind me at 3pm', "
            "or 'remind me tomorrow morning to X'. This is standalone — it does NOT "
            "need a project. Always resolve the time to an absolute timestamp using "
            "the current local time given in the system prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "What to remind the user about, phrased as the reminder",
                },
                "fire_at": {
                    "type": "string",
                    "description": (
                        "Absolute local time in ISO 8601 (YYYY-MM-DDTHH:MM:SS). "
                        "Compute this from the current local time in the system prompt — "
                        "e.g. if it's 14:30 and the user says 'in two minutes', use 14:32."
                    ),
                },
            },
            "required": ["text", "fire_at"],
        },
    },
    {
        "name": "set_recurring_reminder",
        "description": (
            "Set a REPEATING reminder that fires on a schedule — for 'every weekday at 8', "
            "'remind me every Monday to send invoices', 'daily at 9pm'. Unlike set_reminder "
            "(a single ping that nags until confirmed), a recurring reminder just pings each "
            "time it comes round. Resolve the time of day to a 24h hour/minute in local time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind the user about."},
                "hour": {"type": "integer", "description": "Hour of day, 0-23, local time."},
                "minute": {"type": "integer", "description": "Minute of the hour, 0-59 (default 0)."},
                "day_of_week": {
                    "type": "string",
                    "description": (
                        "Which days, APScheduler cron style: '*' = every day, 'mon-fri' = "
                        "weekdays, 'sat,sun' = weekends, or a comma list of lowercase 3-letter "
                        "codes like 'mon,wed,fri'. Default '*'."
                    ),
                },
            },
            "required": ["text", "hour"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the user's upcoming one-off and recurring reminders.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder the user no longer wants (it won't fire). Different from completing one.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to find the reminder (fuzzy matched)",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "complete_reminder",
        "description": (
            "Mark a reminder as DONE when the user confirms they've handled it "
            "(e.g. 'done', 'sorted that', 'I called the bank'). This stops the "
            "follow-up nudges. Use this — not cancel_reminder — when the thing was "
            "actually completed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase identifying which reminder was completed (fuzzy matched)",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "set_job_profile",
        "description": (
            "Save or update the user's job-hunting profile — their CV/background and what "
            "roles they're looking for. Call this when the user shares their CV or describes "
            "the kind of job they want. Used to match against postings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cv": {"type": "string", "description": "The user's CV / background summary (full text is fine)"},
                "criteria": {
                    "type": "string",
                    "description": "What they're looking for: role, stack, remote/location, seniority, salary floor, etc.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_jobs",
        "description": (
            "Fetch fresh software/remote job postings from job boards (RemoteOK, Remotive, "
            "We Work Remotely). Returns new postings; then YOU rank them against the user's "
            "saved CV/criteria (shown in the prompt) and present the best matches with a one-"
            "line fit reason and the apply link. Skip weak matches."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional keywords to filter by (e.g. 'python backend', 'react remote'). Omit to use the user's saved criteria.",
                },
                "limit": {"type": "integer", "description": "Max postings to fetch (default 15)"},
            },
            "required": [],
        },
    },
    {
        "name": "snooze_reminder",
        "description": (
            "Push a reminder's next ping further out — for 'remind me later', 'not now', "
            "'give me an hour', 'ping me about that this evening'. Works on a fired reminder "
            "that's awaiting confirmation (delays the next nudge) or a still-pending one "
            "(moves when it first fires). Defaults to 60 minutes if no time is given. Omit "
            "'query' to push ALL outstanding reminders."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword/phrase identifying which reminder (fuzzy matched). Omit to apply to all outstanding reminders.",
                },
                "minutes": {
                    "type": "integer",
                    "description": "How many minutes to push it out (default 60).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "set_reminder_cadence",
        "description": (
            "Change how often the bot re-nudges about reminders the user hasn't confirmed "
            "done — for 'space the reminders apart', 'stop nagging so often', 'sparse it out', "
            "'nudge me every 3 hours', or 'tighten it up'. Sets the gap between follow-up nudges "
            "in minutes (clamped to a sane range). Higher = less frequent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "Minutes between follow-up nudges (e.g. 180 for every 3 hours).",
                }
            },
            "required": ["minutes"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for current information, facts, prices, news, documentation, or "
            "anything you don't already know or that may have changed since your training. "
            "Returns a ranked list of results (title, URL, snippet). Use this BEFORE answering "
            "any question that depends on up-to-date or external information, then optionally "
            "call web_fetch on the most promising result to read it in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": "How many results to return (default 5, max 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a single web page (or API/text URL) and return its readable text content. "
            "Use after web_search to read a promising result in full, or when the user gives "
            "you a URL to read/summarise. Pass a full http(s):// URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s):// URL to fetch."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_applications",
        "description": (
            "List the user's tracked job applications and their statuses (applied, replied, "
            "interview, offer, rejected). Use when they ask 'what's the status of my "
            "applications', 'which jobs have I heard back from', etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional filter: applied | replied | interview | offer | rejected.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "track_application",
        "description": (
            "Manually start tracking a job application (the bot also auto-detects these from "
            "application-confirmation emails). Use when the user says 'I applied to X for Y'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company applied to."},
                "role": {"type": "string", "description": "Role/title (optional)."},
                "status": {
                    "type": "string",
                    "description": "Initial status (default 'applied'): applied|replied|interview|offer|rejected.",
                },
            },
            "required": ["company"],
        },
    },
    {
        "name": "update_application",
        "description": (
            "Update a tracked application's status or notes — e.g. the user says 'I got an "
            "interview with X', 'X rejected me', 'got the offer from Y'. Fuzzy-matches by company."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Company (or role) identifying which application."},
                "status": {
                    "type": "string",
                    "description": "New status: applied|replied|interview|offer|rejected|closed.",
                },
                "notes": {"type": "string", "description": "Optional note to attach."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_job_replies",
        "description": (
            "Scan the inbox NOW for new application confirmations and employer replies, update "
            "tracking, and report what changed. Use when the user asks 'any replies to my job "
            "applications?', 'check for job updates', etc. (This also runs automatically on a schedule.)"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_expenses",
        "description": (
            "List the user's logged expenses (auto-extracted from payment-receipt emails, plus "
            "any added manually). Use for 'what did I spend on X', 'show my recent expenses'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "today | week | month | all (default month)."},
                "category": {
                    "type": "string",
                    "description": "Optional: food, transport, shopping, subscription, bills, entertainment, travel, health, other.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "expense_summary",
        "description": (
            "Summarise spending over a period — totals broken down by category and currency. "
            "Use for 'how much did I spend this week/month', 'where is my money going'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "today | week | month | all (default month)."},
            },
            "required": [],
        },
    },
    {
        "name": "add_expense",
        "description": (
            "Manually log an expense the bot didn't pick up from email — e.g. the user says "
            "'I spent 50 cedis on lunch', 'log 20 dollars for the taxi'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "description": "Who was paid / what it was for."},
                "amount": {"type": "number", "description": "Amount spent (number)."},
                "currency": {"type": "string", "description": "3-letter currency (default the user's local currency)."},
                "category": {"type": "string", "description": "food|transport|shopping|subscription|bills|entertainment|travel|health|other."},
                "description": {"type": "string", "description": "Optional note."},
            },
            "required": ["merchant", "amount"],
        },
    },
    {
        "name": "delete_expense",
        "description": (
            "Remove a logged expense that's wrong or not actually a purchase — e.g. a bank "
            "credit/transfer the scanner mistook for spending ('that 4000 cedi CalBank entry "
            "isn't an expense, remove it'). Fuzzy-matches by merchant/description; if several "
            "match, also pass the amount to pick the right one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Merchant or description of the expense to remove."},
                "amount": {"type": "number", "description": "Optional exact amount, to disambiguate."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_receipts",
        "description": (
            "Scan the mailboxes NOW for payment receipts and log them, reporting what was found. "
            "Use for 'check for new receipts', 'update my expenses'. Pass 'days' to backfill "
            "historically — e.g. 'scan my receipts from the last month' → days=30. (A forward "
            "scan also runs automatically on a schedule.)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Optional: scan the last N days (historical backfill). Omit for only-new.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_status",
        "description": "Get an overview of projects and their open tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["work", "personal", "all"],
                    "description": "Filter by scope (default: all)",
                },
                "project_name": {
                    "type": "string",
                    "description": "Filter to a specific project (optional, fuzzy matched)",
                },
            },
            "required": [],
        },
    },
]


# Yahoo Mail (read-only over IMAP) — only advertised to the model when configured.
YAHOO_TOOLS: list[dict] = [
    {
        "name": "yahoo_search",
        "description": (
            "Search the user's Yahoo Mail (READ-ONLY) and return matching messages — each with "
            "a uid, sender, subject and date. Pass a query to filter (matches anywhere in the "
            "message), or omit it for the most recent. Then call yahoo_read with a uid to read "
            "a message's full body. Use this whenever the user asks about their Yahoo email."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for; omit for most recent."},
                "folder": {"type": "string", "description": 'Mailbox folder (default "INBOX"; e.g. "Sent", "Bulk").'},
                "limit": {"type": "integer", "description": "Max messages to return (default 10, max 25)."},
            },
            "required": [],
        },
    },
    {
        "name": "yahoo_read",
        "description": (
            "Read the full text body of one Yahoo Mail message by its uid (from yahoo_search), "
            "READ-ONLY. Use this to actually read/summarise an email's contents rather than just "
            "its subject line."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "The message uid returned by yahoo_search."},
                "folder": {"type": "string", "description": 'Folder the message is in (default "INBOX").'},
            },
            "required": ["uid"],
        },
    },
]

if YAHOO_MAIL_ENABLED:
    TOOLS += YAHOO_TOOLS
