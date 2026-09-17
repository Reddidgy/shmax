---
name: context-router-initializer
description: Use this skill to initialize project context documentation by adapting the generic context-router.md template in the ROOT and generating route README documentation inside project-context/ for the user's specific project.
---

# CONTEXT ROUTER INITIALIZATION SKILL

You have been triggered to adapt the generic `context-router.md` template located in the project ROOT into a fully functional, project-specific behavioral rulebook.

Follow these steps strictly in order:

## Step 1: Analyze the Project Context
Before modifying the file, you must understand what this project is.
1. Read `README.md`, `package.json` (or equivalent dependency file), and scan the root directory.
2. Identify the core product vision, target audience, and main technology stack.
3. Identify 2 to 4 primary Business Routes (e.g., Users, Products, Tasks, Auth, Cart).

## Step 2: Update Project Info & Rules
Open `context-router.md` in the ROOT and replace the placeholders:
1. "Project Description": Replace `[Insert brief description...]` with a concise summary of the app's purpose and tech stack.
2. "Project Specific Rule": Replace `[PROJECT SPECIFIC RULE]` with 1-2 critical, non-negotiable constraints based on your project analysis (e.g., "All state mutations must use Redux", "Never use local storage for sensitive data"). If unsure, write a generic best-practice rule for the detected stack and ask the user to refine it later.
3. "Project Terminology": Refine the Application vs Agent definitions in the `Project terminology` section to match the project's stack (e.g., "Application" might be a frontend SPA, a backend service, or a CLI tool).

## Step 3: Define Core Business Routes
MANDATORY FIRST ROUTE: The `about` route must always exist in every project. Before adding any project-specific routes:
1. Create `project-context/about/` if it does not exist.
2. Create `project-context/about/README.md` — following the standard route README contract (TL;DR section first) — and populate it with: the project name, a concise description of what it does, the target audience, the core tech stack, and the main value proposition, all derived from your Step 1 analysis.
3. Add this entry first in the `# Core Business Routes (Behavioral Rules)` section of `context-router.md`, before any other routes:
   ```
   ## About
   High-level overview of what this project is, who it is for, and its core tech stack.
   Directory Path: project-context/about/README.md
   ```

MANDATORY SECOND ROUTE: The `PRD` route must always exist in every project. After creating the `about` route:
4. Create `project-context/PRD/` if it does not exist.
5. Create `project-context/PRD/README.md` using the PRD route template at `templates/prd-route-template.md` (relative to this skill directory). Copy the template skeleton exactly, then fill each section with project-specific content derived from your Step 1 analysis following these rules: translate technical details into product language, use `[REQUIRES PRODUCT FILLING: no data in the code]` for missing information, never hallucinate.
6. Add this entry in the `# Project context map (Routes)` section of `context-router.md`, right after the About route:
   ```
   ## PRD (Product Requirements Document)
   The source of truth for the product vision. It explains WHAT we are building, WHO we are working for, and the core problems we solve.
   ALWAYS read this if your task involves planning new features, writing documentation, or understanding the product's core value proposition.
   Directory Path: project-context/PRD/README.md
   ```

Then transform the generic `[Route X Name]` sections into project-specific routes:
7. Replace the placeholders with the core routes you identified in Step 1.
8. Write a brief 1-sentence description of the business logic each route governs.
9. Assign a valid file path for each pointing to its README.md (e.g., `project-context/users/README.md`). DIRECTORY PATH RULE: Every `Directory Path` MUST be relative to the repository root, never relative to the current file. This applies to context-router.md AND to every sub-router README.md that declares child routes.
10. If these directories do not exist in the file system, CREATE them now.
11. Keep route entries in context-router.md lean: name, description, and `Directory Path` only. All business-rule detail — key files, invariants, constraints — goes exclusively into the route's README.md. context-router.md is the compass; it points agents to the README, not duplicates it.
12. ROADMAP ROUTE: If the project tracks planned milestones, features, or release phases, create a `project-context/roadmap/` directory and add it to context-router.md:
   ```
   ## Roadmap
   Planned and completed work items organized by phase and milestone.
   Directory Path: project-context/roadmap/README.md
   ```
   Create `project-context/roadmap/README.md` using the roadmap route template at `templates/roadmap-route-template.md` (relative to this skill directory). The template works for any project type — apps, CLIs, libraries, APIs, and services. Adapt phase names and items to the project's actual deliverables discovered in Step 1.

