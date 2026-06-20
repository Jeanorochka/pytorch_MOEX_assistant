import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from architecture import APP_DIR
except Exception:
    APP_DIR = Path(__file__).resolve().parent

DB_DIR = APP_DIR / "db"
MOEX_DB_PATH = DB_DIR / "moex_candles.db"

MOEX_INTERVAL_5M = 5
MOEX_CACHE_STALE_SECONDS = 180.0
MOEX_HTTP_TIMEOUT = 7
MOEX_PAGE_LIMIT = 500
MOEX_FAIL_BACKOFF_SECONDS = 180
_MOEX_DOWNLOAD_LOCK = threading.Lock()

# Futures first because the terminal mostly works with MOEX futures tickers.
MOEX_MARKET_ROUTES = [
    ("futures", "forts"),
    ("stock", "shares"),
    ("currency", "selt"),
]

MOEX_MANUAL_ALIAS_PATH = DB_DIR / "moex_aliases.json"
MOEX_CANDLE_INTERVAL_CANDIDATES = [10, 1]

IGNORED_TICKERS = {"TMON", "LQDT", "S", "BMM6", "RUB000UTSTOM"}


def _is_ignored_ticker(ticker: str) -> bool:
    raw = str(ticker or "").upper().strip()
    raw = raw.split("@", 1)[0].replace(" ", "")
    raw = re.sub(r"[^A-Z0-9_\-]", "", raw)
    return raw in IGNORED_TICKERS


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_moex_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moex_candles (
                ticker TEXT NOT NULL,
                interval INTEGER NOT NULL,
                begin TEXT NOT NULL,
                end TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                value REAL,
                source TEXT NOT NULL DEFAULT 'moex',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(ticker, interval, begin)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_moex_candles_lookup ON moex_candles(ticker, interval, begin)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moex_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moex_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                ticker TEXT,
                text TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        ignored = tuple(sorted(IGNORED_TICKERS))
        placeholders = ",".join("?" for _ in ignored)
        conn.execute(f"DELETE FROM moex_candles WHERE UPPER(ticker) IN ({placeholders})", ignored)
        conn.execute(
            f"DELETE FROM moex_meta WHERE "
            f"UPPER(key) IN ({placeholders}) OR "
            f"UPPER(key) LIKE 'FETCH:TMON:%' OR UPPER(key) LIKE 'FAIL:TMON:%' OR "
            f"UPPER(key) LIKE 'FETCH:LQDT:%' OR UPPER(key) LIKE 'FAIL:LQDT:%' OR "
            f"UPPER(key) LIKE 'FETCH:S:%' OR UPPER(key) LIKE 'FAIL:S:%'",
            ignored,
        )
        conn.commit()





def _parse_dt(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None

def _dt_to_moex_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def _read_cached(ticker: str, interval: int, from_dt: datetime | None = None, limit: int | None = None) -> list[dict]:
    ensure_moex_db()
    ticker = str(ticker or "").upper().strip()
    args: list[Any] = [ticker, interval]
    where = "ticker=? AND interval=?"
    if from_dt is not None:
        where += " AND begin >= ?"
        args.append(from_dt.strftime("%Y-%m-%d %H:%M:%S"))
    if limit:
        sql = f"""
            SELECT begin, end, open, high, low, close, volume, value, source
            FROM (
                SELECT begin, end, open, high, low, close, volume, value, source
                FROM moex_candles
                WHERE {where}
                ORDER BY begin DESC
                LIMIT {int(limit)}
            ) ORDER BY begin ASC
        """
    else:
        sql = f"""
            SELECT begin, end, open, high, low, close, volume, value, source
            FROM moex_candles
            WHERE {where}
            ORDER BY begin ASC
        """
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {
            "time": str(row[0]),
            "begin": str(row[0]),
            "end": str(row[1] or ""),
            "open": float(row[2] or 0.0),
            "high": float(row[3] or 0.0),
            "low": float(row[4] or 0.0),
            "close": float(row[5] or 0.0),
            "volume": float(row[6] or 0.0),
            "value": float(row[7] or 0.0),
            "source": str(row[8] or "moex"),
        }
        for row in rows
    ]

def _last_fetch_ts(ticker: str, interval: int) -> float:
    ensure_moex_db()
    key = f"fetch:{ticker.upper()}:{interval}"
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        row = conn.execute("SELECT value FROM moex_meta WHERE key=?", (key,)).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0

def _set_last_fetch_ts(ticker: str, interval: int) -> None:
    ensure_moex_db()
    key = f"fetch:{ticker.upper()}:{interval}"
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO moex_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(time.time()), utc_now_iso()),
        )
        conn.commit()


