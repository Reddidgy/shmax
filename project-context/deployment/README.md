# Deployment

## TL;DR
- Production runs at https://raskolniktv.mooo.com/shmax/ on a shared Oracle VPS behind one nginx server block.
- The frontend pipeline builds on the developer machine and uploads static files to the VPS over SCP.
- The backend pipeline pulls source on the server via git and restarts the FastAPI process there.
- `git push` on main triggers `deploy.sh` via the pre-push hook, which can abort the push.
- Releases are immutable timestamped directories under `releases/`, cut over via an atomic symlink swap.
- `fetcher_shmax.sh` polls `git fetch origin` every 5 seconds on the VPS as the backend supervisor.
- In production only PostgreSQL and Redis run in Docker; the FastAPI backend runs natively.
- nginx routing and WebSocket proxy rules for `/shmax/` live in the separate `oracle_nginx_config` repository.

This route explains how shmax reaches production on the shared Oracle VPS: the push-triggered frontend release pipeline, the server-side git-polling backend supervisor, the containers policy, and the nginx reverse-proxy layer. It covers the mechanism and the rules behind each piece; exact operator commands live in `@DEPLOY.md`.

## Purpose
Give any agent or developer enough context to change, debug, or extend the deployment pipeline without breaking production, and to understand why the frontend and backend are deployed by two entirely different mechanisms on the same shared VPS.

## Core Concepts

### Why two pipelines instead of one
- The VPS hosts many unrelated projects behind a single nginx server; shmax owns only the `/shmax/` path prefix.
- Server checkout lives at `/home/ubuntu/git/shmax` on branch `main`, cloned from `git@github.com:Reddidgy/shmax.git`.
- The deployment pattern is a deliberate copy of the sibling project karaoker's pipeline on the same VPS.
- The Expo web export needs a Node toolchain, so the frontend must be built on the developer machine, not on the VPS.
- The FastAPI backend is cheaper to `git pull` and restart on the server than to rebuild and re-upload like the frontend.
- The frontend and backend pipelines never touch each other's artifacts.

### Frontend release pipeline (@deploy.sh, @scripts/hooks/pre-push)
- Hooks are per-clone and must be installed once with `sh scripts/enable-hooks.sh`.
- `deploy.sh` refuses to deploy any branch other than `main`.
- `SKIP_BUILD=1 git push` bypasses both the build and the smoke check entirely.
- The build command is `npx expo export --platform web --output-dir dist`, run from `frontend/`.
- `deploy.sh` exports `EXPO_BASE_URL=/shmax`, `EXPO_PUBLIC_API_URL=https://raskolniktv.mooo.com/shmax/api`, and `EXPO_PUBLIC_WS_URL=wss://raskolniktv.mooo.com/shmax/api` before building.
- `frontend/app.config.js` layers `experiments.baseUrl` onto `app.json` only when `EXPO_BASE_URL` is set, so local dev and native builds are unaffected.
- Expo inlines `EXPO_PUBLIC_*` variables at build time, so the API and WebSocket URLs are baked into the bundle and require a rebuild to change.
- Native (iOS/Android/desktop) production builds must set the same two `EXPO_PUBLIC_*` variables, or `frontend/services/config.ts` falls back to `localhost:8000`.
- `deploy.sh` deletes every `*.map` file after the build and asserts none remain, so source maps never ship.
- `deploy.sh` asserts `dist/index.html` references `/shmax/_expo/` as proof the base path was applied.
- `deploy.sh` asserts the built bundle literally contains the production API URL as proof `EXPO_PUBLIC_API_URL` was inlined.
- Releases are uploaded as immutable timestamped directories `releases/<UTC timestamp>-<short sha>/`, never mutated after upload.
- `releases/current` is repointed with a temp symlink plus `mv -Tf`, so no request ever sees a half-updated state.
- `.release-meta.json` is written per release and served publicly so the smoke check can prove which release is actually live.
- The smoke check fetches `/shmax/.release-meta.json` over HTTPS, requires it to name the new release, then loads the SPA root.
- On smoke-check failure `deploy.sh` repoints `releases/current` back to the previous release and exits non-zero.
- Retention keeps the newest `RELEASE_KEEP_COUNT` releases (default 5) and never deletes the active or the previous one.

