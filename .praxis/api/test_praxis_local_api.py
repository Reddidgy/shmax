"""Tests for the Praxis Local API skeleton (Slice 2): /health + /api/models.

Run from the ``api/`` directory:

    ./venv/bin/python -m pytest test_praxis_local_api.py -v

These tests use Flask's test client. They cover:

* /health reports ``claude_available: true`` when a resolvable `claude` stub
  exists, and ``false`` (never a 500) when it is absent;
* the availability probe is memoized within its TTL — rapid repeated /health
  calls spawn the probe at most once;
* /api/models returns the expected catalogue shape.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

import praxis_local_api
import settings_ops


@pytest.fixture
def client():
    praxis_local_api.app.config.update(TESTING=True)
    return praxis_local_api.app.test_client()


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """Clear the cached availability probe before and after each test."""
    praxis_local_api._probe_cache.update({"value": None, "timestamp": 0.0, "resolved_path": None})
    yield
    praxis_local_api._probe_cache.update({"value": None, "timestamp": 0.0, "resolved_path": None})


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear per-session chat rate-limit state around each test."""
    with praxis_local_api._chat_rate_limits_lock:
        praxis_local_api._chat_rate_limits.clear()
    yield
    with praxis_local_api._chat_rate_limits_lock:
        praxis_local_api._chat_rate_limits.clear()


@pytest.fixture(autouse=True)
def _isolate_chat_project_root(tmp_path_factory, monkeypatch):
    """Redirect ``CHAT_PROJECT_ROOT`` to a throwaway dir for every test.

    A chat turn with no valid ``project_root`` in its body falls back to
    ``CHAT_PROJECT_ROOT`` (derived from the module's location, i.e. the real
    repo). The debug-input dump writes ``.praxis/chat/debug/last_input.json``
    there — so any test posting ``_valid_chat_body()`` would pollute the real
    working tree. Pinning the fallback to an isolated tmp dir keeps the suite
    from writing artifacts into the repo while still exercising the fallback
    path. Tests that need a specific cwd still pass an explicit ``project_root``.
    """
    isolated_root = tmp_path_factory.mktemp("chat_project_root")
    monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(isolated_root))
    yield


def _valid_chat_body() -> dict:
    return {
        "messages": [{"role": "user", "content": "hello"}],
        "system_prompt": "You are terse.",
    }


def _write_fake_claude(tmp_path, *, version: str = "9.9.9 (Fake Claude)") -> str:
    """Create an executable stub that mimics ``claude --version`` and return it."""
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\necho '{version}'\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_claude_available_true_with_stub(client, tmp_path, monkeypatch):
    """A resolvable, exit-0 `claude` stub yields claude_available: true."""
    fake = _write_fake_claude(tmp_path)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    resp = client.get("/health")
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["service"] == "praxis-local-api"
    assert body["claude_available"] is True
    assert body["default_model"] == praxis_local_api.DEFAULT_MODEL
    assert "version" in body


def test_health_claude_unavailable_when_absent(client, monkeypatch):
    """No resolvable binary → claude_available: false, still HTTP 200 (never 500)."""
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: None)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["claude_available"] is False


def test_health_never_500_when_probe_raises(client, monkeypatch):
    """An exploding probe subprocess degrades to false, never a 500."""

    def _boom(*_args, **_kwargs):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(praxis_local_api.subprocess, "run", _boom)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["claude_available"] is False


def test_probe_memoized_within_ttl(client, tmp_path, monkeypatch):
    """Rapid repeated /health calls invoke the probe at most once within the TTL."""
    fake = _write_fake_claude(tmp_path)

    call_count = {"n": 0}

    def _counting_resolver():
        call_count["n"] += 1
        return fake

    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", _counting_resolver)

    for _ in range(10):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["claude_available"] is True

    assert call_count["n"] == 1


def test_probe_reprobes_after_ttl_expiry(client, tmp_path, monkeypatch):
    """Once the cache entry ages past the TTL, the next /health re-probes."""
    fake = _write_fake_claude(tmp_path)

    call_count = {"n": 0}

    def _counting_resolver():
        call_count["n"] += 1
        return fake

    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", _counting_resolver)

    client.get("/health")
    assert call_count["n"] == 1

    # Age the cached timestamp beyond the hit-TTL window.
    praxis_local_api._probe_cache["timestamp"] -= (
        praxis_local_api.CLAUDE_PROBE_TTL_HIT_SECONDS + 1
    )

    client.get("/health")
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# binary resolution
# ---------------------------------------------------------------------------
def test_resolve_honours_chat_claude_bin(tmp_path, monkeypatch):
    """CHAT_CLAUDE_BIN, when executable, overrides PATH resolution."""
    fake = _write_fake_claude(tmp_path)
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", fake)
    assert praxis_local_api._resolve_claude_binary() == fake


def test_resolve_chat_claude_bin_missing_returns_none(tmp_path, monkeypatch):
    """A configured-but-nonexistent CHAT_CLAUDE_BIN resolves to None."""
    monkeypatch.setattr(
        praxis_local_api, "CHAT_CLAUDE_BIN", str(tmp_path / "does-not-exist")
    )
    assert praxis_local_api._resolve_claude_binary() is None


# ---------------------------------------------------------------------------
# Fallback resolution (POS-1641): known locations + login-shell probe
# ---------------------------------------------------------------------------
def test_resolve_falls_back_to_known_location(tmp_path, monkeypatch):
    """When shutil.which returns None, a claude at a known location is found."""
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", "")
    monkeypatch.setattr(praxis_local_api.shutil, "which", lambda _name: None)

    # Plant a fake `claude` at the first known location.
    fake = _write_fake_claude(tmp_path)
    monkeypatch.setattr(
        praxis_local_api,
        "_KNOWN_CLAUDE_LOCATIONS",
        (fake, "/nonexistent/claude"),
    )

    resolved = praxis_local_api._resolve_claude_binary()
    assert resolved == fake
    assert praxis_local_api._probe_cache["resolved_path"] == fake


