"""Verify maintained source and its exact module/license projection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

MODULES = frozenset(
    [
        "__init__.py",
        "client.py",
        "_search.py",
        "_places.py",
        "_directions.py",
        "_reviews.py",
        "control.py",
        "exceptions.py",
        "grid.py",
        "transport.py",
        "validation.py",
        "_hooks.py",
        "_auth",
        "rpc",
    ]
)
VENDOR = Path("src/maps_mcp/vendor/gmaps")


def full_commit(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Vendor source requires a full immutable commit")
    return value


def git(repository: Path, *args: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            *args,
        ],
        env=environment,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("Required vendor source acquisition or object check failed")
    return result.stdout


def acquire(repository: Path, recipe: dict, commit: str) -> None:
    for url in (recipe["upstream_url"], recipe["fork_url"]):
        if not isinstance(url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+\.git", url):
            raise ValueError("Source acquisition requires canonical HTTPS repository URLs")
    git(repository, "init", "--bare", "--quiet")
    git(repository, "remote", "add", "upstream", recipe["upstream_url"])
    git(repository, "remote", "add", "origin", recipe["fork_url"])
    reference = recipe["baseline_ref"]
    if not reference.startswith("refs/tags/"):
        reference = "refs/tags/" + reference
    git(repository, "check-ref-format", reference)
    git(repository, "fetch", "--no-tags", "upstream", reference + ":" + reference)
    if git(repository, "rev-parse", reference + "^{commit}").decode().strip() != full_commit(recipe["baseline"]):
        raise ValueError("Canonical vendor release tag differs from the reviewed baseline")
    inputs = list(dict.fromkeys([commit, *(full_commit(topic["commit"]) for topic in recipe["topics"])]))
    git(repository, "fetch", "--no-tags", "origin", *inputs)


def projection(repository: Path, commit: str) -> dict[str, tuple[bytes, int]]:
    result = {}
    for row in git(repository, "ls-tree", "-rz", "--full-tree", full_commit(commit)).split(b"\0"):
        if not row:
            continue
        header, raw_path = row.split(b"\t", 1)
        mode, kind, blob = header.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if path == "LICENSE":
            destination = path
        elif path.startswith("src/gmaps/") and path.removeprefix("src/gmaps/").split("/")[0] in MODULES:
            destination = path.removeprefix("src/gmaps/")
        else:
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("Unsupported entry in the vendor projection")
        result[destination] = (git(repository, "cat-file", "blob", blob), 0o755 if mode == "100755" else 0o644)
    if "LICENSE" not in result or {Path(name).parts[0] for name in result} - {"LICENSE"} != MODULES:
        raise ValueError("Vendor source is missing a selected module or license")
    result["VENDORED_COMMIT"] = ((commit + "\n").encode(), 0o644)
    return result


def compare(destination: Path, expected: dict[str, tuple[bytes, int]]) -> None:
    if destination.is_symlink():
        raise ValueError("Vendor destination cannot be a symlink")
    found = {}
    for path in destination.rglob("*"):
        relative = path.relative_to(destination)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("Symlinks are not part of the vendor projection")
        if path.is_file():
            found[relative.as_posix()] = (path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644)
        elif not path.is_dir():
            raise ValueError("Unsupported local vendor entry")
    if found != expected:
        raise ValueError("Vendored modules, modes, license or commit differ from the verified projection")


def verifier_executable(value=None):
    command = value if value is not None else os.environ.get("UPSTREAM_SOURCE")
    if command is not None:
        executable = shutil.which(command)
        if not executable:
            raise ValueError("Required shared upstream-source verifier is unavailable or not executable")
        return executable
    bundle = Path(__file__).resolve().parents[1] / "tools/upstream-source"
    try:
        record = tomllib.loads((bundle / "SOURCE.toml").read_text())
        for name in ("upstream-source", "upstream_source.py"):
            path = bundle / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"][name]:
                raise ValueError("Bundled upstream-source verifier differs from its reviewed snapshot")
        executable = shutil.which(str(bundle / "upstream-source"))
    except (OSError, KeyError, TypeError) as error:
        raise ValueError("Required shared upstream-source verifier is unavailable or incomplete") from error
    if not executable:
        raise ValueError("Required shared upstream-source verifier is unavailable or not executable")
    return executable


def verify(project: Path, refresh: str | None = None) -> None:
    destination = project / VENDOR
    commit = full_commit(refresh if refresh is not None else (destination / "VENDORED_COMMIT").read_text().strip())
    recipe_path = project / "fleet/gmaps-source.json"
    recipe = json.loads(recipe_path.read_text())
    executable = verifier_executable()
    with tempfile.TemporaryDirectory(prefix="gmaps-source-") as directory:
        repository = Path(directory)
        acquire(repository, recipe, commit)
        result = subprocess.run(
            [
                executable,
                "verify",
                "--repository",
                str(repository),
                "--recipe",
                str(recipe_path),
                "--consumed-ref",
                commit,
            ],
            check=False,
        )
        if result.returncode:
            raise ValueError("Maintained vendor source reconstruction refused")
        expected = projection(repository, commit)
    if refresh is None:
        compare(destination, expected)
        return
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("Vendor destination must be a regular directory")
    with tempfile.TemporaryDirectory(prefix=".gmaps-refresh-", dir=destination.parent) as directory:
        staging = Path(directory) / "new"
        staging.mkdir()
        for name, (content, mode) in expected.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)
        backup = Path(directory) / "previous"
        destination.rename(backup)
        try:
            staging.rename(destination)
        except OSError:
            backup.rename(destination)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", help="Explicit immutable source; update the reviewed recipe before a refresh")
    arguments = parser.parse_args()
    try:
        verify(Path(__file__).resolve().parents[1], arguments.refresh)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Vendor source verification refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
