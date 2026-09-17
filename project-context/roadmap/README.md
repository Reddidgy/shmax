# Roadmap

## TL;DR
- Phase 1 (Foundation) and Phase 2 (Auth & Contacts) are complete and merged
- Phase 3 (Core Chat enhancements) is the next milestone
- 6 phases total; Phases 3-6 remain planned but not started
- Backlog items tracked separately for post-v1 consideration

## Phase 1 — Foundation (COMPLETED)

- [x] Project scaffolding: monorepo with backend/ and frontend/ directories
- [x] Backend scaffolding: FastAPI app with SQLAlchemy async, Alembic migrations, Pydantic schemas
- [x] Database models: User, Contact, Conversation, ConversationParticipant, Message
- [x] Docker Compose: PostgreSQL 15, Redis 7, backend service with hot reload
- [x] WebSocket infrastructure: connection manager, auth via token query param, event routing
- [x] Expo Router navigation: auth flow group, main tab group, dynamic conversation route
- [x] Environment configuration: .env.example with all required variables

## Phase 2 — Auth & Contacts (COMPLETED)

- [x] User registration: POST /auth/register with username, password, display_name
- [x] User sign-in: POST /auth/login returning JWT access + refresh token pair
- [x] Token refresh: POST /auth/refresh exchanging refresh token for new pair
- [x] Token logout: POST /auth/logout with Redis-based refresh token blacklist
- [x] Current user profile: GET /auth/me returning authenticated user data
- [x] Contact list: GET /contacts with cursor-based pagination (max 50 per page)
- [x] Add contact: POST /contacts by contact_user_id
- [x] Remove contact: DELETE /contacts/{contact_id}
- [x] User search: GET /contacts/search?q= by username/display_name (excludes private info of non-contacts)
- [x] Login screen: username + password inputs, error display, navigation to register
- [x] Register screen: username, display_name, password, confirm password, optional email/phone
- [x] Contacts screen: FlatList with search bar, online indicator, add/remove actions
- [x] Profile screen: display user info, edit display name, logout button
- [x] Auth store (Zustand): login, register, logout, refreshTokens, loadStoredTokens with expo-secure-store
- [x] Contact store (Zustand): fetchContacts, addContact, removeContact, searchUsers

## Phase 3 — Core Chat (IN PROGRESS)

Basic chat infrastructure was delivered in Phase 1-2 (WebSocket, message model, conversation endpoints, chat UI). Phase 3 focuses on enhancements:

- [ ] Message editing and deletion
- [ ] Read receipts UI (checkmark indicators in message bubbles)
- [ ] Group chat management (add/remove participants, rename group)
- [ ] File and image sharing via S3 upload
- [ ] Offline message queue with retry logic
- [ ] Typing indicators (WebSocket events implemented, UI polish needed)
- [ ] Conversation list pull-to-refresh and real-time updates
- [ ] Unread count badge accuracy across devices

## Phase 4 — Video Messages (IN PROGRESS)

- [x] Camera UI with circular crop overlay and countdown timer
- [x] Client-side video recording (expo-camera CameraView, 60s max)
- [x] Local file upload pipeline (backend/uploads/ — no S3)
- [x] Inline playback in chat bubble (tap to play/unmute, progress bar)
- [x] Front/rear camera toggle during recording
- [x] Client-side video caching (expo-file-system on native, browser cache on web)
- [ ] Thumbnail extraction (first frame) client-side
- [ ] Upload progress indicator in chat UI

## Phase 5 — Video Calls (IN PROGRESS)

- [x] WebRTC signaling server endpoints on FastAPI
- [x] WebRTC peer connection setup with STUN/TURN
- [x] Call initiation and acceptance flow (WebSocket notifications)
- [x] Call UI: caller info, accept/decline, in-call controls
- [ ] Platform-specific call integration (CallKit iOS, ConnectionService Android)
- [x] Network quality indicator (good/fair/poor)
- [x] 30-second ring timeout with auto-decline

## Phase 6 — Polish & Stability

- [ ] Cross-platform QA (web, iOS, Android feature parity)
- [ ] Performance optimization (app launch <3s, message delivery <500ms)
- [ ] Comprehensive error handling and user-facing error messages
- [ ] Push notifications (FCM Android, APNs iOS, Web Push)
- [ ] Rate limiting on all API endpoints
- [ ] Security audit (input validation, SQL injection, XSS)

## Backlog (Post-v1)

- E2E encryption
- Voice-only calls
- File sharing beyond media
- Message reactions
- Threaded replies
- Contact import from device address book
- Block/mute users
- Group video calls
- Screen sharing
- Channels, bots, integrations
