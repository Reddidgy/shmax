# PraxisOS MCP Instructions

<preamble>

MANDATORY: USE MCP TOOLS BEFORE ANY WORK. Always. Even if you know the file path.
YOU MUST ALWAYS START FROM PRAXIS MCP, not other MCP.
Always search project-context/ through the search_project_context tool.
If PraxisOS MCP is unavailable, stop and provide restart instructions before proceeding.

Reference for AI agents using Praxis MCP tools to interact with PraxisOS kanban boards and project files.

</preamble>

<global_rules>

## Global Rules

- Every tool accepts `project_root`  -  an absolute path to a directory containing `.praxis/`. When omitted, falls back to `os.getcwd()` with `.praxis/` presence validation. Invalid path returns `{ error: "Invalid project_root: ..." }`.
- All tools are loopback-only (`127.0.0.1:7865`), tokenless, path-addressed. The file system is the source of truth; MCP is a convenience layer.
- Create tasks or assignees only on explicit user request.
- Always use task IDs returned by the server.
- Always change task status through `update_task_status`  -  it is the only supported method. If MCP is unavailable, leave the task in its current status and tell the user.
- Use execution mode only (skip plan mode unless the user explicitly asks for it).
- Delegating to sub-agent means that we must initialize sub-agent instead of making work directly
- NEVER call the `Agent` tool before Step 5 of the task lifecycle. Steps 1–4 are main-agent-only. See `<step_4_gate>` and `<delegation_constraint>` below.
- You always must use praxis cycle for the work
- Be clear, short, concise, without walls of words, only useful information without duplication
- During and after any task, all explanations, summaries, and text output must be clear, concise, and contain only useful information - no filler, no repetition, no walls of text. Every sentence must earn its place.
- Always ask questions if something unclear for you. Use the `AskUserQuestion` tool to ask  -  never ask inline in plain text output.
- It's possible multiple agents are working on different tasks, do not revert any changes from not your work
- When referencing file paths in tasks (title, description, acceptance criteria), always use the `@`-prefixed relative format: `@artifacts/ai-chat-poc/README.md`, not full absolute paths. This renders as a clickable link in PraxisOS UI.
- When the user tells you a directory is a Praxis project (e.g. "folder /path/to/dir is a Praxis project"), trust that and apply this entire codex: use MCP tools with that directory as `project_root`, follow the task lifecycle, and obey every rule defined here  -  regardless of the project's language, framework, or domain. Do not verify the directory structure yourself.

AD-HOC REQUESTS (no kanban task)
When the user gives a direct request outside the kanban (investigation, validation, question, quick fix), the 7-step lifecycle does not apply  -  but `search_project_context` is STILL mandatory before any file reading, code exploration, or agent spawning  -  unless the user asks otherwise.

MANDATORY TASK LIFECYCLE  -  NON-NEGOTIABLE
1. (main agent) `update_task_status` → `in_progress`  -  FIRST. Before reading spec. Before any work. Always. No exceptions.
2. (main agent) `get_task`  -  read the full specification. Then print a short concise summary: "How I understand task:"  -  the core intent, scope, and expected outcome in your own words (not a spec copy-paste). If anything is unclear or ambiguous, ask before proceeding.
3. (main agent) `search_project_context`  -  domain keywords, mandatory for every task before doing the work (not limited to coding tasks). Skip only if the user explicitly says to. After results arrive, print a one-line summary (≤300 chars) starting with "What I've found in the context:"  -  state what was actually found (or not found). This is a user checkpoint: wrong or missing context caught here saves tokens and prevents hallucination-driven work downstream.
4. (MAIN AGENT ONLY) Prepare execution trajectory.
   <step_4_gate>
   DO NOT call the `Agent` tool, `Workflow` tool, or spawn any subagent during this step.
   You are the same agent that ran Steps 1–3. You already have the task spec, project context, and domain knowledge loaded. Do this work yourself, in this context window.
   WHY THIS RULE EXISTS: a subagent does not inherit your context. Delegating Step 4 forces it to re-read the task, re-search context, and re-discover what you already know  -  doubling token cost, fragmenting the context window, and producing a worse plan because the subagent lacks the reasoning from Steps 1–3.
   If you are thinking "let me delegate the research" or "a subagent can read the files and prepare the plan"  -  that thought is the violation. Stop and do it yourself.
   </step_4_gate>
   This is the ONLY step where research happens. All file reading, code analysis, architecture exploration, and dependency tracing belong here  -  no other step performs research.
   Do all research, reading, analysis, and decision-making the task requires. Read relevant files (source code, docs, configs, prompts  -  whatever applies). Build a complete, self-contained execution plan for the subagent:
    - What exactly to do (concrete actions, not goals)
    - Which files to touch and how (paths, line ranges, snippets for code; outline, structure, tone for writing)
    - Constraints and patterns from project context to follow
    - Acceptance criteria mapped to specific deliverables
   The subagent must not need to search, explore, or decide on approach  -  all thinking is done here.
   The FIRST allowed `Agent` tool call in the lifecycle is Step 5. Any `Agent` call before Step 5 is a rule violation.
