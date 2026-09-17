# Auth Route

## TL;DR
- JWT auth: 15-min access token + 30-day refresh token, bcrypt password hashing (direct bcrypt library)
- Backend endpoints: register, login, refresh, logout (Redis blacklist), me — all in backend/app/routers/auth.py
- Frontend: Zustand auth store with expo-secure-store (mobile) and localStorage (web) token persistence
- Token validation enforced on every API request via get_current_user dependency in backend/app/auth/security.py
- Bcrypt enforces a 72-byte password limit; hash_password and verify_password truncate input to 72 bytes

## Core Concepts

Registration, Sign-In, Token Refresh, Session, Sign-Out

## Key Files

- backend/app/routers/auth.py — Auth API endpoints (register, login, refresh, logout, me)
- backend/app/auth/security.py — Password hashing (bcrypt), JWT creation/decode (python-jose), get_current_user dependency
- backend/app/models/user.py — User SQLAlchemy model (id, username, email, phone, display_name, password_hash, is_active, last_seen)
- backend/app/schemas/auth.py — Pydantic schemas (UserRegister, UserLogin, TokenResponse, TokenRefresh, UserResponse)
- backend/app/config.py — SECRET_KEY, ALGORITHM (HS256), ACCESS_TOKEN_EXPIRE_MINUTES (15), REFRESH_TOKEN_EXPIRE_DAYS (30)
- frontend/store/authStore.ts — Zustand store: login, register, logout, refreshTokens, loadStoredTokens
- frontend/app/(auth)/login.tsx — Login screen UI
- frontend/app/(auth)/register.tsx — Registration screen UI
- frontend/services/config.ts — Dynamic API/WS base URL resolution (derives server address from window.location on web)

## Invariants

- Passwords stored as bcrypt hashes (direct bcrypt library); plaintext never persisted or logged
- Bcrypt 72-byte limit: passwords are truncated to 72 bytes before hashing and verification. Both hash_password() and verify_password() apply identical truncation to ensure consistency
- Access tokens expire in 15 minutes; refresh tokens expire in 30 days
- Refresh tokens stored in expo-secure-store on mobile, localStorage on web
- Logout invalidates refresh token by adding it to Redis blacklist (checked on every refresh attempt)
- All auth endpoints rate-limited (to be enforced in Phase 6)
- Token validation runs on every API request via FastAPI dependency injection (get_current_user)
- Token validation runs on WebSocket connect via token query parameter
- Registration requires unique username; email and phone are optional but unique if provided
- Backend owns all auth logic; client never makes auth decisions locally

## Constraints

- Auth flow identical across all 3 platforms (web, iOS, Android)
- Social login out of scope for v1 (credentials-based only)
- Email/phone verification: [REQUIRES DECISION]
- Password reset flow: [REQUIRES DECISION]
- Account deletion: [REQUIRES DECISION]

## Implementation Notes

- JWT algorithm: HS256 via python-jose[cryptography]
- Password hashing: direct bcrypt library (passlib removed due to incompatibility with bcrypt>=4.1)
- Token storage: expo-secure-store (iOS Keychain / Android EncryptedSharedPreferences), localStorage fallback for web
- Frontend auto-refreshes tokens on 401 response via API interceptor in frontend/services/api.ts
- User model uses UUID primary keys (server-generated)
- All timestamps stored in UTC

## CORS Configuration

The backend uses FastAPI's `CORSMiddleware` with two origin sources in `backend/app/main.py`:
- `allow_origins`: explicit list from `settings.CORS_ORIGINS` in `backend/app/config.py` (localhost variants)
- `allow_origin_regex`: pattern `^https?://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?$` for LAN access without env configuration

### Environment Variable Handling
When running via `docker-compose.yml`, `CORS_ORIGINS` is passed as a JSON string (e.g., `'["http://localhost:19006", "http://localhost:8081", "http://localhost:3000"]'`). A `field_validator` in the `Settings` class automatically parses this into a Python list.

Supported formats:
- **JSON array string**: `'["http://localhost:3000", "http://localhost:8081"]'`
- **Comma-separated string**: `"http://localhost:3000, http://localhost:8081"`
- **Python list** (when set programmatically): `["http://localhost:3000"]`

### Known Issues (Resolved)
- **CORS 500 on `/auth/register`**: Previously, the `CORS_ORIGINS` env var from `docker-compose.yml` was not parsed from its JSON string representation into a list, causing the CORS middleware to fail with a 500 error. Fixed by adding a `field_validator` to `Settings.CORS_ORIGINS`.

### Notes
- The `docker-compose.yml` default includes `http://localhost:19006` (Expo web), `http://localhost:8081` (Metro bundler), and `http://localhost:3000` (web dev server).
- `GET /favicon.ico` 500 errors in the browser console are caused by the absence of a static favicon route — this is cosmetic and not a functional bug.
- Console errors from `bootstrap-autofill-overlay.js` are caused by browser extensions (e.g., password managers) and are not application bugs.
- LAN access (192.168.x.x) is handled by `allow_origin_regex` in `backend/app/main.py` — no CORS_ORIGINS update needed for local network clients.
