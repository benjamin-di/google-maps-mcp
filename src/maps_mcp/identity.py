"""Offline identity from the installed artifact; runtime environment cannot relabel it."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from importlib import resources

from . import __version__


@dataclass(frozen=True)
class BuildIdentity:
    service: str
    version: str
    source_revision: str


def _load(path=None) -> BuildIdentity:
    path = path if path is not None else resources.files(__package__) / "build_identity.json"
    if not path.is_file():
        return BuildIdentity("maps", __version__, "unknown")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["version"] != __version__:
        raise ValueError("Embedded build version differs from installed application version")
    if data["service"] != "maps" or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", data["source_revision"]):
        raise ValueError("Invalid embedded service or source revision")
    return BuildIdentity(**data)


BUILD = _load()
# This labels a running process, separately from its immutable artifact identity.
INSTANCE = uuid.uuid4().hex


def _after_fork() -> None:
    global INSTANCE
    INSTANCE = uuid.uuid4().hex


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
