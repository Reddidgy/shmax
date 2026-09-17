# Video Messages Route

## TL;DR
- Video circles are short circular video clips recorded and played inline in chat conversations
- Max duration 60 seconds; recorded via expo-camera CameraView, uploaded to backend local filesystem
- Playback uses expo-av Video in a 200px circle; tap to play/unmute, progress bar at bottom
- Client-side caching downloads videos to device via expo-file-system on native; browser cache on web
- Key files: frontend/components/VideoCircleBubble.tsx, VideoCircleRecorder.tsx, services/videoCache.ts, backend/app/routers/media.py

This route governs the recording, sending, playback, and client-side caching of circular video messages.

## Purpose

Allow users to record and send short circular video messages (video circles) as a richer alternative to text. Video circles are a first-class message type — they appear inline in the conversation, play without leaving the chat, and work identically on web, iOS, and Android.

## Core Concepts

- Video Circle: A short video message displayed in a circular frame (200x200px) within the chat bubble
- Recording Flow: User taps the video circle button in the input bar → full-screen recorder overlay opens → tap to start recording → tap again to stop → preview with Send/Cancel → upload and send
- Storage: No cloud/S3 storage; videos upload to backend local filesystem (backend/uploads/) via POST /media/upload, served as static files at /uploads/
- Client-Side Caching: On native (iOS/Android), videos are downloaded to device cache via expo-file-system on first view; on web, browser HTTP cache is used; WeChat-style relay+cache model
- Recording on Web: Uses MediaRecorder API as fallback since expo-camera recordAsync is unreliable on web
- Upload Flow: FormData multipart POST to /media/upload → returns {url: "/uploads/<uuid>.mp4"} → send message with message_type=video_circle and media_url

## Key Files

- backend/app/routers/media.py — POST /media/upload endpoint; saves to backend/uploads/, 10MB cap, content-type allowlist
- backend/uploads/ — Local filesystem storage for uploaded media files (gitignored except .gitkeep)
- frontend/components/VideoCircleBubble.tsx — Circular video player: expo-av Video, thumbnail placeholder, play/pause, mute/unmute, animated progress bar, time badge
- frontend/components/VideoCircleRecorder.tsx — Full-screen recording overlay: CameraView (expo-camera/next), front/back toggle, 60s max, timer badge, preview-before-send
- frontend/services/videoCache.ts — Client-side video caching: downloads to cacheDirectory/video-circles/ on native, passthrough URL on web
- frontend/components/MessageBubble.tsx — Routes video_circle message type to VideoCircleBubble
- frontend/app/conversation/[id].tsx — Video circle button in input bar, recorder modal, upload+send handler
- frontend/store/chatStore.ts — sendMessage accepts optional mediaUrl/thumbnailUrl parameters

## Invariants

- Maximum recording duration is 60 seconds; the UI enforces this with a visible countdown and auto-stop
- Upload must complete before the message is sent; never send a message referencing a file that has not finished uploading
- Video circles use the normal chat message pipeline (message_type=video_circle); they do not bypass it
- The circular mask is a UI-layer concern (borderRadius); the actual video file is square
- Camera and microphone permissions must be granted before recording; graceful fallback UI if denied
- Client-side cache falls back to remote URL on download failure; cache miss never blocks playback

## Route-Specific Constraints

- Max file size: 10 MB enforced server-side at the upload endpoint
- Allowed upload content types: video/mp4, video/quicktime, video/webm, image/jpeg, image/png
- Camera defaults to front-facing with a toggle button to switch to rear
- expo-camera v14 import path: CameraView from 'expo-camera/next' (not package root)
- Web recording uses MediaRecorder API producing video/webm format
- Native recording uses CameraView.recordAsync producing video/mp4
- VideoCircleBubble default size is 200x200px; configurable via size prop
- Video plays muted by default; unmutes on first tap; pauses on second tap
