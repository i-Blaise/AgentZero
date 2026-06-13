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
