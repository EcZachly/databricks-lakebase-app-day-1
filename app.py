"""
Databricks App boilerplate:
- Serves a small Flask API
- Reads/writes to Lakebase (Databricks-managed Postgres) via lakebase.py
- Pulls data from the Massive API via massive_client.py and syncs it into Lakebase

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

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
    """Create the watchlist table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE_NAME} (
            symbol TEXT NOT NULL,
            email TEXT NOT NULL,
            latest_price NUMERIC,
            price_change NUMERIC,
            price_change_percent NUMERIC,
            volume BIGINT,
            market_cap NUMERIC,
            day_high NUMERIC,
            day_low NUMERIC,
            week_52_high NUMERIC,
            week_52_low NUMERIC,
            company_name TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, email)
        )
        """
    )


def ensure_ticker_details_table():
    """Create table for storing comprehensive ticker/company details."""
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS ticker_details (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            market TEXT,
            locale TEXT,
            primary_exchange TEXT,
            currency_name TEXT,
            cik TEXT,
            homepage_url TEXT,
            logo_url TEXT,
            market_cap NUMERIC,
            phone_number TEXT,
            address JSONB,
            sic_code TEXT,
            sic_description TEXT,
            total_employees INTEGER,
            list_date DATE,
            share_class_shares_outstanding BIGINT,
            weighted_shares_outstanding BIGINT,
            raw_data JSONB,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def ensure_historical_prices_table():
    """Create table for storing historical OHLC price data."""
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS historical_prices (
            symbol TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            open NUMERIC,
            high NUMERIC,
            low NUMERIC,
            close NUMERIC,
            volume BIGINT,
            vwap NUMERIC,
            transactions INTEGER,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, timestamp)
        )
        """
    )
    # Create index for efficient time-range queries
    lakebase.run_write(
        """
        CREATE INDEX IF NOT EXISTS idx_historical_prices_symbol_time 
        ON historical_prices(symbol, timestamp DESC)
        """
    )


