"""Single entry point for importing Office documents (body + comments).

Two extractors are combined on purpose:

* ``src.docx_render`` (mammoth, via Node) produces a RICH body — tables, bold,
  lists — which a hand-rolled XML walker does not.
* ``src.office_comments`` knows where every comment is anchored, which mammoth
  does not read at all.

The merge is the subtle part. Mammoth drops empty paragraphs, so a Word file's
raw ``<w:p>`` indices do not line up with the blocks it emits (a real document
here: 13 ``<w:p>``, 9 with text, 6 emitted blocks). Anchors are therefore
expressed in "text block" coordinates and then VERIFIED against the block text
before a comment is attached. If verification fails the comment is reported in
the summary as unanchored rather than silently attached to the wrong paragraph —
misanchoring review feedback is worse than admitting it could not be placed.
"""

from __future__ import annotations

import logging
import os
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Refuse to store something that is clearly not text (a renamed .doc, a corrupt
# archive). 30% non-printable is generous — real prose is ~0%.
MAX_NONPRINTABLE_RATIO = 0.30

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class OfficeImportError(Exception):
    """User-facing import failure (bad format, legacy file, junk content)."""


@dataclass
class ImportResult:
    markdown: str
    comments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def comment_count(self) -> int:
        return len(self.comments)


# ── guards ─────────────────────────────────────────────────────────────────


def _looks_like_legacy_doc(path: str) -> bool:
    """True for a Word 97-2003 binary file, even if named .docx."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return head == _OLE2_MAGIC


def _is_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path):
            return True
    except (zipfile.BadZipFile, OSError):
        return False


def nonprintable_ratio(text: str) -> float:
    """Fraction of characters that are neither printable nor whitespace."""
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        if ch.isspace():
            continue
        if not ch.isprintable() or unicodedata.category(ch) in ("Cc", "Cs", "Co"):
            bad += 1
    return bad / max(1, len(text))


def _reject_junk(text: str, path: str) -> None:
    ratio = nonprintable_ratio(text)
    if ratio > MAX_NONPRINTABLE_RATIO:
        raise OfficeImportError(
            f"Could not extract readable text from {os.path.basename(path)} "
            f"({ratio:.0%} of the content is non-printable). If this is a "
            "Word 97-2003 file, open it and use Save As -> .docx."
        )


# ── merge ──────────────────────────────────────────────────────────────────


def _block_matches(block: str, excerpt: Optional[str]) -> bool:
    """Does this Markdown block look like the commented paragraph?"""
    if not excerpt:
        return False
    squeeze = lambda s: " ".join((s or "").split()).strip()  # noqa: E731
    b, e = squeeze(block), squeeze(excerpt)
    if not b or not e:
        return False
    probe = e[:80]
    # Prefix test covers the normal case (excerpt = truncated paragraph).
    if b.startswith(e[:60]) or e.startswith(b[:60]):
        return True
    # Containment covers renderers that MERGE several Word paragraphs into one
    # Markdown block — mammoth does this, so the commented paragraph can sit in
    # the middle of a block. Attaching after that block is still adjacent to the
    # commented text, which is what matters.
    return probe in b


def _find_anchor(blocks: list[str], c: dict) -> Optional[int]:
    """Locate the block a comment belongs after, or None.

    Tries the reported index first, then its neighbours, then a full scan.
    The full scan matters because mammoth merges/splits paragraphs: a document
    with 9 text paragraphs can render as 6 blocks, so a reported index may not
    exist at all. Content comparison is always the authority — an index alone
    would attach feedback to the wrong paragraph.
    """
    idx = c.get("paragraph_index")
    excerpt = c.get("excerpt")
    if idx is not None and isinstance(idx, int):
        for probe in (idx, idx - 1, idx + 1):
            if 0 <= probe < len(blocks) and _block_matches(blocks[probe], excerpt):
                return probe
    for i, block in enumerate(blocks):
        if _block_matches(block, excerpt):
            return i
    return None


def merge_comments(markdown: str, comments: list[dict]) -> tuple[str, list[str]]:
    """Insert inline comment markers, then append the dated summary.

    Returns ``(merged_markdown, warnings)``.

    Placements are resolved against the UNMODIFIED block list first and applied
    in a single rebuild pass. Mutating indices while inserting (the obvious
    approach) cascades: each inserted marker shifts every later index, so two
    comments in one document end up attached to the wrong paragraphs.
    """
    if not comments:
        return markdown, []

    blocks = markdown.split("\n\n") if markdown else []
    warnings: list[str] = []
    markers: dict[int, list[str]] = {}

    for c in comments:
        if c.get("document_level") or c.get("paragraph_index") is None:
            continue
        target = _find_anchor(blocks, c)
        if target is None:
            warnings.append(
                f"comment #{c.get('id')} could not be anchored to its paragraph"
            )
            logger.warning(
                "office import: comment %s anchor not found (paragraph_index=%r)",
                c.get("id"), c.get("paragraph_index"),
            )
            continue
        when = f" ({c['date']})" if c.get("date") else ""
        markers.setdefault(target, []).append(
            f"> 💬 **{c['author']}**{when}: {c['text']}"
        )

    out: list[str] = []
    for i, block in enumerate(blocks):
        out.append(block)
        for marker in markers.get(i, []):
            out.append(marker)

    merged = "\n\n".join(out)
    summary = _summary_section(comments)
    if summary:
        merged = merged.rstrip() + "\n\n" + summary
    return merged, warnings


def _summary_section(comments: list[dict]) -> str:
    from src.office_comments import _summary_section as build

    return build(comments)


# ── entry point ────────────────────────────────────────────────────────────


def import_office_document(path: str) -> ImportResult:
    """Extract a .docx/.odt as Markdown with its comments preserved.

    Raises ``OfficeImportError`` with a user-facing message when the file is a
    legacy .doc, an unreadable container, or yields nothing usable.
    """
    from src.office_comments import convert_office_with_comments

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".docx", ".odt"):
        raise OfficeImportError(
            f"Unsupported Office format '{ext or '(none)'}'. Supported: .docx, .odt."
        )

    if _looks_like_legacy_doc(path):
        raise OfficeImportError(
            f"{os.path.basename(path)} is a Word 97-2003 (.doc) file with a "
            ".docx extension. Open it in Word/LibreOffice and use Save As -> "
            ".docx, then import again."
        )
    if not _is_zip(path):
        raise OfficeImportError(
            f"{os.path.basename(path)} is not a valid {'Word' if ext == '.docx' else 'OpenDocument'} "
            "file (the archive could not be opened)."
        )

    converted = convert_office_with_comments(path)
    if converted is None:
        raise OfficeImportError(
            f"Could not read {os.path.basename(path)} — the document may be corrupt."
        )
    body_with_comments, comments = converted

    warnings: list[str] = []
    markdown = None
    if ext == ".docx":
        try:
            from src.docx_render import docx_to_markdown

            markdown = docx_to_markdown(path)
        except Exception as e:  # never let the rich path break an import
            logger.warning("office import: mammoth path failed for %s: %s", path, e)
            markdown = None
        if markdown is None:
            warnings.append(
                "rich formatting unavailable (Node/mammoth not found) — used the "
                "plain text extractor"
            )

    if markdown is None:
        # .odt always lands here (no mammoth), and .docx when Node is missing.
        # `body_with_comments` already has markers + summary inlined.
        merged = body_with_comments
    else:
        merged, merge_warnings = merge_comments(markdown, comments)
        warnings.extend(merge_warnings)

    _reject_junk(merged, path)
    if not merged.strip():
        raise OfficeImportError(
            f"{os.path.basename(path)} appears to contain no text."
        )
    return ImportResult(markdown=merged, comments=comments, warnings=warnings)
