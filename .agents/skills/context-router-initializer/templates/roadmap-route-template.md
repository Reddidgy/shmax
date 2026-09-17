# Roadmap Route

## TL;DR
- Roadmap tracks planned and completed work organized by phase and milestone using checkbox notation.
- Use - [ ] for planned or in-progress items and - [x] for completed items in every phase.
- Phases are ordered chronologically: completed phases first, then active, then future phases.
- Key file: project-context/roadmap/README.md

This route documents the project's planned and completed milestones, features, and release phases.

## Purpose

Track and communicate project progress. Gives AI agents and team members a single source of truth for what has shipped, what is in progress, and what is planned.

## Core Concepts

- Phase: A named grouping of related milestones (e.g., "v1.0 — Foundation", "v2.0 — Scale").
- Milestone: A significant deliverable within a phase with a clear done state.
- Item: A specific feature, fix, or improvement — always expressed as a checkbox line.

## Invariants

- Every item uses checkbox format: - [ ] planned, - [x] completed. No plain bullet items.
- Completed items are never deleted; they serve as a permanent completion log.
- Phases are sorted chronologically: oldest/completed phases at the top, future phases at the bottom.
- Each item is a single actionable unit — compound items must be split into separate lines.

## Route-Specific Constraints

- Item format: - [ ] Feature name — brief description (why it matters or what it unlocks)
- Completed item format: - [x] Feature name — brief description
- Phase heading format: ## Phase Name (e.g., ## v1.0 — Foundation, ## v2.0 — Growth)
- Each phase opens with a one-sentence description of its goal before listing items.
- Speculative or wish-list items belong in a ## Backlog section, not in versioned phases.
- Phase naming adapts to the project type: semantic versions for releases (v1.0, v2.0), milestone names for non-versioned projects (MVP, Beta, GA), or sprint/quarter labels for time-boxed work (Q1, Sprint 3).

## [First Phase Name]

[One-sentence description of this phase's goal — derived from the project's codebase analysis.]

- [x] [Completed deliverable — brief description of what it does or why it matters]
- [ ] [Planned deliverable — brief description of what it does or why it matters]

## [Next Phase Name]

[One-sentence description of this phase's goal.]

- [ ] [Planned deliverable — brief description]
- [ ] [Planned deliverable — brief description]

## Backlog

Items not yet assigned to a phase.

- [ ] [Backlog item — brief description]