5. (delegate to Sonnet subagent) Execute the trajectory  -  hand the subagent the plan from Step 4. The subagent does the work as specified. It does NOT call `search_project_context`, does NOT explore beyond what the plan provides, does NOT make strategic or architectural decisions. If the subagent hits an ambiguity not covered by the plan, it stops and returns control to the main agent.
6. (main agent) `update_task_status` → `to_review`  -  after work is complete.
7. (main agent) `update_project_context`  -  ALWAYS. After every completed task, no exceptions. The response contains an inline STOP gate  -  4 scope-of-change questions. Evaluate them from the response. If the answer is #4 (trivial change with no structural implications and no findings worth preserving), STOP  -  no actualization needed, do not read SKILL.md. Otherwise, read the skill at the returned `skill` path and follow it from Step 2 onward. If the skill requires writing or updating route READMEs, delegate that writing to a Sonnet subagent. Route READMEs explain logic, reasoning, decisions, and behavioral rules  -  reference project information rather than repeating it. If a route README exceeds ~1000 lines, split it into child routes.
   Context gap rule: If Step 3 returned no relevant results, OR the results do not cover what this task introduced or changed, you MUST fill the documentation gap in Step 7  -  the STOP gate does not override this. New structures, workflows, processes, interfaces, or behavioral patterns absent from project-context are gaps regardless of whether adjacent topics were found.
   `to_review` is a pure handoff signal. After step 6, treat the status change as final  -  the call itself is the signal. Proceed to step 7 immediately.
   Execute every step in order. Step 7 is the last interaction with task tools.
   If MCP is unavailable for step 6, leave the task in `in_progress` and tell the user.
   Re-opening from review: If the user resumes work on a task that is currently `to_review`, call `update_task_status` → `in_progress` BEFORE doing anything else, then continue from step 2. Re-read the spec (requirements may have changed) and re-run `search_project_context` if scope differs.

<delegation_constraint>
HARD RULE  -  WHEN `Agent` TOOL CALLS ARE ALLOWED
The `Agent` tool (subagent spawning) is allowed ONLY at Step 5 and Step 7 (docs writing delegation). Steps 1–4 and Step 6 are ALWAYS executed by the main agent. No exceptions. No "quick helper agents". No "research agents". No "exploration agents".

Violation pattern (DO NOT DO THIS):
  Step 3 complete → "I'll spawn an agent to read the codebase and prepare a plan" → Agent(prompt="Research the codebase and build an execution plan...") ← WRONG. This is Step 4 work. Do it yourself.

Correct pattern:
  Step 3 complete → [you read files, analyze code, build the plan yourself] → Agent(prompt="Execute this specific plan: [concrete file changes, exact code snippets, line numbers]") ← CORRECT. This is Step 5.

Self-check before every `Agent` call: "Am I past Step 4? Is my plan already written?" If no  -  you are about to violate this rule.
</delegation_constraint>

Progress tracking  -  print the full checklist once at Step 1, then report each subsequent step as a single line:
```
- [x] Step 1: update_task_status → in_progress (main agent)
- [ ] Step 2: get_task + "How I understand task:" (main agent)
- [ ] Step 3: search_project_context (main agent)
- [ ] Step 4: prepare execution trajectory (⛔ MAIN AGENT ONLY  -  do NOT call Agent tool here)
- [ ] Step 5: execute trajectory (Sonnet subagent  -  no research, only execution)
- [ ] Step 6: update_task_status → to_review (main agent)
- [ ] Step 7: update_project_context (main agent; delegate docs writing to Sonnet subagent if needed)
```
After Step 1, print only: `> Step N/7: <tool_or_action>`

</global_rules>

<tool_reference>

## Tools at a Glance

Every tool accepts `project_root` (optional, auto-detected from cwd).

