from typing import Any, Dict, List, Optional
import logging
import re
from src.constants import MAX_READ_CHARS
from src.tool_approvals import document_content_digest
from src.tool_utils import _parse_tool_args, get_upload_handler
from src.upload_handler import reserve_upload_references

logger = logging.getLogger(__name__)


def _missing_document_upload(owner: Optional[str], content: Any) -> Optional[str]:
    """Reserve explicit upload URLs before an agent persists document text."""
    return reserve_upload_references(get_upload_handler(), owner, content)

# ---------------------------------------------------------------------------
# Active document state
# ---------------------------------------------------------------------------

_active_document_id: Optional[str] = None
_active_model: Optional[str] = None


def set_active_document(doc_id: Optional[str]):
    """Set the active document ID for document tool execution."""
    global _active_document_id
    _active_document_id = doc_id


def set_active_model(model: Optional[str]):
    """Set the current model name for version summaries."""
    global _active_model
    _active_model = model


def get_active_document():
    return _active_document_id


def _editor_open_active_target(doc: Any, ctx: Optional[dict]) -> bool:
    """True when ``doc`` is the document the browser currently has open.

    ``editor_open`` is an explicit per-turn frontend signal; the in-memory
    pointer is set by stream_agent_loop from the request's active document.
    Requiring both distinguishes "the editor is showing this document" from an
    API caller that merely names it, so target-aware guards do not block edits
    to other documents.
    """
    if not ctx or not ctx.get("editor_open"):
        return False
    # Email compose drafts are a special surface: the suggestion preview lives
    # in the source editor pane, which the rich compose view hides, so
    # suggestions on a draft are not safely applied to the To/Subject/body
    # fields yet. Keep direct update/edit for the compose draft until that UI
    # supports suggestions; normal documents use the suggestion-only policy.
    if (getattr(doc, "language", "") or "").lower() == "email":
        return False
    if _looks_like_email_document(
        getattr(doc, "current_content", "") or "",
        getattr(doc, "title", "") or "",
    ):
        return False
    active_id = get_active_document()
    return bool(
        active_id
        and doc is not None
        and str(getattr(doc, "id", "")) == str(active_id)
    )


def _editor_suggest_only_error() -> Dict:
    return {
        "error": (
            "The document open in the editor can only be changed through "
            "suggest_document. Use suggest_document with FIND/SUGGEST/REASON so "
            "the user can accept or reject the change. Direct edit/update is for "
            "documents that are not open in the editor; target those with an "
            "explicit document_id."
        ),
        "exit_code": 1,
        "policy": "editor_suggest_only",
    }


def _editor_delete_blocked_error() -> Dict:
    return {
        "error": (
            "The document is open in the editor. Close it before deleting it, "
            "or delete it from the Library."
        ),
        "exit_code": 1,
        "policy": "editor_suggest_only",
    }


def clear_active_document(doc_id: Optional[str] = None) -> bool:
    """Clear the in-memory active-document pointer.

    With ``doc_id`` given, only clears when it matches the current pointer, so a
    different active document is left untouched. Returns True if it was cleared.

    Called when a document is detached from its session or deleted (its tab is
    closed): without this, the stale pointer makes the last-resort doc-injection
    path re-surface a closed document in a later, unrelated chat — even one whose
    session no longer matches — because an unlinked doc has session_id NULL (#1160).
    """
    global _active_document_id
    if doc_id is None or _active_document_id == doc_id:
        _active_document_id = None
        return True
    return False


def _owned_document_query(query, Document, owner: Optional[str]):
    if owner is None:
        # A bare Python `False` is not a valid SQL expression — SQLAlchemy 1.4
        # deprecates it and 2.0 raises ArgumentError. Use the SQL `false()`
        # literal to return zero rows for an unscoped (owner-less) query.
        from sqlalchemy import false
        return query.filter(false())
    return query.filter(Document.owner == owner)


def _get_owned_document(db, Document, doc_id: str, owner: Optional[str], active_only: bool = False):
    q = db.query(Document).filter(Document.id == doc_id)
    if active_only:
        q = q.filter(Document.is_active == True)
    q = _owned_document_query(q, Document, owner)
    return q.first()


# ---------------------------------------------------------------------------
# Target resolution for editor-document tools
# ---------------------------------------------------------------------------
#
# A document tool may only act on a target the user actually pointed at. There
# are exactly two ways to name one:
#
#   1. the document open in the editor — the in-memory pointer set by
#      `stream_agent_loop` from the frontend's `active_doc_id` /
#      `displayed_doc_id`, and
#   2. an explicit title or document_id the user said ("read X.md").
#
# Anything else is an error the model can fix in one step. There is deliberately
# NO "most recently updated document" fallback.
#
# Why this matters (hard-won — see also the pointer comment in
# `stream_agent_loop` and the `active_document` branch of `_build_system_prompt`):
#
#   * The old fallback, `_most_recent_owned_document()`, silently retargeted an
#     edit to whatever document had been touched last. `edit_document` returned
#     success, the model answered "Done", and the user's actual document never
#     changed — while a different one quietly gained a new version.
#   * Keeping that fallback "just in case" also taught models to depend on it.
#     After removal, a model that had used it once tried
#     `manage_documents read` with no id as "give me the newest one", got a
#     correct "name a document" error, and told the user the editor was closed.
#     A silent guess is worse than a loud error: the error is recoverable.
#
# So: no fallback, and a pointer owned by the *request* — not by whichever
# prompt-rebuilding pass happened to run last.


def _number_document_lines(content: str) -> str:
    """Prefix each line with its 1-based number and a TAB.

    Same format the active-document prompt block uses, so FIND text derived
    from a numbered read matches the document text exactly.
    """
    return "\n".join(
        f"{idx}\t{line}" for idx, line in enumerate((content or "").split("\n"), 1)
    )


