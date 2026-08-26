"""
AEGIS backend storage API
==========================

A minimal key/value storage API that stands in for Claude.ai's built-in
`window.storage` when the AEGIS display (aegis.html) is served outside a
Claude.ai artifact — e.g. from your own Python app.

Stores five keys used by the frontend: announcements, editorials, events,
weather, posters. Each value is stored exactly as the frontend sends it (a
JSON string) and handed back unmodified, mirroring window.storage's
semantics.

--------------------------------------------------------------------------
Option A — drop into your EXISTING Flask app (recommended):

    from aegis_storage import aegis_bp
    app.register_blueprint(aegis_bp)

That's it. It adds these routes under /api/aegis:
    GET    /api/aegis/<key>     -> {"key": key, "value": <string|null>}
    POST   /api/aegis/<key>     -> body {"value": "<string>"}  -> upserts
    DELETE /api/aegis/<key>     -> deletes the key

It uses its own SQLite file (aegis.db, next to this module) so it won't
collide with your app's existing database/models. Set AEGIS_DB_PATH to
point it elsewhere (e.g. into a shared data directory).

--------------------------------------------------------------------------
Option B — run it standalone (its own tiny server):

    pip install -r requirements.txt
    python aegis_storage.py
    # serves on http://localhost:5000, aegis.html should set:
    #   window.AEGIS_API_BASE = 'http://localhost:5000/api/aegis'

--------------------------------------------------------------------------
Notes:
- No auth is enforced here on purpose, since the original design (Claude's
  `shared` storage) is a single shared, publicly-writable store for this
  office display. If this will be reachable outside a trusted network,
  put it behind your existing app's admin authentication — see
  `require_admin` below for the one line to change.
"""
import os
import sqlite3
import json
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from flask import Flask, Blueprint, request, jsonify, g, send_file

DB_PATH = os.environ.get(
    "AEGIS_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "aegis.db"),
)

# The only keys the AEGIS frontend ever reads/writes. Rejecting anything
# else keeps this endpoint from becoming an arbitrary key/value store.
ALLOWED_KEYS = {"announcements", "editorials", "events", "weather", "posters"}

MAX_VALUE_BYTES = 5 * 1024 * 1024  # mirrors window.storage's 5MB/key cap

# Default coordinates for the live weather/air-quality lookup — Bayombong,
# Nueva Vizcaya (PENRO's location). Override per-request with ?lat=&lon= if
# a display should represent a different municipality.
DEFAULT_LAT = 16.4833
DEFAULT_LON = 121.1500

# WMO weather codes, as used by Open-Meteo (https://open-meteo.com/en/docs) —
# a stable, documented standard, not something specific to one provider.
WMO_CONDITIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, slight hail", 99: "Thunderstorm, heavy hail",
}

aegis_bp = Blueprint("aegis", __name__, url_prefix="/api/aegis")


