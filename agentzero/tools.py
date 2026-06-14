"""
Tool definitions in provider-neutral JSON Schema format.
Each provider adapter in llm.py translates these to its own wire format.
"""

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
        "name": "list_reminders",
        "description": "List the user's upcoming (pending) reminders.",
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
