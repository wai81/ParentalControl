import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PC_SERVER_", env_file=".env", extra="ignore")

    app_name: str = "Родительский контроль"
    debug: bool = False
    data_dir: Path = Path("./data")
    database_url: str = ""  # default: SQLite file in data_dir
    secret_key: str = ""  # signs the web panel session cookie; generated if empty
    # Set when the panel is served over HTTPS (directly or behind a proxy).
    secure_cookies: bool = False
    pairing_code_minutes: int = 15
    device_online_seconds: int = 60

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{(self.data_dir / 'server.db').as_posix()}"

    def resolved_secret_key(self) -> str:
        """The configured key, or one generated once and kept in data_dir/secret.key."""
        if self.secret_key:
            return self.secret_key
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        return path.read_text(encoding="utf-8").strip()


settings = Settings()
