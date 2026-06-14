import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID: int = int(os.environ.get("ALLOWED_CHAT_ID") or "0")
TELEGRAM_MODE: str = os.environ.get("TELEGRAM_MODE", "polling")
WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET: str = os.environ.get("WEBHOOK_SECRET", "")

# MongoDB
MONGODB_URI: str = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB: str = os.environ.get("MONGODB_DB", "agentzero")

# LLM
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai")

OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL: str = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_DIGEST_MODEL: str = os.environ.get("OPENAI_DIGEST_MODEL", "gpt-4o")

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_CHAT_MODEL: str = os.environ.get("ANTHROPIC_CHAT_MODEL", "claude-haiku-4-5")
ANTHROPIC_DIGEST_MODEL: str = os.environ.get("ANTHROPIC_DIGEST_MODEL", "claude-sonnet-4-6")

# Digest thresholds
STALL_DAYS_WORK: int = int(os.environ.get("STALL_DAYS_WORK", "7"))
STALL_DAYS_PERSONAL: int = int(os.environ.get("STALL_DAYS_PERSONAL", "14"))

# Timezone — used for reminders and digests
TIMEZONE: str = os.environ.get("TIMEZONE", "Africa/Accra")

# Autonomy — proactive heartbeat
AUTONOMY_ENABLED: bool = os.environ.get("AUTONOMY_ENABLED", "true").lower() == "true"
HEARTBEAT_MINUTES: int = int(os.environ.get("HEARTBEAT_MINUTES", "30"))
QUIET_HOURS_START: int = int(os.environ.get("QUIET_HOURS_START", "21"))  # 21:00
QUIET_HOURS_END: int = int(os.environ.get("QUIET_HOURS_END", "8"))       # 08:00
NUDGE_COOLDOWN_HOURS: int = int(os.environ.get("NUDGE_COOLDOWN_HOURS", "4"))

# Morning digest — daily rundown
MORNING_DIGEST_ENABLED: bool = os.environ.get("MORNING_DIGEST_ENABLED", "true").lower() == "true"
MORNING_DIGEST_HOUR: int = int(os.environ.get("MORNING_DIGEST_HOUR", "8"))    # 08:00
MORNING_DIGEST_MINUTE: int = int(os.environ.get("MORNING_DIGEST_MINUTE", "0"))

# Evening digest — daily wind-down (tees up tomorrow)
EVENING_DIGEST_ENABLED: bool = os.environ.get("EVENING_DIGEST_ENABLED", "true").lower() == "true"
EVENING_DIGEST_HOUR: int = int(os.environ.get("EVENING_DIGEST_HOUR", "22"))   # 22:30 (10:30pm)
EVENING_DIGEST_MINUTE: int = int(os.environ.get("EVENING_DIGEST_MINUTE", "30"))

# Persistent reminders — a fired reminder keeps nudging until the user confirms it's done
REMINDER_FOLLOWUP_MINUTES: int = int(os.environ.get("REMINDER_FOLLOWUP_MINUTES", "90"))

# MCP — external platform servers (Gmail, Calendar, …)
MCP_ENABLED: bool = os.environ.get("MCP_ENABLED", "false").lower() == "true"
# URL of the Google Workspace MCP server (streamable-http), e.g. http://127.0.0.1:8001/mcp
GOOGLE_MCP_URL: str = os.environ.get("GOOGLE_MCP_URL", "")
# Exact connected Google account emails (comma-separated) — injected into the prompt as
# ground truth so the model uses them verbatim and never guesses/truncates an address.
GOOGLE_ACCOUNTS: list[str] = [
    a.strip() for a in os.environ.get("GOOGLE_ACCOUNTS", "").split(",") if a.strip()
]

# Registry of MCP servers: each {"name": <namespace prefix>, "url": <streamable-http URL>}
MCP_SERVERS: list[dict] = []
if GOOGLE_MCP_URL:
    MCP_SERVERS.append({"name": "google", "url": GOOGLE_MCP_URL})
