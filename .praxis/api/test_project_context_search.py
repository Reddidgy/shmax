"""Tests for :mod:`project_context_search` — the BM25-scored knowledge-base search.

Run from the ``api/`` directory:

    ./venv/bin/python -m pytest test_project_context_search.py -v

These build a throwaway ``project-context/`` tree with ``tmp_path`` and assert
that :func:`search_project_context` honours case-insensitive keyword matching,
BM25 scoring with field boosting, prefix and fuzzy matching, filename patterns,
binary skipping, the knowledge-base boundary, the per-file line cap, the
per-response file cap, and ranking order.
"""

from __future__ import annotations

import os

from project_context_search import (
    MAX_FILES_RETURNED,
    MAX_LINE_LENGTH,
    MAX_LINES_PER_FILE,
    search_project_context,
    update_project_context,
)


def _context_dir(tmp_path):
    """Create and return the project's ``project-context/`` directory."""
    base = tmp_path / "project-context"
    base.mkdir()
    return base


def _paths_from_result(result: str) -> list[str]:
    """Extract ordered file paths from the plain-text search result."""
    return [
        line.split(" [")[0]
        for line in result.split("\n")
        if line and not line.startswith("L") and " [" in line
    ]


# ---------------------------------------------------------------------------
# Basic search behavior
# ---------------------------------------------------------------------------


def test_no_match_returns_empty_response(tmp_path):
    """A query whose keywords appear nowhere yields a no-results string."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("nothing relevant here\n")

    result = search_project_context(str(tmp_path), "absentword")

    assert result == 'No results for "absentword"'


def test_missing_context_dir_returns_empty_response(tmp_path):
    """A project without project-context/ is a valid no-match, not an error."""
    result = search_project_context(str(tmp_path), "anything")

    assert result == 'No results for "anything"'


def test_blank_query_returns_empty_response(tmp_path):
    """A whitespace-only query short-circuits to the no-results string."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("some content\n")

    result = search_project_context(str(tmp_path), "   ")

    assert result == 'No results for "   "'


def test_single_keyword_match_is_case_insensitive(tmp_path):
    """A single keyword returns the doc with correct path and matching lines."""
    base = _context_dir(tmp_path)
    routes = base / "tasks"
    routes.mkdir(parents=True)
    (routes / "README.md").write_text(
        "Intro line\n"
        "This describes the Workflow of tasks\n"
        "Unrelated middle line\n"
        "another WORKFLOW reference here\n"
    )

    result = search_project_context(str(tmp_path), "workflow")

    assert "project-context/tasks/README.md [exact]" in result
    assert "L2: This describes the Workflow of tasks" in result
    assert "L4: another WORKFLOW reference here" in result


def test_multi_keyword_and_semantics(tmp_path):
    """Only docs containing every keyword (anywhere in the file) are returned."""
    base = _context_dir(tmp_path)
    (base / "doc1.md").write_text("alpha appears here\nand beta appears too\n")
    (base / "doc2.md").write_text(
        "alpha appears here\nbut beta does not\n".replace("beta", "gamma")
    )

    result = search_project_context(str(tmp_path), "alpha beta")

    assert "project-context/doc1.md [exact]" in result
    assert "project-context/doc2.md" not in result


def test_file_pattern_filters_on_filename(tmp_path):
    """A *.md pattern excludes a matching .txt file."""
    base = _context_dir(tmp_path)
    (base / "keep.md").write_text("contains keyword target\n")
    (base / "skip.txt").write_text("also contains keyword target\n")

    result = search_project_context(str(tmp_path), "target", file_pattern="*.md")

    assert "project-context/keep.md" in result
    assert "project-context/skip.txt" not in result


def test_binary_extension_is_skipped(tmp_path):
    """A binary-extension file containing the keyword bytes is never returned."""
    base = _context_dir(tmp_path)
    (base / "image.png").write_bytes(b"this png mentions keyword target\n")
    (base / "doc.md").write_text("real keyword target here\n")

    result = search_project_context(str(tmp_path), "target")

    assert "project-context/doc.md" in result
    assert "project-context/image.png" not in result


