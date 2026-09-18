# Deployment

## TL;DR
- Production runs at https://raskolniktv.mooo.com/shmax/ on a shared Oracle VPS behind one nginx server block.
- The frontend pipeline builds on the developer machine and uploads static files to the VPS over SCP.
- The backend pipeline pulls source on the server via git and restarts the FastAPI process there.
- `git push` on main triggers `deploy.sh` via the pre-push hook, which can abort the push.
- Releases are immutable timestamped directories under `releases/`, cut over via an atomic symlink swap.
- `fetcher_shmax.sh` polls `git fetch origin` every 5 seconds on the VPS as the backend supervisor.
- In production only PostgreSQL and Redis run in Docker; the FastAPI backend runs natively.
- coturn TURN relay runs as a systemd service on the VPS for WebRTC NAT traversal (UDP/TCP 3478, TLS 5349).
- nginx routing and WebSocket proxy rules for `/shmax/` live in the separate `oracle_nginx_config` repository.

This route explains how shmax reaches production on the shared Oracle VPS: the push-triggered frontend release pipeline, the server-side git-polling backend supervisor, the containers policy, the coturn TURN relay, and the nginx reverse-proxy layer. It covers the mechanism, the rules, and the operator runbook.

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

### coturn TURN relay (@scripts/coturn/)
- coturn provides STUN and TURN relay for WebRTC calls that cannot connect peer-to-peer through restrictive NATs.
- coturn runs as a systemd service (`coturn.service`), not under the fetcher or any project supervisor.
- coturn listens on UDP/TCP 3478 (STUN + TURN) and TCP 5349 (TURNS over TLS).
- coturn reuses the Let's Encrypt certificate from `/etc/letsencrypt/live/raskolniktv.mooo.com/`; the `turnserver` user has ACL read access.
- Relay port range is restricted to UDP 49152–50175 (1024 ports) to minimize firewall surface.
- Bandwidth limits (`max-bps=1000000`, `total-quota=100`) prevent coturn from starving other services on the shared VPS.
- Credentials use the long-term-credential mechanism; username and password live only in `/etc/turnserver.conf`, never in the repo.
- Backend reads TURN credentials from env vars `TURN_SERVER_URL`, `TURN_SERVER_USERNAME`, `TURN_SERVER_CREDENTIAL` in the repo-root `.env`.
- `scripts/coturn/setup_coturn.sh` is a one-time root script that installs coturn, generates credentials, writes `/etc/turnserver.conf`, and enables the systemd service.
- Logs go to `/var/log/coturn/turnserver.log` with daily rotation (7 days retained).
- Oracle Cloud Security List and OS iptables must both allow the coturn ports; setup_coturn.sh prints the required iptables commands.
- coturn runs as a dedicated `turnserver` system user, never as root.

## Server Layout

```
/home/ubuntu/git/shmax/
  backend/                      # FastAPI app (app/main.py) pulled by fetcher_shmax.sh
  frontend/                     # Expo app; only its build output is deployed (via deploy.sh)
  releases/
    <timestamp>-<sha>/          # one frontend build per deploy
    current -> <timestamp>-<sha>/   # symlink deploy.sh repoints atomically
  venv/                         # created by nohup_fetcher_shmax.sh (repo-root venv)
  logs/
    fetcher_shmax.log
    api_shmax.log
  run/
    fetcher_shmax.pid
    api_shmax.pid
  .env                          # from .env.example, never committed
  deploy.sh
  fetcher_shmax.sh
  nohup_api_shmax.sh
  nohup_fetcher_shmax.sh
  docker-compose.prod.yml
```

## Operator Runbook

