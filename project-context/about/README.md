# About

## TL;DR
- Cross-platform messenger: React Native + Expo (TypeScript) frontend, Python FastAPI backend
- State management: Zustand; navigation: Expo Router; token storage: expo-secure-store
- Backend: FastAPI + SQLAlchemy async (asyncpg) + PostgreSQL 15 + Redis 7 + Alembic migrations
- Infrastructure: Docker Compose (postgres, redis, backend services); all 3 platforms must maintain feature parity
- API base URLs resolve dynamically from browser hostname on web; env vars or localhost fallback on native

## Core Concepts

Monorepo Frontend (single React Native + Expo codebase for web, iOS, Android), Python FastAPI Backend, Platform Parity, Real-Time First (WebSocket)

## Key Files

- docker-compose.yml — Docker Compose orchestration (PostgreSQL 15, Redis 7, backend)
- backend/requirements.txt — Python dependencies (fastapi, sqlalchemy, asyncpg, python-jose, passlib, redis, websockets, boto3)
- backend/Dockerfile — Python 3.11-slim, uvicorn on port 8000
- backend/app/main.py — FastAPI app entry point (CORS, routers, startup/shutdown events, health check at GET /health)
- backend/app/config.py — pydantic-settings configuration (DATABASE_URL, REDIS_URL, SECRET_KEY, S3 settings, CORS_ORIGINS)
- backend/app/database.py — SQLAlchemy async engine, async sessionmaker, Base, get_db dependency
- backend/alembic.ini — Alembic migration configuration
- frontend/package.json — Expo + React Native dependencies
- frontend/app.json — Expo config (name: Messenger, platforms: ios, android, web)
- frontend/tsconfig.json — TypeScript strict mode
- frontend/app/_layout.tsx — Root layout with auth check and WebSocket setup
- frontend/services/api.ts — HTTP client with auth token injection and automatic 401 refresh
- frontend/services/config.ts — Dynamic API/WebSocket base URL resolution (uses window.location.hostname on web, env var or localhost fallback on native)
- .env.example — Environment variable template
- SETUP.md — Developer setup instructions

## Tech Stack (Implemented)

Frontend:
- React Native + Expo SDK (latest)
- TypeScript (strict mode)
- Expo Router (file-based routing with auth and main route groups)
- Zustand (state management — authStore, chatStore, contactStore)
- expo-secure-store (token persistence on mobile)
- WebSocket client with auto-reconnect and exponential backoff

Backend:
- Python 3.11+
- FastAPI + Uvicorn (ASGI)
- SQLAlchemy 2.0+ with async support (asyncpg driver)
- PostgreSQL 15 (primary database)
- Redis 7 (presence tracking, refresh token blacklist)
- Alembic (database migrations)
- python-jose[cryptography] (JWT tokens)
- passlib[bcrypt] (password hashing)
- boto3 (S3-compatible object storage, for future media uploads)
- WebSockets (real-time messaging and presence)

Infrastructure:
- Docker Compose (3 services: postgres, redis, backend)
- Nginx reverse proxy + SSL: [NOT YET CONFIGURED]
- CI/CD: [REQUIRES DECISION]

## Invariants

- All 3 platforms (web, iOS, Android) maintain feature parity at all times
- All date/time stored and transmitted in UTC; local timezone conversion in UI layer only
- All real-time communication through WebSocket; no direct peer-to-peer without server relay
- Backend is single source of truth; clients are untrusted
- All API endpoints prefixed and versioned (currently no version prefix; to be decided)

## Constraints

- Update this document when major dependency added or core architectural decision changes
- Tech choices override assumptions; flag unknowns as [REQUIRES DECISION]
- Target file size under 500 lines for all source files