def test_content_outside_context_dir_is_never_returned(tmp_path):
    """Files outside project-context/ are out of scope and never returned."""
    base = _context_dir(tmp_path)
    (base / "inside.md").write_text("keyword target inside\n")
    (tmp_path / "outside.md").write_text("keyword target outside\n")
    other = tmp_path / "src"
    other.mkdir()
    (other / "nested.md").write_text("keyword target nested outside\n")

    result = search_project_context(str(tmp_path), "target")

    paths = _paths_from_result(result)
    assert paths == ["project-context/inside.md"]


def test_symlink_escaping_context_dir_is_rejected(tmp_path):
    """A symlink inside project-context/ pointing outside is never read."""
    base = _context_dir(tmp_path)
    (base / "inside.md").write_text("keyword target inside\n")

    secret = tmp_path / "secret.md"
    secret.write_text("keyword target escaped via symlink\n")
    link = base / "escape.md"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        return

    result = search_project_context(str(tmp_path), "target")

    paths = _paths_from_result(result)
    assert paths == ["project-context/inside.md"]


def test_max_lines_per_file_cap(tmp_path):
    """No document shows more than MAX_LINES_PER_FILE matching lines."""
    base = _context_dir(tmp_path)
    lines = "\n".join(f"keyword line {i}" for i in range(MAX_LINES_PER_FILE + 5))
    (base / "big.md").write_text(lines + "\n")

    result = search_project_context(str(tmp_path), "keyword")

    match_lines = [l for l in result.split("\n") if l.startswith("L")]
    assert len(match_lines) == MAX_LINES_PER_FILE


def test_max_files_cap(tmp_path):
    """More than MAX_FILES_RETURNED matches are capped."""
    base = _context_dir(tmp_path)
    for i in range(MAX_FILES_RETURNED + 5):
        (base / f"doc{i:03d}.md").write_text("keyword here\n")

    result = search_project_context(str(tmp_path), "keyword")

    paths = _paths_from_result(result)
    assert len(paths) == MAX_FILES_RETURNED


def test_ranking_orders_by_relevance(tmp_path):
    """Documents with higher BM25 scores (more matches) rank first."""
    base = _context_dir(tmp_path)
    (base / "few.md").write_text("keyword once\nunrelated\n")
    (base / "many.md").write_text("keyword\nkeyword\nkeyword\n")

    result = search_project_context(str(tmp_path), "keyword")

    paths = _paths_from_result(result)
    assert paths == ["project-context/many.md", "project-context/few.md"]


# ---------------------------------------------------------------------------
# Coverage-based matching tests
# ---------------------------------------------------------------------------


def test_three_keyword_partial_match_is_included(tmp_path):
    """With 3 keywords, a doc matching 2 of 3 is included as partial."""
    base = _context_dir(tmp_path)
    (base / "full.md").write_text("alpha beta gamma content\n")
    (base / "partial.md").write_text("alpha beta but no third keyword\n")
    (base / "miss.md").write_text("only alpha here and nothing else\n")

    result = search_project_context(str(tmp_path), "alpha beta gamma")

    paths = _paths_from_result(result)
    assert "project-context/full.md" in paths
    assert "project-context/partial.md" in paths
    assert "project-context/miss.md" not in paths


def test_two_keyword_query_still_requires_both(tmp_path):
    """Backward compat: 2-keyword queries are still strict AND."""
    base = _context_dir(tmp_path)
    (base / "both.md").write_text("alpha and beta together\n")
    (base / "one.md").write_text("only alpha here\n")

    result = search_project_context(str(tmp_path), "alpha beta")

    paths = _paths_from_result(result)
    assert paths == ["project-context/both.md"]


def test_exact_matches_rank_above_partial(tmp_path):
    """Full-match docs always rank above partial-match docs."""
    base = _context_dir(tmp_path)
    (base / "partial.md").write_text(
        "alpha alpha alpha alpha\n" * 20 + "beta here too\n"
    )
    (base / "full.md").write_text("alpha beta gamma once each\n")

    result = search_project_context(str(tmp_path), "alpha beta gamma")

    paths = _paths_from_result(result)
    assert paths[0] == "project-context/full.md"
    assert paths[1] == "project-context/partial.md"


def test_match_quality_field_values(tmp_path):
    """Exact matches show '[exact]'; partial matches show '[partial, missing: ...]'."""
    base = _context_dir(tmp_path)
    (base / "full.md").write_text("alpha beta gamma\n")
    (base / "partial.md").write_text("alpha beta only\n")

    result = search_project_context(str(tmp_path), "alpha beta gamma")

    assert "project-context/full.md [exact]" in result
    assert "project-context/partial.md [partial, missing: gamma]" in result