def _missing_target_error(
    db,
    Document,
    owner: Optional[str],
    *,
    action: str,
    requested_id: Optional[str] = None,
    editor_open: bool = False,
    editor_stale: bool = False,
    library_preview_doc_id: Optional[str] = None,
) -> Dict:
    """Build the retry message for a tool call with no resolvable document.

    There is no "most recent document" fallback: a document is reached either by
    naming it (title or id) or by having it open in the editor. Being explicit
    about which of the two is missing stops the model from inventing a third
    option ("I'll read the latest one"), and reporting the *real* state stops it
    from telling the user "the editor says nothing is open" when the editor is
    in fact showing something only a click away from being active.
    """
    if requested_id:
        return {
            "error": (
                f"Document '{requested_id}' not found (it may have been deleted, "
                "or it belongs to another user). Use `manage_documents` "
                "action=list to pick a valid id, or pass the document's title."
            ),
            "exit_code": 1,
        }
    if library_preview_doc_id:
        state = (
            "A document is open in the Library PREVIEW panel, but the preview is "
            "read-only and is not the editor's active document. The user can "
            "click 'Open in editor' on the preview, or you can target it directly "
            "with `manage_documents` action=read and its title or document_id."
        )
    elif editor_stale:
        state = (
            "The document editor pane is on screen but no document is attached to "
            "it (stale pane, typically after switching chats). The user should "
            "reopen the document from the Library, or you can target it by title."
        )
    elif editor_open:
        state = (
            "The document editor is open but no document is registered as active "
            "for this turn, and no document was named."
        )
    else:
        state = (
            "No document is open in the editor, and no document was named."
        )
    msg = (
        f"No document to {action}. {state} There are exactly two ways to target "
        "a document: (1) pass its title or `document_id` here, or (2) ask the "
        "user to open it in the editor. Do NOT fall back to the most recently "
        "updated document — that is not what the user is looking at."
    )
    try:
        total = _owned_document_query(db.query(Document), Document, owner).count()
    except Exception:
        total = None
    if total == 0:
        msg += " This user has no documents yet."
    return {"error": msg, "exit_code": 1, "no_active_document": True}


def _find_documents_by_title(db, Document, owner: Optional[str], raw: str) -> list:
    """Resolve a document by the title the user typed.

    Accepts the title verbatim (``26-10-07-Riassunto-Incontro.md``), a quoted or
    path-shaped form (``documenti/26-10-07-Riassunto-Incontro.md``), and the
    title without its extension. Exact match first, then case-insensitive, then
    a substring pass — every stage only accepts a *unique* hit, so an ambiguous
    name comes back as several candidates rather than a silent guess.
    """
    name = str(raw or "").strip().strip("'\"`")
    if not name:
        return []
    # Path-ish input: keep the basename ("docs/notes.md" → "notes.md").
    if "/" in name:
        name = name.rsplit("/", 1)[-1].strip()
    if not name:
        return []
    variants = [name]
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) <= 5:
        variants.append(stem)

    q = _owned_document_query(db.query(Document), Document, owner)
    for want in variants:
        hits = q.filter(Document.title == want).all()
        if hits:
            return hits
    for want in variants:
        hits = q.filter(Document.title.ilike(want)).all()
        if hits:
            return hits
    for want in variants:
        # Escape LIKE wildcards so a title containing % or _ can't widen the match.
        pat = want.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        hits = q.filter(Document.title.ilike(f"%{pat}%", escape="\\")).all()
        if hits:
            return hits
    return []


def _resolve_document_target(
    db,
    Document,
    *,
    owner: Optional[str],
    target_id: Optional[str],
    ctx: Optional[dict] = None,
    active_only: bool = False,
) -> tuple[Any, Optional[str], Optional[str]]:
    """Resolve the document an editor tool should act on.

    Order: the sealed approval target from ``ctx`` (the user already confirmed
    that exact document), then the frontend's active-document pointer. Returns
    ``(doc, doc_id, requested_id)``. ``doc is None`` means the target could not
    be resolved; ``requested_id`` is what was asked for, so callers can tell
    "no document was named" apart from "that document is gone / not mine".
    """
    requested = None
    for candidate in ((ctx or {}).get("doc_id"), target_id, _active_document_id):
        if not candidate:
            continue
        requested = candidate
        doc = _get_owned_document(db, Document, candidate, owner, active_only=active_only)
        if doc is not None:
            return doc, candidate, candidate
    return None, None, requested


def _approved_document_version_error(doc: Any, ctx: dict) -> Optional[Dict]:
    """Reject a sealed document action when its target changed meanwhile."""
    expected_version = ctx.get("expected_document_version")
    expected_digest = (
        str(ctx.get("expected_document_digest") or "").strip().lower()
    )
    if expected_version is None and not expected_digest:
        return None
    try:
        version_unchanged = (
            expected_version is None
            or int(getattr(doc, "version_count", -1)) == int(expected_version)
        )
    except (TypeError, ValueError):
        version_unchanged = False
    content_unchanged = True
    if expected_digest:
        content_unchanged = (
            doc is not None
            and document_content_digest(getattr(doc, "current_content", ""))
            == expected_digest
        )
    if version_unchanged and content_unchanged:
        return None
    return {
        "error": (
            "The target document changed after this action was proposed. "
            "Review the latest version and request the edit again."
        ),
        "exit_code": 1,
        "document_changed": True,
    }


# ---------------------------------------------------------------------------
# Document tools — create/update/edit/suggest living documents
# ---------------------------------------------------------------------------

