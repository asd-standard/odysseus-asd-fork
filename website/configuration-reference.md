---
layout: default
---

# Configuration reference: ODYSSEUS_* environment variables

<!-- This page is generated from the source tree by `scripts/generate_env_reference.py`. Do not edit it by hand: run the script instead. `tests/test_env_reference.py` fails when the committed page and the source disagree, or when a variable is read without an entry in the script's notes table. -->

Odysseus reads its runtime configuration from the Settings UI. The
environment variables below are the deployment-level escape hatches underneath
that: they are read directly from the process environment, mostly at import or
startup, and most installs never need any of them.

`.env.example` stays a short, deployment-level example on purpose - `APP_BIND`,
`APP_PORT`, `AUTH_ENABLED`, `DATABASE_URL` and a pre-seeded admin password. This
page is the complete list, which is a different job.

Truthiness is not uniform across the codebase. Where a variable is described as
"truthy" the read accepts `1`, `true`, `yes` and usually `on`; where it is
described as a switch that turns something off, the read rejects `0`, `false`,
`no` and `off` and treats everything else as on. The `Default` column is the
value the code falls back to when the variable is unset, quoted from the source.

The source tree reads **45** `ODYSSEUS_*` variables: 40 an operator may want to set, and 5 that are internal - sentinels, fixture switches, capture hooks and development tooling. The internal ones are listed too, in their own section, so this page can be checked against the source mechanically.

> This page is generated. Edit `scripts/generate_env_reference.py` and
> re-run it; `tests/test_env_reference.py` enforces that the committed page
> matches the source.

## Variables you can set

### Deployment and first run

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_ADMIN_PASSWORD` | `''` | `setup.py:101` (+3 more) | Password for the admin account created on first run. Setup refuses a value shorter than its minimum length rather than silently falling back. |
| `ODYSSEUS_ADMIN_USER` | `''` | `setup.py:100` (+1 more) | Username for the admin account created on first run. Setup uses env vars first, then an interactive prompt, then a random password. |
| `ODYSSEUS_ENABLE_HOST_DOCKER` | `''` | `src/host_docker_access.py:41` | Security-relevant. Must be exactly `true` before tools may use a mounted host Docker socket, and the socket itself must exist. |
| `ODYSSEUS_INPROCESS_POLLERS` | `'1'` | `routes/email/email_pollers.py:1523` | The same off switch for the in-process email pollers, when `odysseus-mail poll-scheduled` is the sole external driver. |
| `ODYSSEUS_INPROCESS_TASKS` | `'1'` | `app.py:1135` | Set to 0, false, no or off to stop the in-process scheduled-task runner, for deployments where an external worker drives task firing. |
| `ODYSSEUS_MODEL_KEEPALIVE` | `''` | `app.py:1050` | Opt-in periodic model keep-alive pings. Off by default: the ping path runs model discovery, so stale LAN endpoints add background pressure. |
| `ODYSSEUS_SKIP_ADMIN_PROMPT` | *unset* | `setup.py:111` | Any non-empty value suppresses the interactive admin-credential prompt even on a TTY, for unattended installs. |
| `ODYSSEUS_SLOW_REQUEST_LOG_SECONDS` | `'0.75'` | `app.py:232` | Request duration in seconds above which the middleware logs a slow-request warning. |
| `ODYSSEUS_STARTUP_WARMUPS` | `''` | `app.py:1012` | Opt-in startup pings of the configured model endpoints. Off by default because they compete with the first seconds of UI use. |

### Data directories and paths

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_DATA_DIR` | `get_default_data_dir()` | `src/constants.py:12` | Root directory for every persisted file. Prefer this over the per-path overrides; the rest of `src/constants.py` derives from it. |
| `ODYSSEUS_MAIL_ATTACHMENTS_DIR` | `os.path.join(DATA_DIR, 'mail-attachments')` | `src/constants.py:57` | Dedicated override for the mail attachment store, which otherwise lives under the data directory. |

