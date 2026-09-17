#!/usr/bin/env bash

set -euo pipefail

# Frontend deploy for shmax (same pattern as karaoker/deploy.sh).
# Builds frontend/ locally (Expo web export), uploads dist/ to a timestamped release directory on
# the Oracle VPS, atomically repoints releases/current (served by nginx under /shmax/), smoke-checks
# the live URL (rolling back on failure) and prunes old releases. Backend code is NOT deployed here:
# fetcher_shmax.sh on the server pulls it from git and restarts the API.
#
# Invoked by the pre-push hook as:  DEPLOY_BRANCH=<branch> ./deploy.sh
# When run manually it falls back to the currently checked-out branch. Only main is deployable.

cd "$(dirname "${BASH_SOURCE[0]}")"

DEPLOY_BRANCH="${DEPLOY_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'unknown')}"
DEPLOY_ENV="production"

case "${DEPLOY_BRANCH}" in
  main) ;;
  *)
    printf '[deploy] ERROR: refusing to deploy branch "%s". Only main is deployable.\n' "${DEPLOY_BRANCH}" >&2
    exit 1
    ;;
esac

REMOTE_USER="ubuntu"
REMOTE_HOST="158.101.177.72"
REMOTE_ROOT="/home/ubuntu/git/shmax"
REMOTE_RELEASES_DIR="${REMOTE_ROOT}/releases"
REMOTE_CURRENT_LINK="${REMOTE_RELEASES_DIR}/current"
REMOTE_SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"
SSH_OPTS="-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o BatchMode=yes"
RELEASE_KEEP_COUNT="${RELEASE_KEEP_COUNT:-5}"
FRONTEND_DIR="frontend"
DIST_DIR="${FRONTEND_DIR}/dist"
BASE_PATH="/shmax/"
SMOKE_BASE_URL="${DEPLOY_SMOKE_BASE_URL:-https://raskolniktv.mooo.com/shmax}"
SMOKE_TIMEOUT_SECONDS="${DEPLOY_SMOKE_TIMEOUT_SECONDS:-20}"

log() {
  printf '[deploy:%s] %s\n' "${DEPLOY_ENV}" "$1"
}

fail() {
  printf '[deploy:%s] ERROR: %s\n' "${DEPLOY_ENV}" "$1" >&2
  exit 1
}

require_command() {
  local command_name="$1"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    fail "${command_name} is not installed or not in PATH."
  fi
}

run_remote_script() {
  local remote_script="$1"

  if ! printf '%s\n' "${remote_script}" | ssh ${SSH_OPTS} "${REMOTE_SSH_TARGET}" "bash -seuo pipefail"; then
    fail "Remote command failed on ${REMOTE_SSH_TARGET}."
  fi
}

run_remote_capture() {
  local remote_script="$1"
  local remote_output

  if ! remote_output="$(
    printf '%s\n' "${remote_script}" | ssh ${SSH_OPTS} "${REMOTE_SSH_TARGET}" "bash -seuo pipefail"
  )"; then
    fail "Remote command failed on ${REMOTE_SSH_TARGET}."
  fi

  printf '%s' "${remote_output}"
}

# Passes when the live site serves the given release: its .release-meta.json names that release
# (proves the symlink cutover is what nginx serves) and the SPA entry point loads.
smoke_check() {
  local expected_release_id="$1"
  local release_meta

  if ! release_meta="$(
    curl --fail --silent --show-error --location --max-time "${SMOKE_TIMEOUT_SECONDS}" \
      -H 'Cache-Control: no-cache' "${SMOKE_BASE_URL}/.release-meta.json"
  )"; then
    return 1
  fi

  case "${release_meta}" in
    *"\"release_id\": \"${expected_release_id}\""*) ;;
    *) return 1 ;;
  esac

  curl --fail --silent --show-error --location --max-time "${SMOKE_TIMEOUT_SECONDS}" \
    -H 'Cache-Control: no-cache' "${SMOKE_BASE_URL}/" >/dev/null
}