def _sniff_doc_language(text: str) -> str:
    """Best-effort detect a document's language from its content when the model
    didn't specify one. Defaults to 'markdown' (prose). Recognizes the common
    markup/code types the editor supports so e.g. an SVG isn't saved as markdown."""
    import json as _json, re as _re2
    s = (text or "").strip()
    if not s:
        return "markdown"
    head = s[:600]
    hl = head.lower()
    if _looks_like_email_document(s):
        return "email"
    # Markup (unambiguous)
    if "<svg" in hl:
        return "svg"
    if hl.startswith("<?xml"):
        return "xml"
    if (hl.startswith("<!doctype html") or hl.startswith("<html")
            or _re2.search(r"<(div|body|head|p|span|table|button|h[1-6]|ul|ol|li|img)\b", hl)):
        return "html"
    # JSON
    if s[0] in "{[":
        try:
            _json.loads(s)
            return "json"
        except Exception:
            pass
    # Shebang
    first = s.split("\n", 1)[0].strip().lower()
    if first.startswith("#!"):
        return "python" if "python" in first else "bash"
    # Code by strong leading signals (line-anchored so prose with stray words won't match)
    if _re2.search(r"(?m)^\s*(def \w|class \w|import \w|from \w[\w.]* import )", s):
        return "python"
    if _re2.search(r"(?m)^\s*(function \w|const \w|let \w|export |import .* from )", s):
        return "javascript"
    if _re2.search(r"(?mi)^\s*(select .* from |create table |insert into |update \w)", s):
        return "sql"
    if _re2.search(r"(?m)^[.#]?[\w-]+\s*\{[^{}]*:[^{}]*;", s):
        return "css"
    return "markdown"

def _looks_like_email_document(text: str = "", title: str = "") -> bool:
    import re as _re
    title_l = (title or "").strip().lower()
    if title_l in {"new email", "new mail", "new message"}:
        return True
    s = (text or "").lstrip()
    if "\n---\n" in s and _re.search(r"(?im)^To:\s*", s) and _re.search(r"(?im)^Subject:\s*", s):
        return True
    return bool(_re.search(r"(?im)^To:\s*", s) and _re.search(r"(?im)^Subject:\s*", s))

def _split_email_header_body(text: str) -> tuple[str, str]:
    if "\n---\n" in (text or ""):
        header, body = (text or "").split("\n---\n", 1)
        return header.rstrip(), body.strip()
    return (text or "").strip(), ""

def _split_email_reply_history(body: str) -> tuple[str, str]:
    """Split draft body from quoted/original email history.

    Email reply docs keep the original thread below the user's new reply. Models
    often rewrite only the fresh reply body; this helper keeps the historical
    block from being wiped when update_document/edit_document replaces content.
    """
    text = body or ""
    literal = "---------- Previous message ----------"
    literal_idx = text.find(literal)
    if literal_idx >= 0:
        return text[:literal_idx].strip(), text[literal_idx:].strip()
    patterns = [
        r"(?m)^On .+ wrote:\s*$",
        r"(?m)^> .+",
    ]
    starts = []
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            starts.append(m.start())
    if not starts:
        return text.strip(), ""
    idx = min(starts)
    return text[:idx].strip(), text[idx:].strip()

def _merge_email_headers(old_header: str, new_header: str) -> str:
    """Preserve routing/threading metadata if a model omits it."""
    protected = (
        "In-Reply-To", "References", "X-Source-UID", "X-Source-Folder",
        "X-Attachments", "X-Forward-Attachments",
    )
    lines = [l for l in (new_header or "").splitlines() if l.strip()]
    present = {l.split(":", 1)[0].strip().lower() for l in lines if ":" in l}
    for old_line in (old_header or "").splitlines():
        if ":" not in old_line:
            continue
        key = old_line.split(":", 1)[0].strip()
        if key in protected and key.lower() not in present:
            lines.append(old_line)
            present.add(key.lower())
    return "\n".join(lines).rstrip()

def _coerce_email_document_content(existing: str, incoming: str) -> str:
    """Keep email docs in the To/Subject/---/body shape even if a model writes
    only the body or dumps header labels without the separator."""
    import re as _re
    old = existing or ""
    new = (incoming or "").strip()
    old_header, old_body = _split_email_header_body(old)
    _, old_history = _split_email_reply_history(old_body)
    if "\n---\n" in new:
        new_header, new_body = _split_email_header_body(new)
        new_own, new_history = _split_email_reply_history(new_body)
        if old_history and not new_history:
            new_body = (new_own + "\n\n" + old_history).strip()
        return _merge_email_headers(old_header, new_header).rstrip() + "\n---\n" + new_body
    header = old_header if old_header else "To: \nSubject: "
    if _looks_like_email_document(new):
        lines = new.splitlines()
        last_header_idx = -1
        header_re = _re.compile(r"^(To|Cc|Bcc|Subject|In-Reply-To|References|X-Source-UID|X-Source-Folder|X-Attachments):", _re.I)
        for i, line in enumerate(lines):
            if header_re.match(line.strip()):
                last_header_idx = i
        body_lines = lines[last_header_idx + 1:] if last_header_idx >= 0 else lines
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        body = "\n".join(body_lines).strip()
    else:
        body = new
    _, incoming_history = _split_email_reply_history(body)
    if old_history and not incoming_history:
        body = (body.strip() + "\n\n" + old_history).strip()
    return header.rstrip() + "\n---\n" + body

_DOCUMENT_ID_HEADER_RE = re.compile(r"\A<<<DOCUMENT_ID>>>\n([^\n]+)\n?", re.UNICODE)


def split_document_id_header(content: str) -> tuple[str, Optional[str]]:
    """Split an optional leading ``<<<DOCUMENT_ID>>>`` target from tool content.

    The model can name an explicit target document for edit/update/suggest;
    `to_tool_block` prepends this header when the ``document_id`` argument is
    set. Returns ``(content_without_header, document_id_or_None)``.
    """
    text = content or ""
    m = _DOCUMENT_ID_HEADER_RE.match(text)
    if not m:
        return text, None
    doc_id = m.group(1).strip() or None
    return text[m.end():], doc_id


