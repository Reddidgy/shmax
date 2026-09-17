import os
import json
import sqlite3
import uuid
import re
from urllib.parse import urlparse
import time
import hashlib
import ipaddress
from threading import Lock
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CORS Configuration (OWASP API7:2023 Compliance)
# ---------------------------------------------------------------------------
# CORS is restricted to known, trusted origins to prevent SSRF and data exfiltration.
# Allowed origins are configured via CORS_ALLOWED_ORIGINS environment variable.
# Format: comma-separated list of origins, e.g.:
#   CORS_ALLOWED_ORIGINS=https://praxisos.dev,http://localhost:5173,http://127.0.0.1:5173
#
# For local-first development, localhost origins are permitted.
# The production API should only accept requests from the deployed frontend origin.
# ---------------------------------------------------------------------------

def _get_allowed_origins():
    """
    Parse CORS_ALLOWED_ORIGINS environment variable into a set of allowed origins.
    Falls back to localhost-only defaults if not configured.
    """
    origins_str = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if origins_str:
        return {origin.strip() for origin in origins_str.split(",") if origin.strip()}
    # Default: localhost development origins (secure by default for production)
    return {
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    }


def _is_allowed_origin(origin: str) -> bool:
    """
    Check if the request origin is in the allowed list.
    Also supports dynamic localhost origins in development mode.
    """
    if not origin:
        return False
    
    allowed_origins = _get_allowed_origins()
    
    # Exact match against allowed origins
    if origin in allowed_origins:
        return True
    
    # In development mode, allow any localhost origin dynamically
    # This is controlled by CORS_ALLOW_LOCALHOST_DYNAMIC=true
    if os.environ.get("CORS_ALLOW_LOCALHOST_DYNAMIC", "false").lower() == "true":
        localhost_pattern = re.compile(
            r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
        )
        if localhost_pattern.match(origin):
            return True
    
    return False


def _get_cors_origin(origin: str) -> str:
    """
    Return the origin to echo back in Access-Control-Allow-Origin header.
    Returns empty string if origin is not allowed (CORS will deny the request).
    """
    return origin if _is_allowed_origin(origin) else ""


# Build CORS resources with dynamic origin validation
allowed_origins_list = list(_get_allowed_origins())