def _log_moex_event(kind: str, ticker: str, text: str, payload: dict | None = None) -> None:
    try:
        ensure_moex_db()
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO moex_events(created_at, kind, ticker, text, payload_json) VALUES (?, ?, ?, ?, ?)",
                (utc_now_iso(), str(kind or ""), str(ticker or "").upper(), str(text or ""), json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
            conn.commit()
    except Exception:
        pass
    try:
        print(f"[MOEX] {kind} {ticker}: {text}")
    except Exception:
        pass


def _manual_aliases() -> dict[str, list[str]]:
    if not MOEX_MANUAL_ALIAS_PATH.exists():
        return {}
    try:
        data = json.loads(MOEX_MANUAL_ALIAS_PATH.read_text(encoding="utf-8"))
        out: dict[str, list[str]] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    out[str(key).upper()] = [value.upper()]
                elif isinstance(value, list):
                    out[str(key).upper()] = [str(x).upper() for x in value if str(x).strip()]
        return out
    except Exception:
        return {}


def _simple_secids_from_ticker(ticker: str) -> list[str]:
    raw = str(ticker or "").upper().strip()
    raw = raw.replace(" ", "").replace("-", "")
    if not raw:
        return []
    candidates: list[str] = []
    def add(x: str):
        x = str(x or "").upper().strip()
        if x and x not in candidates:
            candidates.append(x)
    add(raw)
    # T-Invest sometimes uses continuous/marketing tickers. Try a few safe transforms.
    if raw.endswith("F"):
        add(raw[:-1])
    if raw.endswith("RUBF"):
        add(raw[:-1])
        add(raw.replace("RUBF", "RUB"))
    if raw.endswith("RUB"):
        add(raw + "_TOM")
        add(raw + "_TOD")
    if raw in {"CNYRUBF", "CNYRUB"}:
        add("CNYRUB_TOM")
        add("CNYRUB_TOD")
        # Currency futures family is often searched by CR; exact contract still depends on month.
        add("CRM6")
        add("CRU6")
        add("CRZ5")
    if raw.startswith("B") and len(raw) >= 3:
        # Brent futures commonly appear as BR* on MOEX.
        add("BR" + raw[2:])
    return candidates


def _search_moex_secids(query: str) -> list[str]:
    query = str(query or "").upper().strip()
    if not query:
        return []
    out: list[str] = []
    variants = [query]
    if query.endswith("F"):
        variants.append(query[:-1])
    if query.endswith("RUBF"):
        variants.append(query.replace("RUBF", "RUB"))
    for q in variants:
        try:
            params = urllib.parse.urlencode({
                "q": q,
                "limit": 40,
                "iss.meta": "off",
            })
            url = f"https://iss.moex.com/iss/securities.json?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "JTrade/1.0"})
            with urllib.request.urlopen(req, timeout=MOEX_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            block = payload.get("securities") or {}
            columns = block.get("columns") or []
            data = block.get("data") or []
            idx = {name: i for i, name in enumerate(columns)}
            secid_idx = idx.get("secid")
            if secid_idx is None:
                continue
            for row in data:
                secid = str(row[secid_idx] or "").upper().strip()
                if secid and secid not in out:
                    out.append(secid)
        except Exception:
            continue
    return out


def _normalize_ticker_for_moex(ticker: str) -> str:
    raw = str(ticker or "").upper().strip()
    raw = raw.replace(" ", "").strip()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    raw = re.sub(r"[^A-Z0-9_\-]", "", raw)
    return raw


def _is_probably_moex_ticker(ticker: str) -> bool:
    raw = _normalize_ticker_for_moex(ticker)
    if _is_ignored_ticker(raw):
        return False
    if len(raw) < 2:
        return False
    if raw in {"SPB", "OTC", "USD", "EUR"}:
        return False
    return True


def _candidate_secids(ticker: str) -> list[str]:
    original = str(ticker or "").upper().strip()
    ticker = _normalize_ticker_for_moex(original)
    if _is_ignored_ticker(ticker):
        return []
    out: list[str] = []
    def add_many(items):
        for item in items or []:
            secid = _normalize_ticker_for_moex(item)
            if secid and secid not in out and len(secid) >= 2:
                out.append(secid)
    aliases = _manual_aliases()
    add_many(aliases.get(original))
    add_many(aliases.get(ticker))
    add_many(_simple_secids_from_ticker(ticker))
    add_many(_search_moex_secids(ticker))
    if ticker.endswith("F"):
        add_many(_search_moex_secids(ticker[:-1]))
    return out[:35]


def _fetch_security_boards(secid: str) -> list[tuple[str, str, str]]:
    """Return (engine, market, board) candidates discovered from MOEX security description."""
    secid = _normalize_ticker_for_moex(secid)
    if not secid:
        return []
    out: list[tuple[str, str, str]] = []
    try:
        encoded = urllib.parse.quote(secid, safe="")
        url = f"https://iss.moex.com/iss/securities/{encoded}.json?iss.meta=off"
        req = urllib.request.Request(url, headers={"User-Agent": "JTrade/1.0"})
        with urllib.request.urlopen(req, timeout=MOEX_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        boards = payload.get("boards") or {}
        columns = boards.get("columns") or []
        data = boards.get("data") or []
        idx = {name: i for i, name in enumerate(columns)}
        for row in data:
            try:
                board = str(row[idx.get("boardid")]).upper().strip() if idx.get("boardid") is not None else ""
                engine = str(row[idx.get("engine")]).lower().strip() if idx.get("engine") is not None else ""
                market = str(row[idx.get("market")]).lower().strip() if idx.get("market") is not None else ""
                is_traded = row[idx.get("is_traded")] if idx.get("is_traded") is not None else 1
                if board and engine and market and str(is_traded) not in {"0", "False", "false"}:
                    triple = (engine, market, board)
                    if triple not in out:
                        out.append(triple)
            except Exception:
                continue
    except Exception:
        pass
    # Strong fallbacks by common board.
    for triple in (
        ("stock", "shares", "TQBR"),
        ("stock", "shares", "TQTF"),
        ("stock", "shares", "TQIF"),
        ("currency", "selt", "CETS"),
        ("futures", "forts", "RFUD"),
    ):
        if triple not in out:
            out.append(triple)
    return out



def _last_fail_ts(ticker: str, interval: int) -> float:
    ensure_moex_db()
    key = f"fail:{_normalize_ticker_for_moex(ticker)}:{interval}"
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        row = conn.execute("SELECT value FROM moex_meta WHERE key=?", (key,)).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def _set_last_fail_ts(ticker: str, interval: int) -> None:
    ensure_moex_db()
    key = f"fail:{_normalize_ticker_for_moex(ticker)}:{interval}"
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO moex_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(time.time()), utc_now_iso()),
        )
        conn.commit()


def _moex_url(engine: str, market: str, ticker: str, interval: int, from_date: str, till_date: str, start: int, board: str | None = None) -> str:
    encoded = urllib.parse.quote(str(ticker).upper().strip(), safe="")
    params = urllib.parse.urlencode({
        "from": from_date,
        "till": till_date,
        "interval": int(interval),
        "start": int(start),
        "limit": MOEX_PAGE_LIMIT,
        "iss.meta": "off",
    })
    board = str(board or "").upper().strip()
    if board:
        return f"https://iss.moex.com/iss/engines/{engine}/markets/{market}/boards/{board}/securities/{encoded}/candles.json?{params}"
    return f"https://iss.moex.com/iss/engines/{engine}/markets/{market}/securities/{encoded}/candles.json?{params}"


def _fetch_route(engine: str, market: str, ticker: str, interval: int, from_dt: datetime, till_dt: datetime, board: str | None = None) -> list[dict]:
    out: list[dict] = []
    start = 0
    from_date = _dt_to_moex_date(from_dt)
    till_date = _dt_to_moex_date(till_dt)
    query_intervals = MOEX_CANDLE_INTERVAL_CANDIDATES if int(interval) == 5 else [int(interval)]
    for query_interval in query_intervals:
        start = 0
        while True:
            url = _moex_url(engine, market, ticker, query_interval, from_date, till_date, start, board=board)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 JTrade/1.0", "Connection": "close"})
            with urllib.request.urlopen(req, timeout=MOEX_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            block = payload.get("candles") or {}
            columns = block.get("columns") or []
            data = block.get("data") or []
            if not data:
                break
            idx = {name: i for i, name in enumerate(columns)}
            for row in data:
                try:
                    begin = str(row[idx["begin"]])
                    end = str(row[idx["end"]]) if "end" in idx else ""
                    out.append({
                        "time": begin,
                        "begin": begin,
                        "end": end,
                        "open": float(row[idx["open"]] or 0.0),
                        "high": float(row[idx["high"]] or 0.0),
                        "low": float(row[idx["low"]] or 0.0),
                        "close": float(row[idx["close"]] or 0.0),
                        "volume": float(row[idx["volume"]] or 0.0) if "volume" in idx else 0.0,
                        "value": float(row[idx["value"]] or 0.0) if "value" in idx else 0.0,
                        "source": f"moex:{engine}:{market}" + (f":{board}" if board else "") + f":i{query_interval}",
                    })
                except Exception:
                    continue
            if len(data) < MOEX_PAGE_LIMIT:
                break
            start += len(data)
            if start > 5000:
                break
        if out:
            return out
    return out


def _store_candles(ticker: str, interval: int, candles: list[dict]) -> int:
    if not candles:
        return 0
    ensure_moex_db()
    ticker = str(ticker or "").upper().strip()
    now = utc_now_iso()
    inserted = 0
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        for c in candles:
            begin = str(c.get("begin") or c.get("time") or "")
            if not begin:
                continue
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO moex_candles(
                    ticker, interval, begin, end, open, high, low, close, volume, value, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, interval, begin) DO UPDATE SET
                    end=excluded.end,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    value=excluded.value,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    ticker,
                    interval,
                    begin.replace("T", " ")[:19],
                    str(c.get("end") or "").replace("T", " ")[:19],
                    float(c.get("open") or 0.0),
                    float(c.get("high") or 0.0),
                    float(c.get("low") or 0.0),
                    float(c.get("close") or 0.0),
                    float(c.get("volume") or 0.0),
                    float(c.get("value") or 0.0),
                    str(c.get("source") or "moex"),
                    now,
                ),
            )
            if conn.total_changes > before:
                inserted += 1
        conn.commit()
    return inserted