def strip_line_number_gutter(find_text: str) -> str:
    """Remove a copied ``N<TAB>`` line-number gutter from FIND text.

    `manage_documents action=read` renders the ACTIVE document with each line
    prefixed by its 1-based number and a TAB, so the model can refer to line
    numbers and quote exact text for edits. Weaker models sometimes copy that
    gutter into their FIND block ("2<TAB>Some text"), which never matches the
    stored content.

    This is a FALLBACK only: callers first try the literal text, so a document
    that genuinely starts a line with "42<TAB>" is matched literally and never
    rewritten.
    """
    return "\n".join(re.sub(r"^\d+\t", "", line) for line in (find_text or "").split("\n"))


def resolve_find_text(find_text: str, haystack: str) -> Optional[str]:
    """Return the text to match against ``haystack``, or None if nothing matches.

    Tries the literal FIND first; only if that fails, retries with the
    line-number gutter stripped. Returns the string that actually matched.
    """
    if not find_text:
        return None
    if find_text in haystack:
        return find_text
    stripped = strip_line_number_gutter(find_text)
    if stripped != find_text and stripped in haystack:
        return stripped
    return None


_SPAN_TOKEN_RE = re.compile(r"\w+|[^\w\s]+|\s+", re.UNICODE)


def _span_tokens(text: str) -> list:
    """Tokenize text into (text, start, end) spans for word-aware narrowing."""
    return [
        (m.group(0), m.start(), m.end())
        for m in _SPAN_TOKEN_RE.finditer(text or "")
    ]


def _narrow_suggestion_span(find_text: str, replace_text: str,
                            document_text: str) -> tuple:
    """Narrow a suggestion to the minimal changed token span.

    Models often quote a whole line (or block) in FIND even when only a word or
    two changes. The editor already word-diffs paired -/+ lines, but mismatched
    line counts or large blocks can degrade that to full-line marking. Returning
    the minimal changed span keeps the suggestion precise and lets the editor
    highlight only the changed text.

    The returned pair always reproduces the original replacement:
        find_text[:start] + new_span + find_text[end:] == replace_text
    Falls back to the original pair when no unique/safe span exists.
    """
    original = (find_text, replace_text)
    if not find_text or find_text == replace_text:
        return original

    f_tokens = _span_tokens(find_text)
    r_tokens = _span_tokens(replace_text)
    if not f_tokens:
        return original

    f_texts = [tok[0] for tok in f_tokens]
    r_texts = [tok[0] for tok in r_tokens]

    prefix = 0
    prefix_limit = min(len(f_texts), len(r_texts))
    while prefix < prefix_limit and f_texts[prefix] == r_texts[prefix]:
        prefix += 1

    suffix = 0
    suffix_limit = min(len(f_texts), len(r_texts)) - prefix
    while suffix < suffix_limit and f_texts[-1 - suffix] == r_texts[-1 - suffix]:
        suffix += 1

    fs, fe = prefix, len(f_texts) - suffix
    rs, rend = prefix, len(r_texts) - suffix

    # Pure insertion: borrow one adjacent token as an anchor so FIND stays
    # non-empty and still points at the insertion site.
    if fs == fe:
        if fs > 0:
            fs -= 1
            rs -= 1
        elif fe < len(f_texts):
            fe += 1
            rend += 1
        else:
            return original

    if fe <= fs:
        return original

    def _build():
        f_min = find_text[f_tokens[fs][1]:f_tokens[fe - 1][2]]
        r_min = (
            replace_text[r_tokens[rs][1]:r_tokens[rend - 1][2]]
            if rend > rs else ""
        )
        return f_min, r_min

    # Expand until the narrowed FIND is unique in the document. A unique
    # original FIND guarantees this loop can reach the full span; a non-unique
    # one is left alone rather than risking the wrong occurrence.
    f_min, r_min = _build()
    while document_text.count(f_min) != 1:
        if fs > 0:
            fs -= 1
            rs -= 1
        elif fe < len(f_tokens):
            fe += 1
            rend += 1
        else:
            return original
        f_min, r_min = _build()

    if not f_min.strip():
        # Whitespace-only changes stay whole-line; an invisible span is worse
        # than the original context.
        return original

    # Exact positional verification: replacing the narrowed span inside the
    # original FIND must reproduce the original replacement. This catches
    # accidental matches when the same text appears earlier in FIND.
    expected = find_text[:f_tokens[fs][1]] + r_min + find_text[f_tokens[fe - 1][2]:]
    if expected != replace_text:
        return original
    if f_min == find_text and r_min == replace_text:
        return original

    logger.debug(
        "suggest_document: narrowed span %r -> %r (%r -> %r)",
        find_text[:60], f_min[:60], replace_text[:60], r_min[:60],
    )
    return f_min, r_min


def parse_edit_blocks(content: str) -> list:
    """Parse <<<FIND>>>...<<<REPLACE>>>...<<<END>>> blocks."""
    edits = []
    pattern = r'<<<FIND>>>\n(.*?)\n<<<REPLACE>>>\n(.*?)\n<<<END>>>'
    for m in re.finditer(pattern, content, re.DOTALL):
        edits.append({"find": m.group(1), "replace": m.group(2)})
    return edits

def parse_suggest_blocks(content: str) -> list:
    """Parse <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks."""
    suggestions = []
    _skip_phrases = ["no change", "clear", "fine as", "looks good", "no improvement", "keep as"]
    pattern = r'<<<FIND>>>\n(.*?)\n<<<SUGGEST>>>\n(.*?)\n<<<REASON>>>\n(.*?)\n<<<END>>>'
    for m in re.finditer(pattern, content, re.DOTALL):
        find_text = m.group(1)
        replace_text = m.group(2)
        reason = m.group(3).strip()
        # Skip no-op suggestions where find == replace or reason says no change
        if find_text.strip() == replace_text.strip():
            continue
        if any(phrase in reason.lower() for phrase in _skip_phrases):
            continue
        suggestions.append({
            "id": f"sugg-{len(suggestions)+1}",
            "find": find_text,
            "replace": replace_text,
            "reason": reason,
        })
    return suggestions


def _pdf_source_upload_id(content: str) -> Optional[str]:
    try:
        from src.pdf_form_doc import find_source_upload_id
        return find_source_upload_id(content or "")
    except Exception:
        return None