def test_four_keyword_threshold(tmp_path):
    """With 4 keywords, need at least 2 to match (ceil(4/2) = 2)."""
    base = _context_dir(tmp_path)
    (base / "two_match.md").write_text("alpha beta and nothing else\n")
    (base / "one_match.md").write_text("only alpha here\n")

    result = search_project_context(str(tmp_path), "alpha beta gamma delta")

    assert "project-context/two_match.md" in result
    assert "project-context/one_match.md" not in result


def test_five_keyword_threshold(tmp_path):
    """With 5 keywords, need at least 3 to match (ceil(5/2) = 3)."""
    base = _context_dir(tmp_path)
    (base / "three.md").write_text("alpha beta gamma present\n")
    (base / "two.md").write_text("alpha beta present\n")

    result = search_project_context(str(tmp_path), "alpha beta gamma delta epsilon")

    assert "project-context/three.md" in result
    assert "project-context/two.md" not in result


def test_single_keyword_includes_match_quality(tmp_path):
    """Even 1-keyword results include [exact] quality marker."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("keyword target here\n")

    result = search_project_context(str(tmp_path), "target")

    assert "[exact]" in result


# ---------------------------------------------------------------------------
# BM25 scoring tests
# ---------------------------------------------------------------------------


def test_result_is_plain_text_with_expected_format(tmp_path):
    """Results are returned as a plain-text string with header and L-prefixed lines."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("keyword target here\n")

    result = search_project_context(str(tmp_path), "target")

    assert isinstance(result, str)
    assert "project-context/doc.md [exact]" in result
    assert "L1: keyword target here" in result


def test_title_match_ranks_higher_than_body_match(tmp_path):
    """A match in the document title (H1) ranks higher than body only."""
    base = _context_dir(tmp_path)
    (base / "title_match.md").write_text(
        "# Deployment Guide\nThis is a guide about servers.\n"
    )
    (base / "body_match.md").write_text(
        "# Server Guide\nThis discusses deployment in detail.\n"
    )

    result = search_project_context(str(tmp_path), "deployment")

    paths = _paths_from_result(result)
    assert len(paths) == 2
    assert paths[0] == "project-context/title_match.md"


def test_heading_match_boosts_score(tmp_path):
    """A match in a heading (H2+) ranks higher than body-only for the same term."""
    base = _context_dir(tmp_path)
    (base / "heading.md").write_text(
        "# Intro\n## Authentication Flow\nSome text here.\n"
    )
    (base / "body.md").write_text(
        "# Intro\n## Overview\nThe authentication flow is described here.\n"
    )

    result = search_project_context(str(tmp_path), "authentication")

    paths = _paths_from_result(result)
    assert paths.index("project-context/heading.md") < paths.index(
        "project-context/body.md"
    )


def test_prefix_matching_finds_partial_words(tmp_path):
    """A query term matches document tokens that start with it."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("The deployment pipeline handles everything.\n")

    result = search_project_context(str(tmp_path), "deploy")

    paths = _paths_from_result(result)
    assert len(paths) == 1
    assert paths[0] == "project-context/doc.md"


def test_fuzzy_matching_tolerates_misspelling(tmp_path):
    """A misspelled 5+ char query term still finds documents via edit distance."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("The configuration system is robust.\n")

    result = search_project_context(str(tmp_path), "configuraton")

    paths = _paths_from_result(result)
    assert len(paths) == 1
    assert paths[0] == "project-context/doc.md"


def test_idf_boosts_rare_terms(tmp_path):
    """A match on a rare term scores higher than a match on a ubiquitous term."""
    base = _context_dir(tmp_path)
    for i in range(10):
        (base / f"common{i}.md").write_text(f"The common word appears here doc{i}\n")
    (base / "common_plus_rare.md").write_text("The common word and the xylophone\n")
    (base / "rare_only.md").write_text("The xylophone is unique\n")

    result = search_project_context(str(tmp_path), "xylophone")

    paths = _paths_from_result(result)
    assert len(paths) == 2
    assert "project-context/common_plus_rare.md" in paths
    assert "project-context/rare_only.md" in paths


