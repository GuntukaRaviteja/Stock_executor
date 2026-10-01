import logging
import sys
import re
from typing import Any
from app.core.config import settings

# Sensitive keyword patterns to scrub from logs
SENSITIVE_PATTERNS = [
    re.compile(r'(password|token|api_key|secret|authorization)=([^\s&]+)', re.IGNORECASE),
    re.compile(r'mysql\+pymysql://([^:]+):([^@]+)@', re.IGNORECASE),
]


class SensitiveDataFilter(logging.Filter):
    """Filter that masks sensitive tokens, passwords, and API keys."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.mask_sensitive(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.mask_sensitive(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self.mask_sensitive(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True

    @staticmethod
    def mask_sensitive(text: str) -> str:
        for pattern in SENSITIVE_PATTERNS:
            text = pattern.sub(r'\1=***MASKED***', text)
        return text


def setup_logging() -> logging.Logger:
    """Configure structured logging for the application."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(SensitiveDataFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # Avoid duplicate handlers if setup is called multiple times
    root_logger.handlers = [handler]

    # Set third party loggers to reasonable levels
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING if not settings.DEBUG else logging.INFO)

    app_logger = logging.getLogger("app")
    return app_logger


logger = setup_logging()
