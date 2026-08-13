"""
Databricks App boilerplate:
- Serves a small Flask API
- Reads/writes to Lakebase (Databricks-managed Postgres) via lakebase.py
- Pulls data from the Massive API via massive_client.py and syncs it into Lakebase

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import json as _json
import logging
import os
import re

import requests
from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase
from massive_client import MassiveClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("massive-app")

app = Flask(__name__)
_w = WorkspaceClient()

TABLE_NAME = os.environ.get("MASSIVE_TABLE_NAME", "massive_records")
WATCHLIST_TABLE_NAME = os.environ.get("WATCHLIST_TABLE_NAME", "watchlist")
# Per-symbol cache of company profile + price history, shared across all
# users watching that symbol so profile/history are fetched from Massive at
# most once per _DETAILS_MAX_AGE_HOURS rather than on every watchlist add.
DETAILS_TABLE_NAME = os.environ.get("TICKER_DETAILS_TABLE_NAME", "ticker_details")
_DETAILS_MAX_AGE_HOURS = int(os.environ.get("TICKER_DETAILS_MAX_AGE_HOURS", 24))
# Per-symbol cache of news headlines, fetched on demand only (not pre-loaded
# with the rest of the watchlist) and refreshed more often than profile/
# history since news changes faster.
NEWS_TABLE_NAME = os.environ.get("TICKER_NEWS_TABLE_NAME", "ticker_news")
_NEWS_MAX_AGE_HOURS = int(os.environ.get("TICKER_NEWS_MAX_AGE_HOURS", 6))

# Basic stock ticker shape check: 1-10 uppercase letters, with an optional
# ".X" or ".XX" share-class suffix (e.g. "BRK.B"). This rejects obviously
# malformed input before we even call the Massive API.
_TICKER_RE = re.compile(r"^[A-Z]{1,10}(\.[A-Z]{1,2})?$")


def ensure_table():
    """Create the destination table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def ensure_watchlist_table():
    """Create the watchlist table in Lakebase if it doesn't exist yet, and
    backfill newer OHLCV columns onto tables created before they existed."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE_NAME} (
            symbol TEXT NOT NULL,
            email TEXT NOT NULL,
            latest_price NUMERIC,
            open_price NUMERIC,
            high_price NUMERIC,
            low_price NUMERIC,
            volume NUMERIC,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, email)
        )
        """
    )
    lakebase.run_write(
        f"""
        ALTER TABLE {WATCHLIST_TABLE_NAME}
            ADD COLUMN IF NOT EXISTS open_price NUMERIC,
            ADD COLUMN IF NOT EXISTS high_price NUMERIC,
            ADD COLUMN IF NOT EXISTS low_price NUMERIC,
            ADD COLUMN IF NOT EXISTS volume NUMERIC
        """
    )