### Backend git-polling supervisor (@fetcher_shmax.sh, @nohup_api_shmax.sh, @nohup_fetcher_shmax.sh)
- `fetcher_shmax.sh` polls `git fetch origin` every 5 seconds and pulls when the remote has moved.
- The fetcher hard-resets to origin only when the branch diverged, so server-local commits are discarded by design.
- The fetcher doubles as the process supervisor because this VPS has no systemd unit for the app.
- The API is restarted only when the pulled diff touches `backend/` or `nohup_api_shmax.sh`.
- Commits touching only docs, `.praxis` tasks, or `frontend/` leave the running API and its open WebSocket connections alone.
- The fetcher re-execs itself when a pull changes `fetcher_shmax.sh`, so the running supervisor always matches the latest code.
- A dead API is restarted automatically, at most once every 30 seconds, so a crash-looping build is not respawned every poll.
- API shutdown is SIGTERM first, then SIGKILL after a 10 second grace period.
- Before signaling a PID the fetcher verifies the process command line still contains `app.main:app`, guarding against a reused stale PID.
- `venv/` is created once by the fetcher at the repo root using the newest available Python >= 3.9 with venv and ensurepip.
- The VPS system `python3` is 3.8.10 without ensurepip, so the venv is built from `/usr/bin/python3.9`.
- Backend code must stay runnable on Python 3.9: no `match` statements, and no runtime PEP 604 unions without `from __future__ import annotations`.
- `nohup_api_shmax.sh` runs uvicorn from `backend/` as the working directory so pydantic-settings resolves `backend/.env`.
- The launcher uses `exec` so the recorded PID is the uvicorn process itself, not a wrapper shell.
- Defaults are `HOST=127.0.0.1`, `PORT=8100`, `ROOT_PATH=/shmax/api`; the API binds loopback only because nginx is the sole public entry point.
- uvicorn runs with `--root-path /shmax/api --proxy-headers --forwarded-allow-ips 127.0.0.1` so FastAPI generates correct absolute URLs behind the reverse proxy.
- The repo-root `.env` is sourced with plain shell semantics (`set -a` then `. ./.env`), so JSON values such as `CORS_ORIGINS` must be single-quoted or they will not parse.
- Exported values from the repo-root `.env` win over `backend/.env`, because python-dotenv never overrides an already-exported variable.
- The server `.env` is created manually once from `.env.example` and is never committed.
- No alembic step runs anywhere in the pipeline: `init_db()` calls `Base.metadata.create_all` on FastAPI startup.

### Containers policy (@docker-compose.prod.yml)
- On the VPS only PostgreSQL and Redis run in Docker, under compose project name `shmax`.
- Production container names are `shmax-postgres` and `shmax-redis`, distinct from local dev names so the shared VPS has no collision.
- Both containers publish on `127.0.0.1` only (`127.0.0.1:5432` and `127.0.0.1:6379`), never on a public interface.
- Both containers use `restart: unless-stopped`, making them the only part of the stack that survives a VPS reboot automatically.
- The FastAPI backend is never containerised in production; it runs natively from `venv/` under the fetcher.
- `docker-compose.prod.yml` has no backend service at all, unlike the local `docker-compose.yml`.
- `fetcher_shmax.sh` runs `docker compose -f docker-compose.prod.yml up -d` at startup and again whenever a pull changes the compose file.
- A docker compose failure logs an error but never aborts the fetcher, so the git poll loop and the API survive a temporary Docker outage.

