#!/usr/bin/env python3
"""PRD Catalog Extractor — extracts codebase artifacts into a concise JSON catalog.

Usage:
    python3 scripts/prd_catalog_extractor.py [--output PATH] [--src PATH]

Outputs:
    references/codebase-catalog.json        Concise structured catalog
    references/codebase-catalog-summary.md  Human-readable summary
    references/prd-drift-report.md          PRD drift detection report
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_project_root() -> Path:
    current = Path.cwd()
    for d in [current, *current.parents]:
        if (d / ".praxis").is_dir() or (d / "src").is_dir():
            return d
    return current


def git_commit_hash(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def glob_files(base: Path, pattern: str) -> list[Path]:
    return sorted(base.glob(pattern))


def read_head(path: Path, max_lines: int = 40) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:max_lines]
        return "\n".join(lines)
    except Exception:
        return None


def read_full(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


SENSITIVE_RE = re.compile(r"api[_-]?key|secret|password|token|credential|private[_-]?key", re.I)


def extract_description(head: str | None) -> str:
    if not head:
        return ""
    m = re.search(r"/\*\*\s*([\s\S]*?)\*/", head)
    if m:
        lines = [
            re.sub(r"^\s*\*\s?", "", l).strip()
            for l in m.group(1).splitlines()
            if not re.match(r"^\s*\*?\s*@", l)
        ]
        desc = " ".join(l for l in lines if l).strip()
    else:
        cm = re.match(r"^//\s*(.+)", head, re.M)
        desc = cm.group(1).strip() if cm else ""
    if SENSITIVE_RE.search(desc):
        return ""
    return desc[:200]


def extract_exports(head: str | None) -> list[str]:
    if not head:
        return []
    return re.findall(
        r"export\s+(?:default\s+)?(?:function|const|class|type|interface|enum)\s+(\w+)",
        head,
    )


def rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_modals(src: Path, root: Path) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()

    orchestrator = src / "components" / "MainApp" / "MainAppModals.tsx"
    content = read_full(orchestrator) or ""

    # Lazy-loaded modals
    for m in re.finditer(
        r"const\s+(\w+)\s*=\s*lazy\(\(\)\s*=>", content
    ):
        name = m.group(1)
        seen.add(name)
        items.append({"name": name, "type": "modal" if "Modal" in name else "dialog"})

    # Directly imported modals/dialogs
    for m in re.finditer(
        r"import\s*\{\s*(\w+)\s*\}\s*from\s*['\"]\.\/([^'\"]+)['\"]", content
    ):
        name = m.group(1)
        if name not in seen and ("Modal" in name or "Dialog" in name):
            seen.add(name)
            items.append({"name": name, "type": "dialog" if "Dialog" in name else "modal"})

    # Glob for extras not in orchestrator
    for pattern in ("components/**/*Modal*.tsx", "components/**/*Dialog*.tsx"):
        for p in glob_files(src, pattern):
            fname = p.stem
            if fname == "MainAppModals" or ".test" in fname or fname in seen:
                continue
            seen.add(fname)
            items.append({
                "name": fname,
                "file": rel(p, root),
                "type": "dialog" if "Dialog" in fname else "modal",
            })

    # Resolve file paths for items without one
    for item in items:
        if "file" not in item:
            candidates = list(src.rglob(f"{item['name']}.tsx"))
            if candidates:
                item["file"] = rel(candidates[0], root)

    return items


def extract_hooks(src: Path, root: Path) -> list[dict]:
    items: list[dict] = []
    for p in glob_files(src, "hooks/**/use-*.ts"):
        if ".test." in p.name:
            continue
        name = p.stem
        items.append({
            "name": name,
            "file": rel(p, root),
            "domain": _hook_domain(name, p, src),
        })
    return items


def _hook_domain(name: str, path: Path, src: Path) -> str:
    r = str(path.relative_to(src))
    if "project-files/" in r:
        return "project-files"
    for kw, dom in [
        ("assignee", "assignees"), ("task", "tasks"), ("feedback", "feedback"),
        ("theme", "ui"), ("ui-state", "ui"), ("search", "ui"), ("notification", "ui"),
        ("device", "ui"), ("settings", "settings"), ("project", "settings"),
        ("ai", "ai-workflow"), ("elaborat", "ai-workflow"), ("create-tasks", "ai-workflow"),
        ("import", "ai-workflow"),
    ]:
        if kw in name:
            return dom
    return "general"


def extract_services(src: Path, root: Path) -> list[dict]:
    all_files = glob_files(src, "services/*.ts")
    test_bases = {p.name.replace(".test.ts", ".ts") for p in all_files if ".test." in p.name}
    items: list[dict] = []
    for p in all_files:
        if ".test." in p.name:
            continue
        items.append({
            "name": p.stem,
            "file": rel(p, root),
            "tested": p.name in test_bases,
        })
    return items


def extract_components(src: Path, root: Path) -> list[dict]:
    items: list[dict] = []
    for p in glob_files(src, "components/**/*.tsx"):
        if ".test." in p.name:
            continue
        r = rel(p, root)
        domain = "other"
        for d in ("MainApp", "TaskDetail", "ProjectFiles", "WelcomeScreen", "ErrorBoundary", "Notification"):
            if d in r:
                domain = d
                break
        items.append({"name": p.stem, "file": r, "domain": domain})
    return items


def extract_shortcuts(src: Path, root: Path) -> list[dict]:
    items: list[dict] = []
    targets = [
        (src / "components" / "MainApp" / "main-app-keyboard-utils.ts", "global"),
        (src / "components" / "ProjectFiles" / "useProjectFilesKeyboard.ts", "project-files"),
    ]
    for fpath, scope in targets:
        content = read_full(fpath)
        if not content:
            continue
        for m in re.finditer(
            r"matchesShortcutKey\(\w+,\s*\{\s*key:\s*'([^']+)',\s*code:\s*'([^']+)'\s*\}\)",
            content,
        ):
            key = m.group(1)
            # Check for modifier prefix
            before = content[max(0, m.start() - 80):m.start()]
            if "metaKey" in before or "ctrlKey" in before:
                key = f"Cmd/Ctrl+{key}"
            items.append({"key": key, "scope": scope, "file": rel(fpath, root)})

        if "event.key === 'Escape'" in content or "e.key === 'Escape'" in content:
            items.append({"key": "Escape", "scope": scope, "file": rel(fpath, root)})

    return items


# ---------------------------------------------------------------------------
# PRD section mapping
# ---------------------------------------------------------------------------

PRD_MAP_RULES: list[tuple[str, str]] = [
    ("modal", "Behaviours"), ("dialog", "Behaviours"),
]

def map_prd_section(item: dict) -> str:
    t = item.get("type", "")
    if t in ("modal", "dialog"):
        return "Behaviours"
    f = item.get("file", "")
    name = item.get("name", "")
    domain = item.get("domain", "")
    if "TaskDetail/" in f or domain == "tasks":
        return "Task Creation and Editing"
    if "ProjectFiles/" in f or domain == "project-files":
        return "Project Files"
    if "WelcomeScreen/" in f:
        return "Project Management"
    if "keyboard" in name or "key" in item:
        return "Interactions / Accessibility"
    if "assignee" in name.lower() or domain == "assignees":
        return "Assignees"
    if domain == "ai-workflow":
        return "AI Workflows"
    if "feedback" in name.lower() or domain == "feedback":
        return "Feedback"
    if "settings" in name.lower() or domain == "settings":
        return "Settings"
    if domain == "ui":
        return "UI"
    if t == "service" or "ErrorBoundary/" in f:
        return "Technical Details"
    if "MainApp/" in f:
        return "Behaviours"
    return "Uncategorized"


# ---------------------------------------------------------------------------
# Enrichment (lightweight — only description + exports, keeps catalog small)
# ---------------------------------------------------------------------------

def enrich(item: dict, root: Path) -> dict:
    fpath = item.get("file")
    if not fpath:
        return item
    head = read_head(root / fpath)
    desc = extract_description(head)
    if desc:
        item["desc"] = desc
    exports = extract_exports(head)
    if exports:
        item["exports"] = exports
    return item


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def detect_drift(catalog: dict, root: Path) -> dict | None:
    prd = read_full(root / "PRD.md")
    if not prd:
        print("    PRD.md not found, skipping drift detection")
        return None

    prd_lower = prd.lower()
    implemented_not_documented: list[dict] = []

    # Check modals and workflow hooks
    feature_items = catalog["modals"] + [
        h for h in catalog["hooks"] if "workflow" in h.get("name", "")
    ]
    for item in feature_items:
        terms = re.findall(r"[a-z]{4,}", item["name"].replace("-", " ").lower())
        terms = [t for t in terms if t not in ("modal", "dialog", "workflow", "hook", "use", "from", "state")]
        if terms and not any(t in prd_lower for t in terms):
            implemented_not_documented.append({
                "name": item["name"],
                "file": item.get("file", ""),
                "section": item.get("prd", ""),
            })

    # Find [REQUIRES PRODUCT FILLING] markers
    outdated: list[dict] = []
    for m in re.finditer(r"\[REQUIRES PRODUCT FILLING[^\]]*\]", prd):
        line = prd[:m.start()].count("\n") + 1
        # Find nearest heading
        section = "Unknown"
        for hm in re.finditer(r"^#+\s+(.+)", prd[:m.start()], re.M):
            section = hm.group(1).strip()
        outdated.append({"marker": m.group(), "section": section, "line": line})

    print(f"    {len(implemented_not_documented)} implemented but not documented")
    print(f"    {len(outdated)} [REQUIRES PRODUCT FILLING] markers")

    return {
        "implemented_not_documented": implemented_not_documented,
        "potentially_outdated": outdated,
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_summary(catalog: dict, path: Path) -> None:
    lines = [
        "# Codebase Catalog Summary",
        "",
        f"Generated: {catalog['generated_at']}",
        f"Git commit: {catalog.get('git_hash') or 'unknown'}",
        "",
        "## Counts",
        "",
    ]
    for k, v in catalog["counts"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    # Group by prd section
    by_section: dict[str, list[dict]] = {}
    for section_key in ("modals", "hooks", "services"):
        for item in catalog[section_key]:
            sec = item.get("prd", "Uncategorized")
            by_section.setdefault(sec, []).append(item)
    # Components are grouped by domain
    for group in catalog.get("components", []):
        sec = group.get("prd", "Uncategorized")
        for name in group.get("items", []):
            by_section.setdefault(sec, []).append({"name": name, "file": f"src/components/{group['domain']}/"})
    for s in catalog.get("shortcuts", []):
        by_section.setdefault("Interactions / Accessibility", []).append(
            {"name": f"Key: {s['key']}", "file": s.get("file", "")}
        )

    lines.append("## By PRD Section")
    lines.append("")
    for sec in sorted(by_section):
        lines.append(f"### {sec}")
        lines.append("")
        for item in by_section[sec]:
            desc = f" — {item['desc']}" if item.get("desc") else ""
            lines.append(f"- {item['name']} (`{item.get('file', '')}`){desc}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_drift_report(drift: dict, path: Path) -> None:
    lines = [
        "# PRD Drift Report",
        "",
        "## Implemented but Not Documented",
        "",
    ]
    if not drift["implemented_not_documented"]:
        lines.append("None detected.")
    else:
        for item in drift["implemented_not_documented"]:
            lines.append(f"- {item['name']} (`{item['file']}`) — suggested section: {item['section']}")
    lines.append("")

    lines.append("## Potentially Outdated (Requires Product Filling)")
    lines.append("")
    if not drift["potentially_outdated"]:
        lines.append("No placeholder markers found.")
    else:
        for item in drift["potentially_outdated"]:
            lines.append(f"- Line {item['line']}: `{item['marker']}` (Section: {item['section']})")
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_gitignore(root: Path) -> None:
    gi = root / ".gitignore"
    if gi.exists() and "codebase-catalog.json" not in gi.read_text():
        print("\n  Warning: references/codebase-catalog.json contains internal architecture details.")
        print("  Consider adding it to .gitignore.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="PRD Catalog Extractor")
    parser.add_argument("--output", default="references/codebase-catalog.json")
    parser.add_argument("--src", default="src")
    args = parser.parse_args()

    root = find_project_root()
    src = root / args.src
    output = root / args.output
    summary_path = output.with_suffix("").with_name(output.stem + "-summary").with_suffix(".md")
    drift_path = output.parent / "prd-drift-report.md"

    print("PRD Catalog Extractor")
    print(f"  Root: {root}")
    print(f"  Source: {src}")
    print(f"  Output: {output}")
    print()

    # Phase 1: Extract
    print("Extracting...")
    modals = extract_modals(src, root)
    hooks = extract_hooks(src, root)
    services = extract_services(src, root)
    components = extract_components(src, root)
    shortcuts = extract_shortcuts(src, root)

    print(f"  {len(modals)} modals/dialogs, {len(hooks)} hooks, {len(services)} services, {len(components)} components, {len(shortcuts)} shortcuts")

    # Phase 2: Enrich + map PRD section
    print("Enriching...")
    # Only enrich modals, hooks, and services (components are self-descriptive by name)
    for item in modals + hooks + services:
        enrich(item, root)
        item["prd"] = map_prd_section(item)
    for item in components:
        item["prd"] = map_prd_section(item)

    # Compact components: group by domain instead of listing individually
    comp_by_domain: dict[str, list[str]] = {}
    for c in components:
        comp_by_domain.setdefault(c["domain"], []).append(c["name"])
    compact_components = [
        {"domain": d, "prd": map_prd_section({"file": f"src/components/{d}/x", "name": "", "domain": d}), "items": names}
        for d, names in sorted(comp_by_domain.items())
    ]

    # Build concise catalog
    from datetime import datetime, timezone
    catalog: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_hash": git_commit_hash(root),
        "source": args.src,
        "counts": {
            "modals": len(modals),
            "hooks": len(hooks),
            "services": len(services),
            "components": len(components),
            "shortcuts": len(shortcuts),
        },
        "modals": modals,
        "hooks": hooks,
        "services": services,
        "components": compact_components,
        "shortcuts": shortcuts,
    }

    # Write catalog
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  Catalog: {output}")

    # Phase 3: Summary + drift
    print("Summary & drift detection...")
    write_summary(catalog, summary_path)
    print(f"  Summary: {summary_path}")

    drift = detect_drift(catalog, root)
    if drift:
        write_drift_report(drift, drift_path)
        print(f"  Drift report: {drift_path}")

    check_gitignore(root)

    print("\nDone.")


if __name__ == "__main__":
    main()