def ensure_ticker_details_table():
    """Create the per-symbol company profile / price history cache table."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {DETAILS_TABLE_NAME} (
            symbol TEXT PRIMARY KEY,
            profile JSONB,
            price_history JSONB,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def ensure_ticker_news_table():
    """Create the per-symbol news cache table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {NEWS_TABLE_NAME} (
            symbol TEXT PRIMARY KEY,
            news JSONB,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _current_user_email() -> str:
    """
    Resolve the current user's email so the watchlist can be personalized.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page),
    so the frontend's resp.json() call never chokes on HTML."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Simple UI to submit a list of stock symbols to sync from Massive."""
    return render_template("index.html")


@app.route("/records")
def list_records():
    """Read records already synced into Lakebase."""
    limit = int(request.args.get("limit", 100))
    rows = lakebase.run_query(
        f"SELECT id, payload, synced_at FROM {TABLE_NAME} ORDER BY synced_at DESC LIMIT %s",
        (limit,),
    )
    return jsonify(rows)


@app.route("/sync", methods=["POST"])
def sync_from_massive():
    """
    Pull data from the Massive API (paginated, potentially huge dataset) and
    upsert it into Lakebase in batches.
    """
    ensure_table()
    client = MassiveClient()

    path = request.json.get("path", "/records") if request.is_json else "/records"
    batch_size = int(request.args.get("batch_size", 500))

    batch = []
    total = 0
    for item in client.paginated_get(path):
        batch.append(item)
        if len(batch) >= batch_size:
            total += _upsert_batch(batch)
            batch = []

    if batch:
        total += _upsert_batch(batch)

    return jsonify({"synced": total})


@app.route("/watchlist", methods=["GET"])
def get_watchlist():
    """Return the current user's watchlist with OHLCV, plus each symbol's
    cached company profile and price history (no Massive calls here - both
    are populated/refreshed only when a symbol is added)."""
    ensure_watchlist_table()
    ensure_ticker_details_table()
    email = _current_user_email()
    rows = lakebase.run_query(
        f"""
        SELECT w.symbol, w.email, w.latest_price, w.open_price, w.high_price,
               w.low_price, w.volume, w.updated_at, d.profile, d.price_history
        FROM {WATCHLIST_TABLE_NAME} w
        LEFT JOIN {DETAILS_TABLE_NAME} d ON d.symbol = w.symbol
        WHERE w.email = %s
        ORDER BY w.symbol ASC
        """,
        (email,),
    )
    return jsonify(rows)


@app.route("/watchlist", methods=["POST"])
def add_to_watchlist():
    """
    Fetch the latest OHLCV bar for a single stock symbol from Massive using
    exactly ONE API call (see MassiveClient.get_latest_price), refresh that
    symbol's cached company profile / price history if stale (0-2 more
    calls, shared across all users - see _get_or_refresh_ticker_details),
    then add/update that symbol on the watchlist in Lakebase.
    """
    ensure_watchlist_table()

    if request.is_json:
        symbol = request.json.get("symbol", "")
    else:
        symbol = request.form.get("symbol", "")

    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""

    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400

    client = MassiveClient()
    try:
        data = client.get_latest_price(symbol)  # <-- single API call, full OHLCV bar
    except requests.HTTPError:
        # Massive returns a 404/4xx for tickers it doesn't recognize.
        return jsonify({"error": f"Unknown ticker symbol: {symbol}"}), 400

    bar = _extract_ohlcv(data)
    price = bar.get("close")
    if price is None:
        # No usable price in the response (e.g. delisted/invalid ticker
        # that still 200s with an empty result set) - don't add it.
        return jsonify({"error": f"No price data available for ticker: {symbol}"}), 400

    email = _current_user_email()
    details = _get_or_refresh_ticker_details(client, symbol)

    lakebase.run_write(
        f"""
        INSERT INTO {WATCHLIST_TABLE_NAME}
            (symbol, email, latest_price, open_price, high_price, low_price, volume, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (symbol, email) DO UPDATE
            SET latest_price = EXCLUDED.latest_price,
                open_price = EXCLUDED.open_price,
                high_price = EXCLUDED.high_price,
                low_price = EXCLUDED.low_price,
                volume = EXCLUDED.volume,
                updated_at = EXCLUDED.updated_at
        """,
        (symbol, email, price, bar.get("open"), bar.get("high"), bar.get("low"), bar.get("volume")),
    )

    return jsonify(
        {
            "symbol": symbol,
            "email": email,
            "latest_price": price,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "volume": bar.get("volume"),
            "profile": details.get("profile"),
            "price_history": details.get("price_history"),
        }
    )


def _get_or_refresh_ticker_details(client: MassiveClient, symbol: str) -> dict:
    """
    Return cached company profile + price history for a symbol, refreshing
    from Massive only when there's no cached row yet or it's older than
    _DETAILS_MAX_AGE_HOURS.

    This cache is keyed by symbol only (not per-user), so it costs at most
    two extra Massive API calls (profile + history) per symbol per refresh
    window, no matter how many users add that symbol to their watchlist.
    """
    ensure_ticker_details_table()

    cached = lakebase.run_query(
        f"""
        SELECT profile, price_history FROM {DETAILS_TABLE_NAME}
        WHERE symbol = %s AND fetched_at > now() - (%s * INTERVAL '1 hour')
        """,
        (symbol, _DETAILS_MAX_AGE_HOURS),
    )
    if cached:
        return cached[0]

    profile = None
    price_history = None
    try:
        profile = client.get_ticker_details(symbol)
    except requests.HTTPError:
        logger.warning("No ticker details available for %s", symbol)

    try:
        price_history = client.get_price_history(symbol)
    except requests.HTTPError:
        logger.warning("No price history available for %s", symbol)

    lakebase.run_write(
        f"""
        INSERT INTO {DETAILS_TABLE_NAME} (symbol, profile, price_history, fetched_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (symbol) DO UPDATE
            SET profile = EXCLUDED.profile,
                price_history = EXCLUDED.price_history,
                fetched_at = EXCLUDED.fetched_at
        """,
        (
            symbol,
            _json.dumps(profile) if profile is not None else None,
            _json.dumps(price_history) if price_history is not None else None,
        ),
    )

    return {"profile": profile, "price_history": price_history}


@app.route("/watchlist/<symbol>", methods=["DELETE"])
def delete_from_watchlist(symbol):
    """Remove a single symbol from the current user's watchlist."""
    ensure_watchlist_table()

    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400

    email = _current_user_email()

    deleted = lakebase.run_write(
        f"DELETE FROM {WATCHLIST_TABLE_NAME} WHERE symbol = %s AND email = %s",
        (symbol, email),
    )

    if not deleted:
        return jsonify({"error": f"{symbol} is not on your watchlist"}), 404

    return jsonify({"symbol": symbol, "email": email, "deleted": True})


@app.route("/watchlist/<symbol>/news", methods=["GET"])
def get_ticker_news_route(symbol):
    """
    Return recent news headlines for a symbol on the current user's
    watchlist. Fetched on demand (only when the UI's "News" button is
    clicked for that row) rather than pre-loaded for every symbol, and
    cached per-symbol (shared across users) for _NEWS_MAX_AGE_HOURS.
    """
    ensure_watchlist_table()

    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400

    email = _current_user_email()
    owned = lakebase.run_query(
        f"SELECT 1 FROM {WATCHLIST_TABLE_NAME} WHERE symbol = %s AND email = %s",
        (symbol, email),
    )
    if not owned:
        return jsonify({"error": f"{symbol} is not on your watchlist"}), 404

    news = _get_or_refresh_news(MassiveClient(), symbol)
    return jsonify({"symbol": symbol, "news": news})


def _get_or_refresh_news(client: MassiveClient, symbol: str):
    """
    Return cached news for a symbol, refreshing from Massive only when
    there's no cached row yet or it's older than _NEWS_MAX_AGE_HOURS.

    Cache is keyed by symbol only (not per-user), same pattern as
    _get_or_refresh_ticker_details, so repeated clicks (by the same or
    different users) cost at most one Massive call per refresh window.
    """
    ensure_ticker_news_table()

    cached = lakebase.run_query(
        f"""
        SELECT news FROM {NEWS_TABLE_NAME}
        WHERE symbol = %s AND fetched_at > now() - (%s * INTERVAL '1 hour')
        """,
        (symbol, _NEWS_MAX_AGE_HOURS),
    )
    if cached:
        return cached[0]["news"]

    news = None
    try:
        news = client.get_ticker_news(symbol)
    except requests.HTTPError:
        logger.warning("No news available for %s", symbol)

    lakebase.run_write(
        f"""
        INSERT INTO {NEWS_TABLE_NAME} (symbol, news, fetched_at)
        VALUES (%s, %s, now())
        ON CONFLICT (symbol) DO UPDATE
            SET news = EXCLUDED.news,
                fetched_at = EXCLUDED.fetched_at
        """,
        (symbol, _json.dumps(news) if news is not None else None),
    )

    return news


def _pick(d: dict, *keys):
    """Return the first present key's value from d, or None."""
    for key in keys:
        if key in d:
            return d[key]
    return None


def _extract_ohlcv(data: dict) -> dict:
    """Pull a full OHLCV bar out of the Massive 'previous close' response shape.

    The /v2/aggs/ticker/{symbol}/prev endpoint returns "results" as a LIST
    containing a single aggregate bar (not a dict), e.g.:
        {"status": "OK", "resultsCount": 1, "results": [{"c": 148.845, ...}]}
    Unwrap the list here, and check "status"/"resultsCount" so invalid
    tickers (empty results) are detected instead of "succeeding" with a null
    price. Returns {} if no usable bar is found, otherwise a dict with
    open/high/low/close/volume/timestamp keys (any of which may be None).

    Adjust the key lookups here if the real Massive API returns different
    field names for these values.
    """
    if not isinstance(data, dict):
        return {}
    if data.get("status") not in (None, "OK") or data.get("resultsCount") == 0:
        return {}
    results = data.get("results", data)
    if isinstance(results, list):
        results = results[0] if results else None
    if not isinstance(results, dict):
        return {}
    return {
        "open": _pick(results, "o", "open"),
        "high": _pick(results, "h", "high"),
        "low": _pick(results, "l", "low"),
        "close": _pick(results, "c", "p", "price", "last_price", "vw"),
        "volume": _pick(results, "v", "volume"),
        "timestamp": _pick(results, "t", "timestamp"),
    }


def _upsert_batch(items: list[dict]) -> int:
    """Upsert a batch of Massive API items into Lakebase, one statement per row.

    For very large batches, consider psycopg2.extras.execute_values for
    higher throughput instead of per-row execute calls.
    """
    count = 0
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for item in items:
                cur.execute(
                    f"""
                    INSERT INTO {TABLE_NAME} (id, payload, synced_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (id) DO UPDATE
                        SET payload = EXCLUDED.payload,
                            synced_at = EXCLUDED.synced_at
                    """,
                    (str(item.get("id")), _json.dumps(item)),
                )
                count += 1
            conn.commit()
    return count


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")