def test_resolve_falls_back_to_login_shell(tmp_path, monkeypatch):
    """When shutil.which and known locations miss, the login-shell probe finds it."""
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", "")
    monkeypatch.setattr(praxis_local_api.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        praxis_local_api, "_KNOWN_CLAUDE_LOCATIONS", ("/no/such/claude",)
    )

    fake = _write_fake_claude(tmp_path)

    # Stub subprocess.run: the login-shell `which` should return the fake path;
    # all other subprocess.run calls (including the version probe) pass through.
    real_run = praxis_local_api.subprocess.run

    def _patched_run(argv, **kwargs):
        if len(argv) >= 3 and argv[1] == "-lc" and "which claude" in argv[2]:
            import types
            return types.SimpleNamespace(returncode=0, stdout=fake + "\n", stderr="")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(praxis_local_api.subprocess, "run", _patched_run)

    resolved = praxis_local_api._resolve_claude_binary()
    assert resolved == fake
    assert praxis_local_api._probe_cache["resolved_path"] == fake


def test_resolve_uses_cached_resolved_path(tmp_path, monkeypatch):
    """A cached resolved_path is returned without re-running fallback probes."""
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", "")
    monkeypatch.setattr(praxis_local_api.shutil, "which", lambda _name: None)

    fake = _write_fake_claude(tmp_path)
    praxis_local_api._probe_cache["resolved_path"] = fake

    resolved = praxis_local_api._resolve_claude_binary()
    assert resolved == fake


def test_resolve_clears_stale_cached_path(tmp_path, monkeypatch):
    """A cached path that no longer exists is cleared and fallbacks re-run."""
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", "")
    monkeypatch.setattr(praxis_local_api.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        praxis_local_api, "_KNOWN_CLAUDE_LOCATIONS", ()
    )
    # Disable login-shell probe so it doesn't find a real claude on the host.
    monkeypatch.setattr(praxis_local_api, "_probe_login_shell", lambda: None)
    # Put a stale path in the cache
    praxis_local_api._probe_cache["resolved_path"] = "/no/longer/exists"

    # No fallback available either → None
    resolved = praxis_local_api._resolve_claude_binary()
    assert resolved is None
    assert praxis_local_api._probe_cache["resolved_path"] is None


def test_health_finds_claude_via_fallback_when_not_on_path(client, tmp_path, monkeypatch):
    """The /health endpoint returns claude_available:true via fallback resolution
    even when ~/.local/bin is NOT on the process-level PATH (POS-1641 AC §8)."""
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", "")
    monkeypatch.setattr(praxis_local_api.shutil, "which", lambda _name: None)

    fake = _write_fake_claude(tmp_path)
    monkeypatch.setattr(
        praxis_local_api, "_KNOWN_CLAUDE_LOCATIONS", (fake,)
    )

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["claude_available"] is True


def test_health_false_when_genuinely_missing(client, monkeypatch):
    """When Claude is genuinely not installed, all fallbacks return None → false
    (POS-1641 AC §10: no false-positive in the reverse direction)."""
    monkeypatch.setattr(praxis_local_api, "CHAT_CLAUDE_BIN", "")
    monkeypatch.setattr(praxis_local_api.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        praxis_local_api, "_KNOWN_CLAUDE_LOCATIONS", ("/no/such/claude",)
    )
    # Ensure the login-shell probe also fails
    real_run = praxis_local_api.subprocess.run

    def _failing_shell(argv, **kwargs):
        if len(argv) >= 3 and argv[1] == "-lc":
            import types
            return types.SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(praxis_local_api.subprocess, "run", _failing_shell)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["claude_available"] is False


# ---------------------------------------------------------------------------
# Asymmetric probe TTL (POS-1641)
# ---------------------------------------------------------------------------
def test_miss_ttl_shorter_than_hit_ttl(client, tmp_path, monkeypatch):
    """A failed probe (miss) expires sooner than a successful one (hit)."""
    # Start with a miss.
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: None)
    resp = client.get("/health")
    assert resp.get_json()["claude_available"] is False

    # Advance time past the miss TTL but within the hit TTL.
    praxis_local_api._probe_cache["timestamp"] -= (
        praxis_local_api.CLAUDE_PROBE_TTL_MISS_SECONDS + 1
    )

    # Now resolve to a real binary — the re-probe should pick it up.
    fake = _write_fake_claude(tmp_path)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    resp = client.get("/health")
    assert resp.get_json()["claude_available"] is True


def test_hit_ttl_survives_within_window(client, tmp_path, monkeypatch):
    """A successful probe is still fresh within the hit TTL — no re-probe."""
    fake = _write_fake_claude(tmp_path)

    call_count = {"n": 0}

    def _counting_resolver():
        call_count["n"] += 1
        return fake

    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", _counting_resolver)

    client.get("/health")
    assert call_count["n"] == 1

    # Advance past miss TTL but within hit TTL — no re-probe.
    praxis_local_api._probe_cache["timestamp"] -= (
        praxis_local_api.CLAUDE_PROBE_TTL_MISS_SECONDS + 1
    )

    resp = client.get("/health")
    assert resp.get_json()["claude_available"] is True
    assert call_count["n"] == 1  # still 1, not re-probed


# ---------------------------------------------------------------------------
# /api/models
# ---------------------------------------------------------------------------
def test_models_catalog_shape(client):
    """/api/models returns {default, available:[{id,name,tier}]} with all tiers."""
    resp = client.get("/api/models")
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["default"] == praxis_local_api.DEFAULT_MODEL

    available = body["available"]
    assert isinstance(available, list)
    assert len(available) == 5

    for entry in available:
        assert set(entry.keys()) == {"id", "name", "tier"}
        assert all(isinstance(entry[k], str) and entry[k] for k in ("id", "name", "tier"))

    tiers = {entry["tier"] for entry in available}
    assert tiers == {"fast", "balanced", "powerful", "creative"}


