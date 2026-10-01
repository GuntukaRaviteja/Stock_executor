import json
from typing import List, Optional, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    APP_NAME: str = "Stock Intelligence API"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_V1_STR: str = "/api/v1"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Database Settings
    DATABASE_URL: str = "mysql+pymysql://root:@127.0.0.1:3306/stock_ml_db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_PRE_PING: bool = True

    # Market Data Provider
    MARKET_DATA_PROVIDER: str = "mock"
    MARKET_DATA_API_KEY: Optional[str] = None
    MARKET_DATA_TIMEOUT_SECONDS: float = 10.0

    # News / Event Data Provider
    NEWS_DATA_PROVIDER: str = "mock"
    NEWS_DATA_API_KEY: Optional[str] = None
    NEWS_DATA_TIMEOUT_SECONDS: float = 10.0

    # Security & CORS
    CORS_ORIGINS: Union[List[str], str] = ["*"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            if v.strip().startswith("["):
                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [i.strip() for i in v.split(",") if i.strip()]
        return v


settings = Settings()
