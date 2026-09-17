# What is PraxisOS

## TL;DR
- IMPORTANT: This praxis route is visible for every single user and project, not only for praxis owners. This route is explaining what is PraxisOS
- PraxisOS is a local-first OS that puts structure around AI agents: specs written and approved before work begins, a mandatory task lifecycle, and human approval gates. It provides multi-view task management (Board, Table, Templates, Archive, Boosts), built-in project editor, AI Chat with native Claude Code terminal, assignee system with specialist roles, and a searchable project knowledge base — all local, all Git-native.
- AI agents interact with PraxisOS through MCP tools on port 7865  -  no accounts and no cloud storage of project data.
- The mandatory task lifecycle (7 steps) ensures agents receive full context before doing any work.
- Project knowledge lives in project-context/ and is queried via the search_project_context MCP tool.
- Assignees are JSON profiles in .praxis/assignees/ that define how an AI agent should behave on a task.
- The praxis-mcp-codex.md file is the behavioral rulebook every AI agent must read before starting work.
- context-router.md is a bootstrap seed for the initial project-context skeleton, not a live navigation compass.

This route exists so that any AI agent dropped into a PraxisOS-managed project can immediately understand what PraxisOS is, how it works, and how to interact with it. This is the self-awareness route.

## Purpose

When an AI agent starts working in a project that uses PraxisOS, it has no inherent knowledge of what PraxisOS is or how to interact with it. This route closes that gap by explaining:
- What PraxisOS is and why it exists.
- How the .praxis/ directory structures the project.
- What MCP tools are available and what they do.
- What the mandatory task lifecycle is and why it matters.
- How project context, assignees, and tasks work together.

Without this knowledge, an AI agent will treat .praxis/ as arbitrary config files and miss the structured collaboration protocol PraxisOS provides.

## What PraxisOS Is

PraxisOS is a local-first operating system for structured human-AI collaboration. It runs as a browser-based SPA (React) and manages tasks, AI agent roles, project knowledge, and the full development workflow on the user's local machine via the File System Access API. The workspace includes five views (Board, Table, Templates, Archive, Boosts), a built-in Obsidian-like project editor, AI Chat with native Claude Code terminal, an assignee system with specialist AI roles, global search, real-time file sync, and a searchable project knowledge base — all governed by Specification Driven Development and the mandatory 7-step task lifecycle.

PraxisOS solves four problems:
- Context amnesia: AI agents forget project rules every session. PraxisOS stores and delivers them automatically via MCP.
- No structured AI access: Without PraxisOS, developers manually copy-paste context between their project and AI tools. MCP tools eliminate this middleware role.
- AI-unfit task managers: Traditional tools (Jira, Trello) produce tickets for humans, not structured specs AI agents can consume. PraxisOS generates task specs with inlined assignee profiles, project context references, and behavioral constraints.
- Unsafe full autonomy: PraxisOS enforces human oversight at every consequential decision point. Agents execute within human-defined boundaries, never autonomously.

## How PraxisOS Works in a Project

When PraxisOS is initialized in a project, it creates the `.praxis/` directory:

```
.praxis/
  tasks/              # Task JSON files organized by status column
    new/
    in_progress/
    completed/
    on_hold/
    archived/
  assignees/          # AI agent role profiles (JSON files)
    assistant.json    # Default assignee, always present
  prompts/
    praxis-mcp-codex.md   # Behavioral rulebook for AI agents
  api/                # Local MCP server files (Python)
  chat/               # AI chat session data (optional)
```

Alongside `.praxis/`, PraxisOS creates:
- `context-router.md` at the project root: a bootstrap seed used to construct the initial project-context skeleton and to list top-level routes.
- `project-context/`: the knowledge base containing behavioral rules, architectural constraints, and domain documentation.
- `.mcp.json` at the project root (when MCP mode is enabled): tells Claude Code and MCP-compatible tools how to connect to the Praxis MCP server.

All data is local. No cloud service, no account, no sync layer. Everything is Git-trackable.

Project data is never uploaded to any server. The PraxisOS application itself is delivered over the network and requires connectivity to load.

## MCP Tools  -  How AI Agents Interact

The Praxis MCP server runs on `127.0.0.1:7865` and exposes structured tools via Streamable HTTP at `/stream`.
AI agents connect automatically when `.mcp.json` is present in the project root.

