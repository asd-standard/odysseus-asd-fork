# src/research_utils.py
"""Shared utilities for the deep research system.

Centralizes text cleaning, quality filtering, and other logic
used across deep_research.py, research_handler.py, and visual_report.py.
"""

import json
import re
from pathlib import Path

from src.constants import DEEP_RESEARCH_DIR

# ---------------------------------------------------------------------------
# Thinking / reasoning block stripping
# ---------------------------------------------------------------------------

def strip_thinking(text):
    """Strip thinking / reasoning patterns from LLM output.

    Delegates to `src.text_helpers.strip_think` (single source of truth).
    Kept as an alias here so existing `from src.research_utils import strip_thinking`
    callers don't break. Preserves None passthrough — many callers pass an
    `Optional[str]` LLM result and expect None back when the call failed.
    """
    if text is None:
        return None
    from src.text_helpers import strip_think
    return strip_think(text, prose=False, prompt_echo=True)


# ---------------------------------------------------------------------------
# Source quality filtering
# ---------------------------------------------------------------------------

# Markers indicating extracted content is boilerplate, error text, or empty.
# If any marker is found (case-insensitive), the content is filtered out.
LOW_QUALITY_MARKERS = [
    "insufficient to",
    "content is insufficient",
    "no substantive data",
    "does not contain",
    "not relevant to",
    "no relevant information",
    "unable to extract",
    "completely unrelated",
    "boilerplate",
    "footer text",
    # Phrases (not bare "cookie"/"copyright") so we still catch boilerplate
    # like consent banners and footers without discarding legitimate findings
    # that merely discuss cookies or copyright as their subject.
    "cookie consent",
    "cookie banner",
    "cookie notice",
    "copyright notice",
    "copyright footer",
    "all rights reserved",
]


def is_low_quality(summary: str) -> bool:
    """Check if a finding summary indicates useless or irrelevant content."""
    try:
        if not isinstance(summary, str) or not summary:
            return True
        low = summary.lower()
        return any(marker in low for marker in LOW_QUALITY_MARKERS)
    except Exception:
        return False  # fail open


# ---------------------------------------------------------------------------
# Saved-research references (`[Query](#research-rp-...)` pasted in chat)
# ---------------------------------------------------------------------------

RESEARCH_REFERENCE_TOKEN_BUDGET = 32000
RESEARCH_REFERENCE_COMPACT_CHARS = 24000  # ~7.2k tokens at the 0.3 ratio

_RESEARCH_REF_RE = re.compile(r"#research-([A-Za-z0-9_-]+)")
_RESEARCH_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.S)


def research_id_from_reference(value) -> str:
    """Extract a bare research id from an id, anchor, or pasted markdown link."""
    text = str(value or "").strip()
    match = _RESEARCH_REF_RE.search(text)
    if match:
        return match.group(1)
    if _RESEARCH_ID_RE.fullmatch(text):
        return text
    return ""


def extract_research_reference_ids(text) -> list[str]:
    """Return unique research ids referenced in a message.

    Fenced code blocks are ignored so pasted examples/snippets don't trigger an
    accidental report injection. Order is preserved (first mention wins).
    """
    if not isinstance(text, str) or "#research-" not in text:
        return []
    clean = _FENCED_CODE_RE.sub("", text)
    out: list[str] = []
    seen: set[str] = set()
    for match in _RESEARCH_REF_RE.finditer(clean):
        rid = match.group(1)
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def load_owned_research(research_id, owner) -> dict | None:
    """Load a saved report by id, scoped to `owner`.

    Returns None for malformed ids, missing files, and reports owned by another
    user — the same "not found" behavior used by the research routes. Archived
    reports stay referenceable; only Trash removes the file.
    """
    rid = research_id_from_reference(research_id)
    if not rid:
        return None
    root = Path(DEEP_RESEARCH_DIR).resolve()
    path = (root / f"{rid}.json").resolve()
    if path.parent != root or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("owner") or "") != (owner or ""):
        return None
    data["id"] = rid
    return data


def research_report_text(data: dict) -> str:
    """The human-readable report body stored for a saved research result."""
    if not isinstance(data, dict):
        return ""
    for key in ("result", "raw_report", "summary", "report"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def estimate_research_tokens(text: str) -> int:
    """Rough token count using the same chars * 0.3 heuristic as the app."""
    return int(len(text or "") * 0.3)


def compact_research_report(text: str, max_chars: int = RESEARCH_REFERENCE_COMPACT_CHARS) -> str:
    """Rule-based compaction: keep headings and the first sentence of each block.

    This is deliberately deterministic and cheap — no extra LLM call. The goal
    is to preserve the report's structure and main claims while dropping the
    long source-by-source prose that makes oversized reports expensive.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text

    out: list[str] = []
    used = 0
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped.startswith("#"):
            out.append(stripped)
        elif re.match(r"^(?:[-*+]|\d+[.)])\s+", stripped):
            out.append(stripped[:240].rstrip())
        else:
            sentences = re.split(r"(?<=[.!?])\s+", stripped)
            out.append(" ".join(sentences[:2])[:600].rstrip())
        used += len(out[-1]) + 1
        if used >= max_chars:
            break

    compacted = "\n\n".join(out)
    if len(compacted) > max_chars:
        compacted = compacted[:max_chars].rsplit(" ", 1)[0].rstrip() + " …"
    # Very repetitive prose can collapse to almost nothing with the rule above.
    # Keep more of the original rather than returning a near-empty summary.
    if len(compacted) < max_chars // 4:
        compacted = text[:max_chars].rsplit(" ", 1)[0].rstrip() + " …"
    return compacted or text[:max_chars].rstrip() + " …"


def format_research_reference(data: dict, *, compact: bool = False) -> str:
    """Render a saved report as a source block for chat injection."""
    query = " ".join(str(data.get("query") or "(untitled research)").split())
    rid = str(data.get("id") or "").strip()
    body = research_report_text(data)
    if compact:
        body = compact_research_report(body)
    sources = data.get("sources") or []
    lines = [f"# {query}", f"Research id: {rid}", "", body]
    if isinstance(sources, list) and sources:
        lines.append("")
        lines.append("Sources:")
        for src in sources[:(10 if compact else 30)]:
            if not isinstance(src, dict):
                continue
            title = str(src.get("title") or src.get("url") or "").strip()
            url = str(src.get("url") or "").strip()
            lines.append(f"- {title}: {url}" if url else f"- {title}")
    return "\n".join(lines).strip()
