import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Any

try:
    from architecture import APP_DIR
except Exception:
    APP_DIR = Path(__file__).resolve().parent

DB_DIR = APP_DIR / "db"
MOEX_DB_PATH = DB_DIR / "moex_candles.db"
BRAIN_DB_PATH = DB_DIR / "predi_brain.db"

IGNORED_TICKERS = {"TMON", "LQDT", "S", "BMM6", "RUB000UTSTOM"}
MOEX_INTERVAL_5M = 5
MOEX_HTTP_TIMEOUT = 8
MOEX_PAGE_LIMIT = 500
DEFAULT_MINUTES = 43200
LOVED_TICKERS_PATH = DB_DIR / "loved_tickers.json"
MOEX_CANDLE_INTERVAL_CANDIDATES = [10, 1]
ROUTE_CACHE_TTL_SECONDS = 14 * 24 * 3600
BACKGROUND_REFRESH_SECONDS = 240

MOEX_HOSTS = [
    "https://iss.moex.com",
    "http://iss.moex.com",
]

DEFAULT_ALIASES = {
    "TMON@": [],
    "TMON": [],
    "LQDT": [],
    "S": [],
    "CNYRUBF": ["CNYRUB_TOM", "CNYRUB_TOD", "CNYRUB"],
    "CNYRUB": ["CNYRUB_TOM", "CNYRUB_TOD", "CNYRUB"],
    "IMOEXF": ["IMOEX", "MXM6", "MXU6", "MXZ6"],
    "BMM6": ["BMM6", "BRM6"],
    "BRM6": ["BRM6"],
}

