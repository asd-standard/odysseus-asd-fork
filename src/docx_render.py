"""Convert .docx to Markdown using the mammoth bundle already shipped for the UI.

Why a Node subprocess
---------------------
Mammoth gives noticeably richer Markdown than a hand-rolled XML walker — real
tables, bold/italic, nested lists — and the browser bundle
(`static/lib/mammoth.browser.min.js`) is already vendored and is UMD, so Node can
`require` it directly. Importing is not latency-critical, so a ~0.3 s subprocess
is a fair trade for not maintaining a second HTML parser in Python.

Mammoth does NOT read `word/comments.xml`, so comments are extracted separately
by `src.office_comments` and merged afterwards. That split is deliberate: each
half does what it is good at.

If Node is unavailable the caller falls back to the plain comment-aware
extractor, which still yields comments but loses tables/formatting.
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(_REPO_ROOT, "static", "lib", "mammoth.browser.min.js")

_MAMMOTH_TIMEOUT_S = 45


class MammothUnavailable(RuntimeError):
    """Node or the vendored mammoth bundle could not be used."""


# The script is passed with -e so nothing has to be written to disk.
_NODE_SCRIPT = r"""
const path = process.argv[1];
const m = require(process.argv[2]);
const fs = require("fs");
const buf = fs.readFileSync(path);
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
m.convertToHtml({ arrayBuffer: ab })
  .then(r => { process.stdout.write(r.value); })
  .catch(e => { process.stderr.write(String(e && e.message || e)); process.exit(3); });
"""


def mammoth_available() -> bool:
    """True when both Node and the vendored bundle are present."""
    return bool(shutil.which("node")) and os.path.isfile(BUNDLE)


def docx_to_html(path: str) -> str | None:
    """Run mammoth and return its HTML, or None if it cannot be used."""
    if not mammoth_available():
        return None
    try:
        proc = subprocess.run(
            ["node", "-e", _NODE_SCRIPT, path, BUNDLE],
            capture_output=True,
            timeout=_MAMMOTH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("mammoth: node invocation failed for %s: %s", path, e)
        return None
    if proc.returncode != 0:
        logger.warning(
            "mammoth: conversion failed for %s: %s",
            path,
            (proc.stderr or b"").decode("utf-8", "replace")[:300],
        )
        return None
    return proc.stdout.decode("utf-8", "replace")


# ── HTML → Markdown ─────────────────────────────────────────────────────────
#
# Deliberately small: mammoth emits a narrow, predictable subset (p, h1-h6,
# strong, em, ul/ol/li, table/tr/td/th, a, br). Anything else degrades to text,
# which is what matters for import.

_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)[^>]*>")


def _cell_text(fragment: str) -> str:
    return _inline_to_md(fragment).replace("|", "\\|").strip()


def _inline_to_md(text: str) -> str:
    text = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", text, flags=re.S | re.I)
    text = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", text, flags=re.S | re.I)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.S | re.I)
    text = re.sub(r'<a [^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text)


def html_to_markdown(html: str) -> str:
    """Convert mammoth's HTML subset to Markdown.

    Block-level elements become paragraphs separated by a blank line; tables
    become GitHub-style pipe tables.
    """
    if not html:
        return ""

    # Tables first: their inner <p> tags must not become separate blocks.
    def _table(match: re.Match) -> str:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(1), flags=re.S | re.I)
        lines: list[str] = []
        for i, row in enumerate(rows):
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.S | re.I)
            if not cells:
                continue
            lines.append("| " + " | ".join(_cell_text(c) for c in cells) + " |")
            if i == 0:
                lines.append("|" + "|".join("---" for _ in cells) + "|")
        return "\n\n" + "\n".join(lines) + "\n\n"

    html = re.sub(r"<table[^>]*>(.*?)</table>", _table, html, flags=re.S | re.I)

    # Lists: flatten to "- item" / "1. item" one per line.
    html = re.sub(r"<li[^>]*>", "\n- ", html, flags=re.I)
    html = re.sub(r"</li>", "", html, flags=re.I)
    html = re.sub(r"</?(ul|ol)[^>]*>", "\n", html, flags=re.I)

    # Headings.
    for level in range(1, 7):
        html = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m, n=level: "\n\n" + "#" * n + " " + _inline_to_md(m.group(1)).strip() + "\n\n",
            html,
            flags=re.S | re.I,
        )

    # Paragraphs / blockquotes -> blocks.
    html = re.sub(
        r"<p[^>]*>(.*?)</p>",
        lambda m: "\n\n" + _inline_to_md(m.group(1)).strip() + "\n\n",
        html,
        flags=re.S | re.I,
    )
    html = re.sub(
        r"<blockquote[^>]*>(.*?)</blockquote>",
        lambda m: "\n\n> " + _inline_to_md(m.group(1)).strip() + "\n\n",
        html,
        flags=re.S | re.I,
    )
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = _TAG_RE.sub("", html)
    text = _html.unescape(html)

    # Collapse whitespace: many blank lines -> one, trim line ends.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def docx_to_markdown(path: str) -> Optional[str]:
    """Mammoth-backed Markdown for a .docx, or None when unavailable."""
    html = docx_to_html(path)
    if html is None:
        return None
    markdown = html_to_markdown(html)
    return markdown or None
