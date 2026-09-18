from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
from typing import Dict, Set
from uuid import UUID
import json
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db, async_session_maker
from app.auth.security import decode_token
from app.services.presence_service import set_online, set_offline
from app.services.call_service import call_service
from app.models.user import User
from sqlalchemy import select
from app.config import settings

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        # Map user_id to set of WebSocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)
        await set_online(user_id)

    async def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            self.active_connections[user_id].discard(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
                await set_offline(user_id)
                # Update last_seen in database
                async with async_session_maker() as db:
                    result = await db.execute(select(User).where(User.id == user_id))
                    user = result.scalar_one_or_none()
                    if user:
                        user.last_seen = datetime.utcnow()
                        await db.commit()

    async def send_personal_message(self, message: dict, user_id: str):
        """Send message to all connections of a specific user"""
        if user_id in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_json(message)
                except:
                    disconnected.add(connection)
            # Clean up disconnected connections
            for connection in disconnected:
                self.active_connections[user_id].discard(connection)

    async def broadcast_to_conversation(self, message: dict, participant_ids: list[str], exclude_user: str = None):
        """Broadcast message to all participants in a conversation"""
        for user_id in participant_ids:
            if exclude_user and user_id == exclude_user:
                continue
            await self.send_personal_message(message, user_id)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...)
):
    """WebSocket endpoint for real-time communication"""
    # Authenticate
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=1008, reason="Invalid token type")
            return
        user_id = payload.get("sub")
        if not user_id:
            await websocket.close(code=1008, reason="Invalid token")
            return
    except:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    # Connect
    await manager.connect(websocket, user_id)

    try:
        # Notify contacts of online status
        async with async_session_maker() as db:
            # Get user's contacts to notify them
            from app.models.contact import Contact
            result = await db.execute(
                select(Contact.contact_user_id).where(Contact.user_id == user_id)
            )
            contact_ids = [str(row[0]) for row in result.all()]

        # Broadcast presence update
        presence_event = {
            "type": "presence_update",
            "user_id": user_id,
            "status": "online",
            "timestamp": datetime.utcnow().isoformat()
        }
        for contact_id in contact_ids:
            await manager.send_personal_message(presence_event, contact_id)

        # Handle incoming messages
        while True:
            data = await websocket.receive_text()
            event = json.loads(data)

            event_type = event.get("type")

            if event_type == "send_message":
                # Message sending is handled via HTTP POST, not WebSocket
                # This is just for broadcasting
                pass

            elif event_type == "typing_start":
                conversation_id = event.get("conversation_id")
                # Broadcast to conversation participants
                async with async_session_maker() as db:
                    from app.models.conversation_participant import ConversationParticipant
                    result = await db.execute(
                        select(ConversationParticipant.user_id)
                        .where(ConversationParticipant.conversation_id == conversation_id)
                    )
                    participant_ids = [str(row[0]) for row in result.all()]

                typing_event = {
                    "type": "typing_indicator",
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "is_typing": True
                }
                await manager.broadcast_to_conversation(
                    typing_event,
                    participant_ids,
                    exclude_user=user_id
                )

            elif event_type == "typing_stop":
                conversation_id = event.get("conversation_id")
                async with async_session_maker() as db:
                    from app.models.conversation_participant import ConversationParticipant
                    result = await db.execute(
                        select(ConversationParticipant.user_id)
                        .where(ConversationParticipant.conversation_id == conversation_id)
                    )
                    participant_ids = [str(row[0]) for row in result.all()]

                typing_event = {
                    "type": "typing_indicator",
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "is_typing": False
                }
                await manager.broadcast_to_conversation(
                    typing_event,
                    participant_ids,
                    exclude_user=user_id
                )

            elif event_type == "message_read":
                message_id = event.get("message_id")
                conversation_id = event.get("conversation_id")

                # Update message state
                async with async_session_maker() as db:
                    from app.services.chat_service import update_message_state
                    from app.models.message import MessageState, Message
                    from app.models.conversation_participant import ConversationParticipant

                    # Update last_read_at for the user
                    result = await db.execute(
                        select(ConversationParticipant)
                        .where(
                            ConversationParticipant.conversation_id == conversation_id,
                            ConversationParticipant.user_id == user_id
                        )
                    )
                    participant = result.scalar_one_or_none()
                    if participant:
                        participant.last_read_at = datetime.utcnow()
                        await db.commit()

                    # Get conversation participants
                    result = await db.execute(
                        select(ConversationParticipant.user_id)
                        .where(ConversationParticipant.conversation_id == conversation_id)
                    )
                    participant_ids = [str(row[0]) for row in result.all()]

                # Broadcast read state
                read_event = {
                    "type": "message_state_update",
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "state": "read",
                    "user_id": user_id
                }
                await manager.broadcast_to_conversation(
                    read_event,
                    participant_ids
                )

            elif event_type == "call_initiate":
                callee_id = event.get("callee_id")
                print(f"[CALL] {user_id} initiating call to {callee_id}")

                if callee_id not in manager.active_connections:
                    print(f"[CALL] Callee {callee_id} is offline")
                    await manager.send_personal_message({
                        "type": "call_error",
                        "error": "User is unavailable",
                    }, user_id)
                else:
                    call = call_service.initiate_call(user_id, callee_id)
                    if call:
                        async with async_session_maker() as db:
                            result = await db.execute(select(User).where(User.id == user_id))
                            caller_user = result.scalar_one_or_none()

                        if caller_user:
                            await manager.send_personal_message({
                                "type": "incoming_call",
                                "call_id": call.call_id,
                                "caller": {
                                    "id": str(caller_user.id),
                                    "display_name": caller_user.display_name,
                                    "avatar_url": caller_user.avatar_url,
                                },
                                "ice_servers": settings.get_ice_servers(),
                            }, callee_id)

                            await manager.send_personal_message({
                                "type": "call_initiated",
                                "call_id": call.call_id,
                                "ice_servers": settings.get_ice_servers(),
                            }, user_id)
                        else:
                            print(f"[CALL] Caller user not found in DB: {user_id}")
                            call_service.end_call(call.call_id)
                    else:
                        print(f"[CALL] Call initiation failed (user busy)")
                        await manager.send_personal_message({
                            "type": "call_error",
                            "error": "User is busy",
                        }, user_id)

            elif event_type == "call_accept":
                call_id = event.get("call_id")
                call = call_service.accept_call(call_id)
                if call:
                    await manager.send_personal_message({
                        "type": "call_accepted",
                        "call_id": call.call_id,
                    }, call.caller_id)
                else:
                    await manager.send_personal_message({
                        "type": "call_ended",
                        "call_id": call_id,
                    }, user_id)

            elif event_type == "call_decline":
                call_id = event.get("call_id")
                call = call_service.end_call(call_id)
                if call:
                    other_id = call.caller_id if user_id == call.callee_id else call.callee_id
                    await manager.send_personal_message({
                        "type": "call_declined",
                        "call_id": call_id,
                    }, other_id)

            elif event_type == "call_end":
                call_id = event.get("call_id")
                call = call_service.end_call(call_id)
                if call:
                    other_id = call.caller_id if user_id == call.callee_id else call.callee_id
                    await manager.send_personal_message({
                        "type": "call_ended",
                        "call_id": call_id,
                    }, other_id)

            elif event_type == "webrtc_offer":
                call_id = event.get("call_id")
                call = call_service.get_call(call_id)
                if call:
                    target_id = call.callee_id if user_id == call.caller_id else call.caller_id
                    await manager.send_personal_message({
                        "type": "webrtc_offer",
                        "call_id": call_id,
                        "sdp": event.get("sdp"),
                    }, target_id)

            elif event_type == "webrtc_answer":
                call_id = event.get("call_id")
                call = call_service.get_call(call_id)
                if call:
                    target_id = call.callee_id if user_id == call.caller_id else call.caller_id
                    await manager.send_personal_message({
                        "type": "webrtc_answer",
                        "call_id": call_id,
                        "sdp": event.get("sdp"),
                    }, target_id)

            elif event_type == "ice_candidate":
                call_id = event.get("call_id")
                call = call_service.get_call(call_id)
                if call:
                    target_id = call.callee_id if user_id == call.caller_id else call.caller_id
                    await manager.send_personal_message({
                        "type": "ice_candidate",
                        "call_id": call_id,
                        "candidate": event.get("candidate"),
                    }, target_id)

    except WebSocketDisconnect:
        await manager.disconnect(websocket, user_id)

        # End any active call
        active_call = call_service.get_user_call(user_id)
        if active_call:
            ended_call = call_service.end_call(active_call.call_id)
            if ended_call:
                other_id = ended_call.caller_id if user_id == ended_call.callee_id else ended_call.callee_id
                await manager.send_personal_message({
                    "type": "call_ended",
                    "call_id": ended_call.call_id,
                }, other_id)

        # Notify contacts of offline status
        async with async_session_maker() as db:
            from app.models.contact import Contact
            result = await db.execute(
                select(Contact.contact_user_id).where(Contact.user_id == user_id)
            )
            contact_ids = [str(row[0]) for row in result.all()]

        presence_event = {
            "type": "presence_update",
            "user_id": user_id,
            "status": "offline",
            "timestamp": datetime.utcnow().isoformat()
        }
        for contact_id in contact_ids:
            await manager.send_personal_message(presence_event, contact_id)

    except Exception as e:
        print(f"WebSocket error: {e}")
        await manager.disconnect(websocket, user_id)

        active_call = call_service.get_user_call(user_id)
        if active_call:
            ended_call = call_service.end_call(active_call.call_id)
            if ended_call:
                other_id = ended_call.caller_id if user_id == ended_call.callee_id else ended_call.callee_id
                await manager.send_personal_message({
                    "type": "call_ended",
                    "call_id": ended_call.call_id,
                }, other_id)