## Step 4: Populate Route Documentation
For each route directory created in Step 3, generate a `README.md` using the template at `templates/route-readme-template.md` (relative to this skill directory).
1. Copy the template structure for each route.
2. Replace all `[...]` placeholders with project-specific content derived from your Step 1 analysis:
   - TL;DR: write 3-4 bullet points covering the most critical rules, data structures, and key implementation files for this route. Each TL;DR bullet must be a single declarative sentence of ≤ 25 words containing the key domain terms agents would search for. No compound sentences joining unrelated facts. No prose paragraphs. This is what agents read first — make it actionable.
   - Route Name: the actual route name (e.g., "Tasks", "Users", "Auth").
   - Purpose: a concise explanation of what the route owns and why it exists.
   - Core Concepts: list the key entities, states, or workflows the route manages based on your codebase scan.
   - Invariants: write 1-3 real non-negotiable rules you observed or inferred for this route.
   - Route-Specific Constraints: add any data format, API boundary, or validation rules relevant to this route.
3. Keep each README short and factual. Avoid speculative content; only document what you can confirm from the codebase.
4. If a `README.md` already exists in a route directory, do NOT overwrite it.
5. BM25 SEARCHABILITY CONTRACT: `search_project_context` scores individual lines using BM25 with field boosting (headings > body). Every behavioral rule, invariant, and business constraint must occupy its own dedicated line. Multi-rule sentences and prose paragraphs bury key terms and cause recall failures — agents searching for those terms will not find them. The format of the content IS the retrieval contract.
6. PRUNE STALE CONTENT: Remove historical detail, resolved issue narratives, and redundant explanations that dilute term frequency for current rules. Every line should earn its place by being either a current behavioral rule or essential architectural context.
7. ROADMAP ROUTE FORMAT: The Roadmap route uses the template at `templates/roadmap-route-template.md` (relative to this skill directory). Copy the skeleton, then fill each phase with project-specific items from your Step 1 analysis. Use `- [ ]` for planned items and `- [x]` for completed items. Phases are ordered chronologically (past to future). Completed items must not be removed — they serve as a completion log. Phase naming adapts to the project type: semantic versions for releases, milestone names for non-versioned projects, sprint/quarter labels for time-boxed work.

## Step 5: Generate AI Agent Quick Start Protocol and Route Selection Guide
In `context-router.md`, populate the two agent-orientation sections:

1. The `AI Agent Quick Start Protocol` section is already present in the template. Keep it as-is — it tells agents to read this file, identify relevant routes, read those READMEs (TL;DR first), then implement.

2. Populate the `Route Selection Guide` table:
   - For each route, identify 4-8 task keywords or phrases that would indicate the agent needs that route.
   - Group related keywords into rows. When a task touches multiple routes, create a combined row.
   - The goal: an agent should be able to scan this table and know exactly which README files to open, without reading all of them.

## Step 6: Final Cleanup & Validation
1. Remove any remaining `[...]` placeholders from `context-router.md`.
2. Ensure there is absolutely NO "bold formatting" anywhere in `context-router.md`.
3. Verify every `Directory Path` in `context-router.md` AND in sub-router README.md files has a matching directory with a `README.md` on disk, and is relative to the repository root (not relative to the current file).
4. Verify every route README.md starts with a `## TL;DR` section immediately after the `#` heading.
5. Verify every route entry in context-router.md has a `Directory Path:` pointing to its README.md on disk, and contains NO `Key files:` or `Critical rules:` fields — those belong only in the README.md.
6. Verify the Route Selection Guide table covers all routes.
7. Verify all behavioral rules appear as standalone searchable lines (not embedded in multi-sentence prose).
8. Verify each TL;DR bullet is a single declarative sentence of ≤ 25 words containing key domain terms that agents would query.
9. Verify stale or redundant content has been pruned to avoid diluting BM25 term frequency for current rules.
10. Save all files.

