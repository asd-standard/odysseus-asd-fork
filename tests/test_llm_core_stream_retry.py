"""Regression tests for pre-content streaming connect retries.

Background: ``stream_llm`` had no retry, so a keep-alive socket reaped by the
upstream (or a momentary connect blip) surfaced as ``Cannot reach <host>``
(HTTP 503) and aborted an entire agent turn -- even though the endpoint was
healthy moments later. Retries must cover only the connection/response phase:
once a response exists, body-iteration errors propagate unchanged so emitted
tokens can never be replayed.
"""
import asyncio
import json

import httpx

from src import llm_core


class _FakeResponse:
    def __init__(self, lines, body_error=None):
        self.status_code = 200
        self._lines = lines
        self._body_error = body_error

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._body_error is not None:
            raise self._body_error

    async def aread(self):
        return b""


class _FakeStreamContext:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        self._client.attempts += 1
        if self._client.enter_errors:
            raise self._client.enter_errors.pop(0)
        return _FakeResponse(self._client.lines, self._client.body_error)

    async def __aexit__(self, *args):
        return False


class _FakeClient:
    def __init__(self, lines, enter_errors=(), body_error=None):
        self.lines = lines
        self.enter_errors = list(enter_errors)
        self.body_error = body_error
        self.attempts = 0

    def stream(self, method, url, **kwargs):
        return _FakeStreamContext(self)


def _drive(monkeypatch, client):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda *a, **k: False)
    monkeypatch.setattr(llm_core.LLMConfig, "RETRY_DELAY", 0)

    async def run():
        return "".join([
            chunk
            async for chunk in llm_core.stream_llm(
                "https://api.deepseek.com/v1/chat/completions",
                "deepseek-chat",
                [{"role": "user", "content": "hi"}],
                headers={"Authorization": "Bearer k"},
            )
        ])

    return asyncio.run(run())


def _deltas(blob):
    out = []
    for line in blob.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line[6:] != "[DONE]":
            try:
                payload = json.loads(line[6:])
            except ValueError:
                continue
            if "delta" in payload:
                out.append(payload["delta"])
    return out


def _content_lines(text):
    return [
        'data: ' + json.dumps({"choices": [{"delta": {"content": text}}]}),
        'data: [DONE]',
    ]


def test_retries_connect_error_before_any_content(monkeypatch):
    client = _FakeClient(
        _content_lines("Hi"),
        enter_errors=[httpx.ConnectError("stale keep-alive socket")],
    )

    blob = _drive(monkeypatch, client)

    assert client.attempts == 2
    assert "".join(_deltas(blob)) == "Hi"
    assert "event: error" not in blob


def test_retries_remote_protocol_error_before_headers(monkeypatch):
    client = _FakeClient(
        _content_lines("Hi"),
        enter_errors=[httpx.RemoteProtocolError("server disconnected")],
    )

    blob = _drive(monkeypatch, client)

    assert client.attempts == 2
    assert "".join(_deltas(blob)) == "Hi"
    assert "event: error" not in blob


def test_exhausted_retries_still_emit_cannot_reach(monkeypatch):
    client = _FakeClient(
        _content_lines("never"),
        enter_errors=[httpx.ConnectError("down")] * 3,
    )

    blob = _drive(monkeypatch, client)

    assert client.attempts == 3
    assert "Cannot reach https://api.deepseek.com" in blob
    assert '"status": 503' in blob
    assert "".join(_deltas(blob)) == ""


def test_pool_timeout_is_not_retried(monkeypatch):
    # Pool exhaustion is ours, not a stale upstream socket; foreground
    # fallback's 504 contract expects it to advance immediately.
    client = _FakeClient(
        _content_lines("never"),
        enter_errors=[httpx.PoolTimeout("pool timed out")] * 3,
    )

    blob = _drive(monkeypatch, client)

    assert client.attempts == 1
    assert "Connection pool timeout" in blob
    assert '"status": 504' in blob


def test_body_error_after_content_is_not_retried(monkeypatch):
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
    ]
    client = _FakeClient(
        lines,
        body_error=httpx.RemoteProtocolError("connection lost mid-stream"),
    )

    blob = _drive(monkeypatch, client)

    # One attempt only: retrying here would replay already-emitted tokens.
    assert client.attempts == 1
    assert "".join(_deltas(blob)) == "Hi"
    assert "event: error" in blob


def test_retry_config_defaults():
    assert llm_core.LLMConfig.STREAM_CONNECT_ATTEMPTS >= 2
    assert llm_core.LLMConfig.KEEPALIVE_EXPIRY > 0