def _strip_pdf_editor_markers(content: str) -> str:
    """Turn a PDF-wrapper markdown doc into ordinary editable markdown.

    PDF docs use hidden HTML comments for source-upload links, form fields, and
    page annotations. Those comments are necessary for rendering/exporting the
    original PDF, but they make a derived AI text edit keep showing the original
    PDF preview. Remove only the editor plumbing and keep the readable text.
    """
    text = content or ""
    text = re.sub(r'(?im)^\s*<!--\s*pdf(?:_form)?_source\s+[^>]*-->\s*\n*', '', text)
    text = re.sub(r'\s*<!--\s*field=[^>]*-->', '', text)
    text = re.sub(r'\s*<!--\s*annotation\s+[^>]*-->', '', text)
    return text.strip()


def _create_pdf_text_derivative(db, *, source_doc, content: str, owner: Optional[str], summary: str) -> dict:
    import uuid
    from src.database import Document, DocumentVersion

    clean = _strip_pdf_editor_markers(content)
    title_base = (getattr(source_doc, "title", None) or "PDF").strip()
    title = title_base if title_base.lower().endswith("edited") else f"{title_base} edited"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    new_doc = Document(
        id=doc_id,
        session_id=getattr(source_doc, "session_id", None),
        title=title,
        language="markdown",
        current_content=clean,
        version_count=1,
        is_active=True,
        owner=owner if owner is not None else getattr(source_doc, "owner", None),
    )
    ver = DocumentVersion(
        id=ver_id,
        document_id=doc_id,
        version_number=1,
        content=clean,
        summary=summary,
        source="ai",
    )
    db.add(new_doc)
    db.add(ver)
    db.commit()
    set_active_document(doc_id)
    return {
        "action": "create",
        "doc_id": doc_id,
        "title": title,
        "language": "markdown",
        "content": clean,
        "version": 1,
        "source_doc_id": getattr(source_doc, "id", None),
    }


class CreateDocumentTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        """Create a new document. Supports two formats:
        1) Line-based: line 1 = title, line 2 (optional) = language, rest = content
        2) XML-like tags: <title>...</title><language>...</language><content>...</content>
        Some models mix them — strip any XML-style tags and fall back to line parsing."""
        import uuid, re as _re
        from src.database import SessionLocal, Document, DocumentVersion, Session as DbSession

        raw = content or ""
        session_id = ctx.get("session_id")
        owner = ctx.get("owner")

        # Known languages the editor understands (match the <select> in HTML)
        _KNOWN_LANGS = {
            "python", "javascript", "typescript", "html", "css", "markdown", "json",
            "yaml", "bash", "sql", "rust", "go", "java", "c", "cpp", "xml", "toml",
            "ini", "ruby", "php", "csv", "email", "text", "plain", "svg",
        }

        # Try XML tag extraction first
        title = None
        language = None
        content = None
        mt = _re.search(r"<title>\s*(.*?)\s*</title>", raw, _re.DOTALL | _re.IGNORECASE)
        ml = _re.search(r"<language>\s*(.*?)\s*</language>", raw, _re.DOTALL | _re.IGNORECASE)
        mc = _re.search(r"<content>\s*(.*?)\s*</content>", raw, _re.DOTALL | _re.IGNORECASE)
        if mt or mc:
            title = mt.group(1).strip() if mt else None
            language = ml.group(1).strip().lower() if ml else None
            content = mc.group(1) if mc else None

        # Fall back to line-based parsing. First strip any stray XML-ish tags.
        if title is None or content is None:
            cleaned = _re.sub(r"</?(?:title|language|content)>", "", raw)
            lines = cleaned.strip().split("\n")
            if title is None:
                title = lines[0].strip() if lines else "Untitled"
                lines = lines[1:]
            # Only consume second line as language if it looks like a valid short lang token
            if language is None and lines:
                candidate = lines[0].strip().lower()
                if candidate and len(candidate) < 20 and " " not in candidate and candidate in _KNOWN_LANGS:
                    language = candidate
                    lines = lines[1:]
            if content is None:
                content = "\n".join(lines)

        # Validate language: must be in known set, else default based on content
        if language and language not in _KNOWN_LANGS:
            language = None
        if not language:
            # No explicit language — sniff it from the content so an SVG / HTML / JSON
            # / code document isn't silently saved as markdown. Prose → markdown.
            language = _sniff_doc_language(content)
        if _looks_like_email_document(content, title):
            language = "email"

        if not title:
            title = "Untitled"

        if not session_id:
            return {"error": "No session context for document creation"}

        db = SessionLocal()
        try:
            doc_id = str(uuid.uuid4())
            ver_id = str(uuid.uuid4())

            # Inherit ownership from the chat session so the doc survives that
            # session later being deleted (session_id → NULL).
            _sess = db.query(DbSession).filter(DbSession.id == session_id).first()
            if owner is not None and (not _sess or _sess.owner != owner):
                return {"error": "Cannot create document in another user's session"}
            _owner = _sess.owner if _sess else None

            missing_id = _missing_document_upload(_owner, content)
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }

            doc = Document(
                id=doc_id,
                session_id=session_id,
                title=title,
                language=language,
                current_content=content,
                version_count=1,
                is_active=True,
                owner=_owner,
            )
            ver = DocumentVersion(
                id=ver_id,
                document_id=doc_id,
                version_number=1,
                content=content,
                summary=f"Created by {_active_model or 'AI'}",
                source="ai",
            )
            db.add(doc)
            db.add(ver)
            db.commit()

            set_active_document(doc_id)
            try:
                from src.event_bus import fire_event
                fire_event("document_created", _owner)
            except Exception:
                logger.debug("document_created event dispatch failed", exc_info=True)

            return {
                "action": "create",
                "doc_id": doc_id,
                "title": title,
                "language": language,
                "content": content,
                "version": 1,
            }
        except Exception as e:
            db.rollback()
            return {"error": f"Failed to create document: {e}"}
        finally:
            db.close()