# ---------------------------------------------------------------------------
# /api/chat — pre-stream validation (plain JSON, never an in-stream event)
# ---------------------------------------------------------------------------
@pytest.fixture
def _claude_available(monkeypatch):
    """Force is_claude_available() → True so validation/spawn paths run."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("not-an-object", "object"),
        ({}, "messages"),
        ({"messages": [], "system_prompt": "x"}, "non-empty"),
        ({"messages": ["bad"], "system_prompt": "x"}, "must be an object"),
        (
            {"messages": [{"role": "system", "content": "x"}], "system_prompt": "x"},
            "role",
        ),
        (
            {"messages": [{"role": "user", "content": 5}], "system_prompt": "x"},
            "content",
        ),
        (
            {"messages": [{"role": "user", "content": "hi"}]},
            "system_prompt",
        ),
        (
            {"messages": [{"role": "user", "content": "hi"}], "system_prompt": "  "},
            "system_prompt",
        ),
        (
            {
                "messages": [{"role": "assistant", "content": "hi"}],
                "system_prompt": "x",
            },
            "last message",
        ),
    ],
)
def test_chat_validation_returns_400(client, _claude_available, body, fragment):
    resp = client.post("/api/chat", json=body)
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"
    assert fragment in resp.get_json()["error"]


def test_chat_oversized_body_returns_413_json(client):
    """A body over MAX_CONTENT_LENGTH is rejected with a clean 413 JSON error."""
    limit = praxis_local_api.app.config["MAX_CONTENT_LENGTH"]
    oversized = b"x" * (limit + 1)
    resp = client.post(
        "/api/chat", data=oversized, content_type="application/json"
    )
    assert resp.status_code == 413
    assert resp.mimetype == "application/json"
    assert resp.get_json() == {"error": "request body too large"}


def test_chat_503_when_claude_unavailable(client, monkeypatch):
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: False)
    resp = client.post("/api/chat", json=_valid_chat_body())
    assert resp.status_code == 503
    assert "claude" in resp.get_json()["error"].lower()


def test_chat_429_cooldown_with_retry_after(client, monkeypatch, tmp_path):
    """A second request within the cooldown returns 429 + Retry-After."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    # Make the spawned stream trivial + fast so the first request completes.
    fake = _write_streaming_claude(tmp_path, lines=[json.dumps({"type": "result", "is_error": False})])
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    first = client.post("/api/chat", json=_valid_chat_body())
    first.get_data()  # drain the stream so the generator + cleanup run
    assert first.status_code == 200

    second = client.post("/api/chat", json=_valid_chat_body())
    assert second.status_code == 429
    body = second.get_json()
    assert body["retry_after"] >= 1
    assert int(second.headers["Retry-After"]) >= 1


def test_chat_rate_limit_distinct_sessions(client, monkeypatch, tmp_path):
    """Two distinct session-id headers are limited independently."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    fake = _write_streaming_claude(tmp_path, lines=[json.dumps({"type": "result", "is_error": False})])
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    r1 = client.post("/api/chat", json=_valid_chat_body(), headers={"X-Chat-Session-Id": "a"})
    r1.get_data()
    r2 = client.post("/api/chat", json=_valid_chat_body(), headers={"X-Chat-Session-Id": "b"})
    r2.get_data()
    assert r1.status_code == 200
    assert r2.status_code == 200  # different session → not limited


# ---------------------------------------------------------------------------
# /api/chat — real spawn/read path via a fake `claude` stub on PATH
# ---------------------------------------------------------------------------
def _write_streaming_claude(tmp_path, *, lines: list[str], exit_code: int = 0, hang: bool = False) -> str:
    """Write an executable stub that emits ``lines`` of stream-json then exits.

    With ``hang=True`` the stub sleeps forever after emitting its lines, to
    simulate a long-running turn for the disconnect/cleanup test.
    """
    stub = tmp_path / "claude"
    body = ["#!/usr/bin/env python3", "import sys, time"]
    for line in lines:
        body.append(f"sys.stdout.write({line!r} + '\\n')")
    body.append("sys.stdout.flush()")
    if hang:
        body.append("time.sleep(3600)")
    body.append(f"sys.exit({exit_code})")
    stub.write_text("\n".join(body) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub)


def _sse_events(raw: bytes) -> list[tuple[str, dict]]:
    """Parse a raw SSE byte stream into a list of (event, data) tuples."""
    events: list[tuple[str, dict]] = []
    for frame in raw.decode().split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        event_name = None
        data = None
        for fline in frame.splitlines():
            if fline.startswith("event: "):
                event_name = fline[len("event: ") :]
            elif fline.startswith("data: "):
                data = json.loads(fline[len("data: ") :])
        if event_name is not None:
            events.append((event_name, data))
    return events


def test_chat_happy_path_streams_thinking_text_then_done(client, monkeypatch, tmp_path):
    """A scripted stub yields thinking/text then a single terminal done."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)

    scripted = [
        json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                },
            }
        ),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "Hi!"},
                },
            }
        ),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}),
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    resp = client.post("/api/chat", json=_valid_chat_body())
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Accel-Buffering"] == "no"

    events = _sse_events(resp.get_data())
    names = [name for name, _ in events]
    # The first frame is the new detached-run id, then the streamed content.
    assert names == ["run", "thinking", "text", "done"]
    assert isinstance(events[0][1]["run_id"], str) and events[0][1]["run_id"]
    assert events[1][1] == {"text": "hmm"}
    assert events[2][1] == {"text": "Hi!"}
    assert events[-1][1]["full_text"] == "Hi!"
    # exactly one terminal
    assert names.count("done") + names.count("error") == 1