STATIC_ROUTES = [
    ("stock", "shares", "TQBR"),
    ("stock", "shares", "TQTF"),
    ("stock", "shares", "TQIF"),
    ("stock", "shares", "TQPI"),
    ("stock", "bonds", "TQOB"),
    ("stock", "bonds", "TQCB"),
    ("stock", "bonds", "TQIR"),
    ("stock", "index", "SNDX"),
    ("currency", "selt", "CETS"),
    ("futures", "forts", "RFUD"),
    ("futures", "options", "ROPD"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(callback: Callable[[str], None] | None, text: str) -> None:
    msg = f"moex // db: {text}"
    try:
        print(f"[MOEX//DB] {text}")
    except Exception:
        pass
    if callback:
        try:
            callback(msg)
        except Exception:
            pass


def _normalize_ticker(ticker: str) -> str:
    raw = str(ticker or "").upper().strip().replace(" ", "")
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    raw = re.sub(r"[^A-Z0-9_\\-]", "", raw)
    return raw


def _is_train_ticker(ticker: str) -> bool:
    ticker = _normalize_ticker(ticker)
    return bool(ticker and len(ticker) >= 2 and ticker not in IGNORED_TICKERS)


def _clean_tickers(tickers: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for value in tickers or []:
        ticker = _normalize_ticker(value)
        if not _is_train_ticker(ticker):
            continue
        if ticker not in out:
            out.append(ticker)
    return out


def load_loved_tickers() -> list[str]:
    try:
        if not LOVED_TICKERS_PATH.exists():
            return []
        data = json.loads(LOVED_TICKERS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return _clean_tickers(data)
        if isinstance(data, dict):
            return _clean_tickers(data.get("tickers") or [])
    except Exception:
        pass
    return []


def save_loved_tickers(tickers: list[str]) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    clean = _clean_tickers(tickers)
    LOVED_TICKERS_PATH.write_text(json.dumps({"tickers": clean, "updated_at": _now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")


def add_loved_tickers(tickers: list[str] | tuple[str, ...] | None) -> list[str]:
    current = load_loved_tickers()
    added: list[str] = []
    for ticker in _clean_tickers(list(tickers or [])):
        if ticker not in current:
            current.append(ticker)
            added.append(ticker)
    if added:
        save_loved_tickers(current)
    return added


def ensure_db() -> None:
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
        conn.execute("DELETE FROM moex_candles WHERE UPPER(ticker) IN ('TMON','LQDT','S')")
        conn.commit()


def _event(kind: str, ticker: str, text: str, payload: dict | None = None) -> None:
    try:
        ensure_db()
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO moex_events(created_at, kind, ticker, text, payload_json) VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), kind, ticker, text, json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
            conn.commit()
    except Exception:
        pass


def _manual_aliases() -> dict[str, list[str]]:
    path = DB_DIR / "moex_aliases.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out: dict[str, list[str]] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                key = str(key).upper().strip()
                if isinstance(value, str):
                    out[key] = [value.upper()]
                elif isinstance(value, list):
                    out[key] = [str(x).upper().strip() for x in value if str(x).strip()]
        return out
    except Exception:
        return {}


def _urlopen_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 JTrade-MOEX-DB/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(req, timeout=MOEX_HTTP_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _search_secids(ticker: str, log_callback=None) -> list[str]:
    out: list[str] = []
    variants = [ticker]
    if ticker.endswith("F"):
        variants.append(ticker[:-1])
    if ticker.endswith("RUBF"):
        variants.append(ticker.replace("RUBF", "RUB"))

    for query in variants:
        for host in MOEX_HOSTS:
            try:
                params = urllib.parse.urlencode({"q": query, "limit": 50, "iss.meta": "off"})
                url = f"{host}/iss/securities.json?{params}"
                data = _urlopen_json(url)
                block = data.get("securities") or {}
                columns = block.get("columns") or []
                rows = block.get("data") or []
                idx = {name: i for i, name in enumerate(columns)}
                secid_idx = idx.get("secid")
                if secid_idx is None:
                    continue
                for row in rows:
                    secid = str(row[secid_idx] or "").upper().strip()
                    if secid and secid not in out:
                        out.append(secid)
                if out:
                    return out
            except Exception as exc:
                _log(log_callback, f"{ticker}: search SECID через {host} не ответил: {exc}")
                continue
    return out


def _secid_candidates(ticker: str, log_callback=None) -> list[str]:
    ticker = _normalize_ticker(ticker)
    if not _is_train_ticker(ticker):
        return []
    out: list[str] = []

    def add(value):
        secid = _normalize_ticker(value)
        if secid and secid not in out and secid not in IGNORED_TICKERS:
            out.append(secid)

    aliases = _manual_aliases()
    for value in aliases.get(ticker, []):
        add(value)
    for value in DEFAULT_ALIASES.get(ticker, []):
        add(value)

    add(ticker)
    if ticker.endswith("F"):
        add(ticker[:-1])
    if ticker.endswith("RUBF"):
        add(ticker.replace("RUBF", "RUB"))
    if ticker.endswith("RUB"):
        add(ticker + "_TOM")
        add(ticker + "_TOD")
    if ticker.startswith("BM") and len(ticker) >= 4:
        add("BR" + ticker[2:])
    if ticker.startswith("SU"):
        add(ticker)

    for value in _search_secids(ticker, log_callback=log_callback):
        add(value)

    return out[:40]


def _board_routes_for_secid(secid: str, log_callback=None) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    for host in MOEX_HOSTS:
        try:
            encoded = urllib.parse.quote(secid, safe="")
            url = f"{host}/iss/securities/{encoded}.json?iss.meta=off"
            data = _urlopen_json(url)
            block = data.get("boards") or {}
            columns = block.get("columns") or []
            rows = block.get("data") or []
            idx = {name: i for i, name in enumerate(columns)}
            for row in rows:
                try:
                    board = str(row[idx.get("boardid")]).upper().strip() if idx.get("boardid") is not None else ""
                    engine = str(row[idx.get("engine")]).lower().strip() if idx.get("engine") is not None else ""
                    market = str(row[idx.get("market")]).lower().strip() if idx.get("market") is not None else ""
                    is_traded = row[idx.get("is_traded")] if idx.get("is_traded") is not None else 1
                    if engine and market and board and str(is_traded) not in {"0", "False", "false"}:
                        route = (engine, market, board)
                        if route not in out:
                            out.append(route)
                except Exception:
                    continue
            if out:
                break
        except Exception as exc:
            _log(log_callback, f"{secid}: boards lookup через {host} не ответил: {exc}")
            continue

    for route in STATIC_ROUTES:
        if route not in out:
            out.append(route)
    # No-board fallback as None board.
    for engine, market, _board in STATIC_ROUTES:
        route = (engine, market, None)
        if route not in out:
            out.append(route)
    return out


def _candles_url(host: str, engine: str, market: str, board: str | None, secid: str, from_date: str, till_date: str, start: int, moex_interval: int) -> str:
    encoded = urllib.parse.quote(secid, safe="")
    params = urllib.parse.urlencode({
        "from": from_date,
        "till": till_date,
        "interval": int(moex_interval),
        "start": start,
        "limit": MOEX_PAGE_LIMIT,
        "iss.meta": "off",
    })
    if board:
        return f"{host}/iss/engines/{engine}/markets/{market}/boards/{board}/securities/{encoded}/candles.json?{params}"
    return f"{host}/iss/engines/{engine}/markets/{market}/securities/{encoded}/candles.json?{params}"


def _parse_candle_rows(payload: dict, source: str) -> list[dict]:
    block = payload.get("candles") or {}
    columns = block.get("columns") or []
    rows = block.get("data") or []
    idx = {name: i for i, name in enumerate(columns)}
    required = ["begin", "open", "high", "low", "close"]
    if not rows or any(x not in idx for x in required):
        return []
    out: list[dict] = []
    for row in rows:
        try:
            out.append({
                "begin": str(row[idx["begin"]]).replace("T", " ")[:19],
                "end": str(row[idx["end"]]).replace("T", " ")[:19] if "end" in idx else "",
                "open": float(row[idx["open"]] or 0.0),
                "high": float(row[idx["high"]] or 0.0),
                "low": float(row[idx["low"]] or 0.0),
                "close": float(row[idx["close"]] or 0.0),
                "volume": float(row[idx["volume"]] or 0.0) if "volume" in idx else 0.0,
                "value": float(row[idx["value"]] or 0.0) if "value" in idx else 0.0,
                "source": source,
            })
        except Exception:
            continue
    return out


def _fetch_route(secid: str, route: tuple[str, str, str | None], minutes: int, log_callback=None) -> list[dict]:
    engine, market, board = route
    till_dt = datetime.now(timezone.utc) + timedelta(days=1)
    from_dt = datetime.now(timezone.utc) - timedelta(minutes=max(5, int(minutes)))
    from_date = from_dt.strftime("%Y-%m-%d")
    till_date = till_dt.strftime("%Y-%m-%d")
    all_rows: list[dict] = []

    route_label = f"{engine}/{market}" + (f"/{board}" if board else "")
    for moex_interval in MOEX_CANDLE_INTERVAL_CANDIDATES:
        for host in MOEX_HOSTS:
            start = 0
            host_rows: list[dict] = []
            for page in range(30):
                url = _candles_url(host, engine, market, board, secid, from_date, till_date, start, moex_interval=moex_interval)
                try:
                    payload = _urlopen_json(url)
                    rows = _parse_candle_rows(payload, source=f"moex:{route_label}:{secid}:i{moex_interval}")
                    if page == 0:
                        _log(log_callback, f"{secid}: проверяю {route_label}, interval={moex_interval}, {host} → {len(rows)} свечей")
                    if not rows:
                        break
                    host_rows.extend(rows)
                    if len(rows) < MOEX_PAGE_LIMIT:
                        break
                    start += len(rows)
                except Exception as exc:
                    if page == 0:
                        _log(log_callback, f"{secid}: {route_label}, interval={moex_interval}, {host} ошибка: {exc}")
                    break
            if host_rows:
                all_rows.extend(host_rows)
                break
        if all_rows:
            break

    # De-duplicate by begin.
    unique: dict[str, dict] = {}
    for row in all_rows:
        unique[row["begin"]] = row
    return [unique[key] for key in sorted(unique)]


def _store_candles(alias_ticker: str, secid: str, candles: list[dict]) -> int:
    if not candles:
        return 0
    ensure_db()
    now = _now_iso()
    alias_ticker = _normalize_ticker(alias_ticker)
    secid = _normalize_ticker(secid)
    before = _row_count(alias_ticker)
    with sqlite3.connect(MOEX_DB_PATH) as conn:
        for ticker in {alias_ticker, secid}:
            if not ticker or ticker in IGNORED_TICKERS:
                continue
            for c in candles:
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
                        5,
                        c.get("begin", ""),
                        c.get("end", ""),
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
            conn.execute(
                """
                INSERT INTO moex_meta(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (f"fetch:{ticker}:5", str(time.time()), now),
            )
        conn.commit()
    after = _row_count(alias_ticker)
    return max(0, after - before)


def _row_count(ticker: str) -> int:
    ticker = _normalize_ticker(ticker)
    try:
        ensure_db()
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            row = conn.execute("SELECT COUNT(*) FROM moex_candles WHERE UPPER(ticker)=? AND interval=5", (ticker,)).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0



def _meta_get(key: str) -> str | None:
    try:
        ensure_db()
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            row = conn.execute("SELECT value FROM moex_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None
    except Exception:
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        ensure_db()
        with sqlite3.connect(MOEX_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO moex_meta(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, _now_iso()),
            )
            conn.commit()
    except Exception:
        pass


def _last_download_ts(ticker: str) -> float:
    value = _meta_get(f"engine_fetch:{_normalize_ticker(ticker)}")
    try:
        return float(value) if value else 0.0
    except Exception:
        return 0.0


def _set_last_download_ts(ticker: str) -> None:
    _meta_set(f"engine_fetch:{_normalize_ticker(ticker)}", str(time.time()))


def _load_cached_route(ticker: str) -> tuple[str, tuple[str, str, str | None]] | None:
    raw = _meta_get(f"engine_route:{_normalize_ticker(ticker)}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if time.time() - float(payload.get("saved_at") or 0.0) > ROUTE_CACHE_TTL_SECONDS:
            return None
        secid = _normalize_ticker(payload.get("secid") or "")
        engine = str(payload.get("engine") or "").lower().strip()
        market = str(payload.get("market") or "").lower().strip()
        board = payload.get("board")
        board = str(board).upper().strip() if board else None
        if secid and engine and market:
            return secid, (engine, market, board)
    except Exception:
        return None
    return None


def _save_cached_route(ticker: str, secid: str, route: tuple[str, str, str | None]) -> None:
    engine, market, board = route
    payload = {
        "ticker": _normalize_ticker(ticker),
        "secid": _normalize_ticker(secid),
        "engine": str(engine),
        "market": str(market),
        "board": board,
        "saved_at": time.time(),
    }
    _meta_set(f"engine_route:{_normalize_ticker(ticker)}", json.dumps(payload, ensure_ascii=False))


def download_ticker_to_db(ticker: str, minutes: int = DEFAULT_MINUTES, log_callback=None, force: bool = False) -> dict[str, Any]:
    ticker = _normalize_ticker(ticker)
    if not _is_train_ticker(ticker):
        _log(log_callback, f"{ticker}: пропуск, тикер исключён или некорректный")
        return {"ticker": ticker, "ok": False, "rows": 0, "added": 0, "reason": "ignored_or_bad"}

    ensure_db()
    before = _row_count(ticker)
    age = time.time() - _last_download_ts(ticker)
    if not force and before >= 140 and age < BACKGROUND_REFRESH_SECONDS:
        _log(log_callback, f"{ticker}: свежий кэш DB={before}, прошло {age:.0f} сек — повторную докачку пропускаю")
        return {"ticker": ticker, "ok": True, "rows": before, "added": 0, "reason": "fresh_cache"}

    _log(log_callback, f"{ticker}: старт фоновой загрузки, DB={before} свечей")

    cached = _load_cached_route(ticker)
    if cached:
        secid, route = cached
        route_label = f"{route[0]}/{route[1]}" + (f"/{route[2]}" if route[2] else "")
        _log(log_callback, f"{ticker}: использую найденный ранее маршрут SECID={secid}, route={route_label}")
        candles = _fetch_route(secid, route, minutes=minutes, log_callback=log_callback)
        if candles:
            added = _store_candles(ticker, secid, candles)
            rows = _row_count(ticker)
            _set_last_download_ts(ticker)
            _log(log_callback, f"{ticker}: обновил по сохранённому маршруту, DB {before} → {rows}, +{added}")
            return {"ticker": ticker, "ok": True, "rows": rows, "added": added, "secid": secid, "route": route_label, "cached_route": True}
        _log(log_callback, f"{ticker}: сохранённый маршрут пустой, один раз переищу")

    secids = _secid_candidates(ticker, log_callback=log_callback)
    _log(log_callback, f"{ticker}: SECID кандидаты: {', '.join(secids[:12]) if secids else 'нет'}")

    for secid in secids:
        routes = _board_routes_for_secid(secid, log_callback=log_callback)
        _log(log_callback, f"{ticker}: {secid}: маршрутов к проверке {len(routes)}")
        for route in routes:
            candles = _fetch_route(secid, route, minutes=minutes, log_callback=log_callback)
            if not candles:
                continue
            added = _store_candles(ticker, secid, candles)
            rows = _row_count(ticker)
            route_label = f"{route[0]}/{route[1]}" + (f"/{route[2]}" if route[2] else "")
            _save_cached_route(ticker, secid, route)
            _set_last_download_ts(ticker)
            _event("engine_download_ok", ticker, f"{ticker}: {rows} свечей, +{added}, SECID={secid}, route={route_label}", {"secid": secid, "route": route_label, "rows": rows, "added": added})
            _log(log_callback, f"{ticker}: УСПЕХ: SECID={secid}, route={route_label}, DB {before} → {rows}, +{added}. Маршрут сохранён.")
            return {"ticker": ticker, "ok": True, "rows": rows, "added": added, "secid": secid, "route": route_label}

    rows = _row_count(ticker)
    _event("engine_download_empty", ticker, f"{ticker}: свечи не скачались", {"secids": secids})
    _set_last_download_ts(ticker)
    _log(log_callback, f"{ticker}: НЕ СКАЧАЛОСЬ. DB={rows}. Нужен alias или MOEX не отвечает.")
    return {"ticker": ticker, "ok": rows > 0, "rows": rows, "added": max(0, rows - before), "reason": "empty"}


def _brain_confidence(ticker: str) -> float:
    try:
        from . import predi_brain
        func = getattr(predi_brain, "_ticker_learning_confidence", None)
        if func:
            return float(func(ticker))
    except Exception:
        pass
    return 0.0


def run_manual_moex_db_job(
    tickers: list[str] | tuple[str, ...] | None,
    log_callback: Callable[[str], None] | None = None,
    max_workers: int = 1,
    minutes: int = DEFAULT_MINUTES,
) -> dict[str, Any]:
    clean = _clean_tickers(list(tickers or []))
    if not clean:
        _log(log_callback, "нет тикеров после фильтрации")
        return {"tickers": [], "downloaded": 0, "history_inserted": 0, "training_rows": 0}

    ensure_db()
    _log(log_callback, f"engine режим: {len(clean)} тикеров, фоновая качка, история={minutes} минут")
    before_conf = {ticker: _brain_confidence(ticker) for ticker in clean}

    # Debug-first: one ticker at a time. Easier to see which route fails.
    results: list[dict[str, Any]] = []
    for index, ticker in enumerate(clean, start=1):
        _log(log_callback, f"[{index}/{len(clean)}] {ticker}: начинаю")
        result = download_ticker_to_db(ticker, minutes=minutes, log_callback=log_callback, force=False)
        results.append(result)
        _log(log_callback, f"[{index}/{len(clean)}] {ticker}: закончил, ok={result.get('ok')}, rows={result.get('rows')}, added={result.get('added')}")

    downloaded_tickers = [r["ticker"] for r in results if int(r.get("rows") or 0) >= 140]
    _log(log_callback, f"скачивание завершено: для обучения пригодны {len(downloaded_tickers)}/{len(clean)} тикеров")

    history_inserted = 0
    training_rows = 0
    if downloaded_tickers:
        try:
            from . import predi_brain
            _log(log_callback, f"применяю вероятностную модель к истории: {', '.join(downloaded_tickers)}")
            history_inserted = int(predi_brain.backfill_history_experience(force=True, preferred_tickers=downloaded_tickers) or 0)
            _log(log_callback, f"history backfill добавил наблюдений: {history_inserted}")
            predi_brain.rebuild_metrics()
            training_rows = int(predi_brain.train_model_from_observations() or 0)
            _log(log_callback, f"обучение завершено: training_rows={training_rows}")
        except Exception as exc:
            _log(log_callback, f"ошибка обучения после загрузки: {exc}")

    for ticker in clean:
        after_conf = _brain_confidence(ticker)
        delta = after_conf - before_conf.get(ticker, 0.0)
        _log(
            log_callback,
            f"Модель научилась/проверила {ticker}: candles={_row_count(ticker)}, "
            f"Learning confidence {before_conf.get(ticker, 0.0):.1f}% → {after_conf:.1f}% ({delta:+.1f} п.п.)"
        )

    return {
        "tickers": clean,
        "results": results,
        "downloaded": len(downloaded_tickers),
        "history_inserted": history_inserted,
        "training_rows": training_rows,
    }


def run_loved_background_once(log_callback=None, max_tickers: int = 10, minutes: int = DEFAULT_MINUTES) -> dict[str, Any]:
    loved = load_loved_tickers()
    if not loved:
        _log(log_callback, "любимых тикеров пока нет; фоновой докачке нечего делать")
        return {"tickers": [], "downloaded": 0, "history_inserted": 0, "training_rows": 0}
    selected = loved[:max(1, int(max_tickers or 10))]
    _log(log_callback, f"фон: проверяю любимые тикеры: {', '.join(selected)}")
    return run_manual_moex_db_job(selected, log_callback=log_callback, max_workers=1, minutes=minutes)


if __name__ == "__main__":
    import sys
    tickers = sys.argv[1:] or ["SMLT"]
    run_manual_moex_db_job(tickers, log_callback=lambda text: print(text), max_workers=1)