## Step 7: Completion
Report back to the user with a brief summary of:
- The routes identified and initialized.
- The route READMEs created (list file paths).
- Ask if they want to add any specific `Mandatory Execution Rules` or refine route documentation.

# Scaling Guidelines

## When to split a route into child routes
- The route's README.md exceeds ~1000 lines because it documents multiple distinct rule sets.
- Two sub-areas have invariants that are independent or contradictory.
- A sub-area has its own lifecycle (e.g., can be created/modified/deleted independently of the parent route's rules).

## When NOT to split
- The route is small and all rules fit in one readable README.md.
- A child route would have only 1-2 invariants with no potential for further growth.
- The split would create routes that always need to be read together anyway.

## The fractal contract
Every sub-router (README.md) at any depth follows the same structure: Purpose, Core Concepts, Invariants, Constraints, and optional Child Routes. This is what makes the system scale infinitely — there is no special format for depth 1 vs depth 5. The root context-router.md never needs to change when a route splits into children; the split happens inside the sub-router.

## Where growth happens
- context-router.md (root) stays concise: only top-level routes. It rarely changes.
- Sub-routers absorb complexity: when a feature grows, its sub-router declares child routes.
- Leaf routes stay simple: no Child Routes section, just business rules.

# Terminology Reference

When writing or updating documentation, use these terms consistently:
- Context Router: The root `context-router.md` file providing a high-level map of the entire project.
- Route: A specific directory representing a business or technical module. Contains a README.md sub-router.
- Sub-router: The `README.md` file inside any route directory. Acts like the root context router but scoped to its route. Can declare child routes, creating infinite depth.
- Hierarchical routing: Router -> Route (Dir) -> Sub-router (README.md) -> Child Route (Dir) -> Child Sub-router (README.md), infinitely recursive.

# Reference Structure

## context-router.md required sections (in order)

```
# context-router.md Purpose
  - Root router / compass definition
  - "Implementation is the map, router is the compass"
  - Trigger rule

# AI Agent Quick Start Protocol
  - 6-step numbered protocol for agents to follow before coding
  - Emphasizes: read this file -> identify routes -> read TL;DR -> read deeper if needed -> find files -> implement

# Route Selection Guide
  - Markdown table mapping task keywords to route names
  - Helps agents skip irrelevant routes without reading them

# context-router.md terminology
  - Context Router  (this root file)
  - Route           (directory = business/technical module node)
  - Sub-router      (README.md inside a route directory)

# Hierarchical routing principle
  - Recursive tree, no depth limit
  - Router -> Route (Dir) -> Sub-router (README.md) -> Child Route -> ...

# Project terminology
  - Application vs Agent/Copilot/LLM definitions

# Mandatory Execution Rules
  - Start from this file
  - Never hallucinate — read sub-routers
  - Traverse recursively
  - [PROJECT SPECIFIC RULE]
  - Load selectively
  - UPDATE TRIGGER (CRITICAL)

# Project context map (Routes)
  ## About (MANDATORY)
     Directory Path: project-context/about/README.md
  ## PRD (MANDATORY)
     Directory Path: project-context/PRD/README.md

# Core Business Routes (Behavioral Rules)
  ## Route A
     Directory Path: project-context/route-a/README.md
  ## Route B
     Directory Path: project-context/route-b/README.md
  ## Roadmap
     Directory Path: project-context/roadmap/README.md
  ## AI Skills and Agents
     Directory Path: .agents/skills/

# Rules for this file (context-router.md)
```

## Route README.md required sections (in order)

```
# [Route Name] Domain

## TL;DR
- [Single declarative sentence ≤ 25 words with key domain terms]
- [One searchable rule per bullet — no compound sentences]
- [Each bullet independently findable by BM25 keyword search]
- [Key files: list relevant source files]

[One-sentence domain description]

## Purpose
## Core Concepts
## Invariants
## Route-Specific Constraints
## Child Routes (optional — only if sub-areas exist)
```

The TL;DR section is mandatory for every route README. It must appear immediately after the `#` heading, before the one-sentence description. Agents read TL;DR first to decide whether to read the full document. Each bullet must be a single declarative sentence of ≤ 25 words — no compound sentences joining unrelated facts, no prose paragraphs. The format is a retrieval contract: `search_project_context` uses BM25 scoring on individual lines, so each bullet must be independently findable by keyword search.

## Roadmap Route Template

Template file: `templates/roadmap-route-template.md` (relative to this skill directory).

The Roadmap route follows the standard README contract but uses checkbox notation for all work items.
Every item in every phase must use `- [ ]` (planned) or `- [x]` (completed) — no plain list items.
Phases are ordered chronologically: completed phases first, active phase next, future phases last.
Completed items must never be removed; they form a completion log agents can reference.
The template is project-type agnostic — works for apps, CLIs, libraries, APIs, and services.
Phase naming adapts to the project: semantic versions for releases, milestone names for non-versioned projects, sprint/quarter labels for time-boxed work.

## File system structure produced by context-router

```
project-root/
|-- context-router.md              <- Root Router (compass)
|
|-- project-context/
|   |-- config/
|   |
|   |-- routes/
|       |-- about/
|       |   |-- README.md          <- MANDATORY: project overview (name, description, audience, stack)
|       |-- PRD/
|       |   |-- README.md          <- MANDATORY: product requirements document (vision, audience, behaviors)
|       |-- route-a/
|       |   |-- README.md          <- Sub-router for Route A
|       |   |-- child-route-1/
|       |   |   |-- README.md      <- Child sub-router (same contract)
|       |   |-- child-route-2/
|       |       |-- README.md
|       |
|       |-- route-b/
|       |   |-- README.md          <- Sub-router for Route B
|       |
|       |-- route-c/
|       |   |-- README.md          <- Sub-router for Route C
|       |
|       |-- roadmap/
|           |-- README.md          <- Roadmap: - [ ] planned, - [x] completed items by phase
|
|-- .agents/
    |-- skills/                    <- AI Skills and Agents route
        |-- context-router-actualizer/
        |-- context-router-initializer/
```

## Directory Path rule in sub-routers

All Directory Paths MUST be relative to the repository root. This applies equally inside context-router.md and inside any sub-router README.md.

In context-router.md (root router) — points directly to the README.md file:
```
## Route A
Directory Path: project-context/route-a/README.md
```

In project-context/route-a/README.md (sub-router declaring child routes) — points to the child route directory:
```
## Child Route 1
Directory Path: project-context/route-a/child-route-1/

## Child Route 2
Directory Path: project-context/route-a/child-route-2/
```

NEVER use relative paths like `Directory Path: child-route-1/` in a sub-router.

## Traversal flow (how an agent reads context)

```
1. Agent receives task
        |
        v
2. Read context-router.md (root router)
        |
        v
3. Identify which route(s) the task touches
        |
        v
4. Navigate to route directory -> Read README.md (sub-router)
        |
        v
5. Sub-router declares child routes? --YES--> Read child README.md
        |                                          |
        NO                                         v
        |                                    Repeat until leaf
        v
6. Agent has full context -> Execute task
```

# Examples

- `examples/context-router-example/context-router.md` - A generic template with placeholders. Use as the starting point for new projects.
- See the actualizer skill's `examples/context-router-taskpilot.md` for a fully realized context-router from a production project (Python Slack bot with Jira integration). Use as reference for real-world route naming, project-specific rules, and proper structure.
- See the actualizer skill's `examples/scaling-example.md` for how the same routing system scales from a 2-route tiny project to a 3+ level deep project without changing any structural rules. Demonstrates the fractal contract, when to split, and when not to.