### nginx reverse proxy (separate repository)
- nginx rules live in `/Users/rugarov/git/oracle_nginx_config`, file `src/current/raskolniktv.mooo.com.conf`, not in this repo.
- That file is symlinked into `/etc/nginx/sites-enabled` on the server; a fetcher there pulls, runs `nginx -t`, and reloads only if the syntax check passes.
- Deploying an nginx change is a git commit plus git push in that separate repo; the reload lands within a few minutes.
- `location = /shmax` returns a 301 to `/shmax/` so the app always runs under the trailing-slash prefix.
- `location /shmax/` aliases `releases/current/` and falls back to `/shmax/index.html`, because the Expo export is a single-entry SPA with client-side routing.
- `location ^~ /shmax/_expo/` serves the content-hashed bundles with a one-year immutable cache header.
- `location /shmax/api/` proxies to `http://127.0.0.1:8100/` with a trailing slash, stripping the prefix so FastAPI sees plain `/auth/*`, `/conversations/*`, and `/uploads/*` paths.
- `location = /shmax/api/ws` is a separate exact-match block carrying the WebSocket Upgrade headers for chat events and WebRTC call signalling.
- The WebSocket block hardcodes `Connection "upgrade"` because no `$connection_upgrade` map is defined at http level for this server.
- The WebSocket block disables buffering and extends read/send timeouts to 86400s for long-lived connections.
- `client_max_body_size 25m` on the API location gives headroom above the API's own 10 MB media upload cap.
- Media uploads are served back through the same `/shmax/api/` prefix, because the frontend builds media URLs as `API_BASE_URL` plus the `/uploads/...` path the API returns.

## Invariants
- Only main is deployable; `deploy.sh` refuses any other branch.
- A non-zero exit from `deploy.sh` aborts the `git push`, so a broken build never reaches the server.
- Releases are never mutated after upload; a new deploy always creates a new timestamped directory.
- `releases/current` is only ever repointed atomically; requests never observe a half-updated release.
- A failed smoke check always repoints `releases/current` back to the previous release automatically.
- The fetcher never restarts the API for changes outside `backend/` or `nohup_api_shmax.sh`, to avoid dropping live WebSocket connections needlessly.
- The fetcher discards server-local commits on divergence; the VPS checkout is never a source of truth.
- Backend code must remain Python 3.9 compatible because the production venv is pinned to `/usr/bin/python3.9`.
- The FastAPI backend is never containerised in production; only PostgreSQL and Redis run in Docker.
- Both production database containers bind to `127.0.0.1` only; neither is ever exposed publicly.
- A docker compose failure must never abort the fetcher's git poll loop or take down the API.
- `EXPO_PUBLIC_API_URL` and `EXPO_PUBLIC_WS_URL` are baked into the frontend bundle at build time and cannot be changed without a rebuild.
- Source maps must never ship; `deploy.sh` asserts none remain after deletion.
- Any file nginx serves from a release directory must be world-readable, because nginx runs as `www-data` while releases are owned by `ubuntu`.

## Route-Specific Constraints
- `mktemp` creates `.release-meta.json` with mode `0600` by default, which made nginx return 403 and fail the smoke check even though the release was live; `deploy.sh` now chmods it `0644` before upload.
- `/shmax/` returns nginx error 500 ("rewrite or internal redirection cycle") whenever `releases/current` does not resolve; this is expected before the first successful cutover, not a config bug.
- The first-ever deploy must use `SKIP_BUILD=1 git push`, because the smoke check needs nginx and the release path already live.
- Retention default `RELEASE_KEEP_COUNT=5`; the active and previous releases are always exempt from deletion.
- Backend API defaults: `HOST=127.0.0.1`, `PORT=8100`, `ROOT_PATH=/shmax/api`.
- Backend restart grace period is a 10 second window between SIGTERM and SIGKILL.
- Backend crash-loop restart is throttled to at most once every 30 seconds.
- Fetcher poll interval is fixed at 5 seconds.
- Tail `logs/fetcher_shmax.log` and `logs/api_shmax.log` on the server to watch either pipeline.
- Check `run/fetcher_shmax.pid` and `run/api_shmax.pid` against `ps` for liveness.
- Server runtime state (`releases/`, `logs/`, `run/`, `venv/`, `.env`) is gitignored and never part of any pull or push.
- Nothing except the two containers autostarts after a VPS reboot; `./nohup_fetcher_shmax.sh` must be re-run manually.
- Frontend rollback is repointing `releases/current` at an older release directory and needs no API restart.
- Backend rollback is `git revert` plus `git push`; the fetcher picks it up on its next poll.
- A bracketed Praxis task id such as `[12-xmqm3o]` in a commit message auto-completes that task; use an unbracketed `12-xmqm3o:` prefix instead when the commit should not close the task.
- `@DEPLOY.md` at the repo root is the operator runbook with the exact commands; this route explains the mechanism and the rules behind it.
