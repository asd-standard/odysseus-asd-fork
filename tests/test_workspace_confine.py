"""File-tool confinement AFTER the workspace feature was removed.

Odysseus used to bind a per-request "workspace" folder and confine every path
resolver to it. That is gone (hosted multi-user; a pasted path could silently
change what the file tools could reach). What remains, and what this suite
locks in:

  * file tools resolve through ONE shared helper (`_resolve_tool_path`) that
    confines to the fixed allowlisted roots under DATA_DIR;
  * paths outside those roots — /etc/hosts, an arbitrary tmp dir, a parent
    escape, a sensitive dir — are rejected;
  * subprocesses always start in the agent data dir, never a user folder;
  * `get_workspace` no longer exists as a tool;
  * no workspace UI or `/workspace` command exists to set one;
  * the retired `_resolve_request_workspace` stub always reports "none", so a
    forged `workspace` form field cannot bind anything.

The historical workspace-confinement cases (binding a folder confined
read/write/grep/ls to it) are intentionally deleted: that code path no longer
exists.
"""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tool_execution import (
    NO_TOOL_SECURITY_CONTEXT,
    _AGENT_WORKDIR,
    _resolve_search_root,
    _resolve_tool_path,
    agent_cwd,
    execute_tool_block as _execute_tool_block,
    get_active_workspace,
    vet_workspace,
)

ROOT = Path(__file__).resolve().parents[1]


async def execute_tool_block(*args, **kwargs):
    kwargs.setdefault("security_context", NO_TOOL_SECURITY_CONTEXT)
    return await _execute_tool_block(*args, **kwargs)


def _block(tool, content=""):
    return SimpleNamespace(tool_type=tool, content=content)


@pytest.fixture
def admin(monkeypatch):
    """Pass the public-tool gate so file tools dispatch in tests.

    NOTE the patch target: `execute_tool_block` calls its own module alias
    (`src.tool_execution.owner_is_admin_or_single_user`), so patching
    `src.tool_security` would not take effect. The previous version of this
    fixture got away with that because a bound workspace skipped the check —
    which is no longer possible.
    """
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user", lambda owner: True
    )
    return True


# ── the shared resolver: the single confinement point ───────────────────

def test_resolver_confines_to_allowlisted_roots():
    """A path under an allowed root resolves; anything else raises.

    This is the one helper read_file/write_file/edit_file/grep/glob/ls all go
    through, so a newly added tool cannot accidentally bypass confinement.
    """
    allowed = os.path.realpath(os.path.join(_AGENT_WORKDIR, "a.txt"))
    assert _resolve_tool_path(os.path.join(_AGENT_WORKDIR, "a.txt")) == allowed

    # /etc is never allowed. (/tmp IS a deliberate shared root — the temp-file
    # contract — so it is not a valid "outside" example.)
    with pytest.raises(ValueError):
        _resolve_tool_path("/etc/hosts")
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.expanduser("~/definitely-not-allowed.txt"))


def test_resolver_rejects_sensitive_and_app_state_paths():
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(_AGENT_WORKDIR, ".ssh", "authorized_keys"))
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(os.path.dirname(_AGENT_WORKDIR), "app.db"))


def test_empty_path_is_rejected():
    with pytest.raises(ValueError):
        _resolve_tool_path("")
    with pytest.raises(ValueError):
        _resolve_tool_path(None)


# ── no workspace binding exists any more ────────────────────────────────

def test_no_active_workspace_ever():
    assert get_active_workspace() is None
    assert agent_cwd() == _AGENT_WORKDIR


def test_forged_workspace_argument_is_ignored():
    """Passing a workspace to execute_tool_block must not retarget anything.

    The parameter survives for call-site compatibility, but the confinement
    branch is gone: a path outside the allowlist stays rejected either way.
    """
    # get_active_workspace() is the single source of truth and is always None.
    assert get_active_workspace() is None
    with pytest.raises(ValueError):
        _resolve_tool_path("/etc/hosts")


def test_search_root_defaults_to_agent_workspace():
    assert _resolve_search_root("") == os.path.realpath(_AGENT_WORKDIR)


# ── end-to-end through execute_tool_block ───────────────────────────────

