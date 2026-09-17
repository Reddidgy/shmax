"""BM25-scored keyword search over the project knowledge base (pure, no Flask).

A connected MCP agent needs to find the one knowledge-base document that
explains a topic without scanning the entire project file tree.  This module
owns that *search*: it walks ``{project_root}/project-context/`` (and only that
directory), extracts structured fields (title, headings, body, path) from each
file, tokenizes content, and ranks documents using BM25 scoring with field-level
boosting — inspired by how Omnisearch works in Obsidian.

The search supports:

- **BM25 scoring**: term frequency saturation, document length normalization,
  and inverse document frequency for relevance ranking.
- **Field boosting**: matches in titles (4x) and headings (3x) score higher
  than body text (1x), with path components contributing at 0.5x.
- **Prefix matching**: a query term matches document tokens that start with it
  (e.g. ``deploy`` matches ``deployment``), weighted at 0.7x of an exact match.
- **Fuzzy matching**: Levenshtein edit-distance tolerance for misspellings
  (0.2 x term length), weighted at 0.5x of an exact match.

It is deliberately free of any Flask import so the logic is unit-testable and
reusable; the MCP layer (``mcp_tools.py``) is a thin adapter that validates the
``project_root`` and returns this module's result verbatim.

When the project declares ``linkedProjects`` in ``.praxis/config/general.yaml``,
the walk covers the current project's knowledge base *and* each linked
project's, merged into one BM25 corpus so ranking and the response cap are
global rather than per-project.  Results from the current project keep their
project-relative path; results from a linked project are labeled with their
absolute path so the agent can tell which project a file belongs to.

The search is confined to the knowledge base structurally — the walk only ever
roots at ``project-context/`` — and defensively: each file's real path is
verified to lie within the resolved knowledge-base boundary before it is read,
so a symlink cannot escape.  It uses only the standard library — no external
dependencies.
"""

from __future__ import annotations

import fnmatch
import math
import os
import re
from collections import Counter

import settings_ops

__all__ = [
    "CONTEXT_DIRNAME",
    "MAX_FILES_RETURNED",
    "MAX_LINES_PER_FILE",
    "MAX_LINE_LENGTH",
    "PRUNED_DIRS",
    "BINARY_EXTENSIONS",
    "EXCLUDED_RELPATHS",
    "MIN_KEYWORDS_FOR_PARTIAL",
    "BM25_K1",
    "BM25_B",
    "FIELD_BOOSTS",
    "resolve_context_roots",
    "search_project_context",
    "update_project_context",
]

# ── Knowledge-base constants ────────────────────────────────────────

CONTEXT_DIRNAME = "project-context"
MAX_FILES_RETURNED = 8
MAX_LINES_PER_FILE = 6
MAX_LINE_LENGTH = 200

PRUNED_DIRS = frozenset({"__pycache__", ".git", "node_modules"})

BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
        ".ico",
        ".svg",
        ".webp",
        ".bmp",
        ".zip",
        ".gz",
        ".tar",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp3",
        ".mp4",
        ".mov",
        ".pyc",
    }
)

EXCLUDED_RELPATHS = frozenset()

MIN_KEYWORDS_FOR_PARTIAL = 3

# ── BM25 tuning ─────────────────────────────────────────────────────

BM25_K1 = 1.2
BM25_B = 0.75
FIELD_BOOSTS = {"title": 4.0, "headings": 3.0, "body": 1.0, "path": 0.5}

_PREFIX_WEIGHT = 0.7
_FUZZY_WEIGHT = 0.5
_FUZZY_FACTOR = 0.2

# ── Tokenization ────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[^\W_]+")


def _tokenize(text: str) -> list[str]:
    """Split *text* into lowercase Unicode word tokens (letters + digits)."""
    return _TOKEN_RE.findall(text.lower())


# ── Markdown field extraction ────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")


def _extract_fields(
    content: str, filename: str, rel_path: str
) -> dict[str, str]:
    """Parse a markdown file into searchable fields.

    Returns ``title``, ``headings``, ``body``, and ``path`` strings.
    The first H1 heading (plus the filename for non-README files) becomes
    the title; subsequent headings populate the headings field; everything
    else is body.  Code-fenced blocks are routed to body regardless of
    ``#`` characters inside them.
    """
    name = os.path.splitext(filename)[0]
    is_readme = name.upper() == "README"
    title_parts: list[str] = (
        [] if is_readme else [name.replace("-", " ").replace("_", " ")]
    )
    heading_parts: list[str] = []
    body_parts: list[str] = []
    first_h1_captured = False
    in_code_block = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            body_parts.append(line)
            continue
        if in_code_block:
            body_parts.append(line)
            continue

        m = _HEADING_RE.match(line)
        if m:
            heading_text = m.group(2).strip()
            if not heading_text:
                body_parts.append(line)
                continue
            level = len(m.group(1))
            if level == 1 and not first_h1_captured:
                title_parts.append(heading_text)
                first_h1_captured = True
            else:
                heading_parts.append(heading_text)
        else:
            body_parts.append(line)

    return {
        "title": " ".join(title_parts),
        "headings": " ".join(heading_parts),
        "body": "\n".join(body_parts),
        "path": rel_path.replace("/", " ").replace("-", " ").replace("_", " "),
    }


