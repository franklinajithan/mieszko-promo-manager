from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/promo.db"
    data_dir: Path = Path("data")
    cookie_secure: bool = True
    allowed_origin: str = "https://promo.example.com"
    session_hours: int = 12
    max_upload_mb: int = 40
    worker_poll_seconds: float = 2


settings = Settings()
for folder in ("images", "thumbs", "uploads", "prints"):
    (settings.data_dir / folder).mkdir(parents=True, exist_ok=True)
