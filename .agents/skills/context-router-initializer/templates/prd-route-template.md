# PRD

## TL;DR
- PRD is Product Requirements Document
- [PRD is the source of truth for product vision, target audience, and core problems solved]
- [Read this route when planning features, writing documentation, or understanding the product value proposition]
- [Use [REQUIRES PRODUCT FILLING: no data in the code] marker for missing information — never hallucinate]
- Key file: project-context/PRD/README.md

This route documents the product requirements and vision for the project.

## Product Overview

### Problem Statement
* [Describe the exact problem this product is solving for the user. What pain point are we addressing? Keep it concise and focused on the user's perspective.]

### Objectives & Goals
* [State the primary business and product goals. What defines success for this specific product release?]

### Value Proposition
* [Briefly explain why the user will care about this product. What is the core value it delivers?]

## Target Audience

* [Describe the primary and secondary users interacting with this product. Include relevant demographic or behavioral details.]
* [Define the primary contexts in which users will access the app.]

## Behaviours

* [Detail the specific product behaviors required to fulfill the user stories.]

## Design & UX

### Design Assets
* [Provide direct links to Figma, Sketch, or Zeplin files.]
* [Specify which pages/components are final and approved for development.]

### Responsive & Adaptive Breakpoints
* [List the exact screen width breakpoints the frontend must support.]

### Accessibility (a11y)
* [Define the required accessibility standards.]
* [Specify requirements for keyboard navigation, screen reader support, and color contrast.]

### Animations & Micro-interactions
* [Describe any crucial transitions, loading states, or animations that are part of the core experience.]

### Technical Details

All technical details are stored in context-router.md

## Non-Functional Requirements

### Browser Support Matrix
* [Explicitly list supported browsers and versions.]

### Performance Metrics
* [Set specific targets for frontend performance.]

### SEO (Search Engine Optimization)
* [If this is a public-facing app, outline SEO requirements.]

## Analytics & Tracking

### Event Tracking Plan
* [List the crucial user interactions that must trigger an analytics event.]
* [Specify the tool and the event payload structure.]

## Out of Scope
* [Clearly state what is intentionally NOT being built in this iteration to prevent scope creep.]

## Milestones & Timeline
* [Break down the frontend development into logical phases.]

## Route-Specific Constraints

- Keep this file clear and concise. 1 general change must be described in 1-2 sentences maximum. If more details are needed — update documentation in project-context/ and link it here.
- Do not use bold formatting in this file.
- Do not use table formatting in this file.
- MUST NOT invent, assume, or hallucinate any business logic or requirements.
- Use [REQUIRES PRODUCT FILLING: no data in the code] for missing information.
- MUST NOT add, remove, or modify any structural sections from this skeleton.