# ── Edit distance ────────────────────────────────────────────────────


def _edit_distance(s1: str, s2: str) -> int:
    """Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for c1 in s1:
        curr = [prev[0] + 1]
        for j, c2 in enumerate(s2):
            curr.append(
                min(curr[j] + 1, prev[j + 1] + 1, prev[j] + (0 if c1 == c2 else 1))
            )
        prev = curr
    return prev[-1]


# ── Token matching ───────────────────────────────────────────────────


def _token_match_weight(query_tok: str, doc_tok: str) -> float:
    """Return match weight: 1.0 exact, 0.7 prefix, 0.5 fuzzy, 0.0 none."""
    if query_tok == doc_tok:
        return 1.0
    if len(query_tok) >= 2 and doc_tok.startswith(query_tok):
        return _PREFIX_WEIGHT
    max_dist = int(_FUZZY_FACTOR * len(query_tok))
    if max_dist > 0 and abs(len(query_tok) - len(doc_tok)) <= max_dist:
        if _edit_distance(query_tok, doc_tok) <= max_dist:
            return _FUZZY_WEIGHT
    return 0.0


def _has_any_match(query_tok: str, unique_tokens: frozenset[str]) -> bool:
    """Return whether *query_tok* matches any token in *unique_tokens*."""
    if query_tok in unique_tokens:
        return True
    if len(query_tok) >= 2:
        for dt in unique_tokens:
            if dt.startswith(query_tok):
                return True
    max_dist = int(_FUZZY_FACTOR * len(query_tok))
    if max_dist > 0:
        for dt in unique_tokens:
            if abs(len(query_tok) - len(dt)) <= max_dist:
                if _edit_distance(query_tok, dt) <= max_dist:
                    return True
    return False


def _weighted_tf(query_tok: str, token_freqs: Counter) -> float:
    """Weighted term frequency considering exact, prefix, and fuzzy matches."""
    tf = 0.0
    seen_prefix: set[str] = set()
    if query_tok in token_freqs:
        tf += token_freqs[query_tok]
    if len(query_tok) >= 2:
        for dt, count in token_freqs.items():
            if dt != query_tok and dt.startswith(query_tok):
                tf += count * _PREFIX_WEIGHT
                seen_prefix.add(dt)
    max_dist = int(_FUZZY_FACTOR * len(query_tok))
    if max_dist > 0:
        for dt, count in token_freqs.items():
            if dt == query_tok or dt in seen_prefix:
                continue
            if abs(len(query_tok) - len(dt)) <= max_dist:
                if _edit_distance(query_tok, dt) <= max_dist:
                    tf += count * _FUZZY_WEIGHT
    return tf


# ── BM25 components ─────────────────────────────────────────────────


def _bm25_idf(df: int, n: int) -> float:
    """BM25 inverse document frequency (non-negative)."""
    return max(0.0, math.log((n - df + 0.5) / (df + 0.5) + 1.0))


def _bm25_tf_norm(tf: float, dl: int, avgdl: float) -> float:
    """BM25 normalized term frequency with saturation and length normalization."""
    if tf <= 0 or avgdl <= 0:
        return 0.0
    safe_avgdl = max(avgdl, 1.0)
    return (tf * (BM25_K1 + 1)) / (
        tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / safe_avgdl)
    )


# ── Line selection ───────────────────────────────────────────────────


def _select_best_lines(content: str, query_tokens: list[str]) -> list[dict]:
    """Select the most relevant matching lines as context excerpts.

    Lines are scored by how many query tokens they contain (substring match),
    the top :data:`MAX_LINES_PER_FILE` are kept, and returned in document order.
    Lines longer than :data:`MAX_LINE_LENGTH` are truncated with an ellipsis.
    """
    candidates: list[tuple[float, int, str]] = []
    for line_num, line in enumerate(content.splitlines(), start=1):
        line_lower = line.lower()
        score = sum(1.0 for qt in query_tokens if qt in line_lower)
        if score > 0:
            display = line if len(line) <= MAX_LINE_LENGTH else line[:MAX_LINE_LENGTH] + "..."
            candidates.append((score, line_num, display))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    top = candidates[: MAX_LINES_PER_FILE]
    top.sort(key=lambda x: x[1])
    return [{"line_number": ln, "line_content": lc} for _, ln, lc in top]


# ── Helpers ──────────────────────────────────────────────────────────


def _empty_response(query: str) -> str:
    """Build the plain-text response for a no-match search."""
    return f'No results for "{query}"'


def _is_within(file_real: str, base_real: str) -> bool:
    """Return whether *file_real* lies inside the resolved *base_real* boundary.

    Both arguments are expected to be ``os.path.realpath`` results.  The
    ``+ os.sep`` guard prevents a sibling like ``project-context-extra`` from
    being treated as inside ``project-context``.
    """
    return file_real.startswith(base_real + os.sep)


def resolve_context_roots(project_root: str) -> list[str]:
    """Return every project root whose knowledge base participates in a search.

    The current project comes first, followed by linked projects in
    configuration order.  A config read failure degrades to a single-project
    search rather than surfacing an error — search must never fail because a
    linked project is misconfigured.
    """
    try:
        return settings_ops.resolve_project_roots(project_root)
    except Exception:
        return [project_root]


def _collect_docs(
    root: str,
    file_pattern: str | None,
    use_absolute_paths: bool,
) -> list[dict]:
    """Collect scored-document records from one project's knowledge base.

    *use_absolute_paths* labels each document with its absolute path instead of
    its project-relative path — used for linked projects, where a relative path
    would be ambiguous.  A project without ``project-context/`` contributes
    nothing.
    """
    base = os.path.join(root, CONTEXT_DIRNAME)
    if not os.path.isdir(base):
        return []
    base_real = os.path.realpath(base)

    docs: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in PRUNED_DIRS]
        for filename in filenames:
            _, ext = os.path.splitext(filename)
            if ext.lower() in BINARY_EXTENSIONS:
                continue
            if file_pattern is not None and not fnmatch.fnmatch(
                filename, file_pattern
            ):
                continue

            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            if relative in EXCLUDED_RELPATHS:
                continue
            if not _is_within(os.path.realpath(absolute), base_real):
                continue

            try:
                with open(absolute, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue

            label = absolute if use_absolute_paths else relative
            fields = _extract_fields(content, filename, relative)
            field_token_freqs = {
                f: Counter(_tokenize(text)) for f, text in fields.items()
            }
            field_lengths = {
                f: sum(freq.values()) for f, freq in field_token_freqs.items()
            }
            all_unique: frozenset[str] = frozenset().union(
                *(freq.keys() for freq in field_token_freqs.values())
            )

            docs.append(
                {
                    "path": label,
                    "content": content,
                    "field_token_freqs": field_token_freqs,
                    "field_lengths": field_lengths,
                    "all_unique_tokens": all_unique,
                }
            )

    return docs


# ── Main search ──────────────────────────────────────────────────────


def search_project_context(
    project_root: str,
    query: str,
    file_pattern: str | None = None,
) -> str:
    """BM25-scored search over the project's knowledge base.

    Walks ``project-context/`` in the current project and in every linked
    project (``linkedProjects`` in general.yaml), extracts structured fields
    (title, headings, body, path) from each file, tokenizes content, and
    scores documents using BM25 with field-level boosting.  Supports prefix
    matching and fuzzy matching via edit distance.  Linked-project files are
    labeled with their absolute path; current-project files keep their
    project-relative path.

    Inclusion is coverage-based: a file is included when the count of distinct
    query tokens matching its content meets a threshold — all tokens for 1- and
    2-token queries (backward-compatible AND), and at least ``ceil(n / 2)`` for
    queries of :data:`MIN_KEYWORDS_FOR_PARTIAL` tokens or more.

    Returns a plain-text string with one block per result file, ranked by BM25
    score descending, tie-broken by path ascending, capped at
    :data:`MAX_FILES_RETURNED` total across all searched projects.  Each block
    has a header line with path and match quality, followed by up to
    :data:`MAX_LINES_PER_FILE` matching lines.

    A blank query, a missing ``project-context/`` directory, or no matches
    yields ``'No results for "<query>"'`` — never an error, never a raise.
    """
    query_tokens = list(dict.fromkeys(_tokenize(query)))
    if not query_tokens:
        return _empty_response(query)

    # ── Phase 1: collect documents (current project + linked projects) ──

    roots = resolve_context_roots(project_root)
    docs: list[dict] = []
    for index, root in enumerate(roots):
        docs.extend(
            _collect_docs(root, file_pattern, use_absolute_paths=index > 0)
        )

    if not docs:
        return _empty_response(query)

    n_docs = len(docs)

    # ── Phase 2: corpus statistics ──────────────────────────────────

    avg_field_lengths: dict[str, float] = {}
    for field in FIELD_BOOSTS:
        total = sum(d["field_lengths"].get(field, 0) for d in docs)
        avg_field_lengths[field] = total / n_docs

    doc_freqs: dict[str, int] = {}
    for qt in query_tokens:
        doc_freqs[qt] = sum(
            1 for d in docs if _has_any_match(qt, d["all_unique_tokens"])
        )

    # ── Phase 3: score and filter ───────────────────────────────────

    scored: list[dict] = []
    for doc in docs:
        matched_tokens = {
            qt
            for qt in query_tokens
            if _has_any_match(qt, doc["all_unique_tokens"])
        }
        coverage = len(matched_tokens) / len(query_tokens)
        min_required = (
            len(query_tokens)
            if len(query_tokens) < MIN_KEYWORDS_FOR_PARTIAL
            else math.ceil(len(query_tokens) / 2)
        )
        if len(matched_tokens) < min_required:
            continue

        total_score = 0.0
        for qt in matched_tokens:
            idf = _bm25_idf(doc_freqs[qt], n_docs)
            for field, boost in FIELD_BOOSTS.items():
                freqs = doc["field_token_freqs"].get(field, Counter())
                tf = _weighted_tf(qt, freqs)
                if tf > 0:
                    dl = doc["field_lengths"].get(field, 0)
                    avgdl = avg_field_lengths.get(field, 1.0)
                    total_score += boost * idf * _bm25_tf_norm(tf, dl, avgdl)

        matches = _select_best_lines(doc["content"], query_tokens)
        missing = [qt for qt in query_tokens if qt not in matched_tokens]

        scored.append(
            {
                "path": doc["path"],
                "matches": matches,
                "score": round(total_score, 4),
                "coverage": coverage,
                "matched_tokens": sorted(matched_tokens),
                "missing_tokens": missing,
            }
        )

    # ── Phase 4: rank and format response ───────────────────────────

    scored.sort(key=lambda d: (-d["score"], d["path"]))
    scored = scored[:MAX_FILES_RETURNED]

    if not scored:
        return _empty_response(query)

    blocks: list[str] = []
    for doc in scored:
        is_exact = doc["coverage"] == 1.0
        if is_exact:
            header = f'{doc["path"]} [exact]'
        else:
            missing = " ".join(doc["missing_tokens"])
            header = f'{doc["path"]} [partial, missing: {missing}]'
        lines = [header]
        for m in doc["matches"]:
            lines.append(f'L{m["line_number"]}: {m["line_content"]}')
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


# ── Update (skill redirect) ────────────────────────────────────────

CONTEXT_ACTUALIZER_SKILL = ".agents/skills/context-router-actualizer/SKILL.md"


def update_project_context(project_root: str) -> dict:
    """Return instructions directing the agent to the context-router-actualizer skill.

    Instead of writing files directly, this tool tells the calling agent
    to read and follow the skill at
    ``.agents/skills/context-router-actualizer/SKILL.md`` which governs
    how ``project-context/`` routes and ``context-router.md`` should be
    updated.

    Returns ``{"ok": True, "skill": "<path>", "instructions": "..."}``
    on success.
    """
    skill_abs = os.path.join(project_root, CONTEXT_ACTUALIZER_SKILL)
    if not os.path.isfile(skill_abs):
        return {
            "ok": False,
            "error": (
                f"Skill file not found: {CONTEXT_ACTUALIZER_SKILL} — "
                "ensure the project has been initialized with PraxisOS"
            ),
        }

    return {
        "ok": True,
        "skill": CONTEXT_ACTUALIZER_SKILL,
        "instructions": (
            "STOP GATE — evaluate the scope of your changes FIRST:\n"
            "1. Did you modify an existing Route (e.g., added a new required field, "
            "changed how a module works, discovered an architectural constraint or "
            "important system behavior)? → Proceed.\n"
            "2. Did you introduce a completely NEW Route (e.g., a new business entity "
            "or technical module)? → Proceed.\n"
            "3. Did you make a discovery worth preserving — a bug root cause with "
            "architectural implications, a technical constraint, or a system behavior "
            "that agents must know to avoid mistakes? → Proceed.\n"
            "4. Did you only do a trivial refactor, pure style change, or minor bug fix "
            "with no architectural implications and no findings worth preserving? "
            "→ STOP. No actualization needed. Do not read SKILL.md.\n\n"
            "If your answer is #4, you are done. "
            "Otherwise, read and follow the skill at "
            f"`{CONTEXT_ACTUALIZER_SKILL}` starting from Step 2."
        ),
    }