def _route_meta_key(ticker: str, interval: int) -> str:
    return f"route:{_normalize_ticker_for_moex(ticker)}:{int(interval)}"


def _load_cached_route(ticker: str, interval: int) -> tuple[str, str, str, str | None] | None:
    ensure_moex_db()
    key = _route_meta_key(ticker, interval)
    try:
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            row = conn.execute("SELECT value FROM moex_meta WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        secid = _normalize_ticker_for_moex(payload.get("secid") or "")
        engine = str(payload.get("engine") or "").lower().strip()
        market = str(payload.get("market") or "").lower().strip()
        board = payload.get("board")
        board = str(board).upper().strip() if board else None
        if secid and engine and market:
            return secid, engine, market, board
    except Exception:
        return None
    return None


def _save_cached_route(ticker: str, interval: int, secid: str, engine: str, market: str, board: str | None) -> None:
    ensure_moex_db()
    key = _route_meta_key(ticker, interval)
    payload = {"secid": secid, "engine": engine, "market": market, "board": board, "saved_at": time.time()}
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO moex_meta(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(payload, ensure_ascii=False), utc_now_iso()),
        )
        conn.commit()


def fetch_moex_candles(ticker: str, interval: int = MOEX_INTERVAL_5M, minutes: int = 10080) -> list[dict]:
    ensure_moex_db()
    original_ticker = str(ticker or "").upper().strip()
    ticker = _normalize_ticker_for_moex(original_ticker)
    if not ticker or not _is_probably_moex_ticker(ticker):
        reason = "ignored" if _is_ignored_ticker(original_ticker) else "bad_or_non_moex"
        _log_moex_event("skip_ticker", original_ticker, f"пропускаю тикер {original_ticker}: {reason}", {"reason": reason})
        return []
    if time.time() - _last_fail_ts(ticker, interval) < MOEX_FAIL_BACKOFF_SECONDS:
        return _read_cached(ticker, interval, from_dt=datetime.now(timezone.utc) - timedelta(minutes=max(5, int(minutes))))
    till_dt = datetime.now(timezone.utc) + timedelta(days=1)
    from_dt = datetime.now(timezone.utc) - timedelta(minutes=max(5, int(minutes)))
    last_error = None
    tried: list[str] = []
    with _MOEX_DOWNLOAD_LOCK:
        cached_route = _load_cached_route(ticker, interval)
        if cached_route:
            secid, engine, market, board = cached_route
            try:
                candles = _fetch_route(engine, market, secid, interval, from_dt, till_dt, board=board)
                if candles:
                    stored = _store_candles(ticker, interval, candles)
                    if secid != ticker:
                        _store_candles(secid, interval, candles)
                    _set_last_fetch_ts(ticker, interval)
                    _log_moex_event("download_ok_cached_route", ticker, f"обновлено {len(candles)} свечей по сохранённому маршруту SECID={secid}, board={board}, сохранено={stored}", {"secid": secid, "engine": engine, "market": market, "board": board})
                    return _read_cached(ticker, interval, from_dt=from_dt)
            except Exception as exc:
                last_error = exc
                tried.append(f"cached:{engine}/{market}/{board}/{secid}")

        secids = _candidate_secids(ticker) or [ticker]
        for secid in secids:
            board_routes = _fetch_security_boards(secid)
            for engine, market, board in board_routes:
                route_name = f"{engine}/{market}/{board}/{secid}"
                tried.append(route_name)
                try:
                    candles = _fetch_route(engine, market, secid, interval, from_dt, till_dt, board=board)
                    if candles:
                        stored = _store_candles(ticker, interval, candles)
                        if secid != ticker:
                            _store_candles(secid, interval, candles)
                        _save_cached_route(ticker, interval, secid, engine, market, board)
                        _set_last_fetch_ts(ticker, interval)
                        _log_moex_event("download_ok", ticker, f"скачано {len(candles)} свечей MOEX, SECID={secid}, board={board}, сохранено={stored}; маршрут сохранён", {"secid": secid, "engine": engine, "market": market, "board": board, "rows": len(candles)})
                        return _read_cached(ticker, interval, from_dt=from_dt)
                except Exception as exc:
                    last_error = exc
                    continue

            for engine, market in MOEX_MARKET_ROUTES:
                route_name = f"{engine}/{market}/{secid}"
                tried.append(route_name)
                try:
                    candles = _fetch_route(engine, market, secid, interval, from_dt, till_dt, board=None)
                    if candles:
                        stored = _store_candles(ticker, interval, candles)
                        if secid != ticker:
                            _store_candles(secid, interval, candles)
                        _save_cached_route(ticker, interval, secid, engine, market, None)
                        _set_last_fetch_ts(ticker, interval)
                        _log_moex_event("download_ok", ticker, f"скачано {len(candles)} свечей MOEX, SECID={secid}, сохранено={stored}; маршрут сохранён", {"secid": secid, "engine": engine, "market": market, "rows": len(candles)})
                        return _read_cached(ticker, interval, from_dt=from_dt)
                except Exception as exc:
                    last_error = exc
                    continue

    _set_last_fail_ts(ticker, interval)
    _log_moex_event("download_empty", ticker, "MOEX не отдал свечи", {"tried": tried[-80:], "last_error": str(last_error) if last_error else ""})
    if last_error:
        raise RuntimeError(str(last_error))
    return []


