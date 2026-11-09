"""Regression guard: email compose must not accept gallery images.

ZooUI/Odysseus fork policy restricts the agent's userspace to the SQL library
(library, documents, chats, uploads). The email "compose from Odysseus" flow
was narrowed to documents only for the same reason: gallery images are not an
allowed agent-reachable attachment source.

This test pins that policy so an upstream email cherry-pick cannot silently
restore the removed ``kind="gallery"`` branch in
``routes/email/email_routes.py``.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EMAIL_ROUTES = REPO / "routes" / "email" / "email_routes.py"


def _load_module():
    return ast.parse(EMAIL_ROUTES.read_text(encoding="utf-8"))


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_attachment_loader_does_not_handle_gallery():
    """_load_odysseus_attachment_source must only accept documents."""
    fn = _find_function(_load_module(), "_load_odysseus_attachment_source")
    assert fn is not None, "attachment loader not found"

    # Any comparison/branch against a bare "gallery" string literal would
    # reintroduce gallery image access.
    literals = [
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "gallery" not in literals


def test_compose_routes_reject_gallery_kind():
    """The compose routes only accept kind == "document"."""
    tree = _load_module()
    source = EMAIL_ROUTES.read_text(encoding="utf-8")

    # The documented contract is documents only.
    assert 'Expected kind and id' in source
    assert 'kind != "document"' in source or 'kind not in {"document"}' in source

    # And no gallery picker remains in either compose entry point.
    for name in ("compose_from_odysseus", "compose_from_odysseus_zip"):
        fn = _find_function(tree, name)
        assert fn is not None, f"{name} not found"
        literals = [
            node.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert "gallery" not in literals, f"{name} re-added gallery support"


def test_shim_preserves_module_identity():
    """The flat shim must resolve to the canonical module object."""
    import routes.email.email_routes as canonical
    import routes.email_routes as flat

    assert flat is canonical