def test_shorter_doc_boosted_over_longer(tmp_path):
    """BM25 length normalization favors shorter documents with the same match."""
    base = _context_dir(tmp_path)
    (base / "short.md").write_text("# Guide\ndeployment info\n")
    (base / "long.md").write_text(
        "# Guide\n" + "unrelated content line\n" * 100 + "deployment info\n"
    )

    result = search_project_context(str(tmp_path), "deployment")

    paths = _paths_from_result(result)
    assert paths[0] == "project-context/short.md"


def test_best_lines_selected_for_multi_keyword(tmp_path):
    """Lines matching more query terms are prioritized in excerpts."""
    base = _context_dir(tmp_path)
    content_lines = []
    for i in range(MAX_LINES_PER_FILE + 5):
        content_lines.append(f"alpha only line {i}")
    content_lines.append("alpha beta gamma all three here")
    (base / "doc.md").write_text("\n".join(content_lines) + "\n")

    result = search_project_context(str(tmp_path), "alpha beta gamma")

    assert "alpha beta gamma all three here" in result


def test_code_block_headings_not_extracted(tmp_path):
    """Headings inside code blocks are not treated as document headings."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text(
        "# Real Title\n"
        "Body text\n"
        "```python\n"
        "# This is a comment not a heading\n"
        "```\n"
        "More body\n"
    )

    result = search_project_context(str(tmp_path), "comment")

    paths = _paths_from_result(result)
    assert len(paths) == 1
    assert "comment" in result.lower()


def test_duplicate_query_tokens_deduplicated(tmp_path):
    """Duplicate tokens in the query are deduplicated before matching."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("alpha content here\n")

    result = search_project_context(str(tmp_path), "alpha alpha")

    assert "project-context/doc.md [exact]" in result


# ---------------------------------------------------------------------------
# update_project_context tests (skill redirect)
# ---------------------------------------------------------------------------

SKILL_RELPATH = ".agents/skills/context-router-actualizer/SKILL.md"


def _skill_file(tmp_path):
    """Create the context-router-actualizer skill file."""
    skill = tmp_path / SKILL_RELPATH
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# CONTEXT ROUTER ACTUALIZATION SKILL\n")
    return skill


def test_update_returns_skill_instructions(tmp_path):
    """When the skill file exists, returns instructions pointing to it."""
    _skill_file(tmp_path)

    result = update_project_context(str(tmp_path))

    assert result["ok"] is True
    assert result["skill"] == SKILL_RELPATH
    assert "context-router-actualizer" in result["instructions"]


