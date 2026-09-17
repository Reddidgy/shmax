from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
import uuid


@dataclass
class ActiveCall:
    call_id: str
    caller_id: str
    callee_id: str
    state: str  # "ringing", "connecting", "connected", "ended"
    created_at: datetime = field(default_factory=datetime.utcnow)


class CallService:
    def __init__(self):
        self.active_calls: Dict[str, ActiveCall] = {}
        self.user_calls: Dict[str, str] = {}  # user_id -> call_id

    def initiate_call(self, caller_id: str, callee_id: str) -> Optional[ActiveCall]:
        if caller_id in self.user_calls or callee_id in self.user_calls:
            return None
        call_id = str(uuid.uuid4())
        call = ActiveCall(
            call_id=call_id,
            caller_id=caller_id,
            callee_id=callee_id,
            state="ringing",
        )
        self.active_calls[call_id] = call
        self.user_calls[caller_id] = call_id
        self.user_calls[callee_id] = call_id
        return call

    def accept_call(self, call_id: str) -> Optional[ActiveCall]:
        call = self.active_calls.get(call_id)
        if call and call.state == "ringing":
            call.state = "connecting"
            return call
        return None

    def end_call(self, call_id: str) -> Optional[ActiveCall]:
        call = self.active_calls.pop(call_id, None)
        if call:
            call.state = "ended"
            self.user_calls.pop(call.caller_id, None)
            self.user_calls.pop(call.callee_id, None)
        return call

    def get_call(self, call_id: str) -> Optional[ActiveCall]:
        return self.active_calls.get(call_id)

    def get_user_call(self, user_id: str) -> Optional[ActiveCall]:
        call_id = self.user_calls.get(user_id)
        if call_id:
            return self.active_calls.get(call_id)
        return None


call_service = CallService()