def test_chat_spawns_claude_in_project_root(client, monkeypatch, tmp_path):
    """claude is spawned with cwd == CHAT_PROJECT_ROOT, not the server's cwd."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    scripted = [
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
        ),
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)
    monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))

    captured = {}
    real_popen = praxis_local_api.subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(praxis_local_api.subprocess, "Popen", _capturing_popen)

    resp = client.post("/api/chat", json=_valid_chat_body())
    resp.get_data()  # drain the stream so the spawn happens
    assert captured["cwd"] == str(tmp_path)


def _capture_spawn_cwd(client, monkeypatch, tmp_path, body):
    """Spawn claude via /api/chat and return the cwd it was launched with."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    scripted = [
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
        ),
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    captured = {}
    real_popen = praxis_local_api.subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(praxis_local_api.subprocess, "Popen", _capturing_popen)
    client.post("/api/chat", json=body).get_data()
    return captured["cwd"]


def test_chat_uses_request_project_root_as_cwd(client, monkeypatch, tmp_path):
    """A valid `project_root` in the request body overrides CHAT_PROJECT_ROOT."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    # The fallback points elsewhere, so a match proves the request value was used.
    monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path / "fallback"))

    body = {**_valid_chat_body(), "project_root": str(project_dir)}
    cwd = _capture_spawn_cwd(client, monkeypatch, tmp_path, body)
    assert cwd == str(project_dir)


def test_chat_invalid_project_root_falls_back(client, monkeypatch, tmp_path):
    """A missing/relative `project_root` falls back to CHAT_PROJECT_ROOT."""
    monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))

    body = {**_valid_chat_body(), "project_root": "/no/such/directory/here"}
    cwd = _capture_spawn_cwd(client, monkeypatch, tmp_path, body)
    assert cwd == str(tmp_path)


def test_chat_nonzero_exit_yields_single_error_no_500(client, monkeypatch, tmp_path):
    """A stub that exits non-zero produces a single error event, never a 500."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    fake = _write_streaming_claude(tmp_path, lines=["garbage-not-json"], exit_code=2)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    resp = client.post("/api/chat", json=_valid_chat_body())
    assert resp.status_code == 200  # the stream itself opened fine

    events = _sse_events(resp.get_data())
    names = [name for name, _ in events]
    assert names.count("error") == 1
    assert "done" not in names


def test_chat_disconnect_does_not_kill_detached_run(client, monkeypatch, tmp_path):
    """A client disconnect stops only the tail — the detached run keeps running.

    This is the inverse of the old behavior: a generation is now owned by the
    helper's RunManager, not the HTTP connection, so a browser refresh (which
    drops the SSE socket) must NOT kill the ``claude`` subprocess. We stop the
    run explicitly afterward to reap the hanging stub.
    """
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)

    scripted = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "streaming..."},
                },
            }
        )
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted, hang=True)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    # Capture the spawned process so we can assert it survives the disconnect.
    spawned = {}
    real_popen = praxis_local_api.subprocess.Popen

    def _tracking_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned["proc"] = proc
        return proc

    monkeypatch.setattr(praxis_local_api.subprocess, "Popen", _tracking_popen)

    resp = client.post("/api/chat", json=_valid_chat_body())
    gen = resp.response  # the tail generator

    # Pull frames until the streamed text arrives (run frame is first).
    run_id = None
    for chunk in gen:
        if b"event: run" in chunk:
            run_id = json.loads(chunk.decode().split("data: ", 1)[1])["run_id"]
        if b"streaming..." in chunk:
            break
    assert run_id is not None
    proc = spawned["proc"]
    assert proc.poll() is None  # alive while streaming

    # Simulate the client disconnecting mid-stream: close the tail generator.
    # This raises GeneratorExit in the endpoint generator but must NOT touch
    # the detached run/worker.
    gen.close()

    # Give any (incorrect) teardown a chance to land; the process must survive.
    for _ in range(10):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc.poll() is None, "detached run was killed by a client disconnect"
    assert run_id in praxis_local_api.RUNS.active_run_ids()

    # Explicitly stop the run to reap the hanging stub (no orphan left behind).
    praxis_local_api.RUNS.stop(run_id)
    for _ in range(50):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc.poll() is not None, "stop() did not reap the run's process group"
    with pytest.raises(OSError):
        os.kill(proc.pid, 0)


# ---------------------------------------------------------------------------
# /api/chat — debug input dump (.praxis/chat/debug/last_input.json)
# ---------------------------------------------------------------------------
def test_chat_writes_debug_last_input(client, monkeypatch, tmp_path):
    """A successful turn persists the last input to .praxis/chat/debug/."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    scripted = [
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
        ),
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    body = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
        "system_prompt": "You are terse.",
        "model": "opus",
        "project_root": str(tmp_path),
    }
    resp = client.post("/api/chat", json=body)
    resp.get_data()  # drain so the turn runs to completion
    assert resp.status_code == 200

    artifact_path = tmp_path / ".praxis" / "chat" / "debug" / "last_input.json"
    assert artifact_path.exists()
    saved = json.loads(artifact_path.read_text())
    assert saved["system_prompt"] == (
        "You are terse.\n\n" + praxis_local_api._MCP_CODEX_READ_INSTRUCTION
    )
    assert saved["messages"] == body["messages"]
    assert saved["model"] == "opus"
    assert saved["last_user_message"] == "second"


def test_chat_debug_last_input_overwritten_each_turn(client, monkeypatch, tmp_path):
    """The stable last_input.json reflects the most recent send."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    scripted = [
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
        ),
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    def _send(text, session):
        body = {
            "messages": [{"role": "user", "content": text}],
            "system_prompt": "You are terse.",
            "project_root": str(tmp_path),
        }
        client.post("/api/chat", json=body, headers={"X-Chat-Session-Id": session}).get_data()

    _send("alpha", "s1")
    _send("bravo", "s2")  # distinct session sidesteps the per-session cooldown

    artifact_path = tmp_path / ".praxis" / "chat" / "debug" / "last_input.json"
    saved = json.loads(artifact_path.read_text())
    assert saved["last_user_message"] == "bravo"


