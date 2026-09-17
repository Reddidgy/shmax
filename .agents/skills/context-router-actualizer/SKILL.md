---
name: context-router-actualizer
description: Use this skill to actualize context-router.md routes and project-context/ documentation by diving into the project changes and updating behavioral rules, business logic, and architectural constraints.
---

# CONTEXT ROUTER ACTUALIZATION SKILL

You have been triggered to update the project's documentation because a recent task modified the fundamental business logic, data structures, or architectural rules of the application — or produced important findings (discovered technical constraints, bug root causes with systemic implications, architectural behaviors) that agents must know to work effectively on this project.

Your goal is to ensure the `project-context/` documentation and `context-router.md` accurately reflect the *current* behavioral rules and important technical knowledge of the system.

Follow these steps strictly:

## Step 1: Identify the Scope of Change
Analyze the changes you or the user just made to the codebase:
1. Did you modify an existing Route (e.g., added a new required field, changed how a module works, discovered an architectural constraint or important system behavior)? -> Proceed to Step 2.
2. Did you introduce a completely NEW Route (e.g., a new business entity or technical module)? -> Proceed to Step 2, then Step 3.
3. Did you make a discovery worth preserving — a bug root cause with architectural implications, a technical constraint, or a system behavior that agents must know to avoid mistakes? -> Proceed to Step 2 for the affected route.
4. Did you only do a trivial refactor, pure style change, or minor bug fix with no architectural implications and no findings worth preserving for future agents? -> STOP. No actualization is needed.

