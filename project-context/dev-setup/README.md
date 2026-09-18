# Developer Setup

## TL;DR
- Cross-platform messenger: React Native (Expo) frontend + Python FastAPI backend.
- Docker Compose handles PostgreSQL and Redis; backend and frontend run on the host.
- `docker-compose up -d` + `npm start` in `frontend/` gets a working local environment.
- Backend runs on http://localhost:8000, API docs at http://localhost:8000/docs.

Local development environment setup for the shmax messenger. Covers prerequisites, quick start with Docker, manual setup without Docker, database migrations, environment variables, and common troubleshooting.

## Prerequisites
- Docker and Docker Compose (for backend services)
- Python 3.11+ (for local backend development)
- Node.js 18+ and npm (for frontend)
- Expo CLI (`npm install -g expo-cli`)

## Quick Start with Docker

### Start backend services
```bash
cp .env.example .env
docker-compose up -d
docker-compose logs -f backend
```
The backend API will be available at http://localhost:8000. API docs: http://localhost:8000/docs.

### Start frontend
```bash
cd frontend
npm install
npm start
```
Then: `w` for web browser, `a` for Android emulator, `i` for iOS simulator, or scan QR with Expo Go.

## Local Development Setup (without Docker)

### Backend
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
PostgreSQL and Redis must be running. Update `DATABASE_URL` and `REDIS_URL` in `.env`.

### Frontend
```bash
cd frontend
npm install
npm start
```
API URL defaults to `localhost:8000`. Override via `frontend/.env`:
```
EXPO_PUBLIC_API_URL=http://localhost:8000
EXPO_PUBLIC_WS_URL=ws://localhost:8000
```

## Database Migrations
```bash
cd backend
alembic revision --autogenerate -m "description"   # create
alembic upgrade head                                 # apply
alembic downgrade -1                                 # rollback
```

## Environment Variables
See `.env.example` for all available configuration options. Key variables:
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection string
- `SECRET_KEY` — JWT signing key (change in production!)
- `CORS_ORIGINS` — Allowed frontend origins
- `EXPO_PUBLIC_API_URL` — Backend API URL (frontend)
- `EXPO_PUBLIC_WS_URL` — WebSocket URL (frontend)

## Troubleshooting

### Backend won't start
- Check PostgreSQL and Redis are running
- Verify `DATABASE_URL` and `REDIS_URL` in `.env`
- Run migrations: `alembic upgrade head`

### Frontend can't connect to backend
- Check `EXPO_PUBLIC_API_URL` is correct
- On physical device, use computer's local IP (not `localhost`)
- Ensure backend is accessible from your device

### WebSocket not connecting
- Check `EXPO_PUBLIC_WS_URL` uses `ws://` (not `http://`)
- Verify access token is valid
- Check backend WebSocket logs
