# context-router.md Purpose

- context-router.md serves as the mandatory primary entry point and high-level compass for any LLM or AI agent interacting with this repository. It functions as a root router, directing the AI to the appropriate route-specific documentation to understand the project architecture, business logic, and execution rules. It ensures the AI loads only the necessary context by branching down into specific sub-routers.
- context-router.md is the Behavioral Rulebook and Architectural Compass for all AI agents or human-being working on this project.
- IMPLEMENTATION IS THE MAP, ROUTER IS THE COMPASS: Use search_project_context MCP tool and direct file reading to find WHERE files are. Use this context-router exclusively to understand WHY the architecture exists, HOW logical routes interact, and the strict rules you must follow.
- TRIGGER RULE: Whenever you work on a specific feature or entity, you MUST consult its corresponding route directory in this router to understand its business logic and constraints BEFORE making decisions, and completing the task.
- We don't need any other context, history, memory. We're starting from scratch!

# AI Agent Quick Start Protocol

Follow these steps in order before writing any code:

1. Read THIS file completely. The inline summaries below give you enough context to start.
2. Identify which routes your task touches using the Route Selection Guide below.
3. Read ONLY the README.md files for those routes (path given under each route entry).
4. Each route README starts with a TL;DR section — read that first. Go deeper only if your task requires it.
5. Use search_project_context MCP tool or read files directly to locate specific implementation files.
6. Begin implementation only after completing steps 1-5.

Do NOT read all routes. Do NOT skip straight to code. The TL;DR sections exist so you can quickly assess relevance without reading entire documents.

# Route Selection Guide

Match your task keywords to the routes you need to read:

| If your task involves... | Read these routes |
|---|---|
| [keyword group 1, e.g., users, auth, login, sessions] | [Route 1 Name] |
| [keyword group 2, e.g., products, catalog, inventory] | [Route 2 Name] |
| [combined workflow, e.g., checkout, purchase, payment] | [Route 1 Name] + [Route 2 Name] |

# context-router.md terminology

- Context Router: This root file, providing a high-level map of the entire project.
- Route: A specific directory representing a business or technical module. It acts as a node in our architecture tree. Every route directory inherently contains its own context and must include a README.md file, which acts as its sub-router.
- Sub-router: The README.md file located inside any route directory. It acts exactly like the root context router but is isolated to its specific route. A sub-router can declare its own child routes (sub-directories), creating an infinitely deep nested structure where every node follows the same routing contract.

# Hierarchical routing principle

The routing system is a recursive tree with no depth limit. The flow is always: Router -> Route (Directory) -> Sub-router (README.md) -> Child Route (Directory) -> Child Sub-router (README.md), infinitely. Every node follows the exact same contract. Never skip a level when traversing. A route is never just a dead-end folder; if it has complexity, its sub-router will point you to the deeper child routes you need.

# Project terminology
- Application: the core service codebase running without any AI involvement. When a requirement states "Application must do X", it means the standard code handles X with no LLM or agent participation.
- Agent / Copilot / LLM: an AI-powered component or external AI tool performing a task. When a requirement states "Agent must do X" or "LLM must do X", it means an AI model or AI-assisted tool handles X, not the core application code.

# Mandatory Execution Rules
- Always start context gathering from this file.
- Never guess or hallucinate business logic. You must navigate to the relevant route directory and read its sub-router (README.md) to acquire the correct context.
- Traverse the routing tree recursively. Every route directory may contain a README.md sub-router that declares its own child routes. Follow each relevant route downward, reading sub-routers at every level, until you reach the granularity required for the task. There is no depth limit; the hierarchy branches as deep as the project requires.
- [PROJECT SPECIFIC RULE]: Add your absolute, non-negotiable project constraints here (e.g., "Always use UTC for dates", "Never mutate global state directly", "Data must be stored locally").
- Load Selectively: Open ONLY the specific documentation directories strictly required for your task/role. Do not load the entire project context.
- UPDATE TRIGGER (CRITICAL): If your task changes the fundamental business logic, data structure, or rules of a route, OR produces any important finding (architectural constraint, bug root cause with systemic implications, technical behavior that future agents must know to avoid mistakes), you MUST update the corresponding route documentation in project-context/ to reflect this new reality.
- Avoid files more than 500 strings in size for better performance and reliability.

# Project context map (Routes)

## PRD (Product Requirements Document)
The source of truth for the product vision. It explains WHAT we are building, WHO we are working for, and the core problems we solve.
ALWAYS read this if your task involves planning new features, writing documentation, or understanding the product's core value proposition.
Directory Path: project-context/PRD/README.md

## About
High-level overview of what this project is, who it is for, and its core tech stack.
Directory Path: project-context/about/README.md

# Core Business Routes (Behavioral Rules)
ALWAYS read the corresponding route README.md before proceeding with the task related to it. Each README starts with a TL;DR — scan that first to confirm relevance, then read deeper sections as needed.

## [Route 1 Name] Route
[Description of the route: e.g., Rules for user authentication, session management, and role-based access].
Directory Path: project-context/[route-1-name]/README.md

## [Route 2 Name] Route
[Description of the route: e.g., Rules for core entity structures, states, and validation logic].
Directory Path: project-context/[route-2-name]/README.md

## AI Skills and Agents
Available tools and automated skills for the AI agent (e.g., setup scripts, actualizers).
Directory Path: .agents/skills/

# Rules for this file (context-router.md)

- Never use bold formatting in this file.
- Keep this file clear and concise.
- Never add granular feature-level routes to this file. Use search_project_context for detailed routing.
- For proper update of this file you MUST ALWAYS use .agents/skills/context-router-actualizer/SKILL.md