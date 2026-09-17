# Video Calls Route

## TL;DR
- WebRTC peer-to-peer media with FastAPI WebSocket signaling via backend/app/routers/websocket.py
- In-memory call state tracked by backend/app/services/call_service.py (no database persistence)
- Frontend WebRTC abstraction in frontend/services/webrtc.ts uses react-native-webrtc on native, browser APIs on web
- Call state managed by frontend/store/callStore.ts Zustand store with idle/outgoing/incoming/connecting/connected/ended states
- Call UI: frontend/components/CallScreen.tsx (full-screen) and frontend/components/IncomingCallOverlay.tsx (incoming notification)
- ICE candidates are buffered in webrtc.ts until remote SDP is set, then flushed

This route governs real-time 1:1 video calling, including signaling, media streams, and call state management.

## Purpose

Enable users to make real-time 1:1 video calls from any platform. Video calls are initiated from within a conversation via the header video call button. The call must connect quickly, handle network changes gracefully, and provide consistent quality across web, iOS, and Android.

## Core Concepts

- Signaling: SDP offers/answers and ICE candidates exchanged via the existing FastAPI WebSocket server. Signaling is server-relayed, never peer-to-peer.
- WebRTC Peer Connection: Once signaling completes, media flows directly between peers. The server is not in the media path unless TURN relay is needed.
- Call States: idle -> outgoing (caller) / incoming (callee) -> connecting -> connected -> ended. Each state has specific UI and timeout rules.
- STUN/TURN: Configured in backend/app/config.py via STUN_SERVERS, TURN_SERVER_URL, TURN_SERVER_USERNAME, TURN_SERVER_CREDENTIAL env vars. Google public STUN servers are defaults.
- ICE: The protocol that finds the best connection path between peers, trying direct, STUN-assisted, and TURN-relayed paths in order.
- Network Quality: Monitored via RTCStatsReport packet loss ratio — good (<2%), fair (<5%), poor (>=5%).
- CallService: In-memory singleton tracking active calls and user-to-call mappings. Prevents duplicate calls per user.

## Architecture

### Signaling Flow
1. Caller sends `call_initiate` via WebSocket with callee_id
2. Server checks callee is online, creates call via CallService, sends `incoming_call` to callee with caller info
3. Server sends `call_initiated` back to caller with call_id
4. Callee accepts: sends `call_accept`, server forwards `call_accepted` to caller
5. Caller creates WebRTC offer, sends `webrtc_offer` through server to callee
6. Callee creates answer, sends `webrtc_answer` through server to caller
7. ICE candidates exchanged bidirectionally via `ice_candidate` events
8. WebRTC peer connection established, media flows peer-to-peer

### WebSocket Event Types
- `call_initiate` — Caller starts a call (client -> server)
- `incoming_call` — Server notifies callee of incoming call (server -> client)
- `call_initiated` — Server confirms call_id to caller (server -> client)
- `call_accept` / `call_accepted` — Callee accepts (bidirectional relay)
- `call_decline` / `call_declined` — Callee declines (bidirectional relay)
- `call_end` / `call_ended` — Either party ends (bidirectional relay)
- `call_error` — Server reports error (e.g., user busy) (server -> client)
- `webrtc_offer` / `webrtc_answer` / `ice_candidate` — WebRTC negotiation relayed through server

### Cross-Platform WebRTC
- Native (iOS/Android): Uses `react-native-webrtc` package for RTCPeerConnection, mediaDevices, RTCView
- Web: Uses browser-native WebRTC APIs (RTCPeerConnection, navigator.mediaDevices) with HTML video elements
- Platform detection via `Platform.OS !== 'web'` conditional requires at runtime

## Invariants

- Signaling always goes through the server; peers never exchange signaling data directly
- TURN server must always be configured as a fallback; never assume direct connectivity will work
- A call that fails to connect within 30 seconds must timeout and end, not hang indefinitely
- Callee receives incoming_call WebSocket event immediately upon call initiation if online
- Camera and microphone permissions must be obtained before initiating or accepting a call
- Only one active call per user; CallService rejects concurrent call attempts
- WebSocket disconnect automatically ends any active call and notifies the other party
- ICE candidates arriving before remote SDP is set must be buffered and flushed after setRemoteDescription
- WebRTC 'disconnected' state allows 15-second recovery before ending the call; only 'failed' ends immediately
- Backend must verify callee has active WebSocket connections before creating a call; offline callee returns call_error
- Backend must notify the callee with call_ended if call_accept finds the call already ended

## Route-Specific Constraints

- v1 supports 1:1 video calls only; group calls are out of scope
- Voice-only calls are out of scope for v1; every call includes video (user can toggle camera off during the call)
- Call duration is unlimited; no artificial time cap
- Network quality indicator shows good/fair/poor based on WebRTC connection stats (packet loss ratio)
- Incoming call overlay displays caller name, avatar, accept/decline buttons; auto-declines after 30 seconds
- CallScreen renders as absolute overlay at root layout level (zIndex 1000)
- IncomingCallOverlay renders at root layout level (zIndex 999)
- CallService is in-memory only; call state is lost on server restart (acceptable for v1)
- CallKit (iOS) and ConnectionService (Android) integration deferred to future iteration
- Screen sharing deferred to v2
