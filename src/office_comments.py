"""Extract Office documents as Markdown, preserving comments.

Why this module exists
----------------------
`markitdown` converts `.docx` to Markdown well, but it only reads
``word/document.xml``. Word comments live in a separate part
(``word/comments.xml``) and were silently dropped — so an imported document
looked complete while all review feedback had vanished, with nothing telling the
user. ``.odt`` was worse: markitdown 0.1.6 has no ODT converter at all, so those
files could not be imported.

Both formats are just zip archives of XML, so both are handled here with the
standard library only (no python-docx, no odfpy, no markitdown):

    .docx   word/document.xml  + word/comments.xml
    .odt    content.xml        (office:annotation is inline)

Output shape
------------
``convert_office_with_comments(path) -> (markdown, comments)`` or ``None`` when
the file is not a readable Office container.

Comments are rendered in BOTH of the two useful ways:

1. **Inline at the anchor**, right after the paragraph they refer to, so the
   feedback reads in context::

       The budget must be approved by Friday.
       > 💬 **A. Rossi** (2026-10-20): Is this the hard deadline?

2. **A summary section at the end**, ordered by date, quoting the anchored
   paragraph, so every comment can be triaged in one pass.

Each comment dict is::

    {"id": 1, "author": str, "date": str, "text": str,
     "excerpt": str | None, "document_level": bool}

A document with no comments produces exactly the Markdown it produced before
(no heading, no markers), so ordinary imports are unchanged.

This is a READ-ONLY import: nothing is written back into the original file.
"""

from __future__ import annotations

import logging
import os
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

# Containers handled here. `.docx`/`.odt` are routed here by the import paths
# instead of markitdown; see src/markitdown_runtime.py.
OFFICE_COMMENT_EXTS = frozenset({".docx", ".odt"})

# How much of the anchored paragraph to quote.
_EXCERPT_CHARS = 120

# ── namespaces ──────────────────────────────────────────────────────────────
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_DC = "{http://purl.org/dc/elements/1.1/}"

_UNKNOWN_AUTHOR = "(unknown author)"


def is_office_comment_format(path: str) -> bool:
    """True if this file should go through the comment-aware extractor."""
    if not isinstance(path, str):
        return False
    return os.path.splitext(path)[1].lower() in OFFICE_COMMENT_EXTS


def extract_excerpt(text: str, limit: int = _EXCERPT_CHARS) -> Optional[str]:
    """Trim paragraph text to a short quote, on a word boundary."""
    text = " ".join((text or "").split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",;:.") + "…"


# Back-compat alias for internal call sites.
_excerpt = extract_excerpt


