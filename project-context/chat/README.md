# Chat Route

## TL;DR
- Core messaging engine: 1:1 and group conversations with real-time WebSocket delivery via backend/app/routers/websocket.py
- Messages persisted in PostgreSQL (Message model); states: sent → delivered → read, transitions pushed via WebSocket
- Conversation creation auto-detects 1:1 vs group; 1:1 deduplication prevents duplicate conversations
- Key files: backend/app/services/chat_service.py, backend/app/routers/chat.py, frontend/store/chatStore.ts

## Core Concepts

Conversation (1:1 and group), Message, Message States, WebSocket Events, Typing Indicators, Presence

## Key Files

- backend/app/routers/chat.py — Chat API endpoints (create conversation, list conversations, get messages, send message)
- backend/app/routers/websocket.py — WebSocket endpoint (WS /ws), connection manager, event routing (send_message, typing, message_read, presence)
- backend/app/services/chat_service.py — Business logic: create_conversation, send_message, get_conversations (with last message + unread count), get_messages (paginated), update_message_state
- backend/app/services/presence_service.py — Redis-backed presence: set_online, set_offline, is_online, get_last_seen
- backend/app/models/conversation.py — Conversation model (id, title, is_group, created_by, timestamps)
- backend/app/models/conversation_participant.py — ConversationParticipant model (conversation_id, user_id, joined_at, last_read_at)
- backend/app/models/message.py — Message model (id, conversation_id, sender_id, content, message_type enum, media_url, thumbnail_url, state enum, created_at)
- backend/app/schemas/chat.py — Pydantic schemas (ConversationCreate, ConversationResponse, MessageCreate, MessageResponse, MessageStateUpdate, TypingIndicator)
- frontend/store/chatStore.ts — Zustand store: conversations, messages by conversation id, typingUsers, real-time WebSocket event handlers
- frontend/app/(main)/index.tsx — Conversation list screen (sorted by last message, unread badges, FAB for new conversation)
- frontend/app/conversation/[id].tsx — Conversation screen (inverted FlatList, text input, send button, typing indicator)
- frontend/components/MessageBubble.tsx — Message bubble (sent vs received styles, sender name in groups, time, state checkmarks)
- frontend/components/ConversationItem.tsx — Conversation list item (avatar, name, last message preview, time, unread badge)
- frontend/services/websocket.ts — WebSocket client with auto-reconnect (exponential backoff), event handlers, connection state tracking

## Invariants

- Messages displayed chronologically by server timestamp (UTC)
- Message "sent" state only after server acknowledgment; optimistic UI must reconcile on failure
- Group conversations support 100+ participants
- Message content immutable after delivery; editing/deletion tracked for Phase 3
- All message delivery through server; clients never send directly to each other
- WebSocket authentication via token query parameter on connection
- Presence tracked in Redis; online/offline status broadcast to contacts on connect/disconnect

## Constraints

- Max text message length: 4096 characters
- Media attachments uploaded to S3 first, then media_url reference sent as message payload
- Typing indicators are ephemeral WebSocket events; never persisted to database
- Conversation list sorted by last message timestamp (most recent first)
- Unread count tracked server-side via last_read_at on ConversationParticipant
- Message pagination: 30 messages per request, cursor-based
- 1:1 conversation creation checks for existing conversation between the two users before creating new one
- message_type enum: text, video_circle, image

## WebSocket Events

Inbound (client → server): send_message, typing_start, typing_stop, message_read
Outbound (server → client): new_message, message_state_update, typing_indicator, presence_update

## Implementation Notes

- WebSocket connection manager tracks active connections per user_id (supports multiple devices)
- On WebSocket connect: user marked online in Redis, contacts notified of presence change
- On WebSocket disconnect: user last_seen updated, contacts notified of offline status
- Conversation creation: if participant_ids has 1 entry → 1:1 chat; if 2+ → group chat
- Frontend uses inverted FlatList for message display (newest at bottom, scroll up for history)
- Auto-reconnect WebSocket with exponential backoff in frontend/services/websocket.ts