def get_db():
    db = getattr(g, "_aegis_db", None)
    if db is None:
        db = g._aegis_db = sqlite3.connect(DB_PATH)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS aegis_storage (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.commit()
    return db


@aegis_bp.teardown_app_request
def _close_db(_exception):
    db = getattr(g, "_aegis_db", None)
    if db is not None:
        db.close()


def require_admin():
    """
    No-op by default. If this API is exposed beyond a trusted admin panel,
    plug your existing app's auth check in here, e.g.:

        from flask_login import current_user
        if not current_user.is_authenticated or not current_user.is_admin:
            return jsonify({"error": "unauthorized"}), 401

    Called at the top of the two write routes (POST, DELETE). Reads (GET)
    are left open since the live display itself needs to read them without
    an admin session.
    """
    return None


def _bad_key(key):
    return jsonify({"error": f"unknown key '{key}'", "allowed": sorted(ALLOWED_KEYS)}), 400


def _fetch_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "AEGIS-PENRO-NuevaVizcaya/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _aqi_category(us_aqi):
    """US EPA AQI breakpoints — matches the categories Open-Meteo's us_aqi uses."""
    if us_aqi is None:
        return None
    if us_aqi <= 50:
        return "Good"
    if us_aqi <= 100:
        return "Moderate"
    if us_aqi <= 150:
        return "Unhealthy (Sensitive Groups)"
    if us_aqi <= 200:
        return "Unhealthy"
    if us_aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


@aegis_bp.get("/weather/live")
def live_weather():
    """
    Server-side proxy to Open-Meteo (free, no API key) for current temperature,
    humidity, condition, and US AQI. Called both by the admin panel's "Fetch
    live data now" button and by the display itself on a timer, so the browser
    never talks to a third-party API directly (avoids CORS entirely and keeps
    this swappable for a different provider later without touching the
    frontend).
    """
    try:
        lat = float(request.args.get("lat", DEFAULT_LAT))
        lon = float(request.args.get("lon", DEFAULT_LON))
    except ValueError:
        return jsonify({"error": "lat/lon must be numbers"}), 400

    try:
        wx = _fetch_json(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,weather_code"
            "&timezone=Asia%2FManila"
        )
        current = wx.get("current", {}) or {}

        aqi_value = None
        try:
            aq = _fetch_json(
                "https://air-quality-api.open-meteo.com/v1/air-quality"
                f"?latitude={lat}&longitude={lon}"
                "&current=us_aqi&timezone=Asia%2FManila"
            )
            aqi_value = (aq.get("current") or {}).get("us_aqi")
        except Exception:
            pass  # air quality is best-effort — don't fail the whole request over it

        return jsonify({
            "temp": current.get("temperature_2m"),
            "condition": WMO_CONDITIONS.get(current.get("weather_code"), "—"),
            "humidity": current.get("relative_humidity_2m"),
            "aqi": _aqi_category(aqi_value),
            "aqi_value": aqi_value,
            "source": "open-meteo",
            "fetched_at": current.get("time"),
        }), 200
    except (URLError, HTTPError) as e:
        print(f"[AEGIS] live weather fetch failed: {e}")
        return jsonify({"error": "could not reach weather provider", "detail": str(e)}), 502
    except Exception as e:
        print(f"[AEGIS] live weather fetch failed unexpectedly: {e}")
        return jsonify({"error": "unexpected error fetching weather", "detail": str(e)}), 500


@aegis_bp.get("/<key>")
def get_value(key):
    if key not in ALLOWED_KEYS:
        return _bad_key(key)
    db = get_db()
    row = db.execute("SELECT value FROM aegis_storage WHERE key = ?", (key,)).fetchone()
    return jsonify({"key": key, "value": row[0] if row else None}), 200


@aegis_bp.post("/<key>")
def set_value(key):
    if key not in ALLOWED_KEYS:
        return _bad_key(key)
    auth_err = require_admin()
    if auth_err:
        return auth_err

    body = request.get_json(silent=True) or {}
    value = body.get("value")
    if not isinstance(value, str):
        return jsonify({"error": 'body must be JSON: {"value": "<string>"}'}), 400
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return jsonify({"error": "value exceeds 5MB limit"}), 413

    db = get_db()
    db.execute(
        """
        INSERT INTO aegis_storage (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return jsonify({"key": key, "value": value}), 200


@aegis_bp.delete("/<key>")
def delete_value(key):
    if key not in ALLOWED_KEYS:
        return _bad_key(key)
    auth_err = require_admin()
    if auth_err:
        return auth_err

    db = get_db()
    db.execute("DELETE FROM aegis_storage WHERE key = ?", (key,))
    db.commit()
    return jsonify({"key": key, "deleted": True}), 200


def create_app():
    """Standalone runner — only used if you run this file directly."""
    app = Flask(__name__)
    try:
        from flask_cors import CORS
        # Wildcard is fine for local testing, but once this is reachable
        # from the office network or the internet, set AEGIS_CORS_ORIGINS to
        # the exact origin aegis.html is served from, e.g.:
        #   AEGIS_CORS_ORIGINS=https://display.penro-nv.example.gov.ph
        origins = os.environ.get("AEGIS_CORS_ORIGINS", "*")
        CORS(app, resources={r"/api/aegis/*": {"origins": origins}})
    except ImportError:
        pass  # fine if the frontend is served from the same origin

    app.register_blueprint(aegis_bp)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    # Serve aegis.html itself at "/" so you don't need a separate static
    # server for local/simple deployments. Looks for aegis.html next to
    # this file, then one directory up (in case it's kept alongside
    # backend/ rather than inside it). Override with AEGIS_HTML_PATH if
    # you keep it somewhere else.
    here = os.path.dirname(os.path.abspath(__file__))
    default_html_path = os.path.join(here, "aegis.html")
    if not os.path.exists(default_html_path):
        parent_html_path = os.path.join(here, "..", "aegis.html")
        if os.path.exists(parent_html_path):
            default_html_path = parent_html_path
    html_path = os.environ.get("AEGIS_HTML_PATH", default_html_path)

    @app.get("/")
    def index():
        if os.path.exists(html_path):
            return send_file(html_path)
        return (
            "aegis.html not found at " + html_path + ".\n"
            "Either place aegis.html next to aegis_storage.py, or set the "
            "AEGIS_HTML_PATH environment variable to its full path.",
            404,
            {"Content-Type": "text/plain"},
        )

    return app


if __name__ == "__main__":
    flask_app = create_app()
    port = int(os.environ.get("PORT", 5000))
    # Debug mode enables the interactive Werkzeug debugger, which can allow
    # arbitrary code execution if it's ever reachable from outside your own
    # machine. Never run with debug=True on a real server. Set
    # AEGIS_DEBUG=1 only for local development on your own laptop.
    debug_mode = os.environ.get("AEGIS_DEBUG", "0") == "1"
    flask_app.run(host="0.0.0.0", port=port, debug=debug_mode)
else:
    # WSGI entry point for a real production server (recommended for actual
    # deployment instead of the `python aegis_storage.py` dev server above).
    # On Windows, Waitress is the simplest option:
    #   pip install waitress
    #   waitress-serve --listen=0.0.0.0:5000 aegis_storage:app
    app = create_app()
