"""Settings from environment variables (documented in README.md).

Values are read with defaults so modules can be imported in tests; `validate()` enforces the required ones at startup.
"""
import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
_OWNER = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(_OWNER) if _OWNER.isdigit() else 0
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # optional: only this channel + its threads
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # "owner/repo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "")  # empty = repo default branch
MEMORY_DIR = os.getenv("MEMORY_DIR", "memory").strip("/")  # notes live here; empty = repo root
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Comma-separated, tried in order; next one is used when the previous times out / returns 5xx
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-3.5-flash").split(",") if m.strip()]
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low").strip()  # "low" / "high" / empty = model default
GEMINI_TIMEOUT_S = int(os.getenv("GEMINI_TIMEOUT_S", "45"))
GEMINI_MEDIA_TIMEOUT_S = int(os.getenv("GEMINI_MEDIA_TIMEOUT_S", "120"))  # requests with images/PDFs
SYNC_MINUTES = float(os.getenv("SYNC_MINUTES", "5"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
HISTORY_MAX_CHARS = int(os.getenv("HISTORY_MAX_CHARS", "40000"))
MAX_MEDIA = int(os.getenv("MAX_MEDIA", "5"))  # newest N thread images/PDFs sent to Gemini
MAX_ATTACHMENT_MB = int(os.getenv("MAX_ATTACHMENT_MB", "25"))
PORT = os.getenv("PORT")  # set by Render -> starts keep-alive HTTP server

GITHUB_API = "https://api.github.com"
DISCORD_LIMIT = 2000
DISCORD_FILE_LIMIT = 10 * 1024 * 1024  # bot upload limit; bigger files are sent as GitHub links
MAX_INLINE_BYTES = 15 * 1024 * 1024  # per file sent inline to Gemini
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024
MAX_IMAGE_PX = 2048
EDIT_DIFF_CHARS = 1400
INLINE_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}


def validate() -> None:
    """Fail fast with a readable message when required settings are missing."""
    required = {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "GITHUB_REPO": GITHUB_REPO,
        "GEMINI_API_KEY": GEMINI_API_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    if not _OWNER.isdigit():
        raise SystemExit(f"OWNER_ID must be your numeric Discord user ID (Developer Mode → Copy User ID), got {_OWNER!r}")