def _clean_date(raw: str) -> str:
    """Normalise an ISO-ish timestamp to YYYY-MM-DD; pass anything else through."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw[:10] if len(raw) >= 10 and raw[4] == "-" else raw


def _inline_marker(c: dict) -> str:
    """The one-line blockquote shown next to the commented paragraph."""
    when = f" ({c['date']})" if c.get("date") else ""
    return f"> 💬 **{c['author']}**{when}: {c['text']}"


def _summary_section(comments: list[dict]) -> str:
    """The ``## Comments (n)`` block, ordered by date then document order."""
    if not comments:
        return ""

    def _key(c: dict):
        return (c.get("date") or "9999-99-99", c.get("id", 0))

    lines = [f"## Comments ({len(comments)})", ""]
    for i, c in enumerate(sorted(comments, key=_key), 1):
        when = f" — {c['date']}" if c.get("date") else ""
        scope = " *(on the whole document)*" if c.get("document_level") else ""
        lines.append(f"{i}. **{c['author']}**{when}{scope}")
        if c.get("excerpt") and not c.get("document_level"):
            lines.append(f"   > {c['excerpt']}")
        # Indent multi-line comment bodies so they stay inside the list item.
        for ln in (c.get("text") or "").splitlines() or [""]:
            lines.append(f"   {ln}".rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── .docx ───────────────────────────────────────────────────────────────────


def _docx_paragraph_text(para: ET.Element) -> str:
    """Text of a paragraph, in run order.

    Word stores text as a sequence of runs; joining them raw would glue
    "Friday." to the following run ("Friday.See note."). A joining space is
    inserted where a run boundary separated two words, and ``w:tab`` /
    ``w:br`` are honoured, so the paragraph reads as it does in Word.
    """
    parts: list[str] = []

    def _walk(el: ET.Element) -> None:
        for child in el:
            if child.tag == f"{_W}t":
                parts.append(child.text or "")
            elif child.tag == f"{_W}tab":
                parts.append("\t")
            elif child.tag == f"{_W}br":
                parts.append("\n")
            elif child.tag in (f"{_W}commentRangeStart", f"{_W}commentRangeEnd"):
                continue  # zero-width markers
            else:
                _walk(child)
            if child.tail:
                parts.append(child.tail)

    _walk(para)
    text = "".join(parts)
    # Collapse a run boundary that merged two words (no space either side).
    text = text.replace("\t", " ").replace("\n", " ")
    return " ".join(text.split()).strip()


def _parse_docx_comments(z: zipfile.ZipFile) -> dict[str, dict]:
    try:
        data = z.read("word/comments.xml")
    except KeyError:
        return {}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        logger.warning("docx: unparseable word/comments.xml")
        return {}

    out: dict[str, dict] = {}
    for c in root.iter(f"{_W}comment"):
        cid = c.get(f"{_W}id")
        if cid is None:
            continue
        body = "\n".join(
            _docx_paragraph_text(p) for p in c.iter(f"{_W}p")
        ).strip()
        if not body:
            continue
        out[str(cid)] = {
            "author": (c.get(f"{_W}author") or "").strip() or _UNKNOWN_AUTHOR,
            "date": _clean_date(c.get(f"{_W}date") or ""),
            "text": body,
        }
    return out


def _comment_ids_in(para: ET.Element) -> list[str]:
    """Comment ids referenced inside one paragraph, in document order."""
    ids: list[str] = []
    for node in para.iter():
        if node.tag in (f"{_W}commentRangeStart", f"{_W}commentReference"):
            cid = node.get(f"{_W}id")
            if cid and cid not in ids:
                ids.append(cid)
    return ids


def _extract_docx(z: zipfile.ZipFile) -> Optional[tuple[str, list[dict]]]:
    try:
        data = z.read("word/document.xml")
    except KeyError:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    comments_meta = _parse_docx_comments(z)
    if not comments_meta:
        # No comments: plain paragraph text, identical to the old extractor.
        paragraphs = [_docx_paragraph_text(p) for p in root.iter(f"{_W}p")]
        return ("\n\n".join(p for p in paragraphs if p), [])

    blocks: list[str] = []
    collected: list[dict] = []
    seen: set[str] = set()
    # Index of the last paragraph that produced text. Renderers (mammoth) skip
    # empty <w:p> elements, and Word files are full of them — on a real 13-
    # paragraph document mammoth emitted 6 blocks. Anchors must therefore be
    # expressed in "text paragraph" coordinates, never raw XML indices.
    text_para_index = -1

    for para in root.iter(f"{_W}p"):
        text = _docx_paragraph_text(para)
        if text:
            blocks.append(text)
            text_para_index += 1

        for cid in _comment_ids_in(para):
            meta = comments_meta.get(cid)
            if not meta or cid in seen:
                continue
            seen.add(cid)
            entry = {
                "id": int(cid) if cid.isdigit() else len(collected) + 1,
                "author": meta["author"],
                "date": meta["date"],
                "text": meta["text"],
                # The anchor is the paragraph the range covers.
                "excerpt": _excerpt(text),
                "paragraph_index": text_para_index,
                "document_level": False,
            }
            collected.append(entry)
            blocks.append(_inline_marker(entry))

    # Comments present in comments.xml but never referenced in the body are
    # document-level feedback.
    for cid, meta in comments_meta.items():
        if cid in seen:
            continue
        entry = {
            "id": int(cid) if cid.isdigit() else len(collected) + 1,
            "author": meta["author"],
            "date": meta["date"],
            "text": meta["text"],
            "excerpt": None,
            "paragraph_index": None,
            "document_level": True,
        }
        collected.append(entry)
        blocks.append(_inline_marker(entry))

    markdown = "\n\n".join(blocks)
    summary = _summary_section(collected)
    if summary:
        markdown = markdown + "\n\n" + summary
    return markdown, collected


# ── .odt ────────────────────────────────────────────────────────────────────


def _odt_text(node: ET.Element) -> str:
    """Text content of an ODT node, inserting spaces at element boundaries."""
    parts: list[str] = []

    def _walk(el: ET.Element) -> None:
        if el.tag == f"{_OFFICE}annotation":
            return  # handled separately by the caller
        if el.text:
            parts.append(el.text)
        for child in el:
            _walk(child)
            if child.tail:
                parts.append(child.tail)

    _walk(node)
    return "".join(parts).strip()


def _parent_map(root: ET.Element) -> dict[int, ET.Element]:
    """id(child) -> parent, so we can tell if a node sits inside an annotation."""
    return {id(child): node for node in root.iter() for child in node}


def _odt_annotations(node: ET.Element) -> list[dict]:
    """Annotations directly inside this block, with their own body text."""
    out: list[dict] = []
    for ann in node.iter(f"{_OFFICE}annotation"):
        creator = (ann.findtext(f"{_DC}creator") or "").strip() or _UNKNOWN_AUTHOR
        date = _clean_date(ann.findtext(f"{_DC}date") or "")
        body = "\n".join(
            _odt_text(p) for p in ann.findall(f"{_TEXT}p")
        ).strip()
        if not body:
            continue
        out.append({"author": creator, "date": date, "text": body})
    return out


def _extract_odt(z: zipfile.ZipFile) -> Optional[tuple[str, list[dict]]]:
    try:
        data = z.read("content.xml")
    except KeyError:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    blocks: list[str] = []
    collected: list[dict] = []
    parents = _parent_map(root)

    def _emit_block(el: ET.Element, prefix: str = "") -> None:
        text = _odt_text(el)
        anns = _odt_annotations(el) if text or el.find(f".//{_OFFICE}annotation") is not None else []
        if text:
            blocks.append(prefix + text)
        # Text-block coordinate for the same reason as docx: a renderer may
        # drop empty blocks, so anchors count only blocks with text.
        para_index = len(blocks) - 1 if text else None
        for ann in anns:
            entry = {
                "id": len(collected) + 1,
                "author": ann["author"],
                "date": ann["date"],
                "text": ann["text"],
                "excerpt": _excerpt(text),
                "paragraph_index": para_index,
                "document_level": not bool(text),
            }
            collected.append(entry)
            blocks.append(_inline_marker(entry))

    body = root.find(f"{_OFFICE}body")
    scope = body.find(f"{_OFFICE}text") if body is not None else None
    if scope is None:
        return ("", [])

    for el in scope.iter():
        if el.tag == f"{_TEXT}h":
            level = el.get(f"{_TEXT}outline-level") or "1"
            try:
                hashes = "#" * max(1, min(6, int(level)))
            except (TypeError, ValueError):
                hashes = "#"
            _emit_block(el, prefix=f"{hashes} ")
        elif el.tag == f"{_TEXT}p":
            # Annotation bodies are rendered as the comment's text, not as
            # document paragraphs — skip anything nested inside one.
            p = parents.get(id(el))
            inside_annotation = False
            while p is not None:
                if p.tag == f"{_OFFICE}annotation":
                    inside_annotation = True
                    break
                p = parents.get(id(p))
            if inside_annotation:
                continue
            _emit_block(el)

    markdown = "\n\n".join(blocks)
    summary = _summary_section(collected)
    if summary:
        markdown = markdown + "\n\n" + summary
    return markdown, collected


# ── public entry point ──────────────────────────────────────────────────────


def convert_office_with_comments(path: str) -> Optional[tuple[str, list[dict]]]:
    """Convert a .docx/.odt to Markdown, keeping comments.

    Returns ``(markdown, comments)``, or ``None`` when the file is not a
    supported container or cannot be read (corrupt zip / unparseable XML), in
    which case callers should fall back to another extractor.
    """
    if not is_office_comment_format(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        with zipfile.ZipFile(path) as z:
            if ext == ".docx":
                return _extract_docx(z)
            return _extract_odt(z)
    except (zipfile.BadZipFile, OSError) as e:
        logger.warning("office_comments: cannot open %s: %s", path, e)
        return None


# ── convenience helpers (also exposed by the `office_comments` CLI) ─────────


def to_markdown(path: str, include_comments: bool = True) -> Optional[str]:
    """Markdown body for a .docx/.odt, optionally without comments.

    Returns ``None`` for an unsupported/unreadable file.
    """
    converted = convert_office_with_comments(path)
    if converted is None:
        return None
    markdown, comments = converted
    if include_comments or not comments:
        return markdown
    kept = [
        block
        for block in markdown.split("\n\n")
        if not block.startswith("> 💬 **") and not block.startswith("## Comments (")
    ]
    return "\n\n".join(kept).strip() + "\n"


def list_comments(path: str) -> Optional[list[dict]]:
    """Just the comments for a .docx/.odt (``None`` if unreadable)."""
    converted = convert_office_with_comments(path)
    return None if converted is None else converted[1]