def ensure_news_table():
    """Create table for storing market news articles."""
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS market_news (
            id TEXT PRIMARY KEY,
            symbols TEXT[],
            title TEXT NOT NULL,
            description TEXT,
            author TEXT,
            published_utc TIMESTAMPTZ,
            article_url TEXT,
            image_url TEXT,
            keywords TEXT[],
            sentiment TEXT,
            sentiment_reasoning TEXT,
            raw_data JSONB,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Create index for efficient symbol-based queries
    lakebase.run_write(
        """
        CREATE INDEX IF NOT EXISTS idx_news_symbols 
        ON market_news USING GIN(symbols)
        """
    )
    # Create index for time-based queries
    lakebase.run_write(
        """
        CREATE INDEX IF NOT EXISTS idx_news_published 
        ON market_news(published_utc DESC)
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
    """Return the current user's watchlist symbols with comprehensive data."""
    ensure_watchlist_table()
    email = _current_user_email()
    rows = lakebase.run_query(
        f"""
        SELECT symbol, email, latest_price, price_change, price_change_percent,
               volume, market_cap, day_high, day_low, week_52_high, week_52_low,
               company_name, updated_at
        FROM {WATCHLIST_TABLE_NAME}
        WHERE email = %s
        ORDER BY symbol ASC
        """,
        (email,),
    )
    return jsonify(rows)


@app.route("/watchlist", methods=["POST"])
def add_to_watchlist():
    """
    Fetch rich data for a stock symbol from Massive (latest price + ticker details)
    and add/update that symbol on the watchlist in Lakebase with comprehensive info.
    """
    ensure_watchlist_table()
    ensure_ticker_details_table()

    if request.is_json:
        symbol = request.json.get("symbol", "")
    else:
        symbol = request.form.get("symbol", "")

    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""

    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400

    client = MassiveClient()
    
    # Fetch latest price
    try:
        price_data = client.get_latest_price(symbol)
    except requests.HTTPError:
        return jsonify({"error": f"Unknown ticker symbol: {symbol}"}), 400

    price = _extract_latest_price(price_data)
    if price is None:
        return jsonify({"error": f"No price data available for ticker: {symbol}"}), 400

    # Extract additional metrics from the price data
    results = price_data.get("results", [])
    bar = results[0] if isinstance(results, list) and results else {}
    
    volume = bar.get("v")
    day_high = bar.get("h")
    day_low = bar.get("l")
    open_price = bar.get("o")
    price_change = price - open_price if open_price else None
    price_change_percent = (price_change / open_price * 100) if open_price and price_change else None
    
    # Fetch ticker details for company name and additional info
    company_name = None
    market_cap = None
    try:
        details_data = client.get_ticker_details(symbol)
        details = details_data.get("results", {})
        company_name = details.get("name")
        market_cap = details.get("market_cap")
        
        # Store full ticker details in separate table
        _store_ticker_details(symbol, details)
    except requests.HTTPError:
        logger.warning(f"Could not fetch ticker details for {symbol}")

    email = _current_user_email()

    lakebase.run_write(
        f"""
        INSERT INTO {WATCHLIST_TABLE_NAME} (
            symbol, email, latest_price, price_change, price_change_percent,
            volume, market_cap, day_high, day_low, company_name, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (symbol, email) DO UPDATE
            SET latest_price = EXCLUDED.latest_price,
                price_change = EXCLUDED.price_change,
                price_change_percent = EXCLUDED.price_change_percent,
                volume = EXCLUDED.volume,
                market_cap = EXCLUDED.market_cap,
                day_high = EXCLUDED.day_high,
                day_low = EXCLUDED.day_low,
                company_name = EXCLUDED.company_name,
                updated_at = EXCLUDED.updated_at
        """,
        (symbol, email, price, price_change, price_change_percent, volume, 
         market_cap, day_high, day_low, company_name),
    )

    return jsonify({
        "symbol": symbol,
        "email": email,
        "latest_price": price,
        "company_name": company_name,
        "price_change": price_change,
        "price_change_percent": price_change_percent,
        "volume": volume,
        "market_cap": market_cap,
    })


@app.route("/watchlist/<symbol>", methods=["DELETE"])
def delete_from_watchlist(symbol: str):
    """
    Remove a stock symbol from the current user's watchlist.
    """
    ensure_watchlist_table()
    
    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
    
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400
    
    email = _current_user_email()
    
    lakebase.run_write(
        f"""
        DELETE FROM {WATCHLIST_TABLE_NAME}
        WHERE symbol = %s AND email = %s
        """,
        (symbol, email),
    )
    
    return jsonify({"symbol": symbol, "email": email, "deleted": True})


@app.route("/ticker/<symbol>/details", methods=["GET"])
def get_ticker_details(symbol: str):
    """
    Fetch comprehensive company details for a ticker from Massive API
    and cache in Lakebase.
    """
    ensure_ticker_details_table()
    
    symbol = symbol.strip().upper()
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400
    
    # Check if we have cached details
    cache_max_age_hours = int(request.args.get("cache_hours", 24))
    cached = lakebase.run_query(
        """
        SELECT symbol, name, description, market, primary_exchange, 
               currency_name, homepage_url, logo_url, market_cap,
               total_employees, raw_data, synced_at
        FROM ticker_details
        WHERE symbol = %s
          AND synced_at > now() - interval '%s hours'
        """,
        (symbol, cache_max_age_hours),
    )
    
    if cached:
        return jsonify(cached[0])
    
    # Fetch fresh data from Massive
    client = MassiveClient()
    try:
        data = client.get_ticker_details(symbol)
        details = data.get("results", {})
        _store_ticker_details(symbol, details)
        
        return jsonify(details)
    except requests.HTTPError as e:
        return jsonify({"error": f"Failed to fetch ticker details: {str(e)}"}), 400


@app.route("/ticker/<symbol>/history", methods=["GET"])
def get_ticker_history(symbol: str):
    """
    Fetch and cache historical OHLC price data for a ticker.
    Query params: days (default 30), multiplier (default 1), timespan (default day)
    """
    ensure_historical_prices_table()
    
    symbol = symbol.strip().upper()
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400
    
    days = int(request.args.get("days", 30))
    multiplier = int(request.args.get("multiplier", 1))
    timespan = request.args.get("timespan", "day")
    
    # Calculate date range
    from datetime import datetime, timedelta
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    client = MassiveClient()
    try:
        data = client.get_historical_aggregates(
            symbol=symbol,
            multiplier=multiplier,
            timespan=timespan,
            from_date=from_date,
            to_date=to_date,
            limit=5000,
        )
        
        results = data.get("results", [])
        
        # Store in database
        _store_historical_prices(symbol, results)
        
        return jsonify({
            "symbol": symbol,
            "timespan": timespan,
            "from": from_date,
            "to": to_date,
            "results": results,
        })
    except requests.HTTPError as e:
        return jsonify({"error": f"Failed to fetch historical data: {str(e)}"}), 400


@app.route("/ticker/<symbol>/technicals", methods=["GET"])
def get_ticker_technicals(symbol: str):
    """
    Fetch technical indicators for a ticker.
    Query params: indicator (sma/ema/macd/rsi), window (default 50), timespan (default day)
    """
    symbol = symbol.strip().upper()
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400
    
    indicator = request.args.get("indicator", "sma")
    window = int(request.args.get("window", 50))
    timespan = request.args.get("timespan", "day")
    
    client = MassiveClient()
    try:
        data = client.get_technical_indicator(
            symbol=symbol,
            indicator=indicator,
            timespan=timespan,
            window=window,
        )
        return jsonify(data)
    except requests.HTTPError as e:
        return jsonify({"error": f"Failed to fetch technical indicators: {str(e)}"}), 400


@app.route("/ticker/<symbol>/news", methods=["GET"])
def get_ticker_news(symbol: str):
    """
    Fetch and cache news articles for a ticker.
    Query params: limit (default 10)
    """
    ensure_news_table()
    
    symbol = symbol.strip().upper()
    if not symbol or not _TICKER_RE.match(symbol):
        return jsonify({"error": f"Invalid ticker symbol: {symbol!r}"}), 400
    
    limit = int(request.args.get("limit", 10))
    
    client = MassiveClient()
    try:
        data = client.get_market_news(symbol=symbol, limit=limit)
        results = data.get("results", [])
        
        # Store in database
        _store_news_articles(results)
        
        return jsonify({"symbol": symbol, "articles": results})
    except requests.HTTPError as e:
        return jsonify({"error": f"Failed to fetch news: {str(e)}"}), 400


def _extract_latest_price(data: dict) -> float | None:
    """Pull the trade price out of the Massive 'previous close' response shape.

    The /v2/aggs/ticker/{symbol}/prev endpoint returns "results" as a LIST
    containing a single aggregate bar (not a dict), e.g.:
        {"status": "OK", "resultsCount": 1, "results": [{"c": 148.845, ...}]}
    Previously this code treated "results" as a dict, so isinstance(results, dict)
    was always False for this endpoint's real shape and the price silently
    resolved to None. Unwrap the list here, and check "status"/"resultsCount"
    so invalid tickers (empty results) are detected instead of "succeeding"
    with a null price.

    Adjust the key lookup here if the real Massive API returns a different
    field name for the traded/close price.
    """
    if not isinstance(data, dict):
        return None
    if data.get("status") not in (None, "OK") or data.get("resultsCount") == 0:
        return None
    results = data.get("results", data)
    if isinstance(results, list):
        results = results[0] if results else None
    if isinstance(results, dict):
        for key in ("c", "p", "price", "last_price", "vw"):
            if key in results:
                return results[key]
    return None


def _upsert_batch(items: list[dict]) -> int:
    """Upsert a batch of Massive API items into Lakebase, one statement per row.

    For very large batches, consider psycopg2.extras.execute_values for
    higher throughput instead of per-row execute calls.
    """
    import json as _json

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


def _store_ticker_details(symbol: str, details: dict):
    """Store comprehensive ticker details in Lakebase."""
    import json as _json
    
    address = details.get("address")
    
    lakebase.run_write(
        """
        INSERT INTO ticker_details (
            symbol, name, description, market, locale, primary_exchange,
            currency_name, cik, homepage_url, logo_url, market_cap,
            phone_number, address, sic_code, sic_description,
            total_employees, list_date, share_class_shares_outstanding,
            weighted_shares_outstanding, raw_data, synced_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (symbol) DO UPDATE
            SET name = EXCLUDED.name,
                description = EXCLUDED.description,
                market = EXCLUDED.market,
                locale = EXCLUDED.locale,
                primary_exchange = EXCLUDED.primary_exchange,
                currency_name = EXCLUDED.currency_name,
                cik = EXCLUDED.cik,
                homepage_url = EXCLUDED.homepage_url,
                logo_url = EXCLUDED.logo_url,
                market_cap = EXCLUDED.market_cap,
                phone_number = EXCLUDED.phone_number,
                address = EXCLUDED.address,
                sic_code = EXCLUDED.sic_code,
                sic_description = EXCLUDED.sic_description,
                total_employees = EXCLUDED.total_employees,
                list_date = EXCLUDED.list_date,
                share_class_shares_outstanding = EXCLUDED.share_class_shares_outstanding,
                weighted_shares_outstanding = EXCLUDED.weighted_shares_outstanding,
                raw_data = EXCLUDED.raw_data,
                synced_at = EXCLUDED.synced_at
        """,
        (
            symbol,
            details.get("name"),
            details.get("description"),
            details.get("market"),
            details.get("locale"),
            details.get("primary_exchange"),
            details.get("currency_name"),
            details.get("cik"),
            details.get("homepage_url"),
            details.get("branding", {}).get("logo_url"),
            details.get("market_cap"),
            details.get("phone_number"),
            _json.dumps(address) if address else None,
            details.get("sic_code"),
            details.get("sic_description"),
            details.get("total_employees"),
            details.get("list_date"),
            details.get("share_class_shares_outstanding"),
            details.get("weighted_shares_outstanding"),
            _json.dumps(details),
        ),
    )


def _store_historical_prices(symbol: str, results: list[dict]):
    """Store historical OHLC price bars in Lakebase."""
    from datetime import datetime
    
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for bar in results:
                timestamp_ms = bar.get("t")
                if not timestamp_ms:
                    continue
                    
                timestamp = datetime.fromtimestamp(timestamp_ms / 1000)
                
                cur.execute(
                    """
                    INSERT INTO historical_prices (
                        symbol, timestamp, open, high, low, close,
                        volume, vwap, transactions, synced_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (symbol, timestamp) DO UPDATE
                        SET open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            volume = EXCLUDED.volume,
                            vwap = EXCLUDED.vwap,
                            transactions = EXCLUDED.transactions,
                            synced_at = EXCLUDED.synced_at
                    """,
                    (
                        symbol,
                        timestamp,
                        bar.get("o"),
                        bar.get("h"),
                        bar.get("l"),
                        bar.get("c"),
                        bar.get("v"),
                        bar.get("vw"),
                        bar.get("n"),
                    ),
                )
            conn.commit()


def _store_news_articles(articles: list[dict]):
    """Store news articles in Lakebase."""
    import json as _json
    from datetime import datetime
    
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for article in articles:
                article_id = article.get("id")
                if not article_id:
                    continue
                
                published_utc = article.get("published_utc")
                if published_utc:
                    published_utc = datetime.fromisoformat(published_utc.replace("Z", "+00:00"))
                
                insights = article.get("insights", [{}])
                sentiment = insights[0].get("sentiment") if insights else None
                sentiment_reasoning = insights[0].get("sentiment_reasoning") if insights else None
                
                cur.execute(
                    """
                    INSERT INTO market_news (
                        id, symbols, title, description, author, published_utc,
                        article_url, image_url, keywords, sentiment,
                        sentiment_reasoning, raw_data, synced_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE
                        SET symbols = EXCLUDED.symbols,
                            title = EXCLUDED.title,
                            description = EXCLUDED.description,
                            author = EXCLUDED.author,
                            published_utc = EXCLUDED.published_utc,
                            article_url = EXCLUDED.article_url,
                            image_url = EXCLUDED.image_url,
                            keywords = EXCLUDED.keywords,
                            sentiment = EXCLUDED.sentiment,
                            sentiment_reasoning = EXCLUDED.sentiment_reasoning,
                            raw_data = EXCLUDED.raw_data,
                            synced_at = EXCLUDED.synced_at
                    """,
                    (
                        article_id,
                        article.get("tickers", []),
                        article.get("title"),
                        article.get("description"),
                        article.get("author"),
                        published_utc,
                        article.get("article_url"),
                        article.get("image_url"),
                        article.get("keywords", []),
                        sentiment,
                        sentiment_reasoning,
                        _json.dumps(article),
                    ),
                )
            conn.commit()


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")