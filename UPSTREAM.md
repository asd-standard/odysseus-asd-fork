# Keeping this fork in sync with upstream

This fork (`local-odysseus`) is a **deliberate divergence** from
`odysseus-dev/odysseus`. We do not merge or rebase from upstream. Upstream is a
source of individual commits that we inspect, test, and selectively port.

Read this before any upstream intake round. The ledger of what has already been
decided lives in the plan file referenced at the bottom.

## Branch model

| Ref | Role |
|---|---|
| `origin/main` | Upstream's curated release branch. **This fork tracks `main`, not `dev`.** |
| `origin/dev` | Upstream's fast-moving branch (~767 commits ahead of `main` as of 2026-11-10). Read-only source of candidates. |
| `local-odysseus` | The fork. Linear history on top of `origin/main`. |
| `upstream/<YYYY-MM>` | One short-lived branch per intake round. Merged into `local-odysseus` when green. |

`local-odysseus` is never force-pushed and never rebased. Ports land as
ordinary commits on top, so the fork's history stays a readable record of what
was taken and why.

## Why not merge or rebase

A merge from `origin/dev` would drag in the runtime-Waves rewrite
(`src/agent_runtime/`, `src/containment.py`, `src/turn_contract.py`), the
workspace feature, and the frontend module rewrite. Those are excluded by
[policy](#excluded-by-policy), so the fork takes commits one at a time instead.

## The intake loop

### 1. Fetch, never merge

```bash
git fetch origin main dev
git log --oneline local-odysseus..origin/main | wc -l   # curated, usually small
git log --oneline local-odysseus..origin/dev  | wc -l   # raw upstream volume
```

### 2. Triage candidates by type

```bash
git log --no-merges --format='%h %ad %s' --date=short \
  local-odysseus..origin/dev | grep -Ei 'fix\((security|auth|upload|email)\)|feat\(provider\)'
```

| Bucket | Action |
|---|---|
| Security fixes (`fix(security)`, `fix(auth)`, `fix(upload)`) | Always take |
| Docs / CI / test infrastructure | Take, adapt to this fork's layout |
| Provider / model features | Take if additive and low-conflict |
| Runtime Waves, containment, effects, turn contract | **Never** |
| Workspace reintroduction (`routes/workspace_routes.py`, `editor/workspaces.js`) | **Never** |
| `frontend/` module rewrite, CSS fragmentation | Defer while document/editor work is active |
| Whole-spec rewrites (`docs(specs): routine ...`) | Skip — they describe upstream architecture |

### 3. Probe before committing to a port

A cherry-pick only reports truthfully on a clean tree. Probe in a throwaway
worktree:

```bash
git worktree add -q --detach /tmp/opencode/probe origin/dev
cd /tmp/opencode/probe
git cherry-pick -n <sha> && echo CLEAN || echo CONFLICT
git cherry-pick --abort 2>/dev/null; git reset -q --hard HEAD
```

CLEAN means cherry-pick it. CONFLICT means decide whether it is a **hand-port**
or **not applicable**. Check applicability first: several upstream commits fix
surfaces this fork removed (generated-image serving, cookbook tools, bulk email
export, the edit-progress endpoint). A conflict on a missing surface looks
resolvable and is not.

Clean up every probe worktree:

```bash
git worktree remove --force /tmp/opencode/probe && git worktree prune
```

### 4. Port, then prove it passes *our* tests

Applying cleanly is not working. After each port:

```bash
python3 -m py_compile <changed .py files>
python3 -m pytest tests/<touched area> -q
```

Before merging the round:

```bash
python3 -m pytest -q --ignore=tests/e2e
```

### 5. Adapt, never delete

When an upstream test references a surface this fork lacks, **skip it with a
written reason** using `pytest.mark.skip(reason=...)` or `pytest.mark.skipif`.
Never delete coverage, never comment a test out silently. Every skip should
name the missing route/attribute/how it differs.

Counts to keep honest after an intake round:

```bash
python3 scripts/generate_env_reference.py           # then --check
```

### 6. Record the decision

Every candidate gets one of: **DONE** (with fork commit sha), **N/A** (and why),
**SKIPPED** (and why), **SUPERSEDED**. This ledger is what stops the next round
re-litigating settled commits.

## Creating an intake round

```bash
git checkout local-odysseus
git pull --ff-only origin main          # tracked branch: main
git checkout -b upstream/2026-11
# ... ports ...
git checkout local-odysseus
git merge --ff-only upstream/2026-11
```

Use `--ff-only` on the merge so a round that accidentally diverged fails loudly
instead of producing a merge commit.

## Fork invariants — do not regress

These are pinned by tests; a port that breaks one is wrong, not the test:

- **Agent userspace is restricted** to the SQL library, documents, chats and
  uploads. No filesystem/workspace access.
- **Email compose accepts documents only** — gallery images are rejected
  (`tests/test_email_compose_scope.py`).
- **The agent cannot widen its own permissions**: `enable_tool` is refused,
  `disabled_tools` cannot be reset, and `POST /api/tools` /
  `PATCH /api/mcp/servers*` are blocked over `app_api`.

## Known divergences

- No runtime authority/effects/containment layer; no `src/agent_runtime/`.
- No workspace feature, no generated-image serving, no cookbook tools.
- `frontend/` directory and CSS fragmentation not adopted.
- Email modules live in `routes/email/` with self-replacing shims at the old
  flat paths (`routes/email_*.py`).
- CI runs pytest as four `--shard N/M` sections.

## Ledger

The per-commit record of the 2026-11 intake round is at
`/home/asd/.opencode/plan/cherry-pick-plan.md`. For later rounds, add a new
section (or a sibling file) rather than overwriting it.
