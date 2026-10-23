"""Frontend module identity: every core module must be fetched exactly once.

A browser caches ES modules by their full URL, query string included. If
``document.js`` is referenced as both ``document.js?v=A`` and
``document.js?v=B`` — or if it is loaded once by ``app.js``'s ``import`` and
again by a bare ``<script type="module" src=...>`` tag — the page ends up with
**two module instances**. Each owns a private copy of module-level state
(``docs``, ``activeDocId``), so the instance that renders the editor is not the
one the chat asks for its active document.

That is exactly the reported failure: documents visibly open in the editor while
the agent is told "no document is open in the editor". This test locks the
import graph so it cannot regress silently — nothing about it is visible at
runtime except the symptom.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/app.js").read_text(encoding="utf-8")

# Modules whose per-instance state must be unique page-wide.
CORE_MODULES = ("document.js", "chat.js", "chatStream.js", "documentLibrary.js")


def _index_script_srcs() -> list[str]:
    return re.findall(r'<script[^>]*type="module"[^>]*src="([^"]+)"', INDEX)


def _index_preloads() -> list[str]:
    return re.findall(r'<link[^>]*rel="modulepreload"[^>]*href="([^"]+)"', INDEX)


def _app_imports() -> list[str]:
    return re.findall(r"from\s+'([^']+\.js(?:\?[^']*)?)'", APP_JS)


def _basename(src: str) -> str:
    return src.split("?")[0].rsplit("/", 1)[-1]


def test_document_js_is_not_a_bare_script_entry_point():
    # app.js imports and initialises document.js. Loading it again via a script
    # tag instantiates it a second time.
    assert not any(_basename(s) == "document.js" for s in _index_script_srcs()), (
        "document.js must only be loaded via app.js's import, not a script tag"
    )


def test_chat_stream_is_not_a_bare_script_entry_point():
    # chatStream.js registers document-level listeners at import time; a second
    # entry point registers them twice.
    assert not any(_basename(s) == "chatStream.js" for s in _index_script_srcs())


def test_every_core_module_has_a_single_url_form():
    # Collect every reference across the entry points we control.
    refs: dict[str, set[str]] = {m: set() for m in CORE_MODULES}
    for src in _index_script_srcs() + _index_preloads() + _app_imports():
        base = _basename(src)
        if base in refs:
            refs[base].add(src if "?" in src else f"{base} (no query)")

    for name, forms in refs.items():
        if len(forms) <= 1:
            continue
        # app.js imports via "./js/<name>" while index.html uses "/static/js/".
        # Normalise the path component and compare only the query strings.
        queries = {f.split("?")[1] if "?" in f else "" for f in forms}
        assert len(queries) == 1, (
            f"{name} is referenced with {len(queries)} different query strings {sorted(queries)}; "
            "the browser will fetch it as separate modules (duplicate instances). "
            f"References: {sorted(forms)}"
        )


def test_chat_modulepreload_matches_the_script_tag():
    # A modulepreload with a different ?v= does not warm the real module; it
    # makes the browser parse a second copy of the graph.
    preloads = [s for s in _index_preloads() if _basename(s) == "chat.js"]
    scripts = [s for s in _index_script_srcs() if _basename(s) == "chat.js"]
    if not preloads or not scripts:
        return  # neither or only one: nothing to mismatch
    assert preloads == scripts, (
        f"chat.js is modulepreloaded as {preloads} but script-loaded as {scripts}"
    )


def test_core_modules_are_cache_busted_everywhere():
    """Every reference to a core module must carry a ?v= (no stale imports).

    An unversioned import of a module that *is* versioned elsewhere is the same
    duplicate-instance bug in a different disguise, and it also means edits to
    that module can be served stale from the browser cache.
    """
    sources: list[tuple[str, str]] = [
        ("static/index.html", INDEX),
        ("static/app.js", APP_JS),
    ]
    for path in (ROOT / "static/js").glob("*.js"):
        sources.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))

    offenders: list[str] = []
    for name, text in sources:
        for m in re.finditer(r"""['"]([^'"]*/)?([A-Za-z0-9_-]+\.js)(\?[^'"]*)?['"]""", text):
            base = m.group(2)
            if base not in CORE_MODULES:
                continue
            # The module's own file declares nothing about itself; skip a file
            # referencing itself only through non-import strings is fine, we
            # just require the query string to be present everywhere.
            if not m.group(3):
                offenders.append(f"{name}: {m.group(0)}")

    assert not offenders, (
        "core modules referenced without a ?v= cache-busting query string "
        "(stale-cache and duplicate-instance risk):\n" + "\n".join(offenders)
    )


def test_document_js_shares_state_across_instances():
    # Defence in depth: if a duplicate instance ever appears again (a new entry
    # point, a cache-busting edit), the active document must still be visible to
    # the chat instead of silently reporting null.
    source = (ROOT / "static/js/document.js").read_text(encoding="utf-8")
    assert "window.__odysseusDocState" in source
    assert "_publishActiveDoc(" in source
    assert "return activeDocId || _sharedDocState.activeDocId || null;" in source