def test_chat_debug_write_failure_does_not_break_turn(client, monkeypatch, tmp_path):
    """A debug-dump failure is swallowed; the turn still streams to done."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    scripted = [
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}),
    ]
    fake = _write_streaming_claude(tmp_path, lines=scripted)
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    # Force the debug write to fail at the directory-creation step.
    monkeypatch.setattr(praxis_local_api.os, "makedirs", _boom)

    body = {**_valid_chat_body(), "project_root": str(tmp_path)}
    resp = client.post("/api/chat", json=body)
    assert resp.status_code == 200

    names = [name for name, _ in _sse_events(resp.get_data())]
    assert names.count("done") + names.count("error") == 1
    assert "done" in names
    assert not (tmp_path / ".praxis" / "chat" / "debug" / "last_input.json").exists()


# ---------------------------------------------------------------------------
# /api/chat/runs/<run_id> — reattach / stop / list (Slice 2)
# ---------------------------------------------------------------------------
def _start_run(tmp_path, *, lines: list[str], hang: bool = False) -> str:
    """Start a detached run on the module singleton via a scripted stub."""
    fake = _write_streaming_claude(tmp_path, lines=lines, hang=hang)
    return praxis_local_api.RUNS.start_run(
        [{"role": "user", "content": "hello"}],
        "You are terse.",
        "sonnet",
        str(tmp_path),
        claude_binary=fake,
    )


def _wait_status(run_id: str, expected: str, *, attempts: int = 100) -> None:
    """Poll the singleton until ``run_id`` reaches ``expected`` (or give up)."""
    for _ in range(attempts):
        if praxis_local_api.RUNS.get_status(run_id) == expected:
            return
        time.sleep(0.05)


def test_reattach_replays_full_run_from_index_zero(client, tmp_path):
    """GET reattach replays the run's whole event history, including terminal."""
    run_id = _start_run(
        tmp_path,
        lines=[
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hi!"},
                    },
                }
            ),
            json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
            ),
        ],
    )
    _wait_status(run_id, "done")

    resp = client.get(f"/api/chat/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Accel-Buffering"] == "no"

    events = _sse_events(resp.get_data())
    names = [name for name, _ in events]
    # No `run` frame on reattach (that is POST /api/chat-only) — just the buffer.
    assert names == ["text", "done"]
    assert events[0][1] == {"text": "Hi!"}
    assert events[-1][1]["full_text"] == "Hi!"


def test_reattach_from_index_skips_first_n_events(client, tmp_path):
    """`?from=N` replays only events[N:]."""
    run_id = _start_run(
        tmp_path,
        lines=[
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "a"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "b"},
                    },
                }
            ),
            json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
            ),
        ],
    )
    _wait_status(run_id, "done")

    full = _sse_events(client.get(f"/api/chat/runs/{run_id}").get_data())
    assert [n for n, _ in full] == ["text", "text", "done"]

    skipped = _sse_events(client.get(f"/api/chat/runs/{run_id}?from=1").get_data())
    assert [n for n, _ in skipped] == ["text", "done"]


def test_reattach_negative_and_garbage_from_clamp_to_zero(client, tmp_path):
    """A negative or non-integer `?from=` replays from the start, never errors."""
    run_id = _start_run(
        tmp_path,
        lines=[
            json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn"}
            )
        ],
    )
    _wait_status(run_id, "done")

    for query in ("?from=-5", "?from=notanint", ""):
        resp = client.get(f"/api/chat/runs/{run_id}{query}")
        assert resp.status_code == 200
        names = [n for n, _ in _sse_events(resp.get_data())]
        assert names == ["done"]


def test_reattach_unknown_run_returns_404_json(client):
    """An unknown/expired run id → 404 JSON, NOT an SSE error frame."""
    resp = client.get("/api/chat/runs/does-not-exist")
    assert resp.status_code == 404
    assert resp.mimetype == "application/json"
    assert "error" in resp.get_json()


def test_stop_endpoint_unknown_run_returns_404_json(client):
    resp = client.post("/api/chat/runs/does-not-exist/stop")
    assert resp.status_code == 404
    assert resp.mimetype == "application/json"
    assert "error" in resp.get_json()


def test_stop_endpoint_ends_the_run(client, tmp_path):
    """POST .../stop → 200 JSON and the run reaches a terminal/stopped state."""
    run_id = _start_run(
        tmp_path,
        lines=[
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "streaming..."},
                    },
                }
            )
        ],
        hang=True,
    )
    for _ in range(100):
        if run_id in praxis_local_api.RUNS.active_run_ids():
            break
        time.sleep(0.05)
    assert run_id in praxis_local_api.RUNS.active_run_ids()

    resp = client.post(f"/api/chat/runs/{run_id}/stop")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert resp.get_json()["status"] == "stopping"

    _wait_status(run_id, "stopped")
    assert praxis_local_api.RUNS.get_status(run_id) == "stopped"
    assert run_id not in praxis_local_api.RUNS.active_run_ids()


def test_list_runs_lists_active_ids(client, tmp_path):
    """GET /api/chat/runs returns {active: [...]} reflecting running runs."""
    run_id = _start_run(
        tmp_path,
        lines=[
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "x"},
                    },
                }
            )
        ],
        hang=True,
    )
    for _ in range(100):
        if run_id in praxis_local_api.RUNS.active_run_ids():
            break
        time.sleep(0.05)

    resp = client.get("/api/chat/runs")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert run_id in resp.get_json()["active"]

    # Clean up the hanging stub so no orphan leaks into later tests.
    praxis_local_api.RUNS.stop(run_id)
    _wait_status(run_id, "stopped")