- `get_active_tasks`  -  new + in_progress snapshot; returns `id` + `title` only. Quick glance.
- `list_tasks`  -  full metadata (status, priority, labels, assignee). Add `status` to filter one column.
- `get_task`  -  full payload for one `task_id`. Always call before starting work. Task ID format is POS-1920-xiqwsi, where POS is custom label, can be other chars.
- `find_task_by_keywords`  -  AND-matched keywords across title/description/criteria. Find by topic or check duplicates.
- `get_assignees`  -  returns `{ file, name, role }`. Pass `file` (not `name`) to `create_task`. Use when domain is unclear.
- `find_assignee_by_keywords`  -  filtered by name/role/mission. Use when domain is specific. Use `get_assignees` when unsure  -  filtered results can hide valid matches.
- `create_assignee`  -  requires `name`, `role`, `mission`. Optional: `mandatory_constraints`, `edge_cases_and_fallbacks`, `workflow_and_response_format`, `assignee_icon`, `file` (desired filename, e.g. `"my-agent.json"`; auto-generated from `name` when omitted). Returns `{ ok, file, name }`. Project-scoped only (`.praxis/assignees/`).
- `create_task`  -  requires `title`, `why_we_need_this`, `acceptance_criteria`. Server assigns `id`/`status`/timestamps automatically and formats the title with a `[TAG-id]` prefix  -  do not add the prefix yourself; send the plain title only.
- `get_next_task_id`  -  reserves and returns the next safe task ID (increments task_counter atomically). Call this before writing a task file directly. Call exactly once per task  -  each call consumes one ID.
- `update_task`  -  updates content fields only (title, criteria, labels, priority, assignee). Formats the title with `[TAG-id]` prefix automatically  -  do not add the prefix yourself. Use `update_task_status` to move between columns.
- `update_task_status`  -  moves tasks between columns only. Content fields stay unchanged (except `on_hold_reason` on `on_hold`). Requires `task_id`, `status` (`in_progress` | `to_review` | `error` | `completed` | `on_hold` | `archived`). `agent_name` (lowercase, ≤10 chars) is optional and kept only for backward compatibility  -  the board no longer shows the agent name, only a colored AI indicator lamp (green = reported to review, amber = working, red = stopped).
- `search_project_context`  -  BM25-scored keyword search over `project-context/` with field boosting (title > headings > body), prefix matching, and fuzzy tolerance. 3+ keywords require at least half to match. Returns plain text: one block per file, header `<path> [exact]` or `<path> [partial, missing: <terms>]`, match lines as `L<N>: <content>`. See context gap rule and mandatory usage in the lifecycle above.
- `read_context_route`  -  read a specific section of a project-context route README. Parameters: `route` (required, folder name), `mode` (optional: `"toc"` | `"tldr"` | `"section"`, default `"tldr"`), `section_name` (for mode `"section"`; accepts comma-separated names for multiple sections in one call). Use after `search_project_context` to read only what you need instead of the entire file.
- `update_project_context`  -  returns instructions for updating `project-context/` routes. Full policy: see Step 7 above.
- `get_settings` / `update_settings`  -  read and partially update project settings. Always call `get_settings` before `update_settings`. Use only when the user explicitly asks to change project configuration.

</tool_reference>

<workflow_patterns>

## Workflow Patterns

### Finishing a task
1. Complete all acceptance criteria.
2. `update_task_status` → `to_review`. MANDATORY. See handoff rule after Step 7 above.
3. `update_project_context`  -  full policy: see Step 7 above.
4. On explicit user approval only: `update_task_status` → `completed`.

### Creating a task on request
1. `search_project_context` for getting more context of the task
2. `find_assignee_by_keywords` to find the right assignee.
3. `create_task` with title, business justification, behavioral acceptance criteria, and the assignee's `file` value (e.g. `"jarvis.json"`, not `"Jarvis"`). Ensure the title and criteria strictly follow SMART goals.
4. Short Report the returned `id` and `title` and what has to be done.

### Creating an assignee on request
1. `get_assignees` to check for duplicates.
2. `create_assignee`. Always project-scoped (`.praxis/assignees/`). Report the returned `file`.

### Locating relevant docs
1. Extract 2–4 domain keywords from the task title/description.
2. `search_project_context` with those keywords. Prioritize "exact" over "partial" matches.
3. Retry with different terms if results are empty. Try component names, domain terms, architectural concepts.
4. If still empty, read source files directly.
5. Start with `read_context_route(route, mode="tldr")` — the TL;DR is always the first read. Only then, if a specific section is needed for the task, read it with `read_context_route(route, mode="section", section_name="...")`. Do NOT start with `toc` — use `toc` only when you have no idea what the route contains and need to decide whether to read it at all.
6. Threshold rule: if you need more than 4 sections from one route, `Read` the full README instead of calling `read_context_route` per section — fewer MCP round-trips is better than slightly fewer tokens.

</workflow_patterns>
