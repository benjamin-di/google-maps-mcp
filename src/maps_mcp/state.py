"""State that survives restarts, in ``MAPS_STATE_DIR/state.json``: breaker, daily budgets and
the last error. Written atomically (0600) on every change."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .errors import MapsError

log = logging.getLogger(__name__)

BREAKER_STEPS = (30 * 60, 2 * 3600, 8 * 3600)
# A block within this long after the previous breaker closed escalates to the next step.
ESCALATE_WINDOW = 24 * 3600


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


class StateStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        request_budget: int,
        tile_budget: int = 3000,
        budget_tz: str = "UTC",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(state_dir) / "state.json"
        self.request_budget = request_budget
        self.tile_budget = tile_budget
        self.budget_zone = ZoneInfo(budget_tz)
        self._clock = clock
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("state.json unreadable (%s); starting from defaults", type(exc).__name__)
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(self._data, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # The in-memory state still applies; only persistence across restarts is lost.
            log.error("could not persist state.json (%s)", type(exc).__name__)

    # -- budget -------------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), self.budget_zone).date().isoformat()

    def _budget(self) -> dict[str, Any]:
        budget = self._data.get("budget")
        today = self._today()
        if not isinstance(budget, dict) or budget.get("day") != today:
            budget = {"day": today, "requests": 0, "tiles": 0}
            self._data["budget"] = budget
        return budget

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.request_budget - int(self._budget().get("requests") or 0))

    def take(self, count: int = 1, kind: str = "requests") -> None:
        """Spend ``count`` of today's ``requests`` or ``tiles`` budget, or raise budget_exhausted."""
        limit = self.request_budget if kind == "requests" else self.tile_budget
        with self._lock:
            budget = self._budget()
            used = int(budget.get(kind) or 0)
            if used + count > limit:
                raise MapsError("budget_exhausted", log_detail=f"{kind} budget {limit} spent")
            budget[kind] = used + count
            self._save()

    # -- breaker ------------------------------------------------------------------------

    def check_breaker(self) -> None:
        with self._lock:
            until = (self._data.get("breaker") or {}).get("until") or 0
            if until > self._clock():
                raise MapsError("blocked", log_detail="breaker open")

    def trip_breaker(self) -> float:
        """Open the breaker after Google pushed back; returns when it closes."""
        now = self._clock()
        with self._lock:
            breaker = self._data.get("breaker") or {}
            level = int(breaker.get("level") or 0)
            previous_until = breaker.get("until") or 0
            if previous_until > now:
                return float(previous_until)
            level = min(level + 1, len(BREAKER_STEPS)) if now - previous_until < ESCALATE_WINDOW else 1
            until = now + BREAKER_STEPS[level - 1]
            self._data["breaker"] = {"level": level, "until": until, "tripped_at": now}
            self._save()
            log.warning("breaker open for %d s (level %d)", BREAKER_STEPS[level - 1], level)
            return until

    # -- last error ---------------------------------------------------------------------

    def record_error(self, code: str) -> None:
        with self._lock:
            self._data["last_error"] = {"code": code, "at": self._clock()}
            self._save()

    def describe(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            budget = dict(self._budget())
            breaker = self._data.get("breaker") or {}
            until = breaker.get("until") or 0
            last = self._data.get("last_error") or {}
            return {
                "breaker": {
                    "open": until > now,
                    "closes_at": iso(until) if until > now else None,
                    "level": breaker.get("level") or 0,
                },
                "budget": {
                    "day": budget["day"],
                    "time_zone": str(self.budget_zone),
                    "requests_left": max(0, self.request_budget - int(budget.get("requests") or 0)),
                    "requests_daily": self.request_budget,
                    "tiles_left": max(0, self.tile_budget - int(budget.get("tiles") or 0)),
                    "tiles_daily": self.tile_budget,
                },
                "last_error": {"code": last.get("code"), "at": iso(last.get("at"))} if last else None,
            }
