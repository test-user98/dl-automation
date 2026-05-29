from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Literal
from functools import lru_cache


class Settings(BaseSettings):
    # ── LLM ───────────────────────────────────────────────────────────────────
    # Primary provider tried first. Fallback used when primary fails or is paused.
    # Set LLM_PRIMARY_PAUSED=true in .env to force fallback (e.g. while testing Anthropic)
    llm_primary: Literal["openai", "anthropic"] = Field("openai", env="LLM_PRIMARY")
    llm_fallback: Literal["openai", "anthropic", ""] = Field("anthropic", env="LLM_FALLBACK")
    llm_primary_paused: bool = Field(False, env="LLM_PRIMARY_PAUSED")

    anthropic_api_key: str = Field("", env="ANTHROPIC_API_KEY")
    openai_api_key: str = Field("", env="OPENAI_API_KEY")

    # Override model per provider via these vars; defaults are set per provider below
    llm_model: str = Field("", env="LLM_MODEL")
    llm_max_tokens: int = Field(4096, env="LLM_MAX_TOKENS")
    llm_temperature: float = Field(0.1, env="LLM_TEMPERATURE")

    def resolved_model_for(self, provider: str) -> str:
        if self.llm_model:
            return self.llm_model
        return "gpt-4o" if provider == "openai" else "claude-sonnet-4-6"

    # ── CAPTCHA ───────────────────────────────────────────────────────────────
    captcha_provider: Literal["2captcha", "capsolver", "claude", "manual"] = Field(
        "claude", env="CAPTCHA_PROVIDER"
    )
    captcha_api_key: str = Field("", env="CAPTCHA_API_KEY")
    captcha_timeout_seconds: int = Field(60, env="CAPTCHA_TIMEOUT_SECONDS")
    captcha_max_retries: int = Field(4, env="CAPTCHA_MAX_RETRIES")
    captcha_confidence_threshold: float = Field(0.85, env="CAPTCHA_CONFIDENCE_THRESHOLD")
    captcha_manual_timeout_seconds: int = Field(300, env="CAPTCHA_MANUAL_TIMEOUT_SECONDS")

    # ── BROWSER ───────────────────────────────────────────────────────────────
    browser_headless: bool = Field(False, env="BROWSER_HEADLESS")
    browser_slow_mo_ms: int = Field(80, env="BROWSER_SLOW_MO_MS")
    browser_timeout_ms: int = Field(30000, env="BROWSER_TIMEOUT_MS")
    browser_viewport_width: int = Field(1280, env="BROWSER_VIEWPORT_WIDTH")
    browser_viewport_height: int = Field(800, env="BROWSER_VIEWPORT_HEIGHT")
    browser_channel: str = Field("chrome", env="BROWSER_CHANNEL")
    browser_profile_dir: str = Field("./data/chrome_profile", env="BROWSER_PROFILE_DIR")
    browser_locale: str = Field("en-IN", env="BROWSER_LOCALE")
    browser_timezone_id: str = Field("Asia/Kolkata", env="BROWSER_TIMEZONE_ID")
    browser_user_agent: str = Field(
        "",
        env="BROWSER_USER_AGENT",
    )

    # ── SARATHI PORTAL ────────────────────────────────────────────────────────
    sarathi_base_url: str = Field(
        "https://sarathi.parivahan.gov.in/sarathiservice", env="SARATHI_BASE_URL"
    )
    sarathi_default_state: str = Field("DL", env="SARATHI_DEFAULT_STATE")
    sarathi_session_timeout_minutes: int = Field(
        25, env="SARATHI_SESSION_TIMEOUT_MINUTES"
    )

    # ── STATE STORAGE ─────────────────────────────────────────────────────────
    state_backend: Literal["sqlite", "redis"] = Field("sqlite", env="STATE_BACKEND")
    sqlite_db_path: str = Field("./data/agent_state.db", env="SQLITE_DB_PATH")
    redis_url: str = Field("redis://localhost:6379/0", env="REDIS_URL")

    # ── LEARNING STORE ────────────────────────────────────────────────────────
    learning_db_path: str = Field(
        "./data/learning_store.db", env="LEARNING_DB_PATH"
    )
    scenario_similarity_threshold: float = Field(
        0.85, env="SCENARIO_SIMILARITY_THRESHOLD"
    )
    max_auto_retry_with_learning: int = Field(3, env="MAX_AUTO_RETRY_WITH_LEARNING")

    # ── HUMAN IN THE LOOP ─────────────────────────────────────────────────────
    human_loop_backend: Literal["webhook", "firebase", "polling", "console"] = Field(
        "polling", env="HUMAN_LOOP_BACKEND"
    )
    human_loop_webhook_url: str = Field("", env="HUMAN_LOOP_WEBHOOK_URL")
    human_loop_timeout_minutes: int = Field(30, env="HUMAN_LOOP_TIMEOUT_MINUTES")
    firebase_credentials_path: str = Field("", env="FIREBASE_CREDENTIALS_PATH")

    # ── OTP RELAY ─────────────────────────────────────────────────────────────
    otp_wait_timeout_seconds: int = Field(600, env="OTP_WAIT_TIMEOUT_SECONDS")
    otp_relay_poll_interval_seconds: int = Field(3, env="OTP_RELAY_POLL_INTERVAL_SECONDS")

    # ── OCR ───────────────────────────────────────────────────────────────────
    ocr_provider: Literal["claude", "tesseract", "aws_textract"] = Field(
        "claude", env="OCR_PROVIDER"
    )
    ocr_max_attempts: int = Field(2, env="OCR_MAX_ATTEMPTS")
    aws_textract_region: str = Field("ap-south-1", env="AWS_TEXTRACT_REGION")
    aws_access_key_id: str = Field("", env="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str = Field("", env="AWS_SECRET_ACCESS_KEY")

    # ── IMAGE PROCESSING ──────────────────────────────────────────────────────
    photo_max_size_kb: int = Field(20, env="PHOTO_MAX_SIZE_KB")
    signature_max_size_kb: int = Field(10, env="SIGNATURE_MAX_SIZE_KB")
    photo_width_px: int = Field(200, env="PHOTO_WIDTH_PX")
    photo_height_px: int = Field(200, env="PHOTO_HEIGHT_PX")

    # ── API SERVER ────────────────────────────────────────────────────────────
    api_host: str = Field("0.0.0.0", env="API_HOST")
    api_port: int = Field(8000, env="API_PORT")
    api_secret_key: str = Field("change-this-in-production", env="API_SECRET_KEY")

    # ── EMAIL NOTIFICATIONS ──────────────────────────────────────────────────
    email_notifications_enabled: bool = Field(False, env="EMAIL_NOTIFICATIONS_ENABLED")
    smtp_host: str = Field("", env="SMTP_HOST")
    smtp_port: int = Field(587, env="SMTP_PORT")
    smtp_username: str = Field("", env="SMTP_USERNAME")
    smtp_password: str = Field("", env="SMTP_PASSWORD")
    smtp_from: str = Field("", env="SMTP_FROM")

    # ── AGENT BEHAVIOUR ───────────────────────────────────────────────────────
    max_steps_per_job: int = Field(100, env="MAX_STEPS_PER_JOB")
    max_consecutive_step_failures: int = Field(4, env="MAX_CONSECUTIVE_STEP_FAILURES")
    max_repeated_page_states: int = Field(3, env="MAX_REPEATED_PAGE_STATES")
    stuck_threshold_retries: int = Field(3, env="STUCK_THRESHOLD_RETRIES")
    screenshot_on_every_step: bool = Field(True, env="SCREENSHOT_ON_EVERY_STEP")
    log_level: str = Field("INFO", env="LOG_LEVEL")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