## Step 2: Actualize Route Documentation
Go to the specific route directory in `project-context/` (e.g., `project-context/[route-name]/` or `project-context/[route-name]/` depending on the project's convention). Create it if it is a new route.
1. Update or create the sub-router (`README.md`) detailing the NEW business rules, required elements, and constraints for this route.
2. UPDATE THE TL;DR SECTION: Every route README must start with a `## TL;DR` section immediately after the `#` heading. When updating a route, ensure the TL;DR reflects the current state. If the README lacks a TL;DR, add one. Each TL;DR bullet must be a single declarative sentence of ≤ 25 words containing the key domain terms agents would search for. No compound sentences joining unrelated facts. No prose paragraphs. Cover: the most critical rules, key data structures or storage details, main workflow or constraint, and key implementation files.
3. FOCUS ON THE "WHY" AND "WHAT MUST BE DONE": Explain the structural rules and architectural decisions.
4. DO NOT DUPLICATE CODE: Do not paste function signatures, interface definitions, or line-by-line implementations. Code is the map; documentation is the compass.
5. If the route has child routes with their own sub-routers, traverse and update those as needed. Remember the hierarchical routing principle: the routing system is a recursive tree with no depth limit.
6. DIRECTORY PATH RULE: Every `Directory Path` in any router or sub-router MUST be relative to the repository root. Never use paths relative to the current file. Example: a child route inside `route-a` must be `Directory Path: project-context/route-a/child-route/`, not `Directory Path: child-route/`.
7. BM25 SEARCHABILITY CONTRACT: `search_project_context` scores individual lines using BM25 with field boosting (headings > body). Every behavioral rule, invariant, and business constraint must occupy its own dedicated line. Multi-rule sentences and prose paragraphs bury key terms and cause recall failures — agents searching for those terms will not find them. The format of the content IS the retrieval contract.
8. PRUNE STALE CONTENT: Remove historical detail, resolved issue narratives, and redundant explanations that dilute term frequency for current rules. Every line should earn its place by being either a current behavioral rule or essential architectural context.

## Step 3: Evaluate Context Router Impact

**Fast-path check — answer before opening any file:**
Did you create a new `.md` file under `project-context/` that did not exist before this task?
- **YES (new route created)** → Open `context-router.md` and follow the steps below.
- **NO (only updated existing route files)** → Skip the rest of Step 3 and proceed directly to Step 4.

Open `context-router.md` at the repository root: `{project_root}/context-router.md`.
> ⚠️ Path warning: `context-router.md` lives at the **repository root**, not inside `project-context/`. The correct path is `{project_root}/context-router.md`. Never attempt `project-context/context-router.md` or any path relative to the context directory — that path does not exist and will produce a "File does not exist" error.

For NEW routes:
1. Rule Check: ONLY add a new route entry if you introduced a completely NEW distinct Route (business entity or technical module).
2. Action: If a new route was created, add a new entry under the `# Core Business Routes (Behavioral Rules)` section using this format:
   ```markdown
   ## [Route Name] Route
   [1-2 sentences explaining the rules governed by this route]
   Directory Path: project-context/[route-name]/README.md
   ```
3. Add a row to the `Route Selection Guide` table mapping relevant task keywords to the new route.
4. Strict Prohibition: NEVER add routes for individual UI components, helper functions, or specific code files to context-router.md. Only top-level business entities or technical modules qualify.

For UPDATED routes:
5. Do NOT add or update `Key files:` or `Critical rules:` in context-router.md — those details live exclusively in the route's README.md. context-router.md stays lean: name, description, and Directory Path only.
6. If new keywords or task types now apply to the route, update the `Route Selection Guide` table.

## Step 4: Format & Constraints Check
Before finalizing your updates to `context-router.md`, verify:
- [ ] There is absolutely NO bold formatting anywhere in the context-router.md file.
- [ ] The file remains clear, concise, and top-level.
- [ ] Every route listed has a matching directory with a README.md sub-router on disk.
- [ ] Every route README.md starts with a `## TL;DR` section (3-4 bullet points) immediately after the `#` heading.
- [ ] Every route entry in context-router.md contains ONLY a name, a 1-2 sentence description, and a `Directory Path:` pointing to the README.md file. No `Key files:` or `Critical rules:` — those belong exclusively in the route's README.md.
- [ ] The Route Selection Guide table covers all routes and any new keywords introduced by the changes.
- [ ] No orphaned files: every non-README.md file or subdirectory inside a route directory is declared as a child route in that route's sub-router (README.md). If an undeclared file or directory is found, either convert it into a proper child route (move into its own directory with a README.md) or remove it. Loose files break the routing contract because agents will never discover them through traversal.
- [ ] Every `Directory Path` (in context-router.md AND in sub-router README.md files) is relative to the repository root, not relative to the current file.
- [ ] The AI Agent Quick Start Protocol, Route Selection Guide, terminology section, hierarchical routing principle, and project terminology sections are intact and unmodified (unless the change specifically requires updating them).
- [ ] The `about` route exists at `project-context/about/README.md` with a current project overview (name, description, target audience, tech stack, value proposition). If missing, create it now — it is mandatory in every project.
- [ ] The `PRD` route exists at `project-context/PRD/README.md` with the product requirements document (vision, audience, behaviors). If missing, create it using the template at `.agents/skills/context-router-initializer/templates/prd-route-template.md` — it is mandatory in every project.
- [ ] Do all behavioral rules appear as standalone searchable lines (not embedded in multi-sentence prose)?
- [ ] Is each TL;DR bullet a single declarative sentence of ≤ 25 words containing key domain terms that agents would query?
- [ ] Has stale or redundant content been pruned to avoid diluting BM25 term frequency for current rules?

## Step 5: Completion
Provide a brief summary to the user outlining which route rules were updated and if any new routes were added to the context router.

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
|           |-- README.md          <- Sub-router for Route C
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

- `examples/context-router-taskpilot.md` - A fully realized context-router from a production project (Python Slack bot with Jira integration). Use as reference for real-world route naming, project-specific rules, and proper structure.
- `examples/context-router-example/context-router.md` - A generic template with placeholders, matching the AWESOME structure. Use as the starting point for new projects.
- `examples/scaling-example.md` - Shows how the same routing system scales from a 2-route tiny project to a 3+ level deep project without changing any structural rules. Demonstrates the fractal contract, when to split, and when not to.