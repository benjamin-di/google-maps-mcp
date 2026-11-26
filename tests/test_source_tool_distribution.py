"""Require the standalone tool distribution and fail before acquisition on damage."""

import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "verify-vendor.py"


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    project = tmp_path / "standalone checkout with spaces"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / ADAPTER, project / "scripts" / ADAPTER)
    shutil.copytree(ROOT / "tools/upstream-source", project / "tools/upstream-source")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    monkeypatch.delenv("UPSTREAM_SOURCE", raising=False)
    monkeypatch.setenv("PATH", str(commands))
    resolver = runpy.run_path(str(project / "scripts" / ADAPTER))["verifier_executable"]
    return project, commands, resolver


def test_standalone_distribution_runs_without_global_install(standalone):
    project, _, resolver = standalone
    executable = resolver()
    assert Path(executable) == project / "tools/upstream-source/upstream-source"
    result = subprocess.run([executable, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "verify" in result.stdout


@pytest.mark.parametrize("name", ["upstream-source", "upstream_source.py", "SOURCE.toml"])
def test_missing_distribution_refuses(standalone, name):
    project, _, resolver = standalone
    (project / "tools/upstream-source" / name).unlink()
    with pytest.raises(ValueError, match="shared upstream-source verifier is unavailable"):
        resolver()


@pytest.mark.parametrize("name", ["upstream-source", "upstream_source.py"])
@pytest.mark.parametrize("damage", ["content", "symlink"])
def test_changed_distribution_refuses_before_execution(standalone, name, damage):
    project, _, resolver = standalone
    path = project / "tools/upstream-source" / name
    if damage == "content":
        path.write_text("raise SystemExit(0)\n")
    else:
        outside = project / "outside.py"
        shutil.copy2(path, outside)
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(ValueError, match="differs from its reviewed snapshot"):
        resolver()


def test_nonexecutable_distribution_refuses(standalone):
    project, _, resolver = standalone
    (project / "tools/upstream-source/upstream-source").chmod(0o644)
    with pytest.raises(ValueError, match="unavailable or not executable"):
        resolver()


@pytest.mark.parametrize("selection", ["argument", "environment"])
def test_missing_explicit_override_never_falls_back(standalone, monkeypatch, selection):
    project, _, resolver = standalone
    missing = str(project / "missing override")
    monkeypatch.setenv("UPSTREAM_SOURCE", missing)
    with pytest.raises(ValueError, match="unavailable or not executable"):
        resolver(missing if selection == "argument" else None)


def test_argument_precedes_environment_and_installed_command(standalone, monkeypatch):
    project, commands, resolver = standalone
    explicit = project / "explicit verifier; literal"
    explicit.write_text(f"#!{sys.executable}\nraise SystemExit(37)\n")
    explicit.chmod(0o755)
    (commands / "upstream-source").symlink_to(explicit)
    monkeypatch.setenv("UPSTREAM_SOURCE", "/missing/environment-verifier")
    assert resolver(str(explicit)) == str(explicit)
    assert subprocess.run([resolver(str(explicit))], check=False).returncode == 37
    monkeypatch.setenv("UPSTREAM_SOURCE", str(explicit))
    assert resolver() == str(explicit)
    monkeypatch.delenv("UPSTREAM_SOURCE")
    assert resolver() == str(project / "tools/upstream-source/upstream-source")
    assert os.access(resolver(), os.X_OK)