require_command npm
require_command npx
require_command ssh
require_command scp
require_command curl
require_command git
require_command find
require_command du
require_command date

# Pre-flight SSH connectivity check with retry (fail fast before expensive build)
SSH_MAX_RETRIES="${SSH_MAX_RETRIES:-3}"
SSH_RETRY_DELAY="${SSH_RETRY_DELAY:-5}"
log "Checking SSH connectivity to ${REMOTE_SSH_TARGET}..."
ssh_attempt=0
while [ "${ssh_attempt}" -lt "${SSH_MAX_RETRIES}" ]; do
  ssh_attempt=$((ssh_attempt + 1))
  if ssh ${SSH_OPTS} "${REMOTE_SSH_TARGET}" "echo ok" >/dev/null 2>&1; then
    log "SSH connectivity confirmed."
    break
  fi
  if [ "${ssh_attempt}" -lt "${SSH_MAX_RETRIES}" ]; then
    log "SSH attempt ${ssh_attempt}/${SSH_MAX_RETRIES} failed. Retrying in ${SSH_RETRY_DELAY}s..."
    sleep "${SSH_RETRY_DELAY}"
  else
    fail "Cannot reach ${REMOTE_SSH_TARGET} after ${SSH_MAX_RETRIES} attempts. Check server status and network connectivity."
  fi
done

# The repo must already be cloned on the server: creating releases/ first would make the later
# `git clone` fail on a non-empty directory.
if ! ssh ${SSH_OPTS} "${REMOTE_SSH_TARGET}" "test -d '${REMOTE_ROOT}/.git'"; then
  fail "shmax repo is not cloned at ${REMOTE_SSH_TARGET}:${REMOTE_ROOT}. Clone it first (see DEPLOY.md)."
fi

GIT_COMMIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
GIT_BRANCH="${DEPLOY_BRANCH}"
RELEASE_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_ID="${RELEASE_TIMESTAMP}-${GIT_COMMIT_SHA}"
REMOTE_RELEASE_DIR="${REMOTE_RELEASES_DIR}/${RELEASE_ID}"
LOCAL_RELEASE_METADATA_FILE="$(mktemp)"

cleanup_local_metadata() {
  rm -f "${LOCAL_RELEASE_METADATA_FILE}"
}

trap cleanup_local_metadata EXIT

if [ -n "$(git status --porcelain -- "${FRONTEND_DIR}" 2>/dev/null)" ]; then
  log "WARNING: ${FRONTEND_DIR} has uncommitted changes; they are included in this build."
fi

export EXPO_BASE_URL="/shmax"
export EXPO_PUBLIC_API_URL="${DEPLOY_API_URL:-https://raskolniktv.mooo.com/shmax/api}"
export EXPO_PUBLIC_WS_URL="${DEPLOY_WS_URL:-wss://raskolniktv.mooo.com/shmax/api}"
log "Build env: EXPO_BASE_URL=${EXPO_BASE_URL} EXPO_PUBLIC_API_URL=${EXPO_PUBLIC_API_URL} EXPO_PUBLIC_WS_URL=${EXPO_PUBLIC_WS_URL}"

log "Building frontend (cd ${FRONTEND_DIR} && npm install && npx expo export --platform web --output-dir dist)..."
if ! (cd "${FRONTEND_DIR}" && rm -rf dist && npm install && npx expo export --platform web --output-dir dist); then
  fail "Frontend build failed. Deployment aborted before upload."
fi

if [ ! -d "${DIST_DIR}" ]; then
  fail "Build completed but ${DIST_DIR}/ directory was not found."
fi

if [ ! -f "${DIST_DIR}/index.html" ]; then
  fail "Build completed but ${DIST_DIR}/index.html was not found."
fi

