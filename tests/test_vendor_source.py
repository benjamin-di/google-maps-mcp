"""Exercise exact vendor content, modes and immutable refresh inputs without Google."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vendor_source", ROOT / "scripts/verify-vendor.py")
vendor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vendor)


@pytest.fixture
def source(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    vendor.git(repository, "init", "--quiet")
    for name in vendor.MODULES:
        path = repository / "src/gmaps" / name
        if path.suffix != ".py":
            path /= "__init__.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic source\n")
    (repository / "LICENSE").write_text("MIT License\n")
    (repository / "src/gmaps/cli.py").write_text("# excluded module\n")
    vendor.git(repository, "add", ".")
    vendor.git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Synthetic source",
    )
    commit = vendor.git(repository, "rev-parse", "HEAD").decode().strip()
    destination = tmp_path / "vendor"
    destination.mkdir()
    expected = vendor.projection(repository, commit)
    for name, (content, mode) in expected.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
    return repository, commit, destination, expected


def test_projection_copies_only_declared_modules_and_license(source):
    _repository, commit, destination, expected = source
    vendor.compare(destination, expected)
    assert "cli.py" not in expected
    assert expected["VENDORED_COMMIT"][0] == (commit + "\n").encode()


@pytest.mark.parametrize("damage", ["content", "license", "mode", "extra", "commit", "symlink"])
def test_changed_or_expanded_vendor_projection_refuses(source, damage):
    _repository, _commit, destination, expected = source
    if damage == "content":
        (destination / "client.py").write_text("# changed\n")
    elif damage == "license":
        (destination / "LICENSE").unlink()
    elif damage == "mode":
        (destination / "client.py").chmod(0o755)
    elif damage == "extra":
        (destination / "cli.py").write_text("# excluded\n")
    elif damage == "commit":
        (destination / "VENDORED_COMMIT").write_text("f" * 40 + "\n")
    else:
        (destination / "client.py").unlink()
        (destination / "client.py").symlink_to("LICENSE")
    with pytest.raises(ValueError):
        vendor.compare(destination, expected)


def test_missing_verifier_fails_before_acquisition(monkeypatch):
    monkeypatch.setenv("UPSTREAM_SOURCE", "/missing/required-verifier")
    monkeypatch.setattr(vendor, "acquire", lambda *_: pytest.fail("acquired before required tool check"))
    with pytest.raises(ValueError, match="shared upstream-source"):
        vendor.verify(ROOT)


@pytest.mark.parametrize("arguments,code", [([], 64), (["main"], 1), (["abc123"], 1)])
def test_refresh_requires_explicit_full_commit_and_preserves_vendor(arguments, code):
    before = {
        p: p.read_bytes() for p in (ROOT / vendor.VENDOR).rglob("*") if p.is_file() and "__pycache__" not in p.parts
    }
    result = subprocess.run(["bash", str(ROOT / "scripts/vendor-gmaps.sh"), *arguments], capture_output=True)
    assert result.returncode == code
    assert {p: p.read_bytes() for p in before} == before


def test_shared_verifier_failure_preserves_vendor(source, monkeypatch, tmp_path):
    repository, commit, _destination, _expected = source
    project = tmp_path / "project"
    (project / "fleet").mkdir(parents=True)
    shutil.copytree(ROOT / vendor.VENDOR, project / vendor.VENDOR)
    recipe = json.loads((ROOT / "fleet/gmaps-source.json").read_text())
    recipe.update(baseline=commit, topics=[], expected_tree="0" * 40)
    (project / "fleet/gmaps-source.json").write_text(json.dumps(recipe))
    (project / vendor.VENDOR / "VENDORED_COMMIT").write_text(commit + "\n")
    for remote, key in [("origin", "fork_url"), ("upstream", "upstream_url")]:
        vendor.git(repository, "remote", "add", remote, recipe[key])
    vendor.git(repository, "tag", "v0.1.0", commit)
    monkeypatch.setattr(
        vendor, "acquire", lambda path, *_: shutil.copytree(repository / ".git", path, dirs_exist_ok=True)
    )
    before = (project / vendor.VENDOR / "client.py").read_bytes()
    with pytest.raises(ValueError, match="reconstruction refused"):
        vendor.verify(project)
    assert (project / vendor.VENDOR / "client.py").read_bytes() == before


@pytest.mark.parametrize("url", ["file:///tmp/source", "ssh://example.org/source", "ext::helper", "auditproof::source"])
def test_acquisition_rejects_transport_before_git(tmp_path, monkeypatch, url):
    monkeypatch.setattr(vendor, "git", lambda *_: pytest.fail("transport invoked"))
    with pytest.raises(ValueError, match="canonical HTTPS"):
        vendor.acquire(tmp_path, {"upstream_url": url, "fork_url": "https://example.org/fork.git"}, "a" * 40)
