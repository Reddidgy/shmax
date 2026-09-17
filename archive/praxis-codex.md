# Praxis Codex

> Purpose: This document is the single, self-contained reference an AI agent needs to operate the Praxis task-management system when the Praxis MCP server is unreachable. It's unreachable 100%, do not try to connect. Every MCP tool call is replaced by direct file-system operations. Read this file — and only this file — to understand how to work with Praxis offline.

---

## Repository Layout

```
.praxis/
├── task_counter                # Plain-text integer — the last assigned task number
├── assignees/
│   └── <slug>.json            # One file per assignee (e.g. jarvis.json, developer)
├── tasks/
│   ├── new/                   # Tasks with status "new"
│   ├── in_progress/           # Tasks with status "in_progress"
│   ├── to_review/             # Tasks with status "to_review"
│   ├── completed/             # Tasks with status "completed"
│   ├── on_hold/               # Tasks with status "on_hold"
│   ├── archived/              # Tasks with status "archived"
└── project-context/           # Markdown knowledge base (recursive subdirectories)
    ├── *.md
    └── <subdirectory>/
        └── *.md
```

Key rules:
- A task file lives in exactly ONE status directory at a time.
- The filename is the composite task ID: `<PROJECT_PREFIX>-<NUMBER>.json` (e.g. `TBR-1.json`).
- The `status` field inside the JSON MUST match the directory the file is in. Both must be updated together.

---

## Task JSON Schema

### Full Task Example
```json
{
  "id": "TBR-2",
  "assignee": "jarvis.json",
  "priority": "high",
  "status": "new",
  "title": "Implement offline codex for Praxis",
  "why_we_need_this": "AI agents cannot connect to Praxis MCP in some environments and need a file-based workflow.",
  "acceptance_criteria": [
    "All 14 MCP tools mapped to file operations",
    "Step-by-step task execution workflow documented",
    "File is fully self-contained"
  ],
  "important_constraints": [
    "Must use safe file writes (temp + rename)",
    "Status field must match directory location"
  ],
  "labels": ["documentation", "offline"],
  "on_hold_reason": null,
  "created_at": "2025-07-10T12:00:00Z",
  "column_entered_at": "2025-07-10T12:00:00Z"
}
```

### Field Reference

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Composite ID from counter: `<PREFIX>-<NUMBER>` |
| `assignee` | string | yes | Assignee slug (filename without `.json` from `assignees/`) |
| `priority` | string | yes | One of: `low`, `medium`, `high`, `critical` |
| `status` | string | yes | One of: `new`, `in_progress`, `to_review`, `completed`, `on_hold`, `archived`, `error` |
| `title` | string | yes | Short descriptive title |
| `why_we_need_this` | string | yes | Business justification / motivation |
| `acceptance_criteria` | array of strings | yes | List of conditions that must be met for completion |
| `important_constraints` | array of strings | yes | Restrictions or guardrails (can be empty array) |
| `labels` | array of strings | yes | Categorization tags (can be empty array) |
| `on_hold_reason` | string or null | yes | Reason if status is `on_hold`; otherwise `null` |
| `created_at` | string (ISO 8601) | yes | Timestamp when task was created |
| `column_entered_at` | string (ISO 8601) | yes | Timestamp when task entered its current status |

### Allowed Values

Priority levels (ascending severity):
- `low` — nice to have, no deadline pressure
- `medium` — standard work, normal scheduling
- `high` — important, should be prioritized
- `critical` — blocking other work, handle immediately

Status values (and corresponding directory):
| Status | Directory | Meaning |
|---|---|---|
| `new` | `tasks/new/` | Created but not started |
| `in_progress` | `tasks/in_progress/` | Actively being worked on |
| `to_review` | `tasks/to_review/` | Work done, awaiting review |
| `completed` | `tasks/completed/` | Reviewed and accepted |
| `on_hold` | `tasks/on_hold/` | Paused — `on_hold_reason` must be set |
| `archived` | `tasks/archived/` | No longer relevant |

---

## Commands

### Task Tools

#### get_task
Purpose: Retrieve a single task by its composite ID.