# Post-build verification
find "${DIST_DIR}" -name "*.map" -type f -delete
MAP_COUNT=$(find "${DIST_DIR}" -name "*.map" 2>/dev/null | wc -l | tr -d ' ')
if [ "$MAP_COUNT" -ne 0 ]; then
  fail "Found ${MAP_COUNT} source map file(s) in ${DIST_DIR}/ after deletion. Aborting deployment."
fi

JS_COUNT=$(find "${DIST_DIR}/_expo" -name "*.js" 2>/dev/null | wc -l | tr -d ' ')
if [ "$JS_COUNT" -eq 0 ]; then
  fail "No JS files found in ${DIST_DIR}/_expo/. Build may be broken."
fi

if ! grep -q "${BASE_PATH}_expo/" "${DIST_DIR}/index.html"; then
  fail "index.html does not reference ${BASE_PATH}_expo/. Check EXPO_BASE_URL / frontend/app.config.js."
fi

if ! grep -rq "${EXPO_PUBLIC_API_URL}" "${DIST_DIR}/_expo"; then
  fail "built bundle does not contain ${EXPO_PUBLIC_API_URL}; EXPO_PUBLIC_API_URL was not inlined."
fi

FILE_COUNT=$(find "${DIST_DIR}" -type f 2>/dev/null | wc -l | tr -d ' ')
BUNDLE_SIZE=$(du -sh "${DIST_DIR}" | cut -f1)
log "Build verified: ${JS_COUNT} JS files, ${FILE_COUNT} files, 0 source maps, total size: ${BUNDLE_SIZE}"

cat > "${LOCAL_RELEASE_METADATA_FILE}" <<EOF
{
  "release_id": "${RELEASE_ID}",
  "commit_sha": "${GIT_COMMIT_SHA}",
  "branch": "${GIT_BRANCH}",
  "build_timestamp_utc": "${RELEASE_TIMESTAMP}",
  "build_command": "cd frontend && npm install && npx expo export --platform web --output-dir dist"
}
EOF

log "Preparing remote release directory ${REMOTE_RELEASE_DIR}..."
run_remote_script "set -euo pipefail; mkdir -p '${REMOTE_RELEASES_DIR}' '${REMOTE_RELEASE_DIR}'"

log "Uploading release payload to ${REMOTE_SSH_TARGET}:${REMOTE_RELEASE_DIR}..."
if ! scp ${SSH_OPTS} -r "${DIST_DIR}/." "${REMOTE_SSH_TARGET}:${REMOTE_RELEASE_DIR}/"; then
  fail "SCP upload failed. Check SSH key access and remote path."
fi

if ! scp ${SSH_OPTS} "${LOCAL_RELEASE_METADATA_FILE}" "${REMOTE_SSH_TARGET}:${REMOTE_RELEASE_DIR}/.release-meta.json"; then
  fail "Release metadata upload failed."
fi

log "Validating uploaded release structure..."
REMOTE_VALIDATION_OUTPUT="$(
  run_remote_capture "
test -f '${REMOTE_RELEASE_DIR}/index.html'
test -f '${REMOTE_RELEASE_DIR}/.release-meta.json'
test -d '${REMOTE_RELEASE_DIR}/_expo'
js_count=\$(find '${REMOTE_RELEASE_DIR}/_expo' -type f -name '*.js' | wc -l | tr -d ' ')
file_count=\$(find '${REMOTE_RELEASE_DIR}' -type f | wc -l | tr -d ' ')
if [ \"\${js_count}\" -eq 0 ]; then
  echo '0:0'
  exit 1
fi
printf '%s:%s' \"\${js_count}\" \"\${file_count}\"
"
)"

REMOTE_JS_COUNT="${REMOTE_VALIDATION_OUTPUT%%:*}"
REMOTE_FILE_COUNT="${REMOTE_VALIDATION_OUTPUT##*:}"
log "Remote release verified: ${REMOTE_JS_COUNT} JS files, ${REMOTE_FILE_COUNT} files."