### One-time setup
1. Local: `sh scripts/enable-hooks.sh` (installs `scripts/hooks/pre-push` into `.git/hooks` and sets `core.hooksPath`).
2. Server: `git clone git@github.com:Reddidgy/shmax.git /home/ubuntu/git/shmax`, checked out on `main`.
3. Server: `cp .env.example .env` (repo root) and fill in `HOST`, `PORT`, `ROOT_PATH`, `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`, `DATABASE_URL`, `REDIS_URL`, and `SECRET_KEY` (generate with `openssl rand -hex 32`). See the "Production" section appended to `.env.example` for the exact keys and quoting rules.
4. Server: `docker compose -f docker-compose.prod.yml up -d` — starts `shmax-postgres` and `shmax-redis`.
5. Server: `./nohup_fetcher_shmax.sh` — creates `venv/` with the first Python >= 3.9 that has `venv` (on this VPS that's `python3.9`, since the default `python3` is 3.8), installs requirements, ensures the infra containers are up, and starts the API.
6. Local: the FIRST push that introduces these scripts must use `SKIP_BUILD=1 git push`, because the smoke check needs nginx and the API already live on the server before it can pass. After that, `git push` (or `./deploy.sh`) ships the first real frontend release.

### Everyday use
- Backend change: commit + push. `fetcher_shmax.sh` pulls within ~5 s, reinstalls `backend/requirements.txt` if it changed, and restarts `uvicorn` by PID.
- Frontend change: `git push` on `main` triggers the pre-push hook, which runs `deploy.sh` (build + upload + cutover + smoke check).
- `SKIP_BUILD=1 git push` — pushes without building/deploying the frontend.
- `./deploy.sh` — runs the frontend deploy manually (also usable with `DEPLOY_BRANCH=main`).

### Rollback
- Frontend: repoint the symlink to an older release, no restart needed:
  ```
  ln -s releases/<older-timestamp>-<sha> releases/current.tmp && mv -Tf releases/current.tmp releases/current
  ```
- Backend: `git revert` the offending commit and push `main`; `fetcher_shmax.sh` picks it up on its next poll.

### Operations
- Logs: `tail -f logs/fetcher_shmax.log logs/api_shmax.log` (on the server).
- Status: check `run/fetcher_shmax.pid` and `run/api_shmax.pid` against `ps`.
- Infra: `docker compose -f docker-compose.prod.yml ps` to check `shmax-postgres` / `shmax-redis`.
- Stop: stop the fetcher first (`kill "$(cat run/fetcher_shmax.pid)"`), then the API (`kill "$(cat run/api_shmax.pid)"`) — stopping the API alone gets it restarted by the fetcher within ~30 s.
- After a VPS reboot: the two containers (`restart: unless-stopped`) and coturn (`coturn.service`) autostart. `./nohup_fetcher_shmax.sh` must be re-run manually — there is no systemd unit or autostart for the fetcher or the API.

### TURN server (coturn) setup
```
ssh ubuntu@158.101.177.72
cd /home/ubuntu/git/shmax
sudo bash scripts/coturn/setup_coturn.sh
```
After running the script: open ports in Oracle Cloud Console (UDP 3478, TCP 3478, TCP 5349, UDP 49152–50175), open same ports in OS firewall (commands printed by script), add printed `TURN_SERVER_URL`/`TURN_SERVER_USERNAME`/`TURN_SERVER_CREDENTIAL`/`STUN_SERVERS` to `.env`, restart the API: `kill "$(cat run/api_shmax.pid)"`.

- Status: `systemctl status coturn`
- Logs: `tail -f /var/log/coturn/turnserver.log`
- Config: `/etc/turnserver.conf`
- Cert renewal: restart coturn after Let's Encrypt renewal if needed (`sudo systemctl restart coturn`)

### Manual backend commands on the server
`nohup_api_shmax.sh` sources the repo-root `.env` before starting uvicorn. A shell you opened yourself did not, so any command run straight from `backend/` falls back to the defaults baked into `backend/app/config.py` — including a wrong `DATABASE_URL` with password `messenger` instead of the random one in the repo-root `.env`. Fix:
```
cd /home/ubuntu/git/shmax
set -a; . ./.env; set +a
cd backend && ../venv/bin/python -c "from app.config import settings; print(settings.DATABASE_URL)"
```
Do not run `alembic upgrade head` on this deployment. `backend/alembic/versions/` is gitignored and empty; `init_db()` calls `Base.metadata.create_all` on FastAPI startup and is the only schema step that runs. Use the repo-root `venv/` that `fetcher_shmax.sh` creates; a hand-made `backend/venv/` is not used by any script in this pipeline.

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
- `backend/.env` does not exist on the server; it is gitignored, so the repo-root `.env` is the only source of production settings.
- A hand-opened shell does not source the repo-root `.env`, so backend commands run straight from `backend/` fall back to the defaults in `backend/app/config.py`.
- Those defaults carry the password `messenger`, while the container uses the random `POSTGRES_PASSWORD` from the repo-root `.env`, producing `asyncpg.exceptions.InvalidPasswordError`.
- Run `set -a; . ./.env; set +a` from the repo root before any manual backend command on the server.
- Only the repo-root `venv/` created by the fetcher is used by this pipeline; a hand-made `backend/venv/` is ignored by every script.

## Route-Specific Constraints
- `mktemp` creates `.release-meta.json` with mode `0600` by default, which made nginx return 403 and fail the smoke check even though the release was live; `deploy.sh` now chmods it `0644` before upload.
- `/shmax/` returns nginx error 500 ("rewrite or internal redirection cycle") whenever `releases/current` does not resolve; this is expected before the first successful cutover, not a config bug.
- The first-ever deploy must use `SKIP_BUILD=1 git push`, because the smoke check needs nginx and the release path already live.
- Never run `alembic upgrade head` on this deployment: `backend/alembic/versions/` is gitignored and empty, so there is no migration history to apply.
- `init_db()` calls `Base.metadata.create_all` on FastAPI startup and is the only schema-creation step in production.
- Retention default `RELEASE_KEEP_COUNT=5`; the active and previous releases are always exempt from deletion.
- Backend API defaults: `HOST=127.0.0.1`, `PORT=8100`, `ROOT_PATH=/shmax/api`.
- Backend restart grace period is a 10 second window between SIGTERM and SIGKILL.
- Backend crash-loop restart is throttled to at most once every 30 seconds.
- Fetcher poll interval is fixed at 5 seconds.
- Tail `logs/fetcher_shmax.log` and `logs/api_shmax.log` on the server to watch either pipeline.
- Check `run/fetcher_shmax.pid` and `run/api_shmax.pid` against `ps` for liveness.
- Server runtime state (`releases/`, `logs/`, `run/`, `venv/`, `.env`) is gitignored and never part of any pull or push.
- The two containers and coturn autostart after a VPS reboot; `./nohup_fetcher_shmax.sh` must be re-run manually for the fetcher and API.
- Frontend rollback is repointing `releases/current` at an older release directory and needs no API restart.
- Backend rollback is `git revert` plus `git push`; the fetcher picks it up on its next poll.
- A bracketed Praxis task id such as `[12-xmqm3o]` in a commit message auto-completes that task; use an unbracketed `12-xmqm3o:` prefix instead when the commit should not close the task.