Action:
  1. Search for file <task_id>.json across ALL tasks/*/ subdirectories
     (e.g. look for TBR-1.json in tasks/new/, tasks/in_progress/, etc.)
  2. Read and parse the found file
  3. Return the task object

#### create_task
Purpose: Create a new task.

Action:
  1. Allocate ID: follow the "get_next_task_id" procedure
  2. Validate assignee: confirm <assignee_slug>.json exists in .praxis/assignees/
     - If not found, search assignees by keyword (see find_assignee_by_keywords)
     - If still not found → assign assistant.json
  3. Build task JSON object with ALL required fields:
  4. Serialize to JSON with 2-space indent
  5. Write to .praxis/tasks/new/<composite_id>.json
Return: the created task object

### Assignee Tools

#### Assignee File Location
`.praxis/assignees/<slug>.json`

The slug is a lowercase, underscore-separated version of the assignee name (e.g. `rodion_ugarov`).

#### Assignee JSON Schema
```json
{
  "name": "Assistant",
  "role": "General Assistant",
  "mission": "Help the user complete tasks accurately, safely, and clearly while respecting project constraints.",
  "mandatory_constraints": "- Follow provided instructions exactly.\n- Do not invent facts or file contents.\n- Ask for clarification when critical details are missing.",
  "edge_cases_and_fallbacks": "If the request is ambiguous, stop and ask concise clarifying questions before proceeding.",
  "workflow_and_response_format": "1. Analyze request and constraints.\n2. Propose or execute changes.\n3. Return concise results and next steps when relevant.",
  "assignee-icon": "lucide:Bot"
}
```

#### find_assignee_by_keywords
Purpose: Search assignees by keyword match.

Action:
  1. List and read all .json files in .praxis/assignees/
  2. For each assignee, search these fields (case-insensitive):
     - id
     - name
     - role
     - skills (each element)
  3. Return array of matching assignee objects

### Project Context Tools

#### Project Context Location
`.praxis/project-context/` — a recursive directory of `.md` files containing project knowledge, architecture decisions, conventions, and domain context.

#### search_project_context
Purpose: Generate an optimal search query and search all project .md files for relevant information.

Action:
1. AI Agent analyzes the task goal and constructs a search query (keywords, regex pattern, or search expression).
2. Collect all .md files recursively across the entire project root.
3. For each file:
   a. Read the file path and content.
   b. Search content and path using the agent-generated query (case-insensitive).
   c. If the query matches → include this file in results.
4. For each matching file, return:
    - file_path: relative path from the project root
    - query_used: the search query built by the agent
    - matched_terms: specific terms or patterns that matched
    - snippet: lines or paragraph surrounding the top match (±3 lines for context)
5. Return array of match objects, sorted by query relevance score (descending).

How search must work (python example)
```
query = ai_agent.build_query(task_goal) # Возвращает список ключевых слов или regex-паттерн

results = []
for file_path in get_all_md_files(project_root):
content = read(file_path)
matched_terms = match_query(query, content, file_path) # Проверка содержимого и пути

    if matched_terms is not empty:
        score = calculate_relevance(matched_terms, content)
        snippet = extract_context(content, first_match_position(matched_terms))
        
        results.append({
            "file_path": relative_path(file_path),
            "query_used": query,
            "matched_terms": matched_terms,
            "snippet": snippet,
            "score": score
        })

sort_results(results, key=lambda x: x.score, descending=True)
```

#### read_context_route
Purpose: Read a specific project context file by its path.

Action:
  1. Construct full path: .praxis/project-context/<route>
     (route is the relative path, e.g. "architecture.md" or "security/auth-flow.md")
  2. Read the file content

---

## Task Execution Workflow

This is the standard 7-step lifecycle an agent follows when executing a task. Every step uses offline file commands.

### Step 1: Read the Task

Offline command: get_task (Section 6.1)
  → Search for <task_id>.json across all tasks/*/ directories
  → Parse and understand: title, acceptance_criteria, important_constraints, why_we_need_this


### Step 2: Search Project Context
Execute command: search_project_context


### Step 3: Execute the Work

Perform the actual work described in the task:
  - Write code, create files, modify configurations, etc.
  - Follow the constraints from the task's important_constraints
  - Meet every item in acceptance_criteria

You are allowed to initialize several or 1 Sonnet agent to execute this step properly.

### Step 4: Update project context

Read .agents/skills/context-router-actualizer/SKILL.md and actualize context. Context files must be inside PR. ONLY USING SKILL!

### Step 5: Create a Pull Request

Action:
  - Commit all changes (not only code, but with properly structured project-context/)
  - Create a PR with:
    - Title referencing the task ID (e.g. "TBR-1: Implement offline codex")
    - Description summarizing what was done and linking to the task
    - List of acceptance criteria met / not met