PREVIOUS_RELEASE="$(
  run_remote_capture "
if [ -L '${REMOTE_CURRENT_LINK}' ] || [ -e '${REMOTE_CURRENT_LINK}' ]; then
  readlink -f '${REMOTE_CURRENT_LINK}'
fi
"
)"
if [ -n "${PREVIOUS_RELEASE}" ]; then
  log "Current live release before cutover: ${PREVIOUS_RELEASE}"
else
  log "No existing live release symlink found; this will behave like an initial cutover."
fi

log "Atomically repointing ${REMOTE_CURRENT_LINK} to ${REMOTE_RELEASE_DIR}..."
run_remote_script "set -euo pipefail;
  temp_link='${REMOTE_CURRENT_LINK}.tmp.${RELEASE_ID}';
  rm -f \"\${temp_link}\";
  ln -s '${REMOTE_RELEASE_DIR}' \"\${temp_link}\";
  mv -Tf \"\${temp_link}\" '${REMOTE_CURRENT_LINK}';
  test \"\$(readlink -f '${REMOTE_CURRENT_LINK}')\" = '${REMOTE_RELEASE_DIR}'"

log "Running post-cutover smoke check against ${SMOKE_BASE_URL}/ ..."
if ! smoke_check "${RELEASE_ID}"; then
  log "Smoke check failed after cutover. Attempting rollback."

  if [ -n "${PREVIOUS_RELEASE}" ]; then
    run_remote_script "set -euo pipefail;
      if [ ! -d '${PREVIOUS_RELEASE}' ]; then
        exit 1;
      fi;
      temp_link='${REMOTE_CURRENT_LINK}.rollback.${RELEASE_ID}';
      rm -f \"\${temp_link}\";
      ln -s '${PREVIOUS_RELEASE}' \"\${temp_link}\";
      mv -Tf \"\${temp_link}\" '${REMOTE_CURRENT_LINK}';
      test \"\$(readlink -f '${REMOTE_CURRENT_LINK}')\" = '${PREVIOUS_RELEASE}'"

    if smoke_check "$(basename "${PREVIOUS_RELEASE}")"; then
      fail "Smoke check failed after cutover. Rolled back to ${PREVIOUS_RELEASE} successfully."
    fi

    fail "Smoke check failed after cutover and rollback validation also failed. Is the API running on the server (nohup_fetcher_shmax.sh)? Manual intervention required."
  fi

  fail "Smoke check failed after cutover and no previous release was available for rollback. Is the API running on the server (nohup_fetcher_shmax.sh)?"
fi

# Retention: keep the ${RELEASE_KEEP_COUNT} newest releases; the active and the previous (rollback)
# release are never deleted, even when older than that.
log "Post-cutover smoke check passed. Applying release retention policy (keep last ${RELEASE_KEEP_COUNT}, protect active + previous)."
REMOVED_COUNT="$(
  run_remote_capture "
active_release=\$(readlink -f '${REMOTE_CURRENT_LINK}')
rollback_release='${PREVIOUS_RELEASE}'
seen=0
removed=0
for release_path in \$(find '${REMOTE_RELEASES_DIR}' -mindepth 1 -maxdepth 1 -type d | sort -r); do
  seen=\$((seen + 1))
  if [ \"\${seen}\" -le '${RELEASE_KEEP_COUNT}' ]; then
    continue
  fi
  if [ \"\${release_path}\" = \"\${active_release}\" ] || [ \"\${release_path}\" = \"\${rollback_release}\" ]; then
    continue
  fi
  rm -rf \"\${release_path}\"
  removed=\$((removed + 1))
done
printf '%s' \"\${removed}\"
"
)"
log "Removed ${REMOVED_COUNT} old release(s)."

log "Deployment completed successfully (${DEPLOY_ENV}). Live release: ${RELEASE_ID}"
