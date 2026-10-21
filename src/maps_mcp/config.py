"""Settings from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Bad or missing configuration; raised at startup only."""


def default_state_dir(env: Mapping[str, str]) -> Path:
    """Where state lives when MAPS_STATE_DIR is not set: the per-user state directory
    ($XDG_STATE_HOME/google-maps-mcp, else ~/.local/state/google-maps-mcp). The container
    image sets MAPS_STATE_DIR=/state."""
    base = (env.get("XDG_STATE_HOME") or "").strip()
    return (Path(base) if base else Path.home() / ".local" / "state") / "google-maps-mcp"


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path("/state")
    # Requests to google.com per day (in budget_tz), every tool together. A route or a place is
    # one request; a matrix is one per pair. Keep it modest: it shares your IP's standing with
    # anything else of yours that talks to Google.
    daily_request_budget: int = 1000
    # Map tiles (256 px images) per day for maps_static_map; a 640x480 map is about 12 tiles,
    # cached in memory for a day.
    daily_tile_budget: int = 3000
    # The day the budgets count in (an IANA zone); they reset at its midnight.
    budget_tz: str = "UTC"
    # The canary's real check: a walk between two places and one place's details, given as
    # place ids (or text). Defaults: Wien Hauptbahnhof -> Albertina, Vienna.
    canary_from: str = "ChIJUU071tupbUcRyFdx20Nwgqg"
    canary_to: str = "ChIJwVp-8psHbUcRuN43n5iK72Y"
    # Minimum seconds between requests to Google (jittered +-30% by the library).
    min_interval_s: float = 1.0
    request_timeout_s: float = 15.0
    language: str = "en"

    @property
    def cookie_file(self) -> Path:
        return self.state_dir / "google-cookies.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            state_dir=_state_dir(env),
            daily_request_budget=_int(env, "MAPS_DAILY_REQUEST_BUDGET", 1000, 0, 100_000),
            daily_tile_budget=_int(env, "MAPS_DAILY_TILE_BUDGET", 3000, 0, 100_000),
            min_interval_s=_float(env, "MAPS_MIN_INTERVAL_S", 1.0, 0.2, 30.0),
            request_timeout_s=_float(env, "MAPS_REQUEST_TIMEOUT_S", 15.0, 2.0, 60.0),
            language=_language(env),
            budget_tz=_zone(env, "MAPS_BUDGET_TZ", "UTC"),
            canary_from=(env.get("MAPS_CANARY_FROM") or "").strip() or cls.canary_from,
            canary_to=_place_id(env, "MAPS_CANARY_TO", cls.canary_to),
        )


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _zone(env: Mapping[str, str], name: str, default: str) -> str:
    raw = (env.get(name) or "").strip() or default
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{name} must be an IANA time zone such as Europe/Vienna") from None
    return raw


def _place_id(env: Mapping[str, str], name: str, default: str) -> str:
    raw = (env.get(name) or "").strip() or default
    if not raw.startswith("ChIJ"):
        raise ConfigError(f"{name} must be a place id (ChIJ…) of a place with opening hours")
    return raw


def _state_dir(env: Mapping[str, str]) -> Path:
    raw = (env.get("MAPS_STATE_DIR") or "").strip()
    path = Path(raw).expanduser() if raw else default_state_dir(env)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = path / ".write-test"
        probe.write_text("")
        probe.unlink()
    except OSError:
        raise ConfigError(f"the state directory {path} is not writable (set MAPS_STATE_DIR)") from None
    return path


def _language(env: Mapping[str, str]) -> str:
    raw = (env.get("MAPS_LANGUAGE") or "").strip() or "en"
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", raw):
        raise ConfigError("MAPS_LANGUAGE must be a language tag such as en, de or pt-BR")
    return raw
