"""Artifact identity, safe HTML, and structured diagnostics without Google calls."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from mcp.types import SERVER_INFO_META_KEY

from maps_mcp import __version__, logs, server
from maps_mcp.identity import BUILD, INSTANCE, BuildIdentity, _load

ROOT = Path(__file__).resolve().parents[1]


def embed(root, version, revision):
    spec = importlib.util.spec_from_file_location("embed", ROOT / "scripts/embed-build-identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.embed(root, version, revision)


@pytest.mark.parametrize("length", [40, 64])
def test_build_inputs_are_normalized_and_embedded_before_install(tmp_path, length):
    (tmp_path / "src/maps_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    embed(tmp_path, "v1.2.3", "A" * length)
    assert json.loads((tmp_path / "src/maps_mcp/build_identity.json").read_text()) == {
        "service": "maps",
        "version": "1.2.3",
        "source_revision": "a" * length,
    }
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.index('python /tmp/embed-build-identity.py "$VERSION" "$VCS_REF"') < dockerfile.index(
        "uv pip install --system --compile-bytecode --no-deps ."
    )


@pytest.mark.parametrize(
    "version,revision",
    [
        ("", "a" * 40),
        ("2.0.0", "a" * 40),
        ("sha-abcdef", "a" * 40),
        ("1.2.3", ""),
        ("1.2.3", "unknown"),
        ("1.2.3", "a" * 12),
        ("1.2.3", "g" * 40),
        ("1.2.3", "a" * 41),
    ],
)
def test_bad_build_inputs_fail_without_writing_metadata(tmp_path, version, revision):
    (tmp_path / "src/maps_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    with pytest.raises(ValueError):
        embed(tmp_path, version, revision)
    assert not (tmp_path / "src/maps_mcp/build_identity.json").exists()


def test_view_escapes_html_and_script_strings_and_preserves_product_version(service):
    version = '</script><script>alert("x")</script>&\u2028'
    view = server.render_view(BuildIdentity("maps", version, '<b>"&revision-extra'))
    assert version not in view
    assert "&lt;/script&gt;&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;" in view
    assert "\\u003c/script\\u003e" in view
    assert "\\u2028" in view
    assert "&lt;b&gt;&quot;&amp;revisio" in view
    assert "__BUILD_" not in server.VIEW_HTML and "__VERSION_JSON__" not in server.VIEW_HTML
    assert f"Maps v<span>{BUILD.version}</span>" in server.VIEW_HTML
    assert f'title="Source revision: {BUILD.source_revision}"' in server.VIEW_HTML
    assert 'title="Source revision: &lt;b&gt;&quot;&amp;revision-extra"' in view
    assert "Source revision <code>&lt;b&gt;&quot;&amp;revision-extra</code>" in view
    assert server.create_server(service).version == __version__ == BUILD.version


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_native_mcp_product_version_through_supported_protocol_eras(service, mode):
    async with Client(server.create_server(service), mode=mode) as client:
        result = await client.list_tools_mcp()
        if mode == "legacy":
            assert client.server_info.name == "maps"
            assert client.server_info.version == BUILD.version
            assert client.initialize_result.server_info.version == BUILD.version
        else:
            assert client.protocol_version == "2026-07-28"
            assert result.meta[SERVER_INFO_META_KEY]["name"] == "maps"
            assert result.meta[SERVER_INFO_META_KEY]["version"] == BUILD.version


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR])
def test_every_retained_level_has_unspoofable_identity(level):
    record = logging.LogRecord("component", level, "", 0, "hello %s", ("world",), None)
    record.service = "forged"
    record.version = "forged"
    record.extras = {"source_revision": "forged", "instance": "forged", "duration_ms": 12}
    payload = json.loads(logs.JsonFormatter("maps").format(record))
    assert {key: payload[key] for key in ("service", "version", "source_revision", "instance")} == {
        "service": BUILD.service,
        "version": BUILD.version,
        "source_revision": BUILD.source_revision,
        "instance": INSTANCE,
    }
    assert payload["msg"] == "hello world" and payload["duration_ms"] == 12
    assert payload["component"] == "component" and payload["ts"].endswith("Z")


def test_embedded_identity_ignores_runtime_relabeling(tmp_path, monkeypatch):
    path = tmp_path / "build_identity.json"
    data = {"service": "maps", "version": __version__, "source_revision": "a" * 40}
    path.write_text(json.dumps(data))
    monkeypatch.setenv("VERSION", "forged")
    monkeypatch.setenv("VCS_REF", "b" * 40)
    assert _load(path) == BuildIdentity(**data)
    for field, value in (("version", "wrong"), ("service", "wrong"), ("source_revision", "a" * 12)):
        path.write_text(json.dumps({**data, field: value}))
        with pytest.raises(ValueError):
            _load(path)
    assert _load(tmp_path / "missing.json").source_revision == "unknown"


def test_uninstalled_source_reports_explicit_dev_unknown():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from maps_mcp.identity import BUILD; print(BUILD.version, BUILD.source_revision)",
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "VERSION": "forged", "VCS_REF": "b" * 40},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "dev unknown\n"


def test_stdio_stdout_is_only_native_mcp_and_startup_logs_have_identity(tmp_path):
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    result = subprocess.run(
        [sys.executable, "-m", "maps_mcp", "--transport", "stdio"],
        input=json.dumps(initialize) + "\n",
        text=True,
        capture_output=True,
        timeout=30,
        env={
            **os.environ,
            "MAPS_STATE_DIR": str(tmp_path),
            "LOG_LEVEL": "DEBUG",
            "VERSION": "forged",
            "VCS_REF": "b" * 40,
            "SOURCE_REVISION": "b" * 40,
        },
    )
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 1
    assert replies[0]["result"]["serverInfo"]["version"] == BUILD.version
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert any(r["msg"] == "server starting" for r in records)
    assert any(r["level"] == "DEBUG" for r in records)
    assert all(r["version"] == BUILD.version and r["source_revision"] == BUILD.source_revision for r in records)
    assert len({r["instance"] for r in records}) == 1
    assert records[0]["instance"] != INSTANCE


def test_setup_preserves_noisy_logger_suppression(monkeypatch):
    root = logging.getLogger()
    original_handlers, original_level = root.handlers[:], root.level
    names = ("uvicorn.access", "httpx", "httpcore", "maps_mcp.vendor")
    levels = {name: logging.getLogger(name).level for name in names}
    try:
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        logs.setup("maps")
        assert root.level == logging.DEBUG
        assert all(logging.getLogger(name).level == logging.WARNING for name in names)
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
