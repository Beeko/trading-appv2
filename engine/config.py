import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
        populate_by_name=True,
    )

    alpaca_api_key: str = Field(..., alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(..., alias="ALPACA_SECRET_KEY")
    alpaca_paper: bool = Field(True, alias="ALPACA_PAPER")

    # Live trading requires ALL three guards to be satisfied
    allow_live_trading: bool = Field(False, alias="ALLOW_LIVE_TRADING")

    database_url: str = Field(
        "postgresql+asyncpg://trader:changeme@localhost:5432/trading",
        alias="DATABASE_URL",
    )

    kill_switch_path: str = Field("/app/state/killswitch", alias="KILL_SWITCH_PATH")
    config_file: str = Field("/app/config.yaml", alias="CONFIG_FILE")
    log_dir: str = Field("/app/logs", alias="LOG_DIR")

    reddit_client_id: Optional[str] = Field(None, alias="REDDIT_CLIENT_ID")
    reddit_client_secret: Optional[str] = Field(None, alias="REDDIT_CLIENT_SECRET")
    reddit_user_agent: Optional[str] = Field(
        "TradingBot/1.0", alias="REDDIT_USER_AGENT"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


class TradingConfig:
    """Hot-reloadable YAML config. Call reload() to pick up edits without restart."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._raw: dict = {}
        self.reload()

    def reload(self):
        with open(self._path) as f:
            self._raw = yaml.safe_load(f) or {}

    # ── section accessors ─────────────────────────────────────────────────────

    @property
    def trading(self) -> dict:
        return self._raw.get("trading", {})

    @property
    def risk(self) -> dict:
        return self._raw.get("risk", {})

    @property
    def pdt(self) -> dict:
        return self._raw.get("pdt", {})

    @property
    def market_hours(self) -> dict:
        return self._raw.get("market_hours", {})

    @property
    def strategy(self) -> dict:
        return self._raw.get("strategy", {})

    @property
    def wsb_scanner(self) -> dict:
        return self._raw.get("wsb_scanner", {})

    @property
    def screener(self) -> dict:
        return self._raw.get("screener", {})

    @property
    def options(self) -> dict:
        return self._raw.get("options", {})

    def validate_options_config(self) -> list[str]:
        """Return validation error messages. Empty list means valid."""
        errors: list[str] = []
        opts = self.options
        if not opts:
            return errors

        min_dte = int(opts.get("min_dte", 0))
        dte_floor = int(opts.get("dte_floor", 0))
        if min_dte <= dte_floor:
            errors.append(
                f"options.min_dte ({min_dte}) must be > options.dte_floor ({dte_floor})"
            )

        target = float(opts.get("target_delta", 0))
        tol = float(opts.get("delta_tolerance", 0))
        if not (0 < target < 1):
            errors.append(f"options.target_delta must be in (0, 1); got {target}")
        if tol <= 0 or tol >= target:
            errors.append(
                f"options.delta_tolerance ({tol}) must be > 0 and < target_delta ({target})"
            )

        if int(opts.get("max_option_positions", 0)) <= 0:
            errors.append("options.max_option_positions must be > 0")
        if float(opts.get("max_position_pct", 0)) <= 0:
            errors.append("options.max_position_pct must be > 0")

        return errors

    def raw(self) -> dict:
        return self._raw