def load_moex_candles_cached(
    ticker: str,
    interval: int = MOEX_INTERVAL_5M,
    minutes: int = 10080,
    limit: int | None = None,
    max_stale_seconds: float = MOEX_CACHE_STALE_SECONDS,
    allow_fetch: bool = True,
) -> list[dict]:
    ensure_moex_db()
    original_ticker = str(ticker or "").upper().strip()
    ticker = _normalize_ticker_for_moex(original_ticker)
    if not ticker or not _is_probably_moex_ticker(ticker):
        reason = "ignored" if _is_ignored_ticker(original_ticker) else "bad_or_non_moex"
        _log_moex_event("skip_ticker", original_ticker, f"пропускаю тикер {original_ticker}: {reason}", {"reason": reason})
        return []
    from_dt = datetime.now(timezone.utc) - timedelta(minutes=max(5, int(minutes)))
    cached = _read_cached(ticker, interval, from_dt=from_dt, limit=limit)
    if not allow_fetch:
        return cached
    last_fetch = _last_fetch_ts(ticker, interval)
    enough = bool(cached and (limit is None or len(cached) >= min(limit, 64)))
    if enough and time.time() - last_fetch < max_stale_seconds:
        return cached[-limit:] if limit else cached
    try:
        before = len(cached)
        fetch_moex_candles(ticker, interval=interval, minutes=minutes)
        _set_last_fetch_ts(ticker, interval)
        after_rows = _read_cached(ticker, interval, from_dt=from_dt, limit=limit)
        if len(after_rows) > before:
            _log_moex_event("cache_update", ticker, f"MOEX DB обновлена: {before} → {len(after_rows)} свечей", {"interval": interval})
    except Exception as exc:
        # Offline or MOEX unavailable: use DB only. This protects the terminal from API spam.
        _log_moex_event("cache_fallback", ticker, f"MOEX download failed, беру DB-cache: {exc}", {"interval": interval})
        _set_last_fetch_ts(ticker, interval)
    cached = _read_cached(ticker, interval, from_dt=from_dt, limit=limit)
    return cached[-limit:] if limit else cached


def learning_window_candles(ticker: str, window: int = 128, minutes: int = 10080, allow_fetch: bool = True) -> list[dict]:
    window = max(64, min(128, int(window or 128)))
    return load_moex_candles_cached(
        ticker,
        interval=MOEX_INTERVAL_5M,
        minutes=max(minutes, window * 5 * 3),
        limit=window,
        max_stale_seconds=MOEX_CACHE_STALE_SECONDS,
        allow_fetch=allow_fetch,
    )


def moex_cache_status(ticker: str = "", interval: int = MOEX_INTERVAL_5M) -> dict[str, Any]:
    ensure_moex_db()
    ticker = str(ticker or "").upper().strip()
    if ticker:
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(begin), MAX(begin) FROM moex_candles WHERE ticker=? AND interval=?",
                (ticker, interval),
            ).fetchone()
        return {"ticker": ticker, "interval": interval, "rows": int(row[0] or 0), "from": row[1], "to": row[2], "db_path": str(MOEX_DB_PATH)}
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM moex_candles").fetchone()
    return {"ticker": "", "interval": interval, "rows": int(row[0] or 0), "tickers": int(row[1] or 0), "db_path": str(MOEX_DB_PATH)}