### Model routing and providers

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_COPILOT_API_VERSION` | `'2026-06-01'` | `src/copilot.py:39` | Dated API-version header the Copilot models and chat endpoints require. |
| `ODYSSEUS_COPILOT_CLIENT_ID` | `'01ab8ac9400c4e429b23'` | `src/copilot.py:34` | GitHub OAuth client id for the Copilot device flow. The default is the public VS Code client id; override it only with your own allow-listed app. |
| `ODYSSEUS_LOCAL_MODEL_GATE` | `'true'` | `src/llm_core.py:74` | On by default. Set 0, false, no or off to drop the gate that checks a local endpoint before routing a request to it. |
| `ODYSSEUS_MISTRAL_REASONING_EFFORT` | `'high'` | `src/llm_core.py:1530` | Reasoning effort sent to Mistral thinking-capable models. The API accepts high, medium, low and none. |

### Agent loop and tool execution

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_DISABLE_MCP` | `''` | `src/builtin_mcp.py:88` | Truthy disables MCP entirely, as an escape hatch for compatibility problems with a server. |
| `ODYSSEUS_MCP_ALLOWED_COMMANDS` | `''` | `src/agent_tools/admin_tools.py:154` | Security-relevant. Comma-separated allowlist of MCP launcher basenames the agent may start. Empty by default, and the deny list still wins. |

### Browser automation

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_BROWSER_EXECUTABLE` | `''` | `src/builtin_mcp.py:113` | Absolute path to the Chrome or Chromium binary. Empty searches the usual names, then lets Playwright MCP pick its own browser. |
| `ODYSSEUS_BROWSER_ISOLATED` | `'1'` | `src/builtin_mcp.py:138` | Security-relevant. On by default, adding `--isolated` so each browser session starts clean. Set 0, false or no to keep a persistent profile. |
| `ODYSSEUS_BROWSER_MCP_CACHE` | `os.path.join(base_dir, 'data', 'local', 'playwright-mcp-cache')` | `src/builtin_mcp.py:227` | Cache directory handed to the browser MCP server, so its npm download survives a container rebuild. |
| `ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE` | `''` | `src/builtin_mcp.py:89` | Truthy refuses to start the browser MCP server unless its npm package is already in the npx cache, instead of installing it at startup. |
| `ODYSSEUS_BROWSER_NO_SANDBOX` | `'1'` | `src/builtin_mcp.py:141` | Security-relevant. On by default, adding `--no-sandbox` because the Docker image cannot use the Chromium sandbox. Set 0, false or no to keep it. |

### Email

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_DOCUMENT_OWNER` | `''` | `mcp_servers/email_server.py:184` | Owner stamped on documents the email MCP server creates. Stdio MCP tools get no authenticated user, so without this a draft is invisible. |
| `ODYSSEUS_IMAP_TIMEOUT_SECONDS` | *unset* | `routes/email/email_helpers.py:1158` | IMAP socket timeout in seconds, clamped to 5-300. A non-numeric value falls back to 30 rather than failing. |

### Calendar, notes and single-user mode

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_ALLOW_PRIVATE_CALDAV` | `'0'` | `src/caldav_sync.py:52` | Security-relevant. Truthy lets CalDAV sync reach private and link-local addresses. Off by default; this is an SSRF guard. |
| `ODYSSEUS_FALLBACK_OWNER` | `'owner@localhost'` | `routes/calendar_routes.py:65` | Owner address that single-user mode attributes an unauthenticated request to. Only reachable while single-user mode is on. |
| `ODYSSEUS_SINGLE_USER` | `'1'` | `routes/calendar_routes.py:66` | Security-relevant. On by default. Set to 0 on a real multi-user install so unauthenticated calendar writes are rejected rather than absorbed. |

### Upload and media limits

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_CHAT_UPLOAD_MAX_BYTES` | `10 * 1024 * 1024` | `src/upload_limits.py:33` | Maximum bytes accepted for a chat attachment. |
| `ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES` | `25 * 1024 * 1024` | `src/upload_limits.py:53` | Maximum bytes accepted for an attachment added while composing mail. |
| `ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES` | `25 * 1024 * 1024` | `src/upload_limits.py:44` | Maximum bytes accepted for an image handed to a gallery transform. |
| `ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES` | `100 * 1024 * 1024` | `src/upload_limits.py:41` | Maximum bytes accepted for a gallery upload. |
| `ODYSSEUS_ICS_MAX_BYTES` | `10 * 1024 * 1024` | `src/upload_limits.py:59` | Maximum bytes accepted for an imported ICS file. |
| `ODYSSEUS_MEMORY_IMPORT_MAX_BYTES` | `10 * 1024 * 1024` | `src/upload_limits.py:47` (+1 more) | Maximum bytes accepted for a memory import file. |
| `ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES` | `25 * 1024 * 1024` | `src/upload_limits.py:50` (+1 more) | Maximum bytes accepted for a personal-documents upload. |
| `ODYSSEUS_STT_MAX_AUDIO_BYTES` | `25 * 1024 * 1024` | `src/upload_limits.py:56` | Maximum bytes accepted for an audio file submitted for transcription. |

