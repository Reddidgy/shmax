# Operations

## TL;DR
- Documents critical operational procedures for the messenger application infrastructure.
- Database runs on PostgreSQL 15 via Docker container `messenger-postgres`.
- Redis cache runs via Docker container `messenger-redis`.
- All operations target Docker containers, not direct host services.

Runbook for important operational procedures — database management, cache clearing, environment reset, and other infrastructure actions that agents or developers may need to perform.

## Purpose
Provide a single reference for repeatable operational procedures so they are executed consistently and safely. Each procedure documents the exact command, prerequisites, and impact.

## Procedures

### Full Data Wipe (keep schema)
Removes all user data while preserving table structure and Alembic migrations.

Prerequisites: Docker containers `messenger-postgres` and `messenger-redis` must be running.

Steps:
1. Truncate all data tables with cascade:
```
docker exec messenger-postgres psql -U messenger -d messenger -c "TRUNCATE TABLE messages, conversation_participants, conversations, contacts, users CASCADE;"
```
2. Flush Redis:
```
docker exec messenger-redis redis-cli FLUSHALL
```

Impact: All users, conversations, messages, and contacts are permanently deleted. Schema and migrations are preserved. Active sessions are invalidated.

Tables affected: `users`, `contacts`, `conversations`, `conversation_participants`, `messages`.
Tables preserved: `alembic_version`.

### Full Database Reset (drop + recreate)
Drops all data and re-runs migrations from scratch.

Prerequisites: Docker Compose services must be running.

Steps:
1. Drop and recreate the database:
```
docker exec messenger-postgres psql -U messenger -d messenger -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
```
2. Re-run migrations:
```
docker exec messenger-backend sh -c "alembic upgrade head"
```
3. Flush Redis:
```
docker exec messenger-redis redis-cli FLUSHALL
```

Impact: Complete reset — all data and schema destroyed, then rebuilt from migration history.

## Invariants
- Always flush Redis after any database wipe to prevent stale session/cache references.
- Never truncate or drop `alembic_version` during a data-only wipe — it tracks migration state.
- Verify containers are running (`docker ps`) before executing any operation.

## Route-Specific Constraints
- All procedures assume the Docker Compose stack from `docker-compose.yml` at repository root.
- Database credentials: user `messenger`, password `messenger`, database `messenger`.
- Connection string: `postgresql+asyncpg://messenger:messenger@postgres:5432/messenger`.
