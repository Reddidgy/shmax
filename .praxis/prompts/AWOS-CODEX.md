# AWOS Operating Manual

Operating manual for any assignee that works through AWOS (https://github.com/provectus/awos). This manual is project-agnostic: it is the law an AWOS practitioner carries into every repository. Read it fully before acting.

This manual is the single source of AWOS procedure. Assignee profiles (`.praxis/assignees/*.json`) carry only their own identity, voice, and statement of adherence  -  they reference these sections by number, they do not restate them. If a procedural rule would otherwise live in two places, it lives here and the assignee points to it. When you update a rule, update it here; the assignee inherits it by reference.

## 1. What AWOS is

AWOS is a Spec-Driven Development (SDD) framework  -  a small set of slash commands that enforce one discipline: an AI agent performs dramatically better when it receives full, relevant, pre-written context, and far worse when its context window is bloated. AWOS solves this by routing every change through a chain of commands, where each command's only durable output is a committed Markdown file. Because state lives on disk and not in the chat, you may clear the conversation at any moment and resume in a few moves.

The whole framework reduces to nine commands across two phases, plus an audit helper.

Invoke the commands  -  never narrate them. Every AWOS step (`/awos:product`, `/awos:roadmap`, `/awos:architecture`, `/awos:hire`, `/awos:spec`, `/awos:tech`, `/awos:tasks`, `/awos:implement`, `/awos:verify`) is an *executable slash command*  -  a skill you actually run, not a label you describe. In Claude Code that means invoking it through the Skill tool (e.g. `Skill(skill="awos:spec")`); in any other client it means typing the slash command so the client executes it. You MUST actually run the command and let it do its work. You MUST NOT: write the spec/tech/tasks/architecture Markdown by hand, paraphrase what the command "would do," print a mock of its output, or claim a step is done without having invoked it. If you find yourself authoring a `context/` document directly, stop  -  that document is owned by a command you forgot to run. Saying `/awos:spec` in prose is not running `/awos:spec`.

## 2. The two entry scenarios

1. Fresh repository (no AWOS): you must initialize AWOS, then run the Foundation phase to author the product, roadmap, and architecture documents before any feature work.
2. Repository where AWOS already exists (the `context/` directory and AWOS files are present and committed): skip Foundation entirely. Go straight to the Feature Development Cycle. Foundation is a once-per-project act.

Detect the scenario by checking for the `context/` directory and `.awos/` directory at the repo root.

## 3. Installation / initialization

If AWOS is not installed, initialize it:

```
npx @provectusinc/awos
```

This creates:
- `.awos/`  -  commands, templates, scripts (framework internals)
- `.claude/commands/awos/`  -  slash-command wrappers
- `context/`  -  the project documents (product, roadmap, architecture, specs)
- registers the AWOS plugin marketplace

For an existing, non-AWOS codebase, the plugin also offers `/awos:ai-readiness-audit` to assess fit before adoption.

## 4. The `context/` directory is sacred

The `context/` directory is managed exclusively by AWOS commands. You never hand-create, hand-edit, or hand-delete files inside it. Every document there is the output of a command. The entire directory is committed to Git so that multiple people (and parallel agent sessions) collaborate on one shared, version-controlled source of truth. If you have raw notes in scratch Markdown files, feed them to the relevant AWOS command and then delete the scratch files  -  AWOS absorbs them.

## 5. Foundation phase (run once per project)

Run in order  -  and *run* means invoke the slash command, not describe it. Each command asks clarifying questions and writes a Markdown document. You may feed it existing files instead of answering by hand ("read the README and these notes"). Never produce a Foundation document yourself; let the command author it.

1. `/awos:product`  -  Defines the Product: what it does, why, and for whom. Two artifacts: a three-sentence Product Definition Lite and a full Product Definition written from the user's perspective (use cases, pains closed, success metrics, main features). Deliberately contains almost no technology.
2. `/awos:roadmap`  -  Builds the Product Roadmap: high-level phases and their order, covering the system's parts, plus future considerations so nothing is forgotten. Run with no input to get a proposal, then brief and commit.
3. `/awos:architecture`  -  Defines the System Architecture: the long, technology-facing document. Every need from the Product Definition is realized here. Includes Architecture Decision Records (what was tried, what was rejected, what was decided and why), diagrams, and every functional part of the system. Expect many questions  -  answer them or feed a file describing the stack, accounts, and infra.
4. `/awos:hire`  -  Hires specialist agents: finds and installs the right skills and MCP servers from the registry and generates agent files (e.g., a Terraform agent, a Python agent, a browser/Playwright verification agent). Restart the client afterward so newly added MCP servers are picked up. `/awos:hire` can be re-run at any time later when a missing specialist is discovered.

## 6. Feature Development Cycle (repeat for every code change)

Every time you create, change, or delete code, walk this chain by actually invoking each command in turn  -  never substitute prose for an invocation, and never skip ahead. Each command reads the Product Definition, Roadmap, and Architecture as input, so it is always situationally aware. Between every command you may freely clear context  -  the prior step's output is a committed file.

1. `/awos:spec`  -  Creates the Functional Spec: what the feature does for the user, why, and explicitly what is in and out of scope. Pure functional requirements (kept short). Run with no arguments to have it offer uncovered roadmap phases; or describe a need directly. Deletions get specs too.
2. `/awos:tech`  -  Creates the Technical Spec: how the feature is built. It reads existing code, names exact files to modify and libraries to add, surfaces design choices via the Ask tool (e.g., shared module vs. copy-paste, library vs. overkill), and produces a statement so detailed an executor needs nothing else.
3. `/awos:tasks`  -  Breaks the Tech Spec into a task list: vertical slices (story-sized, each ending in something verifiable  -  run a test, invoke a lambda, plan+apply) and subtasks, each annotated with the specialist agent that will execute it. If a needed agent is absent, run `/awos:hire`. In this repository the breakdown does not stop at `context/spec/<dir>/tasks.md`: each slice is then materialized as a PraxisOS task on the kanban board (see §11). The Markdown checklist stays as the spec-linked record; the board becomes the execution surface.
4. `/awos:implement`  -  The only command that actually changes code. Delegates to sub-agents that each receive only the slice of context they personally need (the implementation step does NOT load the full architecture). Run a full slice at once. When a slice completes, clear context before the next one.
5. `/awos:verify`  -  Verifies spec completion: re-reads the Functional Spec against the completed tasks, confirms acceptance criteria are met, and marks Status as Completed. If the feature changed the product or architecture, it tells you exactly which command to run (e.g., paste this text into `/awos:product`) so the foundation documents self-update.

## 7. Specs are a permanent knowledge base

Every spec is kept forever. To answer "why are we doing it this way / how did we get here," query the specs  -  AWOS can trace a decision across them ("decided A in spec 7, changed to B in spec 20, now here"). The `/awos:spec` and `/awos:tech` agents double as on-demand functional and technical assistants who already know where everything lives; they can even field operational/debug questions, clarifying first that the request is debugging rather than a new spec.

## 8. Core operating principles

- BEFORE ANY WORK: Read .praxis/prompts/praxis-mcp-codex.md
- Commands are run, not recited. The leverage of AWOS comes from the command *doing the work* (reading the foundation docs, asking clarifying questions, writing the committed artifact). Mentioning a command by name, summarizing its purpose, or hand-writing what it would output captures none of that and is the single most common way the discipline silently breaks. Whenever a step is due, invoke the actual slash command (via the Skill tool in Claude Code) and wait for its result before moving on.
- Context hygiene is the whole point. A bloated context window causes hallucinations. Clear context aggressively  -  ideally between every command  -  and resume from the committed Markdown.
- Parallelize with Git worktrees: one worktree per change lets multiple sessions run independently while sharing the committed `context/`.
- Trust the Ask tool: AWOS commands present options (often with a recommendation) instead of guessing. Answer them; don't let the agent invent unstated requirements.
- Auto mode runs commands without per-step approval prompts; enable it once you trust the flow, leave it off when you want to inspect each move.
- When something goes wrong with an agent, the fix is usually on the human side of the flow, not the model: repeated manual steps → write a skill/command; recurring same-error → add a gate/hook or a CLAUDE.md rule; weak research → install the right MCP server and document its usage.
- VERY IMPORTANT! In case of work with AWOS + Praxis MCP you must work this way:
  - Before work: by Praxis MCP you're getting task information + project context using praxis mcp tools
  - All remaining functions must be AWOS related untill the finish of the coding work
  - Then you must update routes using praxis mcp