CORS(app, resources={
    r"/submit_feedback": {
        "origins": allowed_origins_list,
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"],
    },
    r"/get_feedback": {
        "origins": allowed_origins_list,
        "methods": ["GET", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-Dev-Token"],
    },
    r"/resolve_feedback": {
        "origins": allowed_origins_list,
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-Dev-Token"],
    },
    r"/health": {
        "origins": allowed_origins_list,
        "methods": ["GET"],
    },
    r"/track": {
        "origins": allowed_origins_list,
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"],
    },
    r"/get_metrics": {
        "origins": allowed_origins_list,
        "methods": ["GET", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-Dev-Token"],
    },
})

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("FEEDBACK_DB_PATH", "feedback.db")
DEV_TOKEN = os.environ.get("DEV_TOKEN", "")

VALID_TYPES = {"bug", "feature_request", "feedback", "error"}
REQUIRED_FIELDS = ["type", "message"]
MAX_SCREENSHOT_SIZE_BYTES = 2 * 1024 * 1024  # 2MB per screenshot
MAX_SCREENSHOTS = 5
MAX_TOTAL_SCREENSHOT_SIZE = 10 * 1024 * 1024  # 10MB total
RATE_LIMIT_SECONDS = max(1, int(os.environ.get("RATE_LIMIT_SECONDS", 120)))
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() == "true"

# Visitor tracking configuration
VISITOR_SALT = os.environ.get("VISITOR_SALT", "default-salt-change-in-prod")
if VISITOR_SALT == "default-salt-change-in-prod":
    import warnings
    warnings.warn("VISITOR_SALT not configured — using default. Set VISITOR_SALT in .env for production.")
TRACK_RATE_LIMIT_SECONDS = max(1, int(os.environ.get("TRACK_RATE_LIMIT_SECONDS", 10)))

# In-memory rate limit state (resets on process restart).
rate_limits = {}
rate_limits_lock = Lock()

# Separate rate limit state for /track endpoint
track_rate_limits = {}
track_rate_limits_lock = Lock()


def _validate_ip(candidate: str) -> str | None:
    value = (candidate or "").strip()
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def get_client_ip() -> str:
    """
    Resolve caller IP from X-Forwarded-For (first hop) or remote address.
    Invalid values are ignored to reduce spoofing impact from malformed headers.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first_hop = forwarded.split(",")[0].strip()
        validated = _validate_ip(first_hop)
        if validated:
            return validated

    validated_remote = _validate_ip(request.remote_addr or "")
    if validated_remote:
        return validated_remote

    return "unknown"


def cleanup_rate_limits(now: float | None = None):
    now_ts = now if now is not None else time.time()
    stale_threshold = RATE_LIMIT_SECONDS * 2
    stale_ips = [ip for ip, ts in rate_limits.items() if now_ts - ts > stale_threshold]
    for ip in stale_ips:
        del rate_limits[ip]


def check_rate_limit(client_ip: str) -> tuple[bool, int]:
    if not RATE_LIMIT_ENABLED:
        return False, 0

    now = time.time()
    with rate_limits_lock:
        cleanup_rate_limits(now)
        last_submission = rate_limits.get(client_ip)

    if last_submission is None:
        return False, 0

    elapsed = now - last_submission
    if elapsed >= RATE_LIMIT_SECONDS:
        return False, 0

    remaining = max(1, int(RATE_LIMIT_SECONDS - elapsed))
    return True, remaining


def mark_rate_limit_submission(client_ip: str):
    if not RATE_LIMIT_ENABLED:
        return
    with rate_limits_lock:
        rate_limits[client_ip] = time.time()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                message     TEXT NOT NULL,
                context     TEXT,
                user_agent  TEXT,
                app_version TEXT,
                created_at  TEXT NOT NULL,
                name        TEXT,
                screenshot  TEXT,
                resolved    INTEGER DEFAULT 0,
                resolved_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                id           TEXT PRIMARY KEY,
                visitor_hash TEXT NOT NULL,
                app_version  TEXT,
                user_agent   TEXT,
                created_at   TEXT NOT NULL,
                page_type    TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_visitor_hash ON visits (visitor_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_created_at ON visits (created_at)")

        # Migration: Add new columns if they don't exist (for existing databases)
        cursor = conn.execute("PRAGMA table_info(feedback)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if "name" not in existing_columns:
            conn.execute("ALTER TABLE feedback ADD COLUMN name TEXT")
        if "screenshot" not in existing_columns:
            conn.execute("ALTER TABLE feedback ADD COLUMN screenshot TEXT")
        if "resolved" not in existing_columns:
            conn.execute("ALTER TABLE feedback ADD COLUMN resolved INTEGER DEFAULT 0")
        if "resolved_at" not in existing_columns:
            conn.execute("ALTER TABLE feedback ADD COLUMN resolved_at TEXT")

        # Migration: Add page_type column to visits if missing
        visits_cursor = conn.execute("PRAGMA table_info(visits)")
        visits_columns = {row[1] for row in visits_cursor.fetchall()}
        if "page_type" not in visits_columns:
            conn.execute("ALTER TABLE visits ADD COLUMN page_type TEXT DEFAULT ''")

        # Migration: fix orphan rows from legacy /track endpoint (empty page_type → landing)
        conn.execute("UPDATE visits SET page_type = 'landing' WHERE page_type = '' OR page_type IS NULL")

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auth decorator: dev-environment only
# ---------------------------------------------------------------------------
_LOCALHOST_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_localhost(value: str) -> bool:
    """Validate that a URL's hostname (not a substring) is a known localhost address."""
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
        return hostname in _LOCALHOST_HOSTNAMES
    except Exception:
        return False


def require_dev_access(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # DEV_TOKEN must be configured — refuse all dev access when unset/empty
        if not DEV_TOKEN:
            return jsonify({"error": "Forbidden: DEV_TOKEN not configured"}), 403

        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        token = request.headers.get("X-Dev-Token", "")

        from_localhost = (origin and _is_localhost(origin)) or (referer and _is_localhost(referer))
        valid_token = token == DEV_TOKEN

        if not (from_localhost or valid_token):
            return jsonify({"error": "Forbidden: development access only"}), 403

        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
BOT_UA_PATTERNS = re.compile(
    r"(bot|crawl|spider|slurp|baiduspider|yandex|googlebot|bingbot|duckduckbot|"
    r"facebookexternalhit|twitterbot|linkedinbot|semrushbot|ahrefsbot|mj12bot|"
    r"dotbot|rogerbot|wget|curl|python-requests|go-http-client|headlesschrome|"
    r"phantomjs|lighthouse|pagespeed|pingdom|uptimerobot)",
    re.IGNORECASE,
)

VALID_PAGE_TYPES = {"landing", "app"}


@app.route("/health", methods=["GET"])
def health():
    page_type = request.args.get("t", "")

    if page_type and page_type in VALID_PAGE_TYPES:
        user_agent = request.headers.get("User-Agent", "")[:256]

        # Skip bot traffic
        if not BOT_UA_PATTERNS.search(user_agent):
            client_ip = get_client_ip()

            # Rate limit check (reuse track rate limit infrastructure)
            now = time.time()
            rate_key = f"{client_ip}:{page_type}"
            with track_rate_limits_lock:
                last_track = track_rate_limits.get(rate_key)
                if last_track is None or (now - last_track) >= TRACK_RATE_LIMIT_SECONDS:
                    track_rate_limits[rate_key] = now

                    # Clean stale entries
                    stale = [k for k, ts in track_rate_limits.items() if now - ts > TRACK_RATE_LIMIT_SECONDS * 2]
                    for k in stale:
                        del track_rate_limits[k]

                    visitor_hash = hashlib.sha256(
                        f"{client_ip}{user_agent}{VISITOR_SALT}".encode()
                    ).hexdigest()

                    entry_id = str(uuid.uuid4())
                    created_at = datetime.now(timezone.utc).isoformat()

                    conn = get_db()
                    try:
                        conn.execute(
                            "INSERT INTO visits (id, visitor_hash, app_version, user_agent, created_at, page_type) VALUES (?, ?, ?, ?, ?, ?)",
                            (entry_id, visitor_hash, "", user_agent, created_at, page_type),
                        )
                        conn.commit()
                    finally:
                        conn.close()

    return jsonify({"status": "ok"}), 200


@app.route("/submit_feedback", methods=["POST"])
def submit_feedback():
    """
    Submit a feedback entry.

    Rate limiting:
    - Applies per client IP with a configurable cooldown (default 120s).
    - Returns HTTP 429 with Retry-After header when limited.
    """
    client_ip = get_client_ip()
    try:
        is_limited, remaining = check_rate_limit(client_ip)
        if is_limited:
            response = jsonify({
                "error": f"Rate limit exceeded. Please wait {remaining} seconds.",
                "retry_after": remaining,
            })
            response.headers["Retry-After"] = str(remaining)
            return response, 429
    except Exception as exc:
        # Fail-open by design: feedback submission should continue if limiter fails.
        app.logger.warning("Rate limiter check failed: %s", exc)

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    if data["type"] not in VALID_TYPES:
        return jsonify({
            "error": f"Invalid type '{data['type']}'. Allowed: {', '.join(sorted(VALID_TYPES))}"
        }), 400

    message = str(data["message"]).strip()
    if not message:
        return jsonify({"error": "'message' cannot be empty"}), 400

    # Optional name field
    name = data.get("name")
    if name:
        name = str(name).strip()[:255]  # Limit length

    # Optional screenshots field (array of base64 data URLs)
    screenshots = data.get("screenshots")
    screenshots_json = None
    
    if screenshots:
        if not isinstance(screenshots, list):
            return jsonify({"error": "screenshots must be an array"}), 400
        
        if len(screenshots) > MAX_SCREENSHOTS:
            return jsonify({"error": f"Maximum {MAX_SCREENSHOTS} screenshots allowed"}), 400
        
        if len(screenshots) == 0:
            screenshots = None
        else:
            total_size = 0
            validated_screenshots = []
            
            for idx, screenshot in enumerate(screenshots):
                if not isinstance(screenshot, str):
                    return jsonify({"error": f"Screenshot {idx + 1} must be a string"}), 400
                
                screenshot_str = str(screenshot)
                screenshot_size = len(screenshot_str)
                
                if screenshot_size > MAX_SCREENSHOT_SIZE_BYTES:
                    return jsonify({
                        "error": f"Screenshot {idx + 1} exceeds maximum size of {MAX_SCREENSHOT_SIZE_BYTES // (1024 * 1024)}MB"
                    }), 400
                
                if not screenshot_str.startswith("data:image/"):
                    return jsonify({
                        "error": f"Screenshot {idx + 1} must be a valid base64 image data URL (data:image/...)"
                    }), 400
                
                total_size += screenshot_size
                validated_screenshots.append(screenshot_str)
            
            if total_size > MAX_TOTAL_SCREENSHOT_SIZE:
                return jsonify({
                    "error": f"Total screenshots size exceeds maximum of {MAX_TOTAL_SCREENSHOT_SIZE // (1024 * 1024)}MB"
                }), 400
            
            screenshots_json = json.dumps(validated_screenshots)

    raw_context = data.get("context")
    context_str = json.dumps(raw_context) if raw_context is not None else None

    entry_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO feedback (id, type, message, context, user_agent, app_version, created_at, name, screenshot, resolved, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                entry_id,
                data["type"],
                message,
                context_str,
                request.headers.get("User-Agent", "")[:512],
                str(data.get("app_version", ""))[:50],
                created_at,
                name,
                screenshots_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        mark_rate_limit_submission(client_ip)
    except Exception as exc:
        app.logger.warning("Rate limiter update failed: %s", exc)

    return jsonify({"id": entry_id, "status": "created"}), 201


@app.route("/get_feedback", methods=["GET"])
@require_dev_access
def get_feedback():
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    feedback_type = request.args.get("type")
    resolved_filter = request.args.get("resolved")  # 'true', 'false', or None

    conn = get_db()
    try:
        # Build query based on filters
        where_clauses = []
        params = []

        if feedback_type:
            if feedback_type not in VALID_TYPES:
                return jsonify({"error": f"Unknown type filter '{feedback_type}'"}), 400
            where_clauses.append("type = ?")
            params.append(feedback_type)

        if resolved_filter is not None:
            if resolved_filter.lower() == "true":
                where_clauses.append("resolved = 1")
            elif resolved_filter.lower() == "false":
                where_clauses.append("resolved = 0")

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        rows = conn.execute(
            f"SELECT * FROM feedback {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM feedback {where_sql}", params
        ).fetchone()[0]
    finally:
        conn.close()

    result = []
    for row in rows:
        item = dict(row)
        if item.get("context"):
            try:
                item["context"] = json.loads(item["context"])
            except (json.JSONDecodeError, TypeError):
                pass
        # Parse screenshots JSON array
        if item.get("screenshot"):
            try:
                item["screenshots"] = json.loads(item["screenshot"])
                del item["screenshot"]  # Remove old field name
            except (json.JSONDecodeError, TypeError):
                # Backward compatibility: if it's not JSON, treat as single screenshot
                item["screenshots"] = [item["screenshot"]]
                del item["screenshot"]
        else:
            item["screenshots"] = []
        # Convert resolved integer to boolean for JSON
        if "resolved" in item:
            item["resolved"] = bool(item["resolved"])
        result.append(item)

    return jsonify({"total": total, "limit": limit, "offset": offset, "data": result}), 200


@app.route("/resolve_feedback", methods=["POST"])
@require_dev_access
def resolve_feedback():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    feedback_id = data.get("id")
    if not feedback_id:
        return jsonify({"error": "Missing required field: id"}), 400

    resolved_at = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    try:
        # Check if entry exists
        existing = conn.execute("SELECT id, resolved FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if not existing:
            return jsonify({"error": f"Feedback entry '{feedback_id}' not found"}), 404

        # Update resolved status
        conn.execute(
            "UPDATE feedback SET resolved = 1, resolved_at = ? WHERE id = ?",
            (resolved_at, feedback_id),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"id": feedback_id, "status": "resolved", "resolved_at": resolved_at}), 200



@app.route("/get_metrics", methods=["GET"])
@require_dev_access
def get_metrics():
    """Return aggregated visit metrics (dev-only)."""
    conn = get_db()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def _unique_counts(page_type_filter: str = ""):
            """Return (total, today, 7d, 30d) unique visitor counts, optionally filtered by page_type."""
            where = ""
            params: list = []
            if page_type_filter:
                where = " AND page_type = ?"
                params = [page_type_filter]

            total = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_hash) FROM visits WHERE 1=1{where}", params
            ).fetchone()[0]

            t = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_hash) FROM visits WHERE DATE(created_at) = ?{where}",
                [today] + params,
            ).fetchone()[0]

            d7 = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_hash) FROM visits WHERE DATE(created_at) >= DATE(?, '-7 days'){where}",
                [today] + params,
            ).fetchone()[0]

            d30 = conn.execute(
                f"SELECT COUNT(DISTINCT visitor_hash) FROM visits WHERE DATE(created_at) >= DATE(?, '-30 days'){where}",
                [today] + params,
            ).fetchone()[0]

            return {"total": total, "today": t, "7d": d7, "30d": d30}

        all_counts = _unique_counts()
        landing_counts = _unique_counts("landing")
        app_counts = _unique_counts("app")

        visits_by_day_rows = conn.execute(
            """
            SELECT DATE(created_at) AS date,
                   COUNT(*) AS visits,
                   COUNT(DISTINCT visitor_hash) AS unique_visitors
            FROM visits
            WHERE DATE(created_at) >= DATE(?, '-14 days')
              AND page_type = 'landing'
            GROUP BY DATE(created_at)
            ORDER BY date
            """,
            (today,),
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "total_visits": all_counts["total"],
        "unique_visitors_today": all_counts["today"],
        "unique_visitors_7d": all_counts["7d"],
        "unique_visitors_30d": all_counts["30d"],
        "landing": landing_counts,
        "app": app_counts,
        "visits_by_day": [
            {"date": row["date"], "visits": row["visits"], "unique_visitors": row["unique_visitors"]}
            for row in visits_by_day_rows
        ],
    }), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("FEEDBACK_API_PORT", 4748))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