class UpdateDocumentTool:    
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Update an existing document. Content = full new document text."""
        import uuid
        from src.database import SessionLocal, Document, DocumentVersion

        target_id = ctx.get("doc_id", None) or _active_document_id
        owner = ctx.get("owner")
        content, header_doc_id = split_document_id_header(content)
        if header_doc_id:
            target_id = header_doc_id

        db = SessionLocal()
        try:
            doc, target_id, _requested = _resolve_document_target(
                db, Document, owner=owner, target_id=target_id, ctx=ctx
            )
            if (
                not doc
                and ctx.get("expected_document_version") is not None
            ):
                return _approved_document_version_error(None, ctx)
            if not doc:
                logger.info("update_document: no active document to target")
                return _missing_target_error(
                    db, Document, owner, action="update", requested_id=_requested
                )

            if _editor_open_active_target(doc, ctx):
                return _editor_suggest_only_error()

            version_error = _approved_document_version_error(doc, ctx)
            if version_error:
                return version_error

            is_email_doc = doc.language == "email" or _looks_like_email_document(doc.current_content or "", doc.title or "")
            new_content = _coerce_email_document_content(doc.current_content or "", content) if is_email_doc else content.strip()
            if is_email_doc:
                doc.language = "email"

            missing_id = _missing_document_upload(owner, new_content)
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }

            if not is_email_doc and _pdf_source_upload_id(doc.current_content or ""):
                return _create_pdf_text_derivative(
                    db,
                    source_doc=doc,
                    content=new_content,
                    owner=owner,
                    summary=f"Created from PDF edit by {_active_model or 'AI'}",
                )

            new_ver = doc.version_count + 1
            ver = DocumentVersion(
                id=str(uuid.uuid4()),
                document_id=target_id,
                version_number=new_ver,
                content=new_content,
                summary=f"Updated by {_active_model or 'AI'}",
                source="ai",
            )
            doc.current_content = new_content
            doc.version_count = new_ver
            db.add(ver)
            db.commit()

            return {
                "action": "update",
                "doc_id": target_id,
                "title": doc.title,
                "language": doc.language,
                "content": new_content,
                "version": new_ver,
            }
        except Exception as e:
            db.rollback()
            return {"error": f"Failed to update document: {e}"}
        finally:
            db.close()

class EditDocumentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Apply targeted FIND/REPLACE edits to an existing document."""
        import uuid
        from src.database import SessionLocal, Document, DocumentVersion

        target_id = ctx.get("doc_id", None) or _active_document_id
        owner = ctx.get("owner")
        content, header_doc_id = split_document_id_header(content)
        if header_doc_id:
            target_id = header_doc_id

        edits = parse_edit_blocks(content)
        if not edits:
            return {"error": "No valid <<<FIND>>>...<<<REPLACE>>>...<<<END>>> blocks found"}

        db = SessionLocal()
        try:
            doc, target_id, _requested = _resolve_document_target(
                db, Document, owner=owner, target_id=target_id, ctx=ctx
            )
            if (
                not doc
                and ctx.get("expected_document_version") is not None
            ):
                return _approved_document_version_error(None, ctx)
            if not doc:
                logger.info("edit_document: no active document to target")
                return _missing_target_error(
                    db, Document, owner, action="edit", requested_id=_requested
                )

            if _editor_open_active_target(doc, ctx):
                return _editor_suggest_only_error()

            version_error = _approved_document_version_error(doc, ctx)
            if version_error:
                return version_error

            is_email_doc = doc.language == "email" or _looks_like_email_document(doc.current_content or "", doc.title or "")
            blank_find_edits = [e for e in edits if not (e.get("find") or "").strip()]
            if blank_find_edits:
                if is_email_doc:
                    replacement_body = (blank_find_edits[0].get("replace") or "").strip()
                    if not replacement_body:
                        return {"error": "No edits applied — blank FIND block had no replacement text"}
                    updated_content = _coerce_email_document_content(doc.current_content or "", replacement_body)
                    applied = 1
                    skipped = max(0, len(edits) - 1)
                    doc.language = "email"
                    missing_id = _missing_document_upload(owner, updated_content)
                    if missing_id:
                        return {
                            "error": f"Referenced upload is no longer available: {missing_id}",
                            "exit_code": 1,
                        }
                    new_ver = doc.version_count + 1
                    ver = DocumentVersion(
                        id=str(uuid.uuid4()),
                        document_id=target_id,
                        version_number=new_ver,
                        content=updated_content,
                        summary=f"Edited email body by {_active_model or 'AI'}",
                        source="ai",
                    )
                    doc.current_content = updated_content
                    doc.version_count = new_ver
                    db.add(ver)
                    db.commit()
                    return {
                        "action": "edit",
                        "doc_id": target_id,
                        "title": doc.title,
                        "language": doc.language,
                        "content": updated_content,
                        "version": new_ver,
                        "applied": applied,
                        "skipped": skipped,
                    }
                return {"error": "No edits applied — FIND text cannot be blank"}

            updated_content = doc.current_content
            applied = 0
            skipped = 0
            for edit in edits:
                _find = edit["find"]
                # Literal match first; the gutter-stripped form is a fallback
                # for models that copy the "N\t" reference prefix from a
                # `manage_documents read` of the active document.
                _matched = resolve_find_text(_find, updated_content)
                if _matched is not None:
                    updated_content = updated_content.replace(_matched, edit["replace"], 1)
                    applied += 1
                    if _matched != _find:
                        logger.info("edit_document: matched after stripping line-number gutter from FIND")
                else:
                    logger.warning(f"edit_document: FIND text not found, skipping: {_find[:80]!r}")
                    skipped += 1

            if applied == 0:
                return {"error": f"No edits applied — none of the FIND blocks matched the document content (skipped {skipped})"}

            missing_id = _missing_document_upload(owner, updated_content)
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }

            if _pdf_source_upload_id(doc.current_content or ""):
                return _create_pdf_text_derivative(
                    db,
                    source_doc=doc,
                    content=updated_content,
                    owner=owner,
                    summary=f"Created from PDF edit by {_active_model or 'AI'} ({applied} edit(s))",
                )

            new_ver = doc.version_count + 1
            ver = DocumentVersion(
                id=str(uuid.uuid4()),
                document_id=target_id,
                version_number=new_ver,
                content=updated_content,
                summary=f"Edited by {_active_model or 'AI'} ({applied} edit(s))",
                source="ai",
            )
            doc.current_content = updated_content
            doc.version_count = new_ver
            db.add(ver)
            db.commit()

            return {
                "action": "edit",
                "doc_id": target_id,
                "title": doc.title,
                "language": doc.language,
                "content": updated_content,
                "version": new_ver,
                "applied": applied,
                "skipped": skipped,
            }
        except Exception as e:
            db.rollback()
            return {"error": f"Failed to edit document: {e}"}
        finally:
            db.close()

class SuggestDocumentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Create inline suggestions for the active document WITHOUT modifying it."""
        from src.database import SessionLocal, Document

        target_id = ctx.get("doc_id", None) or _active_document_id
        owner = ctx.get("owner")
        content, header_doc_id = split_document_id_header(content)
        if header_doc_id:
            target_id = header_doc_id

        suggestions = parse_suggest_blocks(content)
        if not suggestions:
            return {"error": "No valid <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks found"}

        db = SessionLocal()
        try:
            doc, target_id, _requested = _resolve_document_target(
                db, Document, owner=owner, target_id=target_id, ctx=ctx
            )
            if not doc:
                logger.info("suggest_document: no active document to target")
                return _missing_target_error(
                    db, Document, owner, action="suggest on", requested_id=_requested
                )

            version_error = _approved_document_version_error(doc, ctx)
            if version_error:
                return version_error

            # Validate that FIND text exists in document. Match literally first;
            # fall back to the gutter-stripped form for models that copied the
            # "N\t" reference prefix from a `manage_documents read`.
            #
            # Partial matches are NOT fatal here: a suggestion is
            # non-destructive (it only renders accept/reject bubbles), so
            # dropping the unmatched ones and telling the model which ones
            # failed is better than burning a round. `skipped` is reported so
            # the model stops claiming it produced N suggestions when only N-1
            # landed.
            body = doc.current_content or ""
            valid = []
            skipped = []
            for s in suggestions:
                matched = resolve_find_text(s["find"], body)
                if matched is None:
                    logger.warning(
                        "suggest_document: FIND text not found, skipping: %r", s["find"][:80]
                    )
                    skipped.append(s["find"])
                    continue
                if matched != s["find"]:
                    logger.info(
                        "suggest_document: matched after stripping line-number gutter from FIND"
                    )
                    s = {**s, "find": matched}
                narrowed_find, narrowed_replace = _narrow_suggestion_span(
                    s["find"], s["replace"], body
                )
                if narrowed_find != s["find"] or narrowed_replace != s["replace"]:
                    s = {**s, "find": narrowed_find, "replace": narrowed_replace}
                valid.append(s)

            if not valid:
                return {
                    "error": (
                        "No suggestions matched the document content. Read the "
                        "document first with `manage_documents` action=read so the "
                        "FIND text matches the current version exactly, and do not "
                        "include the leading line number or tab."
                    )
                }

            result = {
                "action": "suggest",
                "doc_id": target_id,
                "suggestions": valid,
                "count": len(valid),
            }
            if skipped:
                # Tell the model exactly what did not land, so it can retry those
                # instead of over-reporting success.
                result["skipped"] = {
                    "count": len(skipped),
                    "snippets": [s[:120] for s in skipped[:5]],
                }
                result["note"] = (
                    f"{len(skipped)} suggestion(s) were skipped because their FIND "
                    "text is not in the document verbatim. Tell the user only about "
                    "the suggestions that were applied, or retry the skipped ones "
                    "with exact text from the document."
                )
            return result
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Document management tool (delete, list, organize)
# ---------------------------------------------------------------------------
class ManageDocumentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Manage documents: list, read/view/open, delete, tidy.

        Output format mirrors `manage_session`: list rows include a
        clickable `[Title](#document-<id>)` anchor + relative timestamps
        so the user can click straight from chat to open the editor.
        """
        from core.database import SessionLocal, Document
        from datetime import datetime, timezone

        owner = ctx.get("owner")

        try:
            args = _parse_tool_args(content)
        except ValueError:
            return {"error": "Invalid JSON arguments", "exit_code": 1}

        action = args.get("action", "list")
        db = SessionLocal()

        def _rel(ts):
            if not ts:
                return 'never'
            try:
                now = datetime.now(timezone.utc) if ts.tzinfo is not None else datetime.utcnow()
                diff = (now - ts).total_seconds()
            except Exception:
                return 'unknown'
            if diff < 60: return 'just now'
            if diff < 3600: return f'{int(diff / 60)}m ago'
            if diff < 86400: return f'{int(diff / 3600)}h ago'
            if diff < 86400 * 7: return f'{int(diff / 86400)}d ago'
            return ts.strftime('%Y-%m-%d')

        try:
            if action == "list":
                q = db.query(Document).filter(Document.is_active == True)
                q = _owned_document_query(q, Document, owner)
                if args.get("search"):
                    q = q.filter(Document.title.ilike(f"%{args['search']}%"))
                if args.get("language"):
                    q = q.filter(Document.language == args["language"])
                docs = q.order_by(Document.updated_at.desc()).limit(args.get("limit", 50)).all()
                if not docs:
                    msg = "No documents found" + (f" matching '{args['search']}'" if args.get("search") else "") + "."
                    return {"response": msg, "documents": [], "exit_code": 0}
                lines = []
                items = []
                for i, d in enumerate(docs):
                    size = len(d.current_content or "")
                    lang = d.language or "text"
                    ts = getattr(d, 'updated_at', None) or getattr(d, 'created_at', None)
                    marker = " ← most recent" if i == 0 else ""
                    lines.append(
                        f"- [{d.title}](#document-{d.id}) — {lang}, {size} chars, updated {_rel(ts)}{marker}"
                    )
                    items.append({"id": d.id, "title": d.title, "language": lang, "size": size})
                header = f"Found {len(docs)} document(s), sorted most-recent first. Click a title to open:"
                return {
                    "response": header + "\n" + "\n".join(lines),
                    "documents": items,
                    "exit_code": 0,
                }

            elif action in ("read", "view", "open", "get"):
                doc_id = args.get("document_id") or args.get("id") or args.get("uid")
                title_arg = args.get("title") or args.get("name") or args.get("path")
                # Targeting order, and the ONLY ways to reach a document:
                #   1. an explicit title/path the user said ("read X.md"),
                #   2. the document open in the editor (the active pointer),
                #   3. an explicit id from a earlier `list`.
                # There is deliberately no "most recent document" fallback.
                doc = None
                _requested = None
                if title_arg and not doc_id:
                    _target_name = str(title_arg).strip()
                    candidates = _find_documents_by_title(db, Document, owner, _target_name)
                    active_candidates = [d for d in candidates if d.is_active]
                    if active_candidates:
                        candidates = active_candidates
                    if len(candidates) == 1:
                        doc = candidates[0]
                        doc_id = doc.id
                    elif len(candidates) > 1:
                        listing = "\n".join(
                            f"- [{d.title}](#document-{d.id})" for d in candidates[:10]
                        )
                        return {
                            "error": (
                                f"More than one document matches '{_target_name}'. "
                                "Ask the user which one, or pass one of these ids:\n"
                                + listing
                            ),
                            "exit_code": 1,
                            "ambiguous_title": True,
                        }
                    else:
                        _requested = _target_name
                else:
                    doc, doc_id, _requested = _resolve_document_target(
                        db, Document, owner=owner, target_id=doc_id, active_only=True
                    )
                if not doc:
                    logger.info(
                        "manage_documents read: unresolvable target requested=%r title=%r",
                        _requested, title_arg,
                    )
                    return _missing_target_error(
                        db, Document, owner, action="read",
                        requested_id=_requested or (title_arg if title_arg else None),
                        editor_open=bool(ctx.get("editor_open")),
                        editor_stale=bool(ctx.get("editor_stale")),
                        library_preview_doc_id=ctx.get("library_preview_doc_id"),
                    )
                body = doc.current_content or ""
                try:
                    preview_limit = max(1, min(int(args.get("limit", MAX_READ_CHARS)), MAX_READ_CHARS))
                except (TypeError, ValueError):
                    preview_limit = MAX_READ_CHARS
                try:
                    offset = max(0, int(args.get("offset", 0) or 0))
                except (TypeError, ValueError):
                    offset = 0
                offset = min(offset, len(body))
                end = min(offset + preview_limit, len(body))
                truncated = end < len(body)
                preview = body[offset:end]
                is_active_editor_doc = bool(_active_document_id) and doc.id == _active_document_id
                # Number lines for the editor document so the model can point at
                # "line N" and quote exact FIND text for edit_document.
                if is_active_editor_doc and not offset:
                    preview = _number_document_lines(preview)
                if truncated:
                    preview += f"\n... (truncated, {len(body)} chars total; next_offset={end})"
                anchor = f"[{doc.title}](#document-{doc.id})"
                header = (
                    f"{anchor} — this is the document currently OPEN in the editor.\n"
                    if is_active_editor_doc
                    else f"{anchor} — click to open in editor.\n"
                )
                if is_active_editor_doc and not offset:
                    header += (
                        "Lines are prefixed with their number and a TAB for reference; "
                        "the number and tab are NOT part of the document.\n"
                    )
                return {
                    "response": f"{header}\n```{doc.language or ''}\n{preview}\n```",
                    "document": {
                        "id": doc.id,
                        "title": doc.title,
                        "language": doc.language,
                        "size": len(body),
                        "content": preview,
                        "truncated": truncated,
                        "offset": offset,
                        "next_offset": end if truncated else None,
                        "active": is_active_editor_doc,
                    },
                    "exit_code": 0,
                }

            elif action == "delete":
                doc_id = args.get("document_id") or args.get("id") or args.get("uid")
                doc = None
                if doc_id:
                    doc = _get_owned_document(db, Document, doc_id, owner)
                    if not doc:
                        return {
                            "error": f"Document '{doc_id}' not found",
                            "exit_code": 1,
                        }
                if not doc:
                    # No id and no editor pointer: deleting is NOT resolvable
                    # like an edit is. An edit can be retried from the returned
                    # id, but a delete of the wrong document cannot be undone
                    # by the model. Require the id the user confirmed.
                    doc, _, _requested = _resolve_document_target(
                        db, Document, owner=owner, target_id=None, active_only=True
                    )
                if not doc:
                    return _missing_target_error(
                        db, Document, owner, action="delete", requested_id=_requested
                    )
                if _editor_open_active_target(doc, ctx):
                    return _editor_delete_blocked_error()
                title = doc.title
                from src.trash_service import move_document_to_trash
                move_document_to_trash(doc)
                db.commit()
                if _active_document_id == doc.id:
                    set_active_document(None)
                return {"response": f"Moved document '{title}' to Trash", "exit_code": 0}

            elif action == "tidy":
                from src.document_actions import run_document_tidy
                result = await run_document_tidy(owner or "")
                return {"response": result, "exit_code": 0}

            else:
                return {"error": f"Unknown action: {action}", "exit_code": 1}
        except Exception as e:
            logger.error(f"manage_documents error: {e}")
            return {"error": str(e), "exit_code": 1}
        finally:
            db.close()
