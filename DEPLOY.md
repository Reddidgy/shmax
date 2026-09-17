# Deploy

## Overview

Push to `main` -> the `pre-push` hook runs `deploy.sh`, which builds the frontend (Expo web export)
locally and SCPs it to `releases/<timestamp>-<sha>` on `ubuntu@158.101.177.72`, atomically repoints
`releases/current`, and smoke-checks `https://raskolniktv.mooo.com/shmax/.release-meta.json`
(auto-rolling back to the previous release on failure). Retention keeps the 5 newest releases; the
active and previous (rollback) release are always protected.

Backend deploys separately: `fetcher_shmax.sh` runs continuously on the server, polling git every
5 seconds. On a change it pulls (or hard-resets if diverged); if the change touched `backend/` or
`nohup_api_shmax.sh`, it runs `venv/bin/pip install -r backend/requirements.txt` and restarts the
API by PID. It also restarts the API if it dies (30 s back-off), re-runs
`docker compose -f docker-compose.prod.yml up -d` when a pull touches that file, and re-execs
itself if `fetcher_shmax.sh` itself changed.

**Only postgres and redis run in Docker on the VPS** (`docker-compose.prod.yml`, containers
`shmax-postgres` / `shmax-redis`, both bound to `127.0.0.1`). The FastAPI backend is NOT
containerised — it runs natively from `venv/` under `fetcher_shmax.sh`, same as karaoker's Public
API pattern.

Nginx: the `location` blocks live in the separate `oracle_nginx_config` repo
(`src/current/raskolniktv.mooo.com.conf`) — `/shmax/` serves the static release,
`/shmax/api/` proxies to `127.0.0.1:8100` with the prefix stripped, and `/shmax/api/ws` proxies the
WebSocket. This is not part of the push pipeline for this repo; changes to that file deploy via a
`git push` in the `oracle_nginx_config` repo.

## What is deployed where

- Frontend: static Expo web export, served by nginx from `/home/ubuntu/git/shmax/releases/current`
  at `https://raskolniktv.mooo.com/shmax/`.
- API: FastAPI (`uvicorn app.main:app`), running natively on `127.0.0.1:8100`, proxied by nginx
  under `https://raskolniktv.mooo.com/shmax/api/`.
- WebSocket: `wss://raskolniktv.mooo.com/shmax/api/ws`, proxied to `127.0.0.1:8100/ws`.
- Data services: postgres + redis, containerised via `docker-compose.prod.yml`, bound to
  `127.0.0.1` (never exposed publicly — nginx and the API are the only consumers).

## Server layout

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

## One-time setup

1. Local: `sh scripts/enable-hooks.sh` (installs `scripts/hooks/pre-push` into `.git/hooks` and
   sets `core.hooksPath`).
2. Server: `git clone git@github.com:Reddidgy/shmax.git /home/ubuntu/git/shmax`, checked out on
   `main`.
3. Server: `cp .env.example .env` (repo root) and fill in `HOST`, `PORT`, `ROOT_PATH`,
   `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`, `DATABASE_URL`, `REDIS_URL`, and `SECRET_KEY`
   (generate with `openssl rand -hex 32`). See the "Production" section appended to
   `.env.example` for the exact keys and quoting rules.
4. Server: `docker compose -f docker-compose.prod.yml up -d` — starts `shmax-postgres` and
   `shmax-redis`.
5. Server: `./nohup_fetcher_shmax.sh` — creates `venv/` with the first Python >= 3.9 that has
   `venv` (on this VPS that's `python3.9`, since the default `python3` is 3.8), installs
   requirements, ensures the infra containers are up, and starts the API.
6. Local: the FIRST push that introduces these scripts must use `SKIP_BUILD=1 git push`, because
   the smoke check needs nginx and the API already live on the server before it can pass. After
   that, `git push` (or `./deploy.sh`) ships the first real frontend release.

## Everyday use

- Backend change: commit + push. `fetcher_shmax.sh` pulls within ~5 s, reinstalls
  `backend/requirements.txt` if it changed, and restarts `uvicorn` by PID.
- Frontend change: `git push` on `main` triggers the pre-push hook, which runs `deploy.sh`
  (build + upload + cutover + smoke check).
- `SKIP_BUILD=1 git push` — pushes without building/deploying the frontend.
- `./deploy.sh` — runs the frontend deploy manually (also usable with `DEPLOY_BRANCH=main`).

## Rollback

- Frontend: repoint the symlink to an older release, no restart needed:
  ```
  ln -s releases/<older-timestamp>-<sha> releases/current.tmp && mv -Tf releases/current.tmp releases/current
  ```
- Backend: `git revert` the offending commit and push `main`; `fetcher_shmax.sh` picks it up on
  its next poll.

## Operations

- Logs: `tail -f logs/fetcher_shmax.log logs/api_shmax.log` (on the server).
- Status: check `run/fetcher_shmax.pid` and `run/api_shmax.pid` against `ps`.
- Infra: `docker compose -f docker-compose.prod.yml ps` to check `shmax-postgres` / `shmax-redis`.
- Stop: stop the fetcher first (`kill "$(cat run/fetcher_shmax.pid)"`), then the API
  (`kill "$(cat run/api_shmax.pid)"`) — stopping the API alone gets it restarted by the fetcher
  within ~30 s.
- After a VPS reboot: nothing autostarts except the two containers (`restart: unless-stopped`).
  `./nohup_fetcher_shmax.sh` must be re-run manually — there is no systemd unit or autostart for
  the fetcher or the API.

## Nginx

Rules live in the separate repo `/Users/rugarov/git/oracle_nginx_config`
(`src/current/raskolniktv.mooo.com.conf`). Changes there deploy via a `git push` in that repo, not
via anything in this repo.

## Praxis commit-hook gotcha

When a commit message references a Praxis task id, use an unbracketed `12-xmqm3o:` prefix (e.g.
`12-xmqm3o: add VPS deploy scripts`), never `[12-xmqm3o]` — the bracketed form is interpreted by
the Praxis commit hook as "close this task" and auto-completes it.

## Local development

The dev server stays unaffected — `frontend/app.config.js` only sets `experiments.baseUrl` when
`EXPO_BASE_URL` is set in the environment (which `deploy.sh` does for production builds only).
Local dev and native builds leave `EXPO_BASE_URL` unset and behave exactly as before.