### Speech and vision models

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_TTS_CACHE_MAX_BYTES` | `500 * 1024 * 1024` | `services/tts/tts_service.py:47` | Cap on the synthesized-speech cache. A non-numeric value falls back to the default. |

### Auth and internal API

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_INTERNAL_BASE` | *unset* | `src/constants.py:125` | Base URL the in-app tool layer uses for loopback HTTP calls. Set it when the app is not reachable at the port it thinks it is bound to. |
| `ODYSSEUS_INTERNAL_TOKEN` | *unset* | `core/middleware.py:20` | Security-relevant. Token that lets the in-app tool layer reach admin-gated routes over loopback. Unset generates a fresh per-process token, which is what you want unless something outside the process needs the same value. |

### Integrations (Claude, Codex)

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_API_TOKEN` | `''` | `integrations/claude/skills/odysseus/scripts/odysseus_api.py:31` (+1 more) | API token those scripts authenticate with. Both this and the URL are required; the scripts name whichever is missing. |
| `ODYSSEUS_URL` | `''` | `integrations/claude/skills/odysseus/scripts/odysseus_api.py:30` (+1 more) | Base URL of the Odysseus instance the bundled integration scripts call. |

## Internal and development-only variables

Listed for completeness. Setting one of these on a real install is either a no-op or a way to break something quietly.

### Model routing and providers

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_COPILOT_EDITOR_VERSION` | `'Odysseus/1.0'` | `src/copilot.py:54` | Editor-version header presented to the Copilot API. Kept stable on purpose. |
| `ODYSSEUS_COPILOT_INTEGRATION_ID` | `'vscode-chat'` | `src/copilot.py:51` | Integration id presented to the Copilot API. Kept stable on purpose. |
| `ODYSSEUS_COPILOT_USER_AGENT` | `'Odysseus/1.0'` | `src/copilot.py:48` | Editor-like User-Agent presented to the Copilot API. Kept stable on purpose. |

### Testing, capture and development tooling

| Variable | Default | Read in | What it does |
|---|---|---|---|
| `ODYSSEUS_DOCKER_TEST_IMAGE` | `'odysseus-odysseus:latest'` | `tests/test_docker_devops_hardening.py:142` | Docker image tag the entrypoint hardening test inspects before running. Defaults to the locally built compose image; the test skips when absent. |
| `ODYSSEUS_SKIP_RUN_HINT` | *unset* | `setup.py:283` | Any non-empty value suppresses the `start the server with` hint at the end of setup. `start-macos.sh` sets it because it starts the server itself. |

## How this page is generated

The generator walks the Python sources under `app.py`, `launcher.py`, `setup.py`, `companion`, `config`, `core`, `integrations`, `mcp_servers`, `routes`, `scripts`, `services`, `src`, `tests` and finds
reads three ways, because no single pattern covers the codebase:

- Direct reads: `os.getenv(...)`, and any `.get` / `.setdefault` / `.pop` call or
  subscript keyed by an `ODYSSEUS_*` literal, including names held in a
  module-level constant. The receiver is not required to be `os.environ`, because
  several call sites read through a mapping passed in as an argument
  (`src/tool_index.py`, `src/host_docker_access.py`).
- Calls to env-reader helpers - any function that forwards one of its own
  parameters to an environment read. This is detected rather than hardcoded, so a
  new helper needs no change here. It is what finds the upload caps in
  `src/upload_limits.py` and the media-ingress overrides in
  `src/media_ingress.py`.
- A regex sweep of the raw file text, for reads the AST cannot see.
  `routes/cookbook_helpers.py` builds an Ollama probe script as a list of source
  lines, so one read lives inside a string literal.

The three passes are not redundancy. A line-based grep for a direct
`os.environ.get("ODYSSEUS_...` call finds 32 of the 45 variables on this
page. What it misses is reads through an env-reader helper, reads whose call
spans more than one line, reads whose variable name is held in a module
constant, and reads through a mapping passed in as an argument - which is the
whole reason this page is generated rather than maintained.

The `Default` column shows the expression as written, with one level of
indirection resolved: a module-level constant and a dataclass field default are
replaced by the literal they hold, so `defaults.max_media_files` shows as `4`.
Anything computed at import time - `get_default_data_dir()` - is shown as
written, because that is the honest answer. A few call sites supply their
fallback with `or` rather than a default argument; those show as *unset* and say
so in the last column.

Regenerate it with:

```bash
python3 scripts/generate_env_reference.py
```