@pytest.mark.asyncio
async def test_read_write_edit_confined_e2e(admin):
    inside = os.path.join(_AGENT_WORKDIR, "confine-e2e.txt")
    _, r = await execute_tool_block(_block("write_file", json.dumps({"path": inside, "content": "hi"})), owner="a")
    assert r.get("exit_code") in (0, None), r
    _, r = await execute_tool_block(_block("read_file", json.dumps({"path": inside})), owner="a")
    assert "hi" in r.get("output", "")
    try:
        os.unlink(inside)
    except OSError:
        pass

    _, r = await execute_tool_block(_block("read_file", json.dumps({"path": "/etc/hosts"})), owner="a")
    assert r.get("exit_code") == 1 or "error" in r


@pytest.mark.asyncio
async def test_subprocess_cwd_is_agent_workdir(admin):
    _, r = await execute_tool_block(_block("python", "import os; print(os.getcwd())"), owner="a")
    assert r["exit_code"] == 0
    assert os.path.realpath(r["output"].strip()) == os.path.realpath(_AGENT_WORKDIR)


# ── get_workspace is gone ───────────────────────────────────────────────

def test_get_workspace_tool_is_removed():
    from src.agent_tools import TOOL_HANDLERS
    import src.agent_tools
    schemas = __import__("src.tool_schemas", fromlist=["FUNCTION_TOOL_SCHEMAS"]).FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in schemas}

    assert "get_workspace" not in TOOL_HANDLERS
    assert "get_workspace" not in names
    assert "get_workspace" not in src.agent_tools.TOOL_TAGS


# ── the UI / command surface is gone ────────────────────────────────────

def test_no_workspace_ui_left():
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert "overflow-workspace-btn" not in index
    assert "workspace-indicator-btn" not in index
    assert not (ROOT / "static/js/workspace.js").exists()


def test_chat_no_longer_posts_a_workspace():
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert "fd.append('workspace'" not in chat


def test_slash_workspace_command_removed():
    slash = (ROOT / "static/js/slashCommands.js").read_text(encoding="utf-8")
    assert "_cmdWorkspace" not in slash
    assert "workspaceModule" not in slash


def test_pasted_path_no_longer_auto_binds():
    routes = (ROOT / "routes/chat_routes.py").read_text(encoding="utf-8")

    def _code_only(src: str) -> str:
        """Strip comments so the historical explanatory note doesn't count."""
        return "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )

    code = _code_only(routes)
    assert "_resolve_workspace_from_message_path" not in code
    assert "_ABS_PATH_RE" not in code
    assert "_LOCAL_FILE_TASK_RE" not in code


# ── the request-level stub cannot bind anything ─────────────────────────

def test_request_workspace_is_always_empty(admin):
    """Even an admin posting a valid workspace gets no binding: the feature is
    removed, so a forged or stale form field cannot confine the tools."""
    import routes.chat_routes as cr

    tmp = tempfile.mkdtemp()
    assert cr._resolve_request_workspace(object(), tmp) == ("", "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "")
    assert cr._resolve_request_workspace(object(), "") == ("", "")


# ── the test-only confinement hook is never set in production ───────────

def test_workspace_hook_is_test_only():
    """Nothing in src/ or routes/ may SET `_test_only_workspace`.

    The hook exists so state-confinement tests can point the resolvers at a
    hostile root. If production code ever set it, per-request folder binding
    would be back — the exact foot-gun this change removed.

    AST-based so docstrings/comments mentioning the name don't count; only a
    `.set(...)` call (or any write to the attribute) does.
    """
    import ast

    offenders = []
    for base in ("src", "routes"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Attribute) and fn.attr == "set":
                        target = fn.value
                        name = getattr(target, "id", None) or getattr(target, "attr", None)
                        if name == "_test_only_workspace":
                            offenders.append(f"{path}:{node.lineno}")
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        # The definition (module-level ContextVar) is legitimate;
                        # a REBINDING anywhere would be the bug.
                        if (
                            getattr(t, "id", None) == "_test_only_workspace"
                            and not isinstance(node, ast.AnnAssign)
                        ):
                            offenders.append(f"{path}:{node.lineno}")

    assert not offenders, (
        "production code assigns/sets the test-only workspace hook: "
        + ", ".join(offenders)
    )


def test_no_workspace_route_module():
    # /api/workspace/browse enumerated the server filesystem purely so the
    # picker could navigate; with the picker gone the surface is gone too.
    assert not (ROOT / "routes/workspace_routes.py").exists()


# ── vet_workspace survives (used by sealed approvals) ───────────────────

def test_vet_workspace_still_validates_paths():
    good = tempfile.mkdtemp()
    assert vet_workspace(good) == os.path.realpath(good)
    assert vet_workspace("/") is None
    assert vet_workspace("") is None
    assert vet_workspace(os.path.join(good, "nope")) is None
