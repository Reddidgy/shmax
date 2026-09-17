# Messenger Application - Setup Guide

This is a cross-platform messenger application built with React Native (Expo) for the frontend and Python FastAPI for the backend.

## Prerequisites

- Docker and Docker Compose (for backend services)
- Python 3.11+ (for local backend development)
- Node.js 18+ and npm (for frontend)
- Expo CLI (`npm install -g expo-cli`)

## Quick Start with Docker

### 1. Start Backend Services

```bash
# Copy environment variables
cp .env.example .env

# Start all services (PostgreSQL, Redis, Backend API)
docker-compose up -d

# Check logs
docker-compose logs -f backend
```

The backend API will be available at http://localhost:8000

API docs: http://localhost:8000/docs

### 2. Setup Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start Expo development server
npm start
```

Then:
- Press `w` to open in web browser
- Press `a` to open Android emulator (requires Android Studio)
- Press `i` to open iOS simulator (requires Xcode, macOS only)
- Scan QR code with Expo Go app on your phone

## Local Development Setup (without Docker)

### Backend Setup

```bash
cd backend

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip3 install -r requirements.txt

# Setup PostgreSQL and Redis (must be running)
# Update DATABASE_URL and REDIS_URL in .env

# Run migrations
alembic upgrade head

# Start development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Set API URL (optional, defaults to localhost:8000)
# Create frontend/.env file:
# EXPO_PUBLIC_API_URL=http://localhost:8000
# EXPO_PUBLIC_WS_URL=ws://localhost:8000

# Start development server
npm start
```

## Database Migrations

```bash
cd backend

# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback migration
alembic downgrade -1
```

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── models/          # Database models
│   │   ├── schemas/         # Pydantic schemas
│   │   ├── routers/         # API endpoints
│   │   ├── auth/            # Authentication logic
│   │   ├── services/        # Business logic
│   │   ├── config.py        # Settings
│   │   ├── database.py      # Database connection
│   │   └── main.py          # FastAPI app
│   ├── alembic/             # Database migrations
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── app/                 # Expo Router screens
│   │   ├── (auth)/          # Login, Register
│   │   ├── (main)/          # Chats, Contacts, Profile
│   │   └── conversation/    # Chat screen
│   ├── components/          # Reusable components
│   ├── services/            # API client, WebSocket
│   ├── store/               # Zustand state management
│   └── package.json
├── docker-compose.yml
└── .env.example
```

## Features Implemented

### Phase 1 - Foundation ✓
- Backend scaffolding with FastAPI
- Frontend scaffolding with React Native + Expo
- WebSocket infrastructure
- Database models and migrations
- Navigation shell with tab-based routing

### Phase 2 - Auth & Contacts ✓
- User registration with username/password
- User sign-in with JWT tokens (access + refresh)
- Token refresh and secure storage
- User search by username
- Contact list management (add/remove)
- User profile view

### Phase 3 - Core Chat (Partial)
- 1:1 conversations
- Real-time message delivery via WebSocket
- Message states (sent/delivered/read)
- Typing indicators
- Conversation list with unread counts
- Message pagination

## Testing

### Backend

```bash
cd backend
pytest
```

### Frontend

```bash
cd frontend
npm test
```

## API Endpoints

### Authentication
- `POST /auth/register` - Register new user
- `POST /auth/login` - Login user
- `POST /auth/refresh` - Refresh token
- `POST /auth/logout` - Logout user
- `GET /auth/me` - Get current user

### Contacts
- `GET /contacts` - List contacts
- `POST /contacts` - Add contact
- `DELETE /contacts/{id}` - Remove contact
- `GET /contacts/search` - Search users

### Chat
- `GET /conversations` - List conversations
- `POST /conversations` - Create conversation
- `GET /conversations/{id}` - Get conversation
- `GET /conversations/{id}/messages` - Get messages
- `POST /conversations/{id}/messages` - Send message

### WebSocket
- `WS /ws?token={access_token}` - WebSocket connection

## Environment Variables

See `.env.example` for all available configuration options.

Key variables:
- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string
- `SECRET_KEY` - JWT signing key (change in production!)
- `CORS_ORIGINS` - Allowed frontend origins
- `EXPO_PUBLIC_API_URL` - Backend API URL (frontend)
- `EXPO_PUBLIC_WS_URL` - WebSocket URL (frontend)

## Troubleshooting

### Backend won't start
- Check PostgreSQL and Redis are running
- Verify DATABASE_URL and REDIS_URL in .env
- Run migrations: `alembic upgrade head`

### Frontend can't connect to backend
- Check EXPO_PUBLIC_API_URL is correct
- On physical device, use computer's local IP (not localhost)
- Ensure backend is accessible from your device

### WebSocket not connecting
- Check EXPO_PUBLIC_WS_URL uses `ws://` (not `http://`)
- Verify access token is valid
- Check backend WebSocket logs

## Next Steps

- [ ] Implement group conversations
- [ ] Add message media attachments
- [ ] Implement video circles (Phase 4)
- [ ] Add video calling (Phase 5)
- [ ] Push notifications
- [ ] End-to-end encryption
