# Contacts Route

## TL;DR
- CRUD contact management: add by user_id, remove by contact_id, list with cursor pagination (max 50)
- Friend requests: one-sided requests with accept/decline; mutual requests auto-accept
- Invite links: shareable codes that create instant friend connections upon acceptance
- User search by username/display_name via GET /contacts/search; never exposes email/phone of non-contacts
- Server-authoritative contact list; Zustand store is local cache with fetch-on-mount pattern
- Key files: backend/app/routers/contacts.py, backend/app/routers/friend_requests.py, backend/app/routers/invites.py, backend/app/models/contact.py, backend/app/models/friend_request.py, frontend/store/contactStore.ts, frontend/store/friendRequestStore.ts

## Core Concepts

Contact, User Search, Contact List, User Profile, Online Presence

## Key Files

- backend/app/routers/contacts.py — Contact API endpoints (list, add, remove, search)
- backend/app/routers/friend_requests.py — Friend request API endpoints (send, accept, decline, list incoming/outgoing)
- backend/app/routers/invites.py — Invite link API endpoints (create, resolve, accept, deactivate)
- backend/app/models/contact.py — Contact SQLAlchemy model (id, user_id, contact_user_id, created_at; unique constraint on pair)
- backend/app/models/friend_request.py — FriendRequest and InviteLink SQLAlchemy models
- backend/app/schemas/contact.py — Pydantic schemas (ContactAdd, ContactResponse, UserSearchResult)
- backend/app/schemas/friend_request.py — Pydantic schemas (FriendRequestCreate, FriendRequestResponse, InviteLinkCreate, InviteLinkResponse)
- frontend/store/contactStore.ts — Zustand store: fetchContacts, addContact, removeContact, searchUsers
- frontend/store/friendRequestStore.ts — Zustand store: fetchIncoming, fetchOutgoing, sendRequest, acceptRequest, declineRequest, generateInviteLink
- frontend/app/(main)/contacts.tsx — Contacts screen with FlatList, search bar, friend requests section, invite links
- frontend/components/ContactItem.tsx — Reusable contact list item component (avatar, name, online dot)
- frontend/components/FriendRequestItem.tsx — Reusable friend request list item component with accept/decline actions

## Invariants

- Server is single source of truth for contact list; local Zustand store is cache only
- Friend requests are one-sided: only the recipient must accept; no mutual "both must add" requirement
- Mutual friend requests (A→B and B→A both pending) automatically resolve to accepted + bidirectional contact creation
- Contact creation is always bidirectional: accepting a friend request or invite link creates Contact rows for both users
- Invite links expire after 7 days (configurable) and can be deactivated by the creator
- Cannot message or call non-registered users (no SMS invites in v1)
- Contact add/remove syncs across all user devices within 5 seconds (via API re-fetch)
- User search never exposes private info (email/phone) of non-contacts
- Unique constraint on (user_id, contact_user_id) prevents duplicate contacts at database level
- Unique constraint on (from_user_id, to_user_id) prevents duplicate friend requests

## Constraints

- Max 50 contacts per request with cursor-based pagination
- Contact import from device address book: [REQUIRES DECISION]
- Blocking/muting users: [REQUIRES DECISION]
- Avatars resized to 256x256px max server-side (not yet implemented; tracked for Phase 6)
- Search excludes the requesting user and already-added contacts from results

## Implementation Notes

- Contact model uses UUID primary keys
- Search endpoint: GET /contacts/search?q={query} performs case-insensitive LIKE on username and display_name
- Frontend ContactItem component shows green dot for online presence (presence data from WebSocket)
- Contacts screen supports pull-to-refresh via FlatList onRefresh
