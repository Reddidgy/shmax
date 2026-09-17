# [Route Name] Route

## TL;DR
- [Single declarative sentence ≤ 25 words with key domain terms — the most critical rule for this route]
- [One searchable rule per bullet — key data structure, storage location, or constraint]
- [Each bullet independently findable by BM25 keyword search — main workflow or state transition rule]
- Key files: [list 2-4 primary implementation files or directories]

[One-sentence description of what this route governs.]

This file is the sub-router for the [Route Name] route. It follows the same contract as the root context-router.md: it describes the business rules for this route, and if the route has child routes, it points to them so the agent can traverse deeper.

## Purpose

[Describe the core business purpose of this route. What problem does it solve? What business rules does it own?]

## Core Concepts

[List and briefly explain the key entities, states, or workflows this route manages.]

## Invariants

These rules must never be violated:
- [Add non-negotiable business rule 1]
- [Add non-negotiable business rule 2]

## Route-Specific Constraints

- [Constraint 1: e.g., data format requirements, API boundaries, validation rules]
- [Constraint 2]

## Child Routes

IMPORTANT: Every Directory Path MUST be relative to the repository root, never relative to this file.
If this route has no child routes, remove this entire section. A leaf route with no children is perfectly valid.
Only add child routes when a sub-area has grown complex enough to need its own isolated business rules.

## [Child Route 1 Name]
[1-2 sentence description of what this child route governs.]
Directory Path: project-context/[this-route-name]/[child-route-1-name]/

## [Child Route 2 Name]
[1-2 sentence description of what this child route governs.]
Directory Path: project-context/[this-route-name]/[child-route-2-name]/
