"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _secret(name: str) -> str:
    """Tokens/keys never contain whitespace; strip any that sneaks in when pasting."""
    return "".join(os.environ.get(name, "").split())


def _telegram_token() -> str:
    """Pull the bot token out of whatever got pasted (quotes, spaces, BotFather's surrounding text)."""
    import re
    raw = _secret("TELEGRAM_BOT_TOKEN")
    m = re.search(r"\d{6,12}:[A-Za-z0-9_-]{30,40}", raw)
    return m.group(0) if m else raw


def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


@dataclass
class Settings:
    # Telegram
    telegram_token: str = field(default_factory=_telegram_token)
    allowed_user_ids: set[int] = field(default_factory=lambda: _ids(_env("TELEGRAM_ALLOWED_USER_IDS")))
    claim_code: str = field(default_factory=lambda: _secret("CLAIM_CODE"))
    webhook_secret: str = field(default_factory=lambda: _env("TELEGRAM_WEBHOOK_SECRET"))

    # Public URL (Railway sets RAILWAY_PUBLIC_DOMAIN automatically once a domain exists)
    public_url: str = field(
        default_factory=lambda: _env("PUBLIC_URL")
        or (f"https://{_env('RAILWAY_PUBLIC_DOMAIN')}" if _env("RAILWAY_PUBLIC_DOMAIN") else "")
    )

    # AI
    anthropic_key: str = field(default_factory=lambda: _secret("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-5-5"))
    openai_key: str = field(default_factory=lambda: _secret("OPENAI_API_KEY"))
    openai_transcribe_model: str = field(default_factory=lambda: _env("OPENAI_TRANSCRIBE_MODEL", "whisper-1"))
    # auto = OpenAI if key present, else local faster-whisper; or force: openai | local | none
    transcribe_backend: str = field(default_factory=lambda: _env("TRANSCRIBE_BACKEND", "auto").lower())
    local_whisper_model: str = field(default_factory=lambda: _env("LOCAL_WHISPER_MODEL", "base"))

    # Todoist
    todoist_token: str = field(default_factory=lambda: _secret("TODOIST_API_TOKEN"))
    todoist_project_id: str = field(default_factory=lambda: _env("TODOIST_PROJECT_ID"))
    todoist_queue_label: str = field(default_factory=lambda: _env("TODOIST_QUEUE_LABEL", "agent"))
    todoist_capture_label: str = field(default_factory=lambda: _env("TODOIST_CAPTURE_LABEL", "telegram"))

    # Instagram / downloader
    ig_cookies: str = field(default_factory=lambda: _env("IG_COOKIES"))  # Netscape cookies.txt contents
    max_video_mb: int = field(default_factory=lambda: int(_env("MAX_VIDEO_MB", "200")))
    keep_video: bool = field(default_factory=lambda: _env("KEEP_VIDEO", "0") == "1")

    # Behaviour
    default_text_action: str = field(default_factory=lambda: _env("DEFAULT_TEXT_ACTION", "note").lower())

    # MCP connector for Claude / ChatGPT
    mcp_secret: str = field(default_factory=lambda: _secret("MCP_SECRET"))

    # Storage
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))

    def __post_init__(self) -> None:
        if not self.webhook_secret and self.telegram_token:
            self.webhook_secret = hashlib.sha256(("wh:" + self.telegram_token).encode()).hexdigest()[:48]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)
        (self.data_dir / "tmp").mkdir(exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"

    @property
    def transcriber(self) -> str:
        if self.transcribe_backend in {"openai", "local", "none"}:
            return self.transcribe_backend
        return "openai" if self.openai_key else "local"


settings = Settings()
