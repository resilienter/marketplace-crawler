import re
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """
    Consolidated configuration loaded from environment variables and .env file.
    Follows modern Pydantic v2 BaseSettings standard.
    """

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base filesystem paths
    root_dir: Path = Path(__file__).resolve().parent.parent
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    dumps_dir: Path = Path(__file__).resolve().parent.parent / "dumps"
    logs_dir: Path = Path(__file__).resolve().parent.parent / "logs"

    # Crawler Settings
    crawler_target_name: str = "MetropolisRealty"
    crawler_base_url: str = "https://example-realestate-marketplace.com"
    crawler_max_pages: int = 3
    crawler_concurrency: int = 3
    crawler_connect_timeout: float = 15.0
    crawler_read_timeout: float = 45.0
    crawler_max_attempts: int = 5
    crawler_backoff_cap: float = 60.0
    crawler_jitter_max: float = 1.5

    # HTTP Client Engine Settings (curl_cffi vs requests)
    use_curl_cffi: bool = True
    curl_impersonate: str = "chrome"

    # Anti-bot & 429 Cooldown (Fast sensible defaults for demo / responsive testing)
    enable_global_429_cooldown: bool = True
    global_429_cooldown_base_s: float = 5.0
    global_429_cooldown_multiplier: float = 1.5
    global_429_cooldown_cap_s: float = 30.0
    hard_fail_on_403: bool = True
    hard_fail_on_antibot: bool = True

    # Google Sheets Integration
    google_sheet_url: str = ""
    google_service_account_json: Path = Path("config/service_account.json")
    gsheets_batch_size: int = 300
    gsheets_retries: int = 5
    gsheets_backoff_cap: float = 20.0

    # Telegram Alerting
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Analytics / Alert thresholds
    sharp_drop_pct: float = 5.0

    @property
    def spreadsheet_id(self) -> str:
        """Extract Google spreadsheet ID from URL."""
        if not self.google_sheet_url:
            return ""
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", self.google_sheet_url)
        return m.group(1) if m else ""

    @property
    def resolved_service_account_path(self) -> Path:
        """Resolves service account path absolutely against project root directory."""
        p = self.google_service_account_json
        if not p.is_absolute():
            p = self.root_dir / p
        return p


settings = AppSettings()