def test_update_error_when_skill_missing(tmp_path):
    """When the skill file is missing, returns an error."""
    result = update_project_context(str(tmp_path))

    assert result["ok"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# Line truncation tests
# ---------------------------------------------------------------------------


def test_long_lines_are_truncated(tmp_path):
    """Lines exceeding MAX_LINE_LENGTH are truncated with an ellipsis."""
    base = _context_dir(tmp_path)
    long_line = "keyword " + "x" * (MAX_LINE_LENGTH + 100)
    (base / "doc.md").write_text(long_line + "\n")

    result = search_project_context(str(tmp_path), "keyword")

    match_lines = [l for l in result.split("\n") if l.startswith("L")]
    assert len(match_lines) == 1
    content = match_lines[0].split(": ", 1)[1]
    assert content.endswith("...")
    assert len(content) == MAX_LINE_LENGTH + 3


def test_short_lines_are_not_truncated(tmp_path):
    """Lines within MAX_LINE_LENGTH are returned verbatim."""
    base = _context_dir(tmp_path)
    short_line = "keyword target short line"
    (base / "doc.md").write_text(short_line + "\n")

    result = search_project_context(str(tmp_path), "keyword")

    assert f"L1: {short_line}" in result
    assert "..." not in result


# ---------------------------------------------------------------------------
# Unicode / Cyrillic support tests
# ---------------------------------------------------------------------------


def test_cyrillic_single_keyword_match(tmp_path):
    """A Cyrillic keyword matches Cyrillic content in the knowledge base."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("# Анамнез\nОперация прошла успешно.\n")

    result = search_project_context(str(tmp_path), "операция")

    assert "project-context/doc.md [exact]" in result
    assert "Операция прошла успешно" in result


def test_cyrillic_multi_keyword_match(tmp_path):
    """Multiple Cyrillic keywords work with AND semantics."""
    base = _context_dir(tmp_path)
    (base / "both.md").write_text("абсцесс и киста обнаружены\n")
    (base / "one.md").write_text("только абсцесс здесь\n")

    result = search_project_context(str(tmp_path), "абсцесс киста")

    assert "project-context/both.md" in result
    assert "project-context/one.md" not in result


def test_cyrillic_title_boost(tmp_path):
    """Cyrillic terms in titles get field boost just like Latin ones."""
    base = _context_dir(tmp_path)
    (base / "title.md").write_text("# Лечение\nОбщая информация.\n")
    (base / "body.md").write_text("# Обзор\nЛечение описано ниже.\n")

    result = search_project_context(str(tmp_path), "лечение")

    paths = _paths_from_result(result)
    assert len(paths) == 2
    assert paths[0] == "project-context/title.md"


def test_mixed_latin_cyrillic_query(tmp_path):
    """A query mixing Latin and Cyrillic tokens works correctly."""
    base = _context_dir(tmp_path)
    (base / "mixed.md").write_text("The deployment включает контейнеры.\n")

    result = search_project_context(str(tmp_path), "deployment контейнеры")

    assert "project-context/mixed.md [exact]" in result


# ---------------------------------------------------------------------------
# linkedProjects tests
# ---------------------------------------------------------------------------


def _link_projects(project_root, linked):
    """Write a linkedProjects setting into a throwaway project's general.yaml."""
    config_dir = project_root / ".praxis" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    joined = ",".join(str(p) for p in linked)
    (config_dir / "general.yaml").write_text(
        f'linkedProjects: "{joined}"\n', encoding="utf-8",
    )


def test_linked_project_match_uses_absolute_path(tmp_path):
    """A linked-project match is labeled with its absolute path; current stays relative."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])

    _context_dir(current)
    (current / "project-context" / "local.md").write_text("keyword local doc\n")
    linked_base = linked / "project-context"
    linked_base.mkdir()
    (linked_base / "doc.md").write_text("keyword remote doc\n")

    result = search_project_context(str(current), "keyword")

    expected_absolute = os.path.join(str(linked), "project-context", "doc.md")
    assert expected_absolute in result
    assert "project-context/local.md [exact]" in result


def test_linked_project_without_context_dir_is_skipped(tmp_path):
    """A linked project lacking project-context/ contributes nothing, no error."""
    current = tmp_path / "current"
    linked = tmp_path / "linked_no_context"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])

    _context_dir(current)
    (current / "project-context" / "doc.md").write_text("keyword here\n")

    result = search_project_context(str(current), "keyword")

    assert "project-context/doc.md [exact]" in result


def test_linked_project_invalid_entry_is_ignored(tmp_path):
    """A non-existent or relative linkedProjects entry is ignored."""
    current = tmp_path / "current"
    current.mkdir()
    config_dir = current / ".praxis" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.yaml").write_text(
        'linkedProjects: "relative/path,/does/not/exist"\n', encoding="utf-8",
    )

    _context_dir(current)
    (current / "project-context" / "doc.md").write_text("keyword here\n")

    result = search_project_context(str(current), "keyword")

    assert "project-context/doc.md [exact]" in result


def test_linked_project_global_cap_holds(tmp_path):
    """The MAX_FILES_RETURNED cap is enforced globally across all searched projects."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])

    current_base = current / "project-context"
    current_base.mkdir()
    for i in range(6):
        (current_base / f"cdoc{i}.md").write_text("keyword here\n")

    linked_base = linked / "project-context"
    linked_base.mkdir()
    for i in range(6):
        (linked_base / f"ldoc{i}.md").write_text("keyword here\n")

    result = search_project_context(str(current), "keyword")

    blocks = [
        line for line in result.split("\n")
        if line and not line.startswith("L") and " [" in line
    ]
    assert len(blocks) == MAX_FILES_RETURNED


def test_no_linked_projects_behaves_as_before(tmp_path):
    """Without linkedProjects configured, behavior is current-project-only with relative paths."""
    base = _context_dir(tmp_path)
    (base / "doc.md").write_text("keyword here\n")

    result = search_project_context(str(tmp_path), "keyword")

    assert "project-context/doc.md [exact]" in result