# ---------------------------------------------------------------------------
# POST /api/tasks — path-addressed, tokenless task creation (Slice 1)
# ---------------------------------------------------------------------------
def _make_project(tmp_path, *, counter: str | None = None):
    """Build a minimal PraxisOS project tree and return its root Path."""
    root = tmp_path / "proj"
    (root / ".praxis" / "config").mkdir(parents=True)
    for status in ("new", "in_progress", "completed", "on_hold", "archived"):
        (root / ".praxis" / "tasks" / status).mkdir(parents=True)
    if counter is not None:
        (root / ".praxis" / "config" / "task_counter").write_text(counter)
    return root


def _create_body(root, **overrides) -> dict:
    body = {
        "project_root": str(root),
        "title": "PROBE create",
        "why_we_need_this": "verify the endpoint",
        "acceptance_criteria": "a file appears in new/",
    }
    body.update(overrides)
    return body


def test_create_task_happy_path_201(client, tmp_path):
    """A valid create returns 201 and writes a schema-correct file in new/."""
    root = _make_project(tmp_path, counter="10")

    resp = client.post("/api/tasks", json=_create_body(root))
    assert resp.status_code == 201
    assert resp.mimetype == "application/json"

    body = resp.get_json()
    assert body["ok"] is True
    assert body["id"] == 11
    assert body["path"] == ".praxis/tasks/new/11.json"

    written = root / ".praxis" / "tasks" / "new" / "11.json"
    assert written.exists()
    task = json.loads(written.read_text())
    assert task["id"] == 11
    assert task["status"] == "new"
    assert task["title"] == "[11] PROBE create"
    assert task["on_hold_reason"] == ""
    assert task["contextRouterIncluded"] is True
    assert task["labels"] == []
    assert task["assignee"] is None
    assert "priority" not in task


def test_create_task_ignores_caller_id_and_status(client, tmp_path):
    """Caller-sent id/status are ignored; server allocates + sets 'new'."""
    root = _make_project(tmp_path, counter="50")
    body = _create_body(root, id=3, status="completed")

    resp = client.post("/api/tasks", json=body)
    assert resp.status_code == 201
    assert resp.get_json()["id"] == 51

    task = json.loads((root / ".praxis" / "tasks" / "new" / "51.json").read_text())
    assert task["id"] == 51
    assert task["status"] == "new"
    assert not (root / ".praxis" / "tasks" / "new" / "3.json").exists()


@pytest.mark.parametrize(
    "field", ["title", "why_we_need_this", "acceptance_criteria"]
)
def test_create_task_missing_required_field_400(client, tmp_path, field):
    """A missing required field → 400 naming that field, no file written."""
    root = _make_project(tmp_path)
    body = _create_body(root)
    del body[field]

    resp = client.post("/api/tasks", json=body)
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["field"] == field
    assert field in payload["error"]
    assert list((root / ".praxis" / "tasks" / "new").glob("*.json")) == []


def test_create_task_missing_project_root_404(client):
    """No project_root → 404 project not found."""
    resp = client.post("/api/tasks", json={"title": "x", "why_we_need_this": "y", "acceptance_criteria": "z"})
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "project not found"}


def test_create_task_relative_project_root_404(client):
    """A non-absolute project_root → 404."""
    resp = client.post("/api/tasks", json=_create_body("relative/path"))
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "project not found"


def test_create_task_nonexistent_project_root_404(client, tmp_path):
    """An absolute path that does not exist → 404."""
    resp = client.post("/api/tasks", json=_create_body(tmp_path / "nope"))
    assert resp.status_code == 404


def test_create_task_dir_without_praxis_404(client, tmp_path):
    """An existing dir that is not a PraxisOS project (no .praxis) → 404."""
    plain = tmp_path / "plain"
    plain.mkdir()
    resp = client.post("/api/tasks", json=_create_body(plain))
    assert resp.status_code == 404
    assert list(plain.iterdir()) == []  # nothing was created


def test_create_task_invalid_json_body_400(client):
    """A non-JSON / non-object body → 400, never a 500."""
    resp = client.post("/api/tasks", data="not json", content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# GET /api/files — path-addressed project file listing (Mention files with @)
# ---------------------------------------------------------------------------
def test_files_happy_path_200_sorted_relative(client, tmp_path):
    """A valid project_root returns 200 with sorted relative POSIX file paths."""
    root = _make_project(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "b.py").write_text("b")
    (root / "src" / "a.py").write_text("a")
    (root / "README.md").write_text("readme")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("x")

    resp = client.get("/api/files", query_string={"project_root": str(root)})
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"

    body = resp.get_json()
    assert body["ok"] is True
    files = body["files"]
    assert files == sorted(files)
    assert "README.md" in files
    assert "src/a.py" in files
    assert "src/b.py" in files
    assert not any(path.startswith("node_modules/") for path in files)
    assert not any(path.startswith(".git/") for path in files)


def test_files_missing_project_root_404(client):
    """No project_root → 404 project not found."""
    resp = client.get("/api/files")
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "project not found"}


def test_files_relative_project_root_404(client):
    """A non-absolute project_root → 404."""
    resp = client.get("/api/files", query_string={"project_root": "relative/path"})
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "project not found"}


def test_files_nonexistent_project_root_404(client, tmp_path):
    """An absolute path that does not exist → 404."""
    resp = client.get(
        "/api/files", query_string={"project_root": str(tmp_path / "nope")}
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "project not found"}


