# Incident Report — Agent stopped with HTTP 503 after tool calls

- **Date:** 2026-10-25 (diagnosed and fixed)
- **Component:** `src/llm_core.py` — streaming model calls
- **Provider observed:** DeepSeek OpenAI-compatible API
- **Status:** Fixed, tested, and verified in the local suite

## Summary

Long agent turns intermittently ended with:

```
[Agent stopped: Model request failed (HTTP 503)]
```

The failure appeared only after the agent had already answered and run tool
calls. Server logs showed the terminal error came from the streaming path:

```
[agent-timing] stream_error round=9  elapsed=2.693s
  chunk='event: error\ndata: {"error": "Cannot reach https://api.deepseek.com", "status": 503}\n\n'

[agent-timing] stream_error round=10 elapsed=11.388s
  chunk='event: error\ndata: {"error": "Cannot reach https://api.deepseek.com", "status": 503}\n\n'
```

The endpoint itself was healthy. The real problem was that the **streaming
model call had no retry for a transient connection failure**, so one stale
keep-alive socket or momentary connect blip aborted the whole agent turn.
Non-streaming calls already retried; the streaming path did not.

## Impact

- A single transient connect failure could discard an otherwise successful
  multi-round agent run.
- Failures were more visible late in a turn (rounds 9–10), when several
  sequential requests had used the shared HTTP client and stale pooled
  connections were most likely to be reaped upstream.
- No data loss: the transcript and tool events were persisted, but the turn
  stopped before the model could consume the tool results.

## Diagnosis

### What the logs showed

- Both failures were **HTTP 503 with `Cannot reach <host>`**, emitted by the
  `httpx.ConnectError` / `ConnectTimeout` handlers in the streaming path.
- Round 9 failed after ~2.7 s and round 10 after ~11.4 s — consistent with a
  connection-level failure and the 10 s connect budget, not a model generation
  error.
- No `Upstream … marked unreachable (cooldown active)` line appeared, so the
  failure was not primarily the dead-host cooldown gate.

### What the network showed

From inside the Docker container:

```bash
python -c "import socket;print(socket.getaddrinfo('api.deepseek.com',443))"
# → one IPv4 address only (no IPv6 path to hang on)

for i in 1 2 3 4 5; do
  curl -o /dev/null -s -w "%{http_code} connect=%{time_connect} tls=%{time_appconnect}\n" \
    --max-time 15 https://api.deepseek.com/v1/models
  sleep 2
done
```

Result: 5/5 successful connects, 16–25 ms connect, ~40–50 ms TLS (the `401`
was only because no API key was passed to `curl`). DNS, routing, TLS, and IPv6
were ruled out.

### Root cause

`src/llm_core.py` streams through a process-wide `httpx.AsyncClient` with
keep-alive reuse. An upstream load balancer can close an idle pooled socket on
its own schedule. The next request may pick that dead socket, producing a
connect-level failure.

- `llm_call_async` (non-streaming) already retried connect failures up to
  `LLMConfig.MAX_RETRIES`.
- `stream_llm` caught the connect error and immediately emitted
  `event: error … status: 503`, with no retry.
- `agent_loop` treats a pre-content stream error as terminal, appending
  `[Agent stopped: …]` to the round text.

The result: a transient, retryable transport failure became a permanent agent
stop.

## Fix

### New retry wrapper: `_RetryingStream`

A small async context manager opens the upstream stream and retries only the
**connection / response-acquisition phase**:

- Retryable errors:
  - `httpx.ConnectError`
  - `httpx.ConnectTimeout`
  - pre-header `httpx.RemoteProtocolError` (the shape a reaped keep-alive
    socket often takes)
- Not retried:
  - `httpx.PoolTimeout` — our own connection pool is exhausted; the existing
    foreground-fallback 504 contract expects an immediate hand-off.
  - Any failure after the response object exists (including body-iteration
    errors). Retrying there could replay tokens that were already streamed.
- Backoff: `RETRY_DELAY × attempt` (0.5 s, then 1.0 s with the defaults).
- The response is acquired via explicit `__aenter__`, so body errors never
  re-enter the retry loop.

It is wired into all four streaming providers:

- OpenAI-compatible
- Anthropic
- Ollama
- ChatGPT Subscription / Responses

### Configuration

New `LLMConfig` values, overridable by environment:

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| `STREAM_CONNECT_ATTEMPTS` | `LLM_STREAM_CONNECT_ATTEMPTS` | `3` | Connection-phase attempts per streaming call. Set `1` to disable retries. |
| `KEEPALIVE_EXPIRY` | `LLM_KEEPALIVE_EXPIRY` | `30` | How long httpx reuses an idle pooled connection. Lower it if the upstream reaps idle sockets sooner. |

`KEEPALIVE_EXPIRY` is now actually applied to the shared `httpx.Limits`; it was
previously hard-coded.

### Interaction with model fallback

- Retries happen **inside** the primary candidate, before the foreground
  fallback chain sees the error.
- If all attempts still fail, the existing 503 behavior remains: the error is
  emitted, and a configured foreground fallback can still advance.
- No retry can occur after substantive output, preserving the "commit a
  candidate once it streams content" contract.

## Verification

### Automated tests

New: `tests/test_llm_core_stream_retry.py`

- retries a pre-content `ConnectError`, then succeeds;
- retries a pre-header `RemoteProtocolError`;
- emits `Cannot reach …` / 503 after attempts are exhausted;
- does **not** retry `PoolTimeout` (504 stays immediate/fallback-eligible);
- does **not** retry a body error after content was emitted;
- pins the config defaults.

Results:

- New retry tests: **6 passed**
- All `tests/test_llm_core*.py`: **284 passed**
- `tests/test_foreground_model_routing.py`: **101 passed**, with one
  pre-existing failure that also reproduces on pristine `HEAD` and is unrelated
  to this change
- `tests/test_live_fallback_round_attribution.py`: **12 passed**

### Expected production behavior

A transient connect failure now logs:

```
Stream connect to https://api.deepseek.com failed (attempt 1/3): ...; retrying in 0.50s
```

and transparently continues if a later attempt connects. Only after all
attempts fail does the chat see the terminal `event: error`, so a brief blip no
longer ends a long agent turn.

## Deployment notes

- Rebuild/restart the Odysseus container after deploying.
- Optional tuning in the container environment:

```yaml
LLM_STREAM_CONNECT_ATTEMPTS: "3"
LLM_KEEPALIVE_EXPIRY: "10"   # if the upstream closes idle sockets aggressively
```

- If 503s persist after all retries, the upstream is genuinely unavailable or
  rejecting the request. Configure a foreground fallback model and inspect
  `docker logs` for the retry lines plus the provider response.

## Files changed

- `src/llm_core.py` — config, `_RetryingStream`, all four streaming call sites
- `tests/test_llm_core_stream_retry.py` — regression coverage
- `specs/llm-models.md` — documents the retry/keep-alive contract
- `specs/incidents/2026-10-25-stream-503-connect-retry.md` — this report

## Limitations / follow-ups

- Read timeouts (`httpx.ReadTimeout`) are still terminal 504s; this change only
  covers the connection phase.
- Retry logs are warning-level only; there is no user-visible "retrying"
  indicator.
- If stale sockets remain frequent, lowering `LLM_KEEPALIVE_EXPIRY` is the
  first tuning step; the retry is a safety net, not a substitute for a correct
  keep-alive budget.
