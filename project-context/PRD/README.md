# PRD

## TL;DR
- PRD is Product Requirements Document
- PRD is the source of truth for product vision, target audience, and core problems solved
- Read this route when planning features, writing documentation, or understanding the product value proposition
- Use [REQUIRES PRODUCT FILLING: no data in the code] marker for missing information — never hallucinate
- Key file: project-context/PRD/README.md

This route documents the product requirements and vision for the project.

## Product Overview

### Problem Statement
* Users need a single messenger that works identically across web, iOS, and Android without feature gaps or behavioral differences between platforms. Existing solutions either lack cross-platform parity or compromise on modern features like circular video messages.

### Objectives & Goals
* Deliver a stable, production-ready messenger with chat, video messages (circles), and video calls
* Achieve identical behavior and UX across web, iOS, and Android from a single codebase
* Provide reliable real-time message delivery with minimal latency

### Value Proposition
* One messenger, three platforms, zero compromises — users get the same full-featured experience whether they open the app on their phone or in a browser.

## Target Audience

* Primary: individuals and small groups who communicate across multiple devices and platforms daily
* Secondary: teams or communities that need lightweight group messaging with video capabilities
* Users access the app on mobile (commuting, on-the-go) and web (desktop, work) interchangeably

## Behaviours

* Users can register and sign in with credentials, then immediately access their contacts and conversations
* Users send and receive text messages in 1:1 and group chats with real-time delivery
* Users record and send circular video messages (video circles) inline within any conversation
* Users initiate and receive 1:1 video calls with real-time audio and video
* Users manage a contact list to find and organize people they communicate with
* All actions produce identical results regardless of platform (web, iOS, Android)

## Design & UX

### Design Assets
* [REQUIRES PRODUCT FILLING: no data in the code]

### Responsive & Adaptive Breakpoints
* Mobile: 320px - 428px (standard phone sizes)
* Tablet: 429px - 1024px
* Web Desktop: 1025px+
* The app must be fully usable at every breakpoint with no hidden or broken features

### Accessibility (a11y)
* [REQUIRES PRODUCT FILLING: no data in the code]

### Animations & Micro-interactions
* Video circle recording: circular progress indicator during capture
* Message delivery: subtle send/delivered/read state transitions
* Video call: connection state indicators (connecting, connected, reconnecting)
* [REQUIRES PRODUCT FILLING: detailed animation specs]

### Technical Details

All technical details are stored in context-router.md

## Non-Functional Requirements

### Browser Support Matrix
* Chrome (latest 2 versions)
* Safari (latest 2 versions)
* Firefox (latest 2 versions)
* Edge (latest 2 versions)
* Mobile Safari (iOS 15+)
* Chrome for Android (latest)

### Performance Metrics
* App launch to interactive: under 3 seconds on mid-range devices
* Message send-to-deliver latency: under 500ms on stable connection
* Video call connection setup: under 5 seconds
* Video circle recording start: under 1 second after tap

### SEO (Search Engine Optimization)
* Not applicable — this is a private messenger, not a public-facing content site

## Analytics & Tracking

### Event Tracking Plan
* [REQUIRES PRODUCT FILLING: no data in the code]

## Out of Scope
* End-to-end encryption (future iteration)
* Voice-only calls (video calls only in v1)
* File sharing beyond media (documents, archives — future iteration)
* Channels or broadcast lists
* Bots or integrations
* Message reactions or threads

## Milestones & Timeline
* See project-context/roadmap/README.md for the full phased roadmap

## Route-Specific Constraints

- Keep this file clear and concise. 1 general change must be described in 1-2 sentences maximum. If more details are needed — update documentation in project-context/ and link it here.
- Do not use bold formatting in this file.
- Do not use table formatting in this file.
- MUST NOT invent, assume, or hallucinate any business logic or requirements.
- Use [REQUIRES PRODUCT FILLING: no data in the code] for missing information.
- MUST NOT add, remove, or modify any structural sections from this skeleton.
