import json
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True
    )

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://messenger:messenger@localhost:5432/messenger"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    SECRET_KEY: str = "change-me-in-production-use-a-secure-random-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # S3
    S3_BUCKET_NAME: str = ""
    S3_ENDPOINT_URL: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:19006", "http://localhost:8081", "http://localhost:3000"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                return [origin.strip() for origin in v.split(",")]
        return v

    # WebRTC
    STUN_SERVERS: list[str] = ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]
    TURN_SERVER_URL: str = ""
    TURN_SERVER_USERNAME: str = ""
    TURN_SERVER_CREDENTIAL: str = ""

    @field_validator("STUN_SERVERS", mode="before")
    @classmethod
    def parse_stun_servers(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                return [s.strip() for s in v.split(",")]
        return v


settings = Settings()