def test_files_dir_without_praxis_404(client, tmp_path):
    """An existing dir that is not a PraxisOS project (no .praxis) → 404."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.py").write_text("x")
    resp = client.get("/api/files", query_string={"project_root": str(plain)})
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "project not found"}


def test_create_task_concurrent_distinct_ids_no_clobber(client, tmp_path):
    """Concurrent endpoint creates yield distinct ids and never clobber a file."""
    root = _make_project(tmp_path)
    n = 20
    results: list[int] = []
    lock = __import__("threading").Lock()

    def _fire(_i: int) -> None:
        resp = praxis_local_api.app.test_client().post("/api/tasks", json=_create_body(root))
        assert resp.status_code == 201
        with lock:
            results.append(resp.get_json()["id"])

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(_fire, range(n)))

    assert len(set(results)) == n
    files = list((root / ".praxis" / "tasks" / "new").glob("*.json"))
    assert len(files) == n


def test_create_task_stale_counter_uses_filesystem_max(client, tmp_path):
    """A counter lower than an existing task filename → next id = fs_max + 1."""
    root = _make_project(tmp_path, counter="2")
    (root / ".praxis" / "tasks" / "in_progress" / "777.json").write_text("{}")

    resp = client.post("/api/tasks", json=_create_body(root))
    assert resp.status_code == 201
    assert resp.get_json()["id"] == 778
    assert not (root / ".praxis" / "tasks" / "new" / "3.json").exists()


# ---------------------------------------------------------------------------
# Server-side context safety net — context-router and PRD permanently
# excluded (POS-1651); only MCP Codex safety net remains.
# ---------------------------------------------------------------------------
def test_inject_never_adds_context_router_even_when_requested(tmp_path):
    """Context-router is permanently excluded from AI Chat (POS-1651)."""
    (tmp_path / "context-router.md").write_text("ROUTER BODY", encoding="utf-8")
    out = praxis_local_api._inject_missing_context(
        "ASSIGNEE",
        str(tmp_path),
        {"context_router_requested": True, "prd_requested": False},
    )
    assert "ROUTER BODY" not in out


def test_inject_never_adds_prd_even_when_requested(tmp_path):
    """PRD is permanently excluded from AI Chat (POS-1651)."""
    (tmp_path / "PRD.md").write_text("PRD BODY", encoding="utf-8")
    out = praxis_local_api._inject_missing_context(
        "ASSIGNEE", str(tmp_path), {"context_router_requested": False, "prd_requested": True}
    )
    assert "PRD BODY" not in out


def test_inject_tolerates_non_dict_flags(tmp_path):
    """Malformed/absent context_flags → no crash, no context-router/PRD injection."""
    (tmp_path / "context-router.md").write_text("ROUTER BODY", encoding="utf-8")
    (tmp_path / "PRD.md").write_text("PRD BODY", encoding="utf-8")
    out = praxis_local_api._inject_missing_context("ASSIGNEE", str(tmp_path), None)
    assert "ROUTER BODY" not in out
    assert "PRD BODY" not in out


def test_inject_mcp_codex_unconditionally_when_missing(tmp_path):
    """Codex sentinel absent from prompt → read instruction injected (no flag)."""
    out = praxis_local_api._inject_missing_context(
        "ASSIGNEE ONLY", str(tmp_path), {"context_router_requested": False, "prd_requested": False}
    )
    assert praxis_local_api._MCP_CODEX_READ_INSTRUCTION in out


def test_inject_mcp_codex_noop_when_already_present(tmp_path):
    """Frontend already inlined the codex (heading sentinel present) → no injection."""
    prompt = "You MUST FOLLOW THESE RULES\n\n# PraxisOS MCP Instructions\n\n<preamble>\ncodex body here\n</preamble>"
    out = praxis_local_api._inject_missing_context(prompt, str(tmp_path), {})
    assert out == prompt


# ---------------------------------------------------------------------------
# POST /api/resolve-path
# ---------------------------------------------------------------------------

class TestResolvePath:
    """Tests for the resolve-path endpoint used in production builds."""

    def test_resolve_path_matching_marker(self, client, tmp_path, monkeypatch):
        praxis_dir = tmp_path / ".praxis"
        praxis_dir.mkdir()
        (praxis_dir / ".resolve-path").write_text("test-marker-123", encoding="utf-8")
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))

        resp = client.post(
            "/api/resolve-path",
            json={"dirName": tmp_path.name, "marker": "test-marker-123"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["path"] == str(tmp_path).rstrip("/") + "/"

    def test_resolve_path_wrong_marker(self, client, tmp_path, monkeypatch):
        praxis_dir = tmp_path / ".praxis"
        praxis_dir.mkdir()
        (praxis_dir / ".resolve-path").write_text("real-marker", encoding="utf-8")
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))

        resp = client.post(
            "/api/resolve-path",
            json={"dirName": tmp_path.name, "marker": "wrong-marker"},
        )
        assert resp.status_code == 404

    def test_resolve_path_no_marker_file(self, client, tmp_path, monkeypatch):
        (tmp_path / ".praxis").mkdir()
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))

        resp = client.post(
            "/api/resolve-path",
            json={"dirName": tmp_path.name, "marker": "any"},
        )
        assert resp.status_code == 404

    def test_resolve_path_bad_body(self, client):
        resp = client.post("/api/resolve-path", json={"dirName": 123})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POS-1846: pending message queue endpoints
# ---------------------------------------------------------------------------


def _start_run_with_session(tmp_path, *, session_id: str, lines: list[str], hang: bool = False) -> str:
    """Start a detached run on the module singleton with a session_id, so its
    queue is registered on the QUEUE singleton (see RunManager.start_run)."""
    fake = _write_streaming_claude(tmp_path, lines=lines, hang=hang)
    return praxis_local_api.RUNS.start_run(
        [{"role": "user", "content": "hello"}],
        "You are terse.",
        "sonnet",
        str(tmp_path),
        claude_binary=fake,
        session_id=session_id,
    )


def _hanging_run_lines() -> list[str]:
    return [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "streaming..."},
                },
            }
        )
    ]


def test_queue_append_unknown_run_returns_404(client):
    resp = client.post("/api/chat/runs/does-not-exist/queue", json={"message": "hi"})
    assert resp.status_code == 404
    assert resp.mimetype == "application/json"
    assert "error" in resp.get_json()


def test_queue_get_unknown_run_returns_404(client):
    resp = client.get("/api/chat/runs/does-not-exist/queue")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_queue_delete_unknown_run_returns_404(client):
    resp = client.delete("/api/chat/runs/does-not-exist/queue")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_queue_drain_unknown_run_returns_404(client):
    resp = client.post("/api/chat/runs/does-not-exist/queue/drain")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_queue_append_missing_message_returns_400(client, tmp_path):
    run_id = _start_run_with_session(
        tmp_path, session_id="queue-session-1", lines=_hanging_run_lines(), hang=True
    )
    try:
        resp = client.post(f"/api/chat/runs/{run_id}/queue", json={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

        resp = client.post(f"/api/chat/runs/{run_id}/queue", json={"message": "   "})
        assert resp.status_code == 400
    finally:
        praxis_local_api.RUNS.stop(run_id)


def test_queue_append_get_roundtrip(client, tmp_path):
    run_id = _start_run_with_session(
        tmp_path, session_id="queue-session-2", lines=_hanging_run_lines(), hang=True
    )
    try:
        resp = client.post(f"/api/chat/runs/{run_id}/queue", json={"message": "first"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["queue"] == ["first"]
        assert body["length"] == 1

        resp = client.post(f"/api/chat/runs/{run_id}/queue", json={"message": "second"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["queue"] == ["first", "second"]
        assert body["length"] == 2

        resp = client.get(f"/api/chat/runs/{run_id}/queue")
        assert resp.status_code == 200
        assert resp.get_json()["queue"] == ["first", "second"]
    finally:
        praxis_local_api.RUNS.stop(run_id)


def test_queue_delete_clears_queue(client, tmp_path):
    run_id = _start_run_with_session(
        tmp_path, session_id="queue-session-3", lines=_hanging_run_lines(), hang=True
    )
    try:
        client.post(f"/api/chat/runs/{run_id}/queue", json={"message": "to-clear"})

        resp = client.delete(f"/api/chat/runs/{run_id}/queue")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "cleared"

        resp = client.get(f"/api/chat/runs/{run_id}/queue")
        assert resp.get_json()["queue"] == []
    finally:
        praxis_local_api.RUNS.stop(run_id)


def test_queue_drain_returns_and_clears(client, tmp_path):
    run_id = _start_run_with_session(
        tmp_path, session_id="queue-session-4", lines=_hanging_run_lines(), hang=True
    )
    try:
        client.post(f"/api/chat/runs/{run_id}/queue", json={"message": "m1"})
        client.post(f"/api/chat/runs/{run_id}/queue", json={"message": "m2"})

        resp = client.post(f"/api/chat/runs/{run_id}/queue/drain")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["drained"] == ["m1", "m2"]
        assert body["length"] == 2

        resp = client.get(f"/api/chat/runs/{run_id}/queue")
        assert resp.get_json()["queue"] == []
    finally:
        praxis_local_api.RUNS.stop(run_id)


def test_queue_unregistered_after_run_finishes(client, tmp_path):
    """A run started WITHOUT a session_id never registers a queue; once a
    run finishes (with a session_id), its queue entry is gone."""
    run_id = _start_run_with_session(
        tmp_path, session_id="queue-session-5", lines=["done"], hang=False
    )
    _wait_status(run_id, "done")

    resp = client.get(f"/api/chat/runs/{run_id}/queue")
    assert resp.status_code == 404


class TestEnsureAbsoluteProjectPath:
    """`_ensure_absolute_project_path` delegates to `settings_ops.update_settings`
    (POS-2296) so general.yaml has a single Python write path. These tests
    monkeypatch `CHAT_PROJECT_ROOT` to a tmp dir and call the function directly."""

    def test_writes_path_when_config_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))

        praxis_local_api._ensure_absolute_project_path()

        config_file = tmp_path / ".praxis" / "config" / "general.yaml"
        content = config_file.read_text(encoding="utf-8")
        expected_path = str(tmp_path).rstrip("/\\") + os.sep

        assert settings_ops.read_general_config(str(tmp_path))["absoluteProjectPath"] == expected_path
        assert content.startswith("# PraxisOS Configuration")

    def test_fills_in_when_key_present_but_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        original = 'absoluteProjectPath: ""\nchatEnabled: true\n'
        config_file = config_dir / "general.yaml"
        config_file.write_text(original, encoding="utf-8")

        praxis_local_api._ensure_absolute_project_path()

        content = config_file.read_text(encoding="utf-8")
        expected_path = str(tmp_path).rstrip("/\\") + os.sep
        assert settings_ops.read_general_config(str(tmp_path))["absoluteProjectPath"] == expected_path
        assert "chatEnabled: true" in content

        backup_file = config_dir / "general.yaml.bak"
        assert backup_file.read_text(encoding="utf-8") == original

    def test_no_op_when_already_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        original = "absoluteProjectPath: /already/set/\nchatEnabled: true\n"
        config_file = config_dir / "general.yaml"
        config_file.write_text(original, encoding="utf-8")

        praxis_local_api._ensure_absolute_project_path()

        assert config_file.read_text(encoding="utf-8") == original
        assert not (config_dir / "general.yaml.bak").exists()

    def test_preserves_onboarding_block_scalar_byte_for_byte(self, tmp_path, monkeypatch):
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        onboarding_block = (
            "onboardingData:\n"
            "  role: Other\n"
            "  roleCustom: |-\n"
            "    line one\n"
            "    line two\n"
        )
        original = onboarding_block + "chatEnabled: true\n"
        config_file = config_dir / "general.yaml"
        config_file.write_text(original, encoding="utf-8")

        praxis_local_api._ensure_absolute_project_path()

        content = config_file.read_text(encoding="utf-8")
        assert onboarding_block in content

    def test_does_not_raise_when_config_is_a_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(praxis_local_api, "CHAT_PROJECT_ROOT", str(tmp_path))
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "general.yaml").mkdir()

        # Must not raise even though general.yaml is unwritable (a directory).
        praxis_local_api._ensure_absolute_project_path()