Core tools available to agents:
- `get_task`  -  fetch a task's full specification with inlined assignee profile.
- `get_active_tasks`  -  list tasks in new and in_progress columns.
- `list_tasks`  -  query tasks across all status columns.
- `find_task_by_keywords`  -  search tasks by keyword.
- `create_task`  -  create a new task on the kanban board.
- `update_task`  -  update task content fields.
- `update_task_status`  -  move a task between status columns (new, in_progress, to_review, completed, on_hold, archived).
- `get_assignees`  -  list all available AI agent profiles.
- `find_assignee_by_keywords`  -  search assignees by domain.
- `create_assignee`  -  create a new AI agent profile.
- `search_project_context`  -  keyword search over the project knowledge base (project-context/).
- `update_project_context`  -  get instructions for updating project documentation.
- `get_settings` / `update_settings`  -  read and update project configuration.
- `health`  -  check if the MCP server and Claude CLI are available.
- `AskUserQuestion`  -  ask the user a clarifying question mid-task.

All tools are loopback-only, tokenless, and path-addressed. The file system is the source of truth.

## The Mandatory Task Lifecycle

Every AI agent working on a PraxisOS task must follow this lifecycle. It is non-negotiable.

1. `update_task_status` -> `in_progress`  -  claim the task before any work.
2. `get_task`  -  read the full task specification (includes inlined assignee profile).
3. `search_project_context`  -  load domain knowledge from project-context/. Mandatory for every task, not just coding tasks.
4. Prepare execution trajectory  -  research, read files, build a plan. This step stays on the main agent, never delegated.
5. Execute the work  -  optionally delegate to a sub-agent with the plan from step 4.
6. `update_task_status` -> `to_review`  -  hand off for human review.
7. `update_project_context`  -  document what changed for future agents.

This lifecycle prevents context amnesia by ensuring agents always receive full task specs, role constraints, and project knowledge before starting work. Step 7 ensures new knowledge is captured for future sessions.

## Project Context System

The project knowledge base lives in `project-context/`. Each route is a directory with a `README.md` that documents behavioral rules, architectural constraints, and domain knowledge for one area of the project.

The `context-router.md` file at the project root is a bootstrap seed. It is used to construct the initial project-context skeleton and to list top-level routes.
context-router.md is not the live navigation mechanism. Agents navigate the knowledge base with the `search_project_context` MCP tool.

The `search_project_context` MCP tool uses BM25 keyword search to find relevant documentation across all routes. Agents should always search before working.

Routes follow a recursive tree structure with no depth limit. Each README can declare child routes for sub-areas.

## Assignees

Assignees are AI agent role profiles stored as JSON files in `.praxis/assignees/`. They define:
- Name and role (e.g., "Frontend Engineer", "Documentation Architect").
- Mission  -  what the agent is responsible for.
- Mandatory constraints  -  rules the agent must always follow.
- Edge cases and fallbacks  -  how to handle ambiguous situations.
- Workflow and response format  -  the expected output structure.

When `get_task` is called, the assignee profile is resolved and inlined into the task spec. The agent receives its role, constraints, and workflow instructions automatically.

Every project has a default assignee (`assistant.json`) that serves as the fallback when no specific assignee is assigned to a task.

## The MCP Codex

The behavioral rulebook source is `prompts/praxis-mcp-codex.md` (`.praxis/prompts/praxis-mcp-codex.md` is a runtime copy). It defines:
- The mandatory task lifecycle (above).
- Tool usage reference and parameter details.
- Workflow patterns (finishing tasks, creating tasks, locating docs).
- Global rules for all AI agents.

AI agents must read this file before starting any work. It is delivered via a read instruction in the system prompt or task prompt.

## Core Principles

- Local-first: all project data stays on the user's machine and is never uploaded to any server.
- The application itself is delivered over the network; local-first describes data ownership, not offline operation.
- Git-native: all tasks, assignees, and context are plain files  -  committable, diffable, branchable.
- Human control: every consequential decision requires human approval. Agents propose; humans decide.
- Specification-driven development (SDD): write the spec before writing the code. AI elaborates; human approves; AI executes.
- MCP-first: MCP tools are the primary integration layer. Clipboard prompts are a degraded-mode fallback.

## Invariants

- The `.praxis/` directory is the sole persistence layer for task and assignee data.
- The MCP server is always loopback-only (127.0.0.1:7865). No remote access.
- The file system is the source of truth. MCP is a convenience layer over files.
- No AI agent should act without reading its task spec and project context first.
- The task lifecycle is non-negotiable  -  agents must follow all 7 steps.

## Route-Specific Constraints

- This route is informational only. It explains PraxisOS to AI agents that are new to a PraxisOS-managed project.
- This route is intended to be copied into every user project during initialization.
- Content must remain tool-agnostic (not Claude-specific)  -  any MCP-compatible AI agent should understand it.
- Do not duplicate implementation details from other routes. Reference them instead.
- Keep language accessible to any AI model  -  no jargon without explanation.
