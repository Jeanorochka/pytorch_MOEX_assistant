import json
import math
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    from architecture import APP_DIR, load_chart_candles_by_ticker, find_instrument, get_instrument_id, post, q_to_decimal
except Exception:  # keeps module importable during isolated syntax checks
    APP_DIR = Path(__file__).resolve().parent
    load_chart_candles_by_ticker = None
    find_instrument = None
    get_instrument_id = None
    post = None
    q_to_decimal = None

try:
    from .predi_torch_model import (
        get_torch_status,
        predict_torch_probability,
        train_torch_model_from_db,
    )
except Exception:
    get_torch_status = None
    predict_torch_probability = None
    train_torch_model_from_db = None

try:
    from .predi_moex import load_moex_candles_cached, learning_window_candles, moex_cache_status
except Exception:
    load_moex_candles_cached = None
    learning_window_candles = None
    moex_cache_status = None

DB_DIR = APP_DIR / "db"
BRAIN_DB_PATH = DB_DIR / "predi_brain.db"
TRADE_DB_PATH = DB_DIR / "jtrade_trades.db"

MODEL_VERSION = 2
MIN_TICKER_SIDE_SAMPLES = 35
MIN_TICKER_SAMPLES = 55
TARGET_MODEL_WEIGHT = 0.30
MAX_MODEL_WEIGHT = 0.45
HORIZON_BARS_DEFAULT = 78
OBSERVATION_LOOKBACK_MINUTES = 10080
ORDERBOOK_CACHE_SECONDS = 2.0
WORKER_SLEEP_SECONDS = 12
HISTORY_BACKFILL_EVERY_SECONDS = 45
HISTORY_BACKFILL_TICKER_LIMIT = 8
HISTORY_MAX_SAMPLES_PER_TICKER_SIDE = 420
HISTORY_STRIDE_BARS = 3

DEFAULT_STATE = {
    "version": MODEL_VERSION,
    "bias": 0.0,
    "target_weight": TARGET_MODEL_WEIGHT,
    "max_weight": MAX_MODEL_WEIGHT,
    "weights": {},
    "feature_names": [],
    "trained_samples": 0,
    "updated_at": "",
}

_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_ORDERBOOK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LLM_CACHE: dict[str, Any] = {"ready": None, "model": None}
_SEEN_TICKERS: set[str] = set()
_LAST_HISTORY_BACKFILL_TS = 0.0
_BACKFILL_BUSY_TICKERS: set[str] = set()
_LAST_CONFIDENCE_BY_TICKER: dict[str, float] = {}

IGNORED_TICKERS = {"TMON", "LQDT", "S", "BMM6", "RUB000UTSTOM"}



def _normalize_train_ticker(ticker: str) -> str:
    raw = str(ticker or "").upper().strip().replace(" ", "")
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    raw = re.sub(r"[^A-Z0-9_\\-]", "", raw)
    return raw


def _is_trainable_ticker(ticker: str) -> bool:
    raw = _normalize_train_ticker(ticker)
    if raw in IGNORED_TICKERS:
        return False
    if len(raw) < 2:
        return False
    if raw in {"SPB", "OTC", "USD", "EUR"}:
        return False
    return True


# ---------------------------- storage ----------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_brain_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_metrics (
                scope TEXT NOT NULL,
                ticker TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                samples INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                accuracy_pct REAL,
                avg_edge REAL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(scope, ticker, side)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                ticker TEXT,
                side TEXT,
                base_probability REAL,
                model_probability REAL,
                final_probability REAL,
                model_weight REAL,
                accuracy_pct REAL,
                samples INTEGER,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brain_observations (
                uid TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'live',
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price REAL,
                target_price REAL,
                stop_price REAL,
                horizon_bars INTEGER NOT NULL DEFAULT 78,
                base_probability REAL,
                model_probability REAL,
                final_probability REAL,
                model_weight REAL,
                features_json TEXT NOT NULL DEFAULT '{}',
                patterns_json TEXT NOT NULL DEFAULT '[]',
                orderbook_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                outcome TEXT,
                label INTEGER,
                evaluated_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_brain_obs_status ON brain_observations(status, ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_brain_obs_ticker_side ON brain_observations(ticker, side, status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brain_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                ticker TEXT,
                side TEXT,
                text TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        ignored = tuple(sorted(IGNORED_TICKERS))
        placeholders = ",".join("?" for _ in ignored)
        conn.execute(f"DELETE FROM brain_observations WHERE UPPER(ticker) IN ({placeholders})", ignored)
        conn.execute(f"DELETE FROM model_metrics WHERE UPPER(ticker) IN ({placeholders})", ignored)
        conn.execute(f"DELETE FROM model_predictions WHERE UPPER(ticker) IN ({placeholders})", ignored)
        conn.commit()


def _load_state() -> dict[str, Any]:
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        row = conn.execute("SELECT value FROM model_state WHERE key = ?", ("active_model",)).fetchone()
    if not row:
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(row[0])
        if not isinstance(data, dict):
            return dict(DEFAULT_STATE)
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_STATE)


def save_model_state(state: dict[str, Any]) -> None:
    ensure_brain_db()
    state = state or dict(DEFAULT_STATE)
    state["version"] = MODEL_VERSION
    state["updated_at"] = utc_now_iso()
    payload = json.dumps(state, ensure_ascii=False)
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO model_state(key, value, updated_at)
            VALUES(?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            ("active_model", payload),
        )
        conn.commit()


# ---------------------------- features ----------------------------

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _norm_key(value: str) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("-", "_")
        .replace(".", "_")
    )


def _component_features(components: list[tuple[str, str, float]] | None, side: str) -> dict[str, float]:
    features: dict[str, float] = {}
    side_mult = 1.0 if side == "BUY" else -1.0
    for name, _value, score in components or []:
        key = _norm_key(name)
        if not key:
            continue
        features[f"component_{key}"] = _clamp(float(score) * side_mult / 22.0, -3.0, 3.0)
    return features


def _pattern_features(patterns: list[str] | None, side: str) -> dict[str, float]:
    features: dict[str, float] = {}
    side_mult = 1.0 if side == "BUY" else -1.0
    for pattern in patterns or []:
        p = str(pattern).lower()
        if not p:
            continue
        bias = 0.0
        if any(x in p for x in ("быч", "bull", "выкуп", "возврат выше", "пробой high", "отскок", "retest support", "ретест поддержки")):
            bias = 1.0
        if any(x in p for x in ("медв", "bear", "отказ", "потеря", "пробой low", "нож", "retest resistance", "ретест сопротивления")):
            bias = -1.0
        if "ложный пробой high" in p or "снятие high" in p:
            bias = -1.2
        if "ложный пробой low" in p or "снятие low" in p:
            bias = 1.2
        if abs(bias) > 0:
            features[f"pattern_{_norm_key(pattern)[:48]}"] = _clamp(bias * side_mult, -2.0, 2.0)
    return features


def _sequence_micro_features(candles: list[dict], side: str = "BUY", idx: int | None = None, window: int = 96) -> dict[str, float]:
    """Encode 64-128 MOEX 5m candles: body, wicks, volume and micro-movement."""
    if not candles:
        return {}
    window = max(64, min(128, int(window or 96)))
    end = len(candles) if idx is None else max(0, min(len(candles), int(idx) + 1))
    start = max(0, end - window)
    chunk = candles[start:end]
    if len(chunk) < 64:
        return {"seq_not_enough_moex_candles": -1.0}

    side_sign = 1.0 if side == "BUY" else -1.0
    opens = [_safe_float(c.get("open")) for c in chunk]
    highs = [_safe_float(c.get("high")) for c in chunk]
    lows = [_safe_float(c.get("low")) for c in chunk]
    closes = [_safe_float(c.get("close")) for c in chunk]
    volumes = [_safe_float(c.get("volume")) for c in chunk]
    ranges = [max(1e-9, h - l) for h, l in zip(highs, lows)]
    avg_range = sum(ranges[-32:]) / max(1, len(ranges[-32:]))
    avg_volume = sum(volumes[-64:]) / max(1, len(volumes[-64:]))
    base_close = max(1e-9, closes[-1])
    features: dict[str, float] = {
        "seq_window_len": len(chunk) / 128.0,
        "seq_avg_range_pct": _clamp((avg_range / base_close) * 1000.0, 0.0, 5.0),
    }

    # Full recent microstructure, newest sequence compressed into stable numeric feature keys.
    for j, c in enumerate(chunk[-window:]):
        o = _safe_float(c.get("open"))
        h = _safe_float(c.get("high"))
        l = _safe_float(c.get("low"))
        cl = _safe_float(c.get("close"))
        v = _safe_float(c.get("volume"))
        rng = max(1e-9, h - l)
        prev_close = closes[max(0, len(chunk) - len(chunk[-window:]) + j - 1)] if j > 0 else o
        body = (cl - o) / max(avg_range, 1e-9)
        upper = (h - max(o, cl)) / max(avg_range, 1e-9)
        lower = (min(o, cl) - l) / max(avg_range, 1e-9)
        ret = (cl - prev_close) / max(avg_range, 1e-9)
        vol_z = (v - avg_volume) / max(avg_volume, 1.0)
        k = f"seq_{j:03d}"
        features[f"{k}_body_dir"] = _clamp(body * side_sign, -4.0, 4.0)
        features[f"{k}_ret_dir"] = _clamp(ret * side_sign, -4.0, 4.0)
        features[f"{k}_upper_wick"] = _clamp(upper, 0.0, 4.0)
        features[f"{k}_lower_wick"] = _clamp(lower, 0.0, 4.0)
        features[f"{k}_range"] = _clamp(rng / max(avg_range, 1e-9), 0.0, 5.0)
        features[f"{k}_volume_z"] = _clamp(vol_z, -4.0, 4.0)

    # Higher-level micro-dynamics summaries.
    last16 = chunk[-16:]
    last32 = chunk[-32:]
    def _sum_body(rows):
        return sum((_safe_float(x.get("close")) - _safe_float(x.get("open"))) for x in rows) / max(avg_range, 1e-9)
    def _sum_volume(rows):
        return sum(_safe_float(x.get("volume")) for x in rows) / max(1.0, avg_volume * len(rows))
    features["seq_body_pressure_16"] = _clamp(_sum_body(last16) * side_sign / 8.0, -4.0, 4.0)
    features["seq_body_pressure_32"] = _clamp(_sum_body(last32) * side_sign / 14.0, -4.0, 4.0)
    features["seq_volume_pressure_16"] = _clamp(_sum_volume(last16) - 1.0, -3.0, 3.0)
    features["seq_volume_pressure_32"] = _clamp(_sum_volume(last32) - 1.0, -3.0, 3.0)
    features["seq_close_vs_64"] = _clamp(((closes[-1] - closes[-64]) / max(avg_range, 1e-9)) * side_sign / 12.0, -4.0, 4.0)
    features["seq_high_break_pressure"] = _clamp((closes[-1] - max(highs[-65:-1])) / max(avg_range, 1e-9) * side_sign, -4.0, 4.0) if len(highs) >= 65 else 0.0
    features["seq_low_break_pressure"] = _clamp((min(lows[-65:-1]) - closes[-1]) / max(avg_range, 1e-9) * side_sign, -4.0, 4.0) if len(lows) >= 65 else 0.0
    return features


def _moex_features_for_ticker(ticker: str, side: str, window: int = 96) -> dict[str, float]:
    if not learning_window_candles:
        return {}
    try:
        candles = learning_window_candles(ticker, window=window, minutes=max(10080, window * 5 * 3), allow_fetch=True)
        return _sequence_micro_features(candles, side=side, idx=None, window=window)
    except Exception:
        return {"seq_moex_error": -1.0}


def _aggregate_candles_for_tf(candles: list[dict], group: int) -> list[dict]:
    """Aggregate cached intraday candles into larger TA structures."""
    if not candles or group <= 1:
        return list(candles or [])
    usable = list(candles)
    start = len(usable) % group
    if start:
        usable = usable[start:]
    out: list[dict] = []
    for i in range(0, len(usable), group):
        chunk = usable[i:i + group]
        if len(chunk) < group:
            continue
        try:
            out.append({
                "time": chunk[-1].get("time") or chunk[-1].get("begin") or "",
                "begin": chunk[0].get("begin") or chunk[0].get("time") or "",
                "open": _safe_float(chunk[0].get("open")),
                "high": max(_safe_float(c.get("high")) for c in chunk),
                "low": min(_safe_float(c.get("low")) for c in chunk),
                "close": _safe_float(chunk[-1].get("close")),
                "volume": sum(_safe_float(c.get("volume")) for c in chunk),
                "source": "ta_aggregated",
            })
        except Exception:
            continue
    return out


def _swing_points(candles: list[dict], left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    if len(candles) < left + right + 5:
        return highs, lows
    high_values = [_safe_float(c.get("high")) for c in candles]
    low_values = [_safe_float(c.get("low")) for c in candles]
    for i in range(left, len(candles) - right):
        h = high_values[i]
        l = low_values[i]
        left_highs = high_values[i - left:i]
        right_highs = high_values[i + 1:i + right + 1]
        left_lows = low_values[i - left:i]
        right_lows = low_values[i + 1:i + right + 1]
        if h >= max(left_highs + right_highs) and h == max(high_values[i - left:i + right + 1]):
            highs.append((i, h))
        if l <= min(left_lows + right_lows) and l == min(low_values[i - left:i + right + 1]):
            lows.append((i, l))
    return highs[-8:], lows[-8:]


def _add_ta_pattern(
    features: dict[str, float],
    patterns: list[str],
    tf_label: str,
    side: str,
    key: str,
    text: str,
    bias: float,
    strength: float,
) -> None:
    """bias > 0 bullish, bias < 0 bearish; side feature says whether it helps current side."""
    side_sign = 1.0 if side == "BUY" else -1.0
    clean_key = _norm_key(f"{tf_label}_{key}")[:64]
    raw = _clamp(float(bias) * float(strength), -2.5, 2.5)
    side_value = _clamp(raw * side_sign, -2.5, 2.5)
    features[f"ta_pattern_{clean_key}_raw"] = raw
    features[f"ta_pattern_{clean_key}_side"] = side_value
    features[f"ta_pattern_{tf_label}_bias_side"] = _clamp(features.get(f"ta_pattern_{tf_label}_bias_side", 0.0) + side_value, -5.0, 5.0)
    label = f"{tf_label}: {text}"
    if label not in patterns:
        patterns.append(label)


def _ta_patterns_from_candles(candles: list[dict], side: str, tf_label: str = "15m") -> tuple[dict[str, float], list[str]]:
    features: dict[str, float] = {}
    patterns: list[str] = []
    if len(candles) < 18:
        return features, patterns

    closes = [_safe_float(c.get("close")) for c in candles]
    highs = [_safe_float(c.get("high")) for c in candles]
    lows = [_safe_float(c.get("low")) for c in candles]
    opens = [_safe_float(c.get("open")) for c in candles]
    last = closes[-1]
    if last <= 0:
        return features, patterns

    avg_range = sum(max(0.0, h - l) for h, l in zip(highs[-20:], lows[-20:])) / max(1, len(highs[-20:]))
    atr_like = max(avg_range, last * 0.0015)
    tol = max(atr_like * 0.85, last * 0.0035)

    swing_highs, swing_lows = _swing_points(candles, left=2, right=2)
    recent_high = max(highs[-24:]) if len(highs) >= 24 else max(highs)
    recent_low = min(lows[-24:]) if len(lows) >= 24 else min(lows)
    prev_high = max(highs[-25:-1]) if len(highs) >= 25 else recent_high
    prev_low = min(lows[-25:-1]) if len(lows) >= 25 else recent_low

    if highs[-1] > prev_high and closes[-1] < prev_high:
        _add_ta_pattern(features, patterns, tf_label, side, "false_breakout_high", "ложный пробой high / снятие ликвидности сверху", -1.0, 1.10)
    if lows[-1] < prev_low and closes[-1] > prev_low:
        _add_ta_pattern(features, patterns, tf_label, side, "false_breakout_low", "ложный пробой low / выкуп ликвидности снизу", 1.0, 1.10)
    if closes[-1] > prev_high:
        _add_ta_pattern(features, patterns, tf_label, side, "breakout_high", "пробой диапазона вверх", 1.0, 0.78)
    if closes[-1] < prev_low:
        _add_ta_pattern(features, patterns, tf_label, side, "breakout_low", "пробой диапазона вниз", -1.0, 0.78)

    if len(swing_highs) >= 2:
        (i1, h1), (i2, h2) = swing_highs[-2], swing_highs[-1]
        valley = min(lows[i1:i2 + 1]) if i2 > i1 else min(lows[-12:])
        if abs(h1 - h2) <= tol * 1.25:
            rejected = closes[-1] < valley + (h2 - valley) * 0.55
            _add_ta_pattern(features, patterns, tf_label, side, "double_top", "двойная вершина / сопротивление", -1.0, 0.85 + (0.35 if rejected else 0.0))

    if len(swing_lows) >= 2:
        (i1, l1), (i2, l2) = swing_lows[-2], swing_lows[-1]
        peak = max(highs[i1:i2 + 1]) if i2 > i1 else max(highs[-12:])
        if abs(l1 - l2) <= tol * 1.25:
            reclaimed = closes[-1] > l2 + (peak - l2) * 0.45
            _add_ta_pattern(features, patterns, tf_label, side, "double_bottom", "двойное дно / поддержка", 1.0, 0.85 + (0.35 if reclaimed else 0.0))

    if len(swing_highs) >= 3:
        (a_i, a), (b_i, b), (c_i, c) = swing_highs[-3], swing_highs[-2], swing_highs[-1]
        shoulders_near = abs(a - c) <= max(tol * 1.8, last * 0.006)
        head_higher = b > max(a, c) + tol * 0.45
        if shoulders_near and head_higher:
            neck_left = min(lows[a_i:b_i + 1]) if b_i > a_i else min(lows[-24:])
            neck_right = min(lows[b_i:c_i + 1]) if c_i > b_i else min(lows[-24:])
            neckline = (neck_left + neck_right) / 2.0
            broken = closes[-1] < neckline
            _add_ta_pattern(features, patterns, tf_label, side, "head_shoulders", "голова-плечи / давление к neckline", -1.0, 1.05 + (0.35 if broken else 0.0))

    if len(swing_lows) >= 3:
        (a_i, a), (b_i, b), (c_i, c) = swing_lows[-3], swing_lows[-2], swing_lows[-1]
        shoulders_near = abs(a - c) <= max(tol * 1.8, last * 0.006)
        head_lower = b < min(a, c) - tol * 0.45
        if shoulders_near and head_lower:
            neck_left = max(highs[a_i:b_i + 1]) if b_i > a_i else max(highs[-24:])
            neck_right = max(highs[b_i:c_i + 1]) if c_i > b_i else max(highs[-24:])
            neckline = (neck_left + neck_right) / 2.0
            broken = closes[-1] > neckline
            _add_ta_pattern(features, patterns, tf_label, side, "inverse_head_shoulders", "перевёрнутая голова-плечи / выкуп к neckline", 1.0, 1.05 + (0.35 if broken else 0.0))

    if len(swing_highs) >= 3 and len(swing_lows) >= 3:
        high_vals = [x[1] for x in swing_highs[-3:]]
        low_vals = [x[1] for x in swing_lows[-3:]]
        flat_highs = max(high_vals) - min(high_vals) <= tol * 1.45
        flat_lows = max(low_vals) - min(low_vals) <= tol * 1.45
        lows_rising = low_vals[-1] > low_vals[0] + tol * 0.35
        highs_falling = high_vals[-1] < high_vals[0] - tol * 0.35
        if flat_highs and lows_rising:
            _add_ta_pattern(features, patterns, tf_label, side, "ascending_triangle", "восходящий треугольник / сжатие под сопротивлением", 1.0, 0.95)
        if flat_lows and highs_falling:
            _add_ta_pattern(features, patterns, tf_label, side, "descending_triangle", "нисходящий треугольник / сжатие над поддержкой", -1.0, 0.95)
        if lows_rising and highs_falling:
            bias = 1.0 if closes[-1] >= (recent_high + recent_low) / 2.0 else -1.0
            _add_ta_pattern(features, patterns, tf_label, side, "symmetrical_triangle", "симметричный треугольник / сжатие волатильности", bias, 0.45)

        higher_highs = high_vals[-1] > high_vals[-2] + tol * 0.25
        lower_highs = high_vals[-1] < high_vals[-2] - tol * 0.25
        higher_lows = low_vals[-1] > low_vals[-2] + tol * 0.25
        lower_lows = low_vals[-1] < low_vals[-2] - tol * 0.25
        if higher_highs and higher_lows:
            _add_ta_pattern(features, patterns, tf_label, side, "hh_hl_trend", "структура HH/HL", 1.0, 0.70)
        if lower_highs and lower_lows:
            _add_ta_pattern(features, patterns, tf_label, side, "lh_ll_trend", "структура LH/LL", -1.0, 0.70)

    body = closes[-1] - opens[-1]
    candle_range = max(1e-9, highs[-1] - lows[-1])
    upper = (highs[-1] - max(opens[-1], closes[-1])) / candle_range
    lower = (min(opens[-1], closes[-1]) - lows[-1]) / candle_range
    if lower >= 0.55 and closes[-1] >= opens[-1]:
        _add_ta_pattern(features, patterns, tf_label, side, "hammer_rejection", "молот / нижний выкуп", 1.0, 0.72)
    if upper >= 0.55 and closes[-1] <= opens[-1]:
        _add_ta_pattern(features, patterns, tf_label, side, "shooting_star_rejection", "падающая звезда / верхний отказ", -1.0, 0.72)

    raw_bias = sum(float(v) for k, v in features.items() if k.endswith("_raw"))
    side_bias = sum(float(v) for k, v in features.items() if k.endswith("_side"))
    features[f"ta_pattern_{tf_label}_raw_bias_total"] = _clamp(raw_bias, -5.0, 5.0)
    features[f"ta_pattern_{tf_label}_side_bias_total"] = _clamp(side_bias, -5.0, 5.0)
    return features, patterns


def _multi_tf_ta_pattern_features(candles_5m: list[dict], side: str) -> tuple[dict[str, float], list[str]]:
    features: dict[str, float] = {}
    patterns: list[str] = []
    source = list(candles_5m or [])
    if len(source) < 30:
        return features, patterns

    for tf_label, group, max_rows in (("5m", 1, 96), ("15m", 3, 120), ("30m", 6, 144), ("60m", 12, 192)):
        tf_candles = _aggregate_candles_for_tf(source[-max_rows:], group)
        tf_features, tf_patterns = _ta_patterns_from_candles(tf_candles, side, tf_label=tf_label)
        features.update(tf_features)
        for item in tf_patterns:
            if item not in patterns:
                patterns.append(item)
    return features, patterns


def _ta_pattern_features_for_ticker(ticker: str, side: str) -> tuple[dict[str, float], list[str]]:
    if not learning_window_candles:
        return {}, []
    try:
        candles = learning_window_candles(ticker, window=240, minutes=14400, allow_fetch=True)
        return _multi_tf_ta_pattern_features(candles, side)
    except Exception:
        return {"ta_pattern_error": -1.0}, ["TA patterns: error"]


def learning_confidence_pct(brain: dict[str, Any] | None = None) -> float:
    brain = brain or {}
    samples = int(brain.get("samples") or brain.get("ticker_samples") or 0)
    ticker_samples = int(brain.get("ticker_samples") or 0)
    torch_info = brain.get("torch_info") or {}
    torch_samples = int(torch_info.get("samples") or 0)
    torch_acc = torch_info.get("val_accuracy_pct")
    acc = brain.get("accuracy_pct")
    sample_score = _clamp(max(samples, ticker_samples, torch_samples) / 2500.0, 0.0, 1.0)
    ticker_score = _clamp(ticker_samples / 500.0, 0.0, 1.0)
    acc_value = None
    try:
        acc_value = float(torch_acc if torch_acc is not None else acc)
    except Exception:
        acc_value = None
    acc_score = 0.25 if acc_value is None else _clamp((acc_value - 45.0) / 25.0, 0.0, 1.0)
    trained_bonus = 0.18 if brain.get("trained") else 0.0
    torch_bonus = 0.17 if torch_info.get("trained") else 0.0
    confidence = (sample_score * 0.45) + (ticker_score * 0.18) + (acc_score * 0.22) + trained_bonus + torch_bonus
    return _clamp(confidence * 100.0, 0.0, 100.0)


def _tp_sl_features(side: str, last_price: float | None, target_price: float | None, stop_price: float | None, base_probability: float | None = None) -> dict[str, float]:
    features: dict[str, float] = {}
    last = _safe_float(last_price)
    tp = _safe_float(target_price)
    sl = _safe_float(stop_price)
    if last <= 0 or tp <= 0 or sl <= 0:
        return features
    if side == "BUY":
        risk = last - sl
        reward = tp - last
    else:
        risk = sl - last
        reward = last - tp
    if risk <= 0 or reward <= 0:
        features["risk_invalid_geometry"] = -3.0
        return features
    rr = reward / risk
    features["rr"] = _clamp((rr - 1.0) / 1.5, -2.0, 2.0)
    features["risk_distance_pct"] = _clamp((risk / last) * 100.0 / 2.0, 0.0, 3.0)
    features["reward_distance_pct"] = _clamp((reward / last) * 100.0 / 3.0, 0.0, 3.0)
    if base_probability is not None:
        features["breakeven_gap"] = _clamp((_safe_float(base_probability) - break_even_wr(rr)) / 25.0, -3.0, 3.0)
    return features


def _orderbook_features(ticker: str, side: str) -> tuple[dict[str, float], dict[str, Any]]:
    ticker = str(ticker or "").upper().strip()
    if not ticker or not (find_instrument and get_instrument_id and post and q_to_decimal):
        return {}, {}

    now = time.time()
    cached = _ORDERBOOK_CACHE.get(ticker)
    if cached and now - cached[0] <= ORDERBOOK_CACHE_SECONDS:
        raw = cached[1]
    else:
        try:
            inst = find_instrument(ticker)
            instrument_id = get_instrument_id(inst)
            raw = post("MarketDataService/GetOrderBook", {"instrumentId": instrument_id, "depth": 20})
            _ORDERBOOK_CACHE[ticker] = (now, raw)
        except Exception as exc:
            return {}, {"error": str(exc)}

    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    bid_qty = sum(_safe_float(x.get("quantity") or x.get("qty")) for x in bids if isinstance(x, dict))
    ask_qty = sum(_safe_float(x.get("quantity") or x.get("qty")) for x in asks if isinstance(x, dict))
    bid = _safe_float(q_to_decimal(bids[0].get("price"))) if bids and isinstance(bids[0], dict) and q_to_decimal else 0.0
    ask = _safe_float(q_to_decimal(asks[0].get("price"))) if asks and isinstance(asks[0], dict) and q_to_decimal else 0.0
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else max(bid, ask, 0.0)
    spread_bps = ((ask - bid) / mid * 10000.0) if bid > 0 and ask > 0 and mid > 0 else 0.0
    imbalance = (bid_qty - ask_qty) / max(1.0, bid_qty + ask_qty)
    side_mult = 1.0 if side == "BUY" else -1.0
    top_bid_qty = _safe_float(bids[0].get("quantity") if bids and isinstance(bids[0], dict) else 0)
    top_ask_qty = _safe_float(asks[0].get("quantity") if asks and isinstance(asks[0], dict) else 0)
    avg_bid_qty = bid_qty / max(1, len(bids))
    avg_ask_qty = ask_qty / max(1, len(asks))
    bid_wall = top_bid_qty / max(1.0, avg_bid_qty)
    ask_wall = top_ask_qty / max(1.0, avg_ask_qty)

    features = {
        "orderbook_imbalance_side": _clamp(imbalance * side_mult * 2.5, -3.0, 3.0),
        "orderbook_spread_penalty": _clamp(-spread_bps / 25.0, -2.0, 0.0),
        "orderbook_bid_wall": _clamp((bid_wall - 1.0) / 2.0 * side_mult, -2.0, 2.0),
        "orderbook_ask_wall": _clamp((ask_wall - 1.0) / 2.0 * -side_mult, -2.0, 2.0),
    }
    info = {
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "imbalance": imbalance,
        "bid_wall": bid_wall,
        "ask_wall": ask_wall,
    }
    return features, info


# ---------------------------- probability / risk helpers ----------------------------

def _sigmoid(value: float) -> float:
    if value > 35:
        return 1.0
    if value < -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def break_even_wr(rr: float) -> float:
    rr = max(0.01, float(rr))
    return 100.0 / (1.0 + rr)


def recommended_rr_from_probability(probability: float | None) -> float:
    p = _safe_float(probability, 50.0)
    if p < 30.0:
        return 0.0
    # p=50 -> 1R, p=70 -> 2R. Clamp keeps the target sane.
    return _clamp((p - 30.0) / 20.0, 0.5, 3.0)


def auto_target_from_probability(side: str, last_price: float, stop_price: float | None, probability: float | None) -> dict[str, Any]:
    last = _safe_float(last_price)
    sl = _safe_float(stop_price)
    rr = recommended_rr_from_probability(probability)
    if last <= 0 or sl <= 0 or rr <= 0:
        return {"available": False, "tp": None, "rr": rr, "text": "auto TP: нужен корректный SL"}
    if side == "BUY":
        risk = last - sl
        if risk <= 0:
            return {"available": False, "tp": None, "rr": rr, "text": "auto TP: SL не ниже входа"}
        tp = last + risk * rr
    else:
        risk = sl - last
        if risk <= 0:
            return {"available": False, "tp": None, "rr": rr, "text": "auto TP: SL не выше входа"}
        tp = last - risk * rr
    return {
        "available": True,
        "tp": tp,
        "rr": rr,
        "break_even_wr": break_even_wr(rr),
        "text": f"auto TP по RR={rr:.2f}; breakeven WR={break_even_wr(rr):.1f}%",
    }


def format_trade_quality_notes(side: str, probability: float, last_price: float, stop_price, target_price, tp_sl: dict | None = None) -> str:
    last = _safe_float(last_price)
    sl = _safe_float(stop_price)
    tp = _safe_float(target_price)
    if _safe_float(probability) < 30.0:
        return "Сделку лучше не открывать: вероятность ниже 30%, edge не подтверждён."
    if last <= 0 or sl <= 0 or tp <= 0:
        auto = auto_target_from_probability(side, last, sl, probability)
        return f"TP/SL неполные. {auto.get('text', 'auto TP недоступен')}"
    if side == "BUY":
        risk = last - sl
        reward = tp - last
    else:
        risk = sl - last
        reward = last - tp
    if risk <= 0 or reward <= 0:
        return "TP/SL стоят не с той стороны: сетап нельзя считать валидным."
    rr = reward / risk
    be = break_even_wr(rr)
    tone = "соотношение TP/SL хорошее" if rr >= 1.4 else "соотношение TP/SL скромное" if rr >= 0.9 else "тейк слишком близко относительно риска"
    extra = ""
    if tp_sl:
        atr = _safe_float(tp_sl.get("atr14"))
        tp_dist = _safe_float(tp_sl.get("tp_distance"))
        sl_dist = _safe_float(tp_sl.get("sl_distance"))
        if atr > 0:
            tp_atr = tp_dist / atr
            sl_atr = sl_dist / atr
            if tp_atr > 4.0:
                extra = f" TP далековат: {tp_atr:.2f} ATR."
            elif sl_atr < 0.30:
                extra = f" SL очень плотный: {sl_atr:.2f} ATR."
            elif 0.45 <= sl_atr <= 2.5 and 0.5 <= tp_atr <= 3.5:
                extra = " Геометрия выглядит рабочей."
    return f"{tone}; RR={rr:.2f}, breakeven WR={be:.1f}%.{extra}"


# ---------------------------- model inference ----------------------------

def _load_metrics(scope: str, ticker: str = "", side: str = "") -> dict[str, Any] | None:
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM model_metrics WHERE scope=? AND ticker=? AND side=?",
            (scope, str(ticker or "").upper(), side if side in {"BUY", "SELL"} else ""),
        ).fetchone()
    return dict(row) if row else None


def _best_metrics(ticker: str, side: str) -> dict[str, Any]:
    ticker = str(ticker or "").upper()
    side = side if side in {"BUY", "SELL"} else ""
    for scope, t, s in (
        ("ticker_side", ticker, side),
        ("ticker", ticker, ""),
        ("global", "", ""),
    ):
        row = _load_metrics(scope, t, s)
        if row:
            if row.get("accuracy_pct") is None and row.get("samples"):
                row["accuracy_pct"] = 100.0 * float(row.get("wins") or 0) / max(1.0, float(row.get("samples") or 0))
            return row
    return {"scope": "empty", "ticker": ticker, "side": side, "samples": 0, "wins": 0, "accuracy_pct": None, "avg_edge": None}


def _ticker_samples(ticker: str, side: str) -> tuple[int, int]:
    ts = _load_metrics("ticker_side", ticker, side) or {}
    t = _load_metrics("ticker", ticker, "") or {}
    return int(ts.get("samples") or 0), int(t.get("samples") or 0)


def _model_probability_from_state(state: dict[str, Any], features: dict[str, float]) -> float | None:
    weights = state.get("weights") or {}
    if not isinstance(weights, dict) or not weights:
        return None
    z = float(state.get("bias") or 0.0)
    used = 0
    for key, value in features.items():
        if key not in weights:
            continue
        try:
            z += float(weights[key]) * float(value)
            used += 1
        except Exception:
            continue
    if used <= 0:
        return None
    return _clamp(_sigmoid(z) * 100.0, 0.0, 100.0)


def get_brain_snapshot(
    ticker: str = "",
    side: str = "BUY",
    components: list[tuple[str, str, float]] | None = None,
    patterns: list[str] | None = None,
    base_probability: float | None = None,
    last_price: float | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    tp_sl: dict | None = None,
    store_observation: bool = False,
) -> dict[str, Any]:
    ensure_brain_db()
    start_brain_worker_once()
    side = side if side in {"BUY", "SELL"} else "BUY"
    ticker = _normalize_train_ticker(ticker)
    if ticker in IGNORED_TICKERS:
        return {
            "ticker": ticker,
            "side": side,
            "trained": False,
            "ticker_ready": False,
            "samples": 0,
            "ticker_side_samples": 0,
            "ticker_samples": 0,
            "wins": 0,
            "accuracy_pct": None,
            "avg_edge": None,
            "model_probability": None,
            "raw_model_probability": None,
            "logistic_probability": None,
            "torch_probability": None,
            "torch_info": {},
            "model_backend": "ignored",
            "model_weight": 0.0,
            "features": {},
            "patterns": [],
            "base_probability": base_probability,
            "orderbook": {},
            "db_path": str(BRAIN_DB_PATH),
            "state_updated_at": "",
            "learning_confidence_pct": 0.0,
            "data_status": "ignored_ticker",
        }
    if ticker and _is_trainable_ticker(ticker):
        _SEEN_TICKERS.add(ticker)
        _kick_ticker_backfill(ticker)
    state = _load_state()

    ta_pattern_features, detected_patterns = _ta_pattern_features_for_ticker(ticker, side)
    combined_patterns = list(dict.fromkeys(list(patterns or []) + list(detected_patterns or [])))

    features = {}
    features.update(_component_features(components, side))
    features.update(_pattern_features(combined_patterns, side))
    features.update(_tp_sl_features(side, last_price, target_price, stop_price, base_probability))
    features.update(_moex_features_for_ticker(ticker, side, window=128))
    features.update(ta_pattern_features)
    orderbook_features, orderbook_info = _orderbook_features(ticker, side)
    features.update(orderbook_features)

    logistic_probability = _model_probability_from_state(state, features)
    torch_info: dict[str, Any] = {}
    torch_probability = None
    if predict_torch_probability:
        try:
            torch_info = predict_torch_probability(BRAIN_DB_PATH, features) or {}
            if torch_info.get("trained") and torch_info.get("probability") is not None:
                torch_probability = float(torch_info.get("probability"))
        except Exception as exc:
            torch_info = {"available": True, "trained": False, "reason": str(exc)}
    model_probability = torch_probability if torch_probability is not None else logistic_probability
    model_backend = "torch" if torch_probability is not None else "logistic"
    metrics = _best_metrics(ticker, side)
    ticker_side_samples, ticker_samples = _ticker_samples(ticker, side)
    samples = int(metrics.get("samples") or 0)
    acc = metrics.get("accuracy_pct")

    ticker_ready = ticker_side_samples >= MIN_TICKER_SIDE_SAMPLES or ticker_samples >= MIN_TICKER_SAMPLES
    trained = bool(ticker_ready and model_probability is not None)
    if trained:
        sample_factor = _clamp(max(ticker_side_samples, ticker_samples) / 260.0, 0.35, 1.0)
        acc_factor = 1.0
        try:
            acc_factor = _clamp((float(acc) - 45.0) / 25.0, 0.45, 1.0)
        except Exception:
            pass
        weight = _clamp(TARGET_MODEL_WEIGHT * sample_factor * acc_factor, 0.10, TARGET_MODEL_WEIGHT)
    else:
        weight = 0.0

    snapshot = {
        "ticker": ticker,
        "side": side,
        "trained": trained,
        "ticker_ready": ticker_ready,
        "samples": samples,
        "ticker_side_samples": ticker_side_samples,
        "ticker_samples": ticker_samples,
        "wins": int(metrics.get("wins") or 0),
        "accuracy_pct": acc,
        "avg_edge": metrics.get("avg_edge"),
        "model_probability": model_probability if trained else None,
        "raw_model_probability": model_probability,
        "logistic_probability": logistic_probability,
        "torch_probability": torch_probability,
        "torch_info": torch_info,
        "model_backend": model_backend,
        "model_weight": weight,
        "features": features,
        "patterns": combined_patterns,
        "base_probability": base_probability,
        "orderbook": orderbook_info,
        "db_path": str(BRAIN_DB_PATH),
        "state_updated_at": state.get("updated_at") or "",
        "learning_confidence_pct": 0.0,
        "data_status": "trained" if trained else "insufficient_ticker_data",
    }

    snapshot["learning_confidence_pct"] = learning_confidence_pct(snapshot)

    if store_observation:
        _store_live_observation(snapshot, last_price, stop_price, target_price, tp_sl)

    return snapshot


def blend_probability_with_brain(base_probability: float, brain: dict[str, Any]) -> dict[str, Any]:
    base = _clamp(float(base_probability), 0.0, 100.0)
    model_probability = brain.get("model_probability")
    weight = float(brain.get("model_weight") or 0.0)
    if model_probability is None or weight <= 0:
        final = base
        impact = 0.0
    else:
        model = _clamp(float(model_probability), 0.0, 100.0)
        final = (base * (1.0 - weight)) + (model * weight)
        final = _clamp(final, 0.0, 100.0)
        impact = final - base
    return {
        "final_probability": final,
        "base_probability": base,
        "impact_pp": impact,
        "model_weight": weight,
    }


def forecast_bias_from_brain(brain: dict[str, Any]) -> float:
    if not brain:
        return 0.0
    prob = brain.get("model_probability") if brain.get("trained") else brain.get("raw_model_probability")
    if prob is None:
        return 0.0
    side_sign = 1.0 if brain.get("side") == "BUY" else -1.0
    strength = ((float(prob) - 50.0) / 50.0) * side_sign
    if not brain.get("trained"):
        strength *= 0.25
    return _clamp(strength, -1.0, 1.0)




def _llm_or_fallback_comment(context: dict[str, Any], fallback: str) -> str:
    """Optional tiny local LLM note. Works only if llama-cpp-python and db/models/tiny.gguf exist."""
    model_path = DB_DIR / "models" / "tiny.gguf"
    if not model_path.exists():
        return fallback
    try:
        if _LLM_CACHE.get("ready") is False:
            return fallback
        if _LLM_CACHE.get("model") is None:
            from llama_cpp import Llama  # type: ignore
            _LLM_CACHE["model"] = Llama(model_path=str(model_path), n_ctx=1024, n_threads=4, verbose=False)
            _LLM_CACHE["ready"] = True
        model = _LLM_CACHE.get("model")
        prompt = (
            "Ты краткий русский торговый аналитик. Без гарантий. "
            "Дай одну короткую заметку по сетапу, максимум 35 слов.\n"
            f"Контекст: {json.dumps(context, ensure_ascii=False, default=str)[:1600]}\n"
            "Заметка:"
        )
        out = model(prompt, max_tokens=90, temperature=0.25, stop=["\n\n", "</s>"])
        text = str(out.get("choices", [{}])[0].get("text", "")).strip()
        if text:
            return text.replace("Мысль:", "").strip()
    except Exception:
        _LLM_CACHE["ready"] = False
    return fallback

def compute_backend_status() -> dict[str, Any]:
    """Report compute backend. PyTorch uses CUDA when available; logistic fallback remains CPU-safe."""
    status = {
        "backend": "CPU",
        "cuda_available": False,
        "gpu_name": "",
        "llm_available": False,
        "torch_available": False,
        "torch_model_trained": False,
        "torch_samples": 0,
        "torch_val_accuracy_pct": None,
    }
    if get_torch_status:
        try:
            torch_status = get_torch_status(BRAIN_DB_PATH) or {}
            status["torch_available"] = bool(torch_status.get("available"))
            status["cuda_available"] = bool(torch_status.get("cuda_available"))
            status["gpu_name"] = str(torch_status.get("gpu_name") or "")
            status["torch_model_trained"] = bool(torch_status.get("model_trained"))
            status["torch_samples"] = int(torch_status.get("samples") or 0)
            status["torch_val_accuracy_pct"] = torch_status.get("val_accuracy_pct")
            if status["torch_available"]:
                status["backend"] = "PyTorch CUDA" if status["cuda_available"] else "PyTorch CPU"
        except Exception:
            pass
    try:
        import llama_cpp  # type: ignore  # noqa: F401
        model_path = DB_DIR / "models" / "tiny.gguf"
        status["llm_available"] = model_path.exists()
    except Exception:
        pass
    return status

def format_accuracy(brain: dict[str, Any]) -> str:
    samples = int((brain or {}).get("samples") or 0)
    acc = (brain or {}).get("accuracy_pct")
    if acc is None:
        return f"accuracy — | n={samples}"
    try:
        return f"accuracy {float(acc):.1f}% | n={samples}"
    except Exception:
        return f"accuracy — | n={samples}"


def build_brain_analysis_text(result: dict[str, Any]) -> str:
    brain = result.get("brain") or {}
    side = "LONG" if result.get("side") == "BUY" else "SHORT"
    patterns = result.get("patterns") or brain.get("patterns") or []
    pattern_text = ", ".join(str(x) for x in patterns[:6]) if patterns else "сильный паттерн не найден"
    samples = int(brain.get("samples") or 0)
    ticker_samples = int(brain.get("ticker_samples") or 0)
    acc = brain.get("accuracy_pct")
    trained = bool(brain.get("trained"))
    base = brain.get("base_probability")
    final = result.get("probability")
    impact = float(brain.get("impact_pp") or 0.0)
    model_prob = brain.get("model_probability")
    orderbook = brain.get("orderbook") or {}
    tp_sl = result.get("tp_sl") or {}
    quality = format_trade_quality_notes(
        result.get("side", "BUY"),
        float(final or base or 50.0),
        float(result.get("last_close") or 0.0),
        result.get("effective_sl_price") or result.get("sl_price") or tp_sl.get("sl_price"),
        result.get("effective_tp_price") or result.get("tp_price") or tp_sl.get("tp_price"),
        tp_sl,
    )

    backend_name = str(brain.get("model_backend") or "logistic")
    if trained:
        model_line = f"AI={float(model_prob):.1f}% | backend={backend_name} | вес={float(brain.get('model_weight') or 0.0) * 100:.0f}% | влияние={impact:+.1f} п.п."
        thought = "нейрослой имеет право голоса; математика используется как входные признаки"
    else:
        model_line = "AI: недостаточно данных по тикеру | вес=0%"
        thought = "по этому тикеру мозг пока не принимает решение; показываю математику и коплю опыт"

    comment = _llm_or_fallback_comment({
        "side": side,
        "accuracy": acc,
        "samples": samples,
        "ticker_samples": ticker_samples,
        "ta_probability": base,
        "ai_probability": model_prob,
        "final_probability": final,
        "impact_pp": impact,
        "patterns": patterns[:8],
        "orderbook": orderbook,
        "tp_sl_quality": quality,
    }, thought)

    acc_line = f"Accuracy: {float(acc):.1f}% | samples={samples}" if acc is not None else f"Accuracy: — | samples={samples}"
    learn_line = f"Learning confidence: {float(brain.get('learning_confidence_pct') or 0.0):.1f}%"
    base_line = f"TA={float(base):.1f}%" if base is not None else "TA=—"
    final_line = f"Final={float(final):.1f}%" if final is not None else "Final=—"

    ob_line = "Стакан: нет данных"
    if orderbook and not orderbook.get("error"):
        ob_line = (
            f"Стакан: spread={float(orderbook.get('spread_bps') or 0.0):.1f} bps, "
            f"imbalance={float(orderbook.get('imbalance') or 0.0):+.2f}"
        )

    return (
        f"AI brain: {side}\n"
        f"{acc_line} | ticker samples={ticker_samples} | {learn_line}\n"
        f"{base_line} | {model_line} | {final_line}\n"
        f"{ob_line}\n"
        f"Паттерны: {pattern_text}\n"
        f"TP/SL: {quality}\n"
        f"{comment}\n"
    )



# ---------------------------- range recommendations / historical backfill ----------------------------

def _round_float_to_step(value: float, step_value=None) -> float:
    try:
        step = float(step_value or 0.0)
    except Exception:
        step = 0.0
    if step <= 0:
        return float(value)
    return round(round(float(value) / step) * step, 8)


def _history_basic_features(candles: list[dict], idx: int, side: str) -> tuple[dict[str, float], list[str], float]:
    window = candles[max(0, idx - 80): idx + 1]
    if len(window) < 25:
        return {}, ["недостаточно истории"], 50.0
    closes = [_safe_float(c.get("close")) for c in window]
    highs = [_safe_float(c.get("high")) for c in window]
    lows = [_safe_float(c.get("low")) for c in window]
    opens = [_safe_float(c.get("open")) for c in window]
    last = closes[-1]
    side_sign = 1.0 if side == "BUY" else -1.0
    local_high = max(highs[-24:])
    local_low = min(lows[-24:])
    local_range = max(1e-9, local_high - local_low)
    range_pos = _clamp((last - local_low) / local_range, 0.0, 1.0)
    ret1 = (closes[-1] - closes[-2]) / local_range if len(closes) >= 2 else 0.0
    ret3 = (closes[-1] - closes[-4]) / local_range if len(closes) >= 4 else 0.0
    ret6 = (closes[-1] - closes[-7]) / local_range if len(closes) >= 7 else 0.0
    body = (closes[-1] - opens[-1]) / local_range
    wick_up = (highs[-1] - max(opens[-1], closes[-1])) / local_range
    wick_down = (min(opens[-1], closes[-1]) - lows[-1]) / local_range
    avg_range = sum(max(0.0, h - l) for h, l in zip(highs[-20:], lows[-20:])) / max(1, len(highs[-20:]))
    atr_norm = avg_range / max(1e-9, last)

    features = {
        "hist_ret1_dir": _clamp(ret1 * side_sign, -3.0, 3.0),
        "hist_ret3_dir": _clamp(ret3 * side_sign, -3.0, 3.0),
        "hist_ret6_dir": _clamp(ret6 * side_sign, -3.0, 3.0),
        "hist_body_dir": _clamp(body * side_sign, -3.0, 3.0),
        "hist_range_pos_long": _clamp((0.5 - range_pos) * 2.0, -1.0, 1.0),
        "hist_range_pos_short": _clamp((range_pos - 0.5) * 2.0, -1.0, 1.0),
        "hist_atr_norm": _clamp(atr_norm * 1000.0, 0.0, 5.0),
        "hist_wick_reject_dir": _clamp((wick_down - wick_up) * side_sign, -3.0, 3.0),
    }
    patterns: list[str] = []
    prev_high = max(highs[-21:-1]) if len(highs) >= 21 else local_high
    prev_low = min(lows[-21:-1]) if len(lows) >= 21 else local_low
    if closes[-1] > prev_high:
        patterns.append("пробой high20")
    if closes[-1] < prev_low:
        patterns.append("пробой low20")
    if highs[-1] > prev_high and closes[-1] < prev_high:
        patterns.append("ложный пробой high / закол")
    if lows[-1] < prev_low and closes[-1] > prev_low:
        patterns.append("ложный пробой low / закол")
    if wick_down > abs(body) * 2.0 and closes[-1] > opens[-1]:
        patterns.append("нижний выкуп / отскок")
    if wick_up > abs(body) * 2.0 and closes[-1] < opens[-1]:
        patterns.append("верхний отказ / отскок вниз")
    if abs(body) > max(1e-9, avg_range) / local_range * 1.25:
        patterns.append("импульс / нож")

    ta_features, ta_patterns = _multi_tf_ta_pattern_features(window, side)
    features.update(ta_features)
    for item in ta_patterns:
        if item not in patterns:
            patterns.append(item)

    if not patterns:
        patterns.append("чистый диапазон без явного паттерна")

    raw_score = 50.0 + 18.0 * _clamp((ret3 + ret6) * side_sign, -1.0, 1.0)
    if side == "BUY":
        raw_score += 8.0 * _clamp(0.35 - range_pos, -0.7, 0.7)
    else:
        raw_score += 8.0 * _clamp(range_pos - 0.65, -0.7, 0.7)
    raw_score += 5.0 * _clamp((wick_down - wick_up) * side_sign, -1.0, 1.0)
    base_probability = _clamp(raw_score, 20.0, 82.0)
    return features, patterns, base_probability



def _recommend_trade_levels_from_window(candles: list[dict], idx: int, side: str, entry_price: float, probability: float | None = None) -> dict[str, Any]:
    """Historical SL/TP from the candles available at that exact moment. No current-market leakage."""
    side = side if side in {"BUY", "SELL"} else "BUY"
    entry = _safe_float(entry_price)
    if entry <= 0:
        return {"available": False, "reason": "bad entry"}
    window = candles[max(0, idx - 128): idx + 1]
    if len(window) < 64:
        return {"available": False, "reason": "not enough historical window"}
    highs = [_safe_float(c.get("high")) for c in window]
    lows = [_safe_float(c.get("low")) for c in window]
    closes = [_safe_float(c.get("close")) for c in window]
    ranges = [max(0.0, h - l) for h, l in zip(highs[-32:], lows[-32:])]
    atr = sum(ranges) / max(1, len(ranges))
    local_high = max(highs[-48:])
    local_low = min(lows[-48:])
    trade_range = max(atr, local_high - local_low, entry * 0.0005)
    cushion = max(atr * 0.22, trade_range * 0.035, entry * 0.00015)
    rr = _clamp(recommended_rr_from_probability(probability if probability is not None else 60.0), 1.0, 2.4)
    if side == "BUY":
        sl = min(local_low - cushion, entry - max(atr * 0.75, trade_range * 0.22))
        risk = max(entry - sl, atr * 0.65, entry * 0.0002)
        tp = entry + risk * rr
    else:
        sl = max(local_high + cushion, entry + max(atr * 0.75, trade_range * 0.22))
        risk = max(sl - entry, atr * 0.65, entry * 0.0002)
        tp = entry - risk * rr
    if side == "BUY" and not (sl < entry < tp):
        return {"available": False, "reason": "invalid historical long levels"}
    if side == "SELL" and not (tp < entry < sl):
        return {"available": False, "reason": "invalid historical short levels"}
    return {
        "available": True,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": abs(tp - entry) / max(1e-12, abs(entry - sl)),
        "breakeven_wr": break_even_wr(abs(tp - entry) / max(1e-12, abs(entry - sl))),
        "atr": atr,
        "local_low": local_low,
        "local_high": local_high,
        "reason": f"historical MOEX 5m window {local_low:.4f}-{local_high:.4f}, ATR≈{atr:.4f}",
    }


def recommend_trade_levels(ticker: str, side: str, entry_price, step=None, probability: float | None = None) -> dict[str, Any]:
    """Recommend SL/TP from the current 5m trading range. CPU-light and safe to call from main preview."""
    side = side if side in {"BUY", "SELL"} else "BUY"
    ticker = str(ticker or "").upper().strip()
    entry = _safe_float(entry_price)
    if not ticker or entry <= 0:
        return {"available": False, "reason": "no ticker/entry"}
    try:
        candles = []
        if load_moex_candles_cached:
            candles = load_moex_candles_cached(ticker, interval=5, minutes=5 * 180, limit=128, max_stale_seconds=MOEX_CACHE_STALE_SECONDS if 'MOEX_CACHE_STALE_SECONDS' in globals() else 3.0)
        if not candles and load_chart_candles_by_ticker:
            data = load_chart_candles_by_ticker(ticker, "CANDLE_INTERVAL_5_MIN", minutes=5 * 180)
            candles = data.get("candles") or []
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    candles = candles[-128:]
    if len(candles) < 24:
        return {"available": False, "reason": "not enough candles"}
    highs = [_safe_float(c.get("high")) for c in candles]
    lows = [_safe_float(c.get("low")) for c in candles]
    closes = [_safe_float(c.get("close")) for c in candles]
    ranges = [max(0.0, h - l) for h, l in zip(highs[-24:], lows[-24:])]
    atr = sum(ranges) / max(1, len(ranges))
    local_high = max(highs[-36:])
    local_low = min(lows[-36:])
    trade_range = max(atr, local_high - local_low, entry * 0.0005)
    cushion = max(atr * 0.22, trade_range * 0.035, entry * 0.00015)
    rr = recommended_rr_from_probability(probability if probability is not None else 60.0)
    rr = _clamp(rr, 1.0, 2.4)
    if side == "BUY":
        sl = min(local_low - cushion, entry - max(atr * 0.75, trade_range * 0.22))
        risk = max(entry - sl, atr * 0.65, entry * 0.0002)
        tp = entry + risk * rr
    else:
        sl = max(local_high + cushion, entry + max(atr * 0.75, trade_range * 0.22))
        risk = max(sl - entry, atr * 0.65, entry * 0.0002)
        tp = entry - risk * rr
    sl = _round_float_to_step(sl, step)
    tp = _round_float_to_step(tp, step)
    if side == "BUY" and not (sl < entry < tp):
        return {"available": False, "reason": "invalid long levels"}
    if side == "SELL" and not (tp < entry < sl):
        return {"available": False, "reason": "invalid short levels"}
    be = break_even_wr(abs(tp - entry) / max(1e-12, abs(entry - sl)))
    return {
        "available": True,
        "ticker": ticker,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": abs(tp - entry) / max(1e-12, abs(entry - sl)),
        "breakeven_wr": be,
        "atr": atr,
        "local_low": local_low,
        "local_high": local_high,
        "reason": f"5m range {local_low:.4f}-{local_high:.4f}, ATR≈{atr:.4f}",
    }


def _candidate_backfill_tickers(limit: int = HISTORY_BACKFILL_TICKER_LIMIT) -> list[str]:
    tickers: list[str] = []
    def add_ticker(value):
        t = _normalize_train_ticker(value)
        if t and _is_trainable_ticker(t) and t not in tickers:
            tickers.append(t)

    for ticker in sorted(_SEEN_TICKERS):
        add_ticker(ticker)

    if TRADE_DB_PATH.exists():
        try:
            with sqlite3.connect(TRADE_DB_PATH) as conn:
                rows = conn.execute(
                    """
                    SELECT UPPER(ticker) AS ticker, COUNT(*) AS n
                    FROM trades
                    WHERE ticker IS NOT NULL AND ticker != ''
                      AND UPPER(REPLACE(SUBSTR(ticker, 1, CASE WHEN INSTR(ticker, '@') > 0 THEN INSTR(ticker, '@') - 1 ELSE LENGTH(ticker) END), ' ', '')) NOT IN ('TMON','LQDT','S')
                    GROUP BY UPPER(ticker)
                    ORDER BY n DESC, MAX(time) DESC
                    LIMIT ?
                    """,
                    (limit * 3,),
                ).fetchall()
            for row in rows:
                add_ticker(row[0])
        except Exception:
            pass
    if BRAIN_DB_PATH.exists():
        try:
            with sqlite3.connect(BRAIN_DB_PATH) as conn:
                rows = conn.execute(
                    """
                    SELECT UPPER(ticker) AS ticker, COUNT(*) AS n
                    FROM brain_observations
                    WHERE ticker IS NOT NULL AND ticker != ''
                    GROUP BY UPPER(ticker)
                    ORDER BY n DESC
                    LIMIT ?
                    """,
                    (limit * 3,),
                ).fetchall()
            for row in rows:
                add_ticker(row[0])
        except Exception:
            pass
    return tickers[:limit]


def _history_sample_count(ticker: str, side: str) -> int:
    try:
        with sqlite3.connect(BRAIN_DB_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM brain_observations WHERE source='history_backfill' AND ticker=? AND side=?",
                (ticker, side),
            ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _insert_history_observation(ticker: str, side: str, candle: dict, entry: float, tp: float, sl: float, base_prob: float, features: dict[str, float], patterns: list[str], outcome: str, label: int) -> bool:
    entry_time = str(candle.get("time") or utc_now_iso())
    uid = _stable_uid(["history", ticker, side, entry_time, round(entry, 6), round(tp, 6), round(sl, 6)])
    raw = {"source": "history_backfill", "patterns": patterns}
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO brain_observations (
                uid, source, created_at, ticker, side, entry_time, entry_price,
                target_price, stop_price, horizon_bars, base_probability, features_json,
                patterns_json, status, outcome, label, evaluated_at, raw_json
            ) VALUES (?, 'history_backfill', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'labeled', ?, ?, ?, ?)
            """,
            (
                uid, utc_now_iso(), ticker, side, entry_time, entry, tp, sl, HORIZON_BARS_DEFAULT,
                base_prob, json.dumps(features or {}, ensure_ascii=False), json.dumps(patterns or [], ensure_ascii=False),
                outcome, label, utc_now_iso(), json.dumps(raw, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
        return conn.total_changes > before


def backfill_history_experience(force: bool = False, preferred_tickers: list[str] | None = None) -> int:
    """Continuously apply the brain to recent 5m history so new tickers gain experience."""
    global _LAST_HISTORY_BACKFILL_TS
    now_ts = time.time()
    if not force and now_ts - _LAST_HISTORY_BACKFILL_TS < HISTORY_BACKFILL_EVERY_SECONDS:
        return 0
    _LAST_HISTORY_BACKFILL_TS = now_ts
    ensure_brain_db()
    if not load_chart_candles_by_ticker:
        return 0
    inserted = 0
    candidates = []
    for t in preferred_tickers or []:
        t = _normalize_train_ticker(t)
        if t and _is_trainable_ticker(t) and t not in candidates:
            candidates.append(t)
    for t in _candidate_backfill_tickers():
        if t and t not in candidates:
            candidates.append(t)
    for ticker in candidates:
        ticker = _normalize_train_ticker(ticker)
        if not ticker or not _is_trainable_ticker(ticker):
            continue
        before_conf = _ticker_learning_confidence(ticker)
        try:
            candles = []
            if load_moex_candles_cached:
                candles = load_moex_candles_cached(
                    ticker,
                    interval=5,
                    minutes=OBSERVATION_LOOKBACK_MINUTES,
                    limit=None,
                    max_stale_seconds=HISTORY_BACKFILL_EVERY_SECONDS,
                    allow_fetch=True,
                )
            if not candles and load_chart_candles_by_ticker:
                data = load_chart_candles_by_ticker(ticker, "CANDLE_INTERVAL_5_MIN", minutes=OBSERVATION_LOOKBACK_MINUTES)
                candles = data.get("candles") or []
        except Exception as exc:
            _log_note("history_error", ticker, "", str(exc), {})
            continue
        if len(candles) < 140:
            _log_note("history_wait_data", ticker, "", f"MOEX: мало свечей для обучения ({len(candles)}), жду историю", {"candles": len(candles)})
            continue
        for side in ("BUY", "SELL"):
            existing = _history_sample_count(ticker, side)
            if existing >= HISTORY_MAX_SAMPLES_PER_TICKER_SIDE:
                continue
            need = HISTORY_MAX_SAMPLES_PER_TICKER_SIDE - existing
            made = 0
            last_idx = len(candles) - HORIZON_BARS_DEFAULT - 1
            start_idx = max(80, last_idx - need * HISTORY_STRIDE_BARS * 2)
            for idx in range(start_idx, last_idx, HISTORY_STRIDE_BARS):
                if made >= need:
                    break
                candle = candles[idx]
                entry = _safe_float(candle.get("close"))
                if entry <= 0:
                    continue
                features, patterns, base_prob = _history_basic_features(candles, idx, side)
                if not features:
                    continue
                features.update(_sequence_micro_features(candles, side=side, idx=idx, window=96))
                rec = _recommend_trade_levels_from_window(candles, idx, side, entry, base_prob)
                if not rec.get("available"):
                    continue
                tp = _safe_float(rec.get("tp"))
                sl = _safe_float(rec.get("sl"))
                future_candles = candles[idx + 1: idx + 1 + HORIZON_BARS_DEFAULT]
                # label directly from the future slice; no waiting needed for historical samples
                outcome = None
                label = None
                for fc in future_candles:
                    high = _safe_float(fc.get("high"))
                    low = _safe_float(fc.get("low"))
                    open_ = _safe_float(fc.get("open"))
                    if side == "BUY":
                        hit_tp = high >= tp
                        hit_sl = low <= sl
                    else:
                        hit_tp = low <= tp
                        hit_sl = high >= sl
                    if hit_tp and hit_sl:
                        outcome, label = (("win", 1) if abs(tp - open_) < abs(sl - open_) else ("loss", 0))
                        break
                    if hit_tp:
                        outcome, label = "win", 1
                        break
                    if hit_sl:
                        outcome, label = "loss", 0
                        break
                if label is None:
                    continue
                if _insert_history_observation(ticker, side, candle, entry, tp, sl, base_prob, features, patterns, outcome, label):
                    inserted += 1
                    made += 1
        after_conf = _ticker_learning_confidence(ticker)
        delta = after_conf - before_conf
        ticker_metrics = _load_metrics("ticker", ticker, "") or {}
        ticker_samples = int(ticker_metrics.get("samples") or 0)
        if ticker_samples > 0:
            _log_note(
                "learning_progress",
                ticker,
                "",
                f"Модель научилась на MOEX истории: тикер {ticker}, samples={ticker_samples}, Learning confidence {before_conf:.1f}% → {after_conf:.1f}% ({delta:+.1f} п.п.)",
                {"before_confidence": before_conf, "after_confidence": after_conf, "delta": delta, "samples": ticker_samples, "inserted_total": inserted},
            )
    if inserted:
        rebuild_metrics()
        trained_rows = train_model_from_observations()
        _log_note(
            "learning_batch",
            "",
            "",
            f"Модель обновила веса: добавлено исторических наблюдений={inserted}, training_rows={trained_rows}",
            {"inserted": inserted, "training_rows": trained_rows},
        )
    return inserted

# ---------------------------- observation / background learning ----------------------------

def _stable_uid(parts: list[Any]) -> str:
    raw = "|".join(str(x) for x in parts)
    return str(abs(hash(raw)))


def _store_live_observation(snapshot: dict[str, Any], last_price, stop_price, target_price, tp_sl: dict | None = None) -> None:
    ticker = snapshot.get("ticker") or ""
    side = snapshot.get("side") or "BUY"
    entry = _safe_float(last_price)
    sl = _safe_float(stop_price)
    tp = _safe_float(target_price)
    base = snapshot.get("base_probability")
    if entry <= 0 or sl <= 0:
        return
    if tp <= 0:
        auto = auto_target_from_probability(side, entry, sl, base)
        if auto.get("available"):
            tp = _safe_float(auto.get("tp"))
    if tp <= 0:
        return

    now = utc_now_iso()
    uid = _stable_uid(["live", ticker, side, now[:16], round(entry, 6), round(tp, 6), round(sl, 6)])
    raw = {"tp_sl": tp_sl or {}, "brain": {k: v for k, v in snapshot.items() if k not in {"features"}}}
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO brain_observations (
                uid, source, created_at, ticker, side, entry_time, entry_price,
                target_price, stop_price, horizon_bars, base_probability, model_probability,
                final_probability, model_weight, features_json, patterns_json, orderbook_json, raw_json
            ) VALUES (?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid, now, ticker, side, now, entry, tp, sl, HORIZON_BARS_DEFAULT,
                _safe_float(base, None),
                None if snapshot.get("model_probability") is None else _safe_float(snapshot.get("model_probability")),
                None if snapshot.get("final_probability") is None else _safe_float(snapshot.get("final_probability")),
                _safe_float(snapshot.get("model_weight")),
                json.dumps(snapshot.get("features") or {}, ensure_ascii=False),
                json.dumps(snapshot.get("patterns") or [], ensure_ascii=False),
                json.dumps(snapshot.get("orderbook") or {}, ensure_ascii=False),
                json.dumps(raw, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()



def _ticker_learning_confidence(ticker: str) -> float:
    ticker = str(ticker or "").upper().strip()
    metrics = _load_metrics("ticker", ticker, "") or {}
    samples = int(metrics.get("samples") or 0)
    acc = metrics.get("accuracy_pct")
    fake = {
        "samples": samples,
        "ticker_samples": samples,
        "accuracy_pct": acc,
        "trained": samples >= MIN_TICKER_SAMPLES,
    }
    return learning_confidence_pct(fake)


def _kick_ticker_backfill(ticker: str) -> None:
    ticker = _normalize_train_ticker(ticker)
    if not ticker or not _is_trainable_ticker(ticker):
        return
    try:
        ts, total = _ticker_samples(ticker, "BUY")
        _, total_sell = _ticker_samples(ticker, "SELL")
        total = max(total, total_sell)
        if total >= MIN_TICKER_SAMPLES:
            return
    except Exception:
        pass
    if ticker in _BACKFILL_BUSY_TICKERS:
        return

    def run():
        _BACKFILL_BUSY_TICKERS.add(ticker)
        try:
            _log_note("learning_start", ticker, "", f"Запускаю фоновое обучение по тикеру {ticker}: качаю MOEX 5m и применяю модель к истории", {})
            before = _ticker_learning_confidence(ticker)
            inserted = backfill_history_experience(force=True, preferred_tickers=[ticker])
            rebuild_metrics()
            trained_rows = train_model_from_observations()
            after = _ticker_learning_confidence(ticker)
            _log_note(
                "learning_progress",
                ticker,
                "",
                f"Модель научилась на тикере {ticker}: добавлено={inserted}, training_rows={trained_rows}, Learning confidence {before:.1f}% → {after:.1f}% ({after - before:+.1f} п.п.)",
                {"inserted": inserted, "trained_rows": trained_rows, "before_confidence": before, "after_confidence": after, "delta": after - before},
            )
        except Exception as exc:
            _log_note("learning_error", ticker, "", f"Ошибка фонового обучения {ticker}: {exc}", {})
        finally:
            _BACKFILL_BUSY_TICKERS.discard(ticker)

    threading.Thread(target=run, daemon=True).start()


def start_brain_worker_once() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        thread = threading.Thread(target=_brain_worker_loop, daemon=True)
        thread.start()


def _brain_worker_loop() -> None:
    ensure_brain_db()
    while True:
        try:
            synced = sync_trade_db_samples()
            inserted = backfill_history_experience()
            evaluated = evaluate_pending_observations()
            trained_rows = train_model_from_observations()
            if synced or inserted or evaluated:
                _log_note(
                    "learning_cycle",
                    "",
                    "",
                    f"Фоновое обучение: trades={synced}, history={inserted}, evaluated={evaluated}, training_rows={trained_rows}",
                    {"trades": synced, "history": inserted, "evaluated": evaluated, "training_rows": trained_rows},
                )
        except Exception as exc:
            try:
                _log_note("worker_error", "", "", str(exc), {})
            except Exception:
                pass
        time.sleep(WORKER_SLEEP_SECONDS)


def _parse_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _candle_time(candle: dict) -> datetime | None:
    return _parse_time(str(candle.get("time") or candle.get("date") or ""))


def _label_from_future(candles: list[dict], entry_time: str, side: str, tp: float, sl: float, horizon_bars: int) -> tuple[str | None, int | None]:
    entry_dt = _parse_time(entry_time)
    if not entry_dt:
        return None, None
    future = []
    for candle in candles or []:
        cdt = _candle_time(candle)
        if cdt and cdt > entry_dt:
            future.append(candle)
    if len(future) < max(5, min(12, horizon_bars)):
        return None, None
    for candle in future[:horizon_bars]:
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))
        open_ = _safe_float(candle.get("open"))
        if side == "BUY":
            hit_tp = high >= tp
            hit_sl = low <= sl
        else:
            hit_tp = low <= tp
            hit_sl = high >= sl
        if hit_tp and hit_sl:
            # Intrabar ambiguity: use distance from open as a conservative ordering proxy.
            tp_dist = abs(tp - open_)
            sl_dist = abs(sl - open_)
            return ("win", 1) if tp_dist < sl_dist else ("loss", 0)
        if hit_tp:
            return "win", 1
        if hit_sl:
            return "loss", 0
    if len(future) >= horizon_bars:
        return "timeout", None
    return None, None


def evaluate_pending_observations(limit: int = 400) -> int:
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM brain_observations
            WHERE status='pending'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    if not rows:
        return 0

    by_ticker: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_ticker.setdefault(str(row["ticker"]).upper(), []).append(row)

    updated = 0
    for ticker, ticker_rows in by_ticker.items():
        if not ticker or not load_chart_candles_by_ticker:
            continue
        try:
            data = load_chart_candles_by_ticker(ticker, "CANDLE_INTERVAL_5_MIN", minutes=OBSERVATION_LOOKBACK_MINUTES)
            candles = data.get("candles") or []
        except Exception:
            continue
        for row in ticker_rows:
            outcome, label = _label_from_future(
                candles,
                row["entry_time"],
                row["side"],
                _safe_float(row["target_price"]),
                _safe_float(row["stop_price"]),
                int(row["horizon_bars"] or HORIZON_BARS_DEFAULT),
            )
            if outcome is None:
                continue
            with sqlite3.connect(BRAIN_DB_PATH) as conn:
                conn.execute(
                    """
                    UPDATE brain_observations
                    SET status=?, outcome=?, label=?, evaluated_at=?
                    WHERE uid=?
                    """,
                    ("labeled" if label is not None else "timeout", outcome, label, utc_now_iso(), row["uid"]),
                )
                conn.commit()
            updated += 1
    if updated:
        rebuild_metrics()
    return updated


def sync_trade_db_samples(limit: int = 1000) -> int:
    if not TRADE_DB_PATH.exists():
        return 0
    ensure_brain_db()
    try:
        with sqlite3.connect(TRADE_DB_PATH) as tconn:
            tconn.row_factory = sqlite3.Row
            trades = tconn.execute(
                """
                SELECT uid, source, time, ticker, side, price, tp_price, sl_price
                FROM trades
                WHERE ticker IS NOT NULL AND ticker != ''
                  AND side IN ('BUY','SELL')
                  AND price IS NOT NULL AND price > 0
                  AND tp_price IS NOT NULL AND tp_price > 0
                  AND sl_price IS NOT NULL AND sl_price > 0
                ORDER BY time DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except Exception:
        return 0

    inserted = 0
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        for tr in trades:
            uid = f"trade_db:{tr['uid']}"
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO brain_observations (
                    uid, source, created_at, ticker, side, entry_time, entry_price,
                    target_price, stop_price, horizon_bars, features_json, raw_json
                ) VALUES (?, 'trade_db', ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    uid,
                    utc_now_iso(),
                    str(tr["ticker"] or "").upper(),
                    tr["side"],
                    tr["time"],
                    _safe_float(tr["price"]),
                    _safe_float(tr["tp_price"]),
                    _safe_float(tr["sl_price"]),
                    HORIZON_BARS_DEFAULT,
                    json.dumps({"trade_uid": tr["uid"], "source": tr["source"]}, ensure_ascii=False),
                ),
            )
            if conn.total_changes > before:
                inserted += 1
        conn.commit()
    return inserted


def train_model_from_observations() -> int:
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT features_json, label
            FROM brain_observations
            WHERE status='labeled' AND label IN (0,1)
              AND features_json IS NOT NULL AND features_json != '{}' AND features_json != ''
            ORDER BY evaluated_at DESC
            LIMIT 3000
            """
        ).fetchall()
    if len(rows) < 25:
        rebuild_metrics()
        return 0

    dataset = []
    feature_names: set[str] = set()
    for row in rows:
        try:
            features = json.loads(row["features_json"] or "{}")
            if not isinstance(features, dict) or not features:
                continue
            clean = {str(k): _clamp(float(v), -5.0, 5.0) for k, v in features.items()}
            label = int(row["label"])
            dataset.append((clean, label))
            feature_names.update(clean.keys())
        except Exception:
            continue
    if len(dataset) < 25:
        rebuild_metrics()
        return 0

    state = _load_state()
    weights = state.get("weights") if isinstance(state.get("weights"), dict) else {}
    weights = {str(k): float(v) for k, v in weights.items()}
    for name in feature_names:
        weights.setdefault(name, 0.0)
    bias = float(state.get("bias") or 0.0)

    lr = 0.035
    l2 = 0.0008
    for _epoch in range(4):
        for features, label in dataset:
            z = bias + sum(weights.get(k, 0.0) * v for k, v in features.items())
            pred = _sigmoid(z)
            err = float(label) - pred
            bias += lr * err
            for k, v in features.items():
                weights[k] = weights.get(k, 0.0) + lr * (err * v - l2 * weights.get(k, 0.0))

    # Keep the model stable and readable.
    weights = {k: _clamp(v, -4.0, 4.0) for k, v in weights.items() if abs(v) > 0.0005}
    save_model_state({
        "version": MODEL_VERSION,
        "bias": _clamp(bias, -8.0, 8.0),
        "target_weight": TARGET_MODEL_WEIGHT,
        "max_weight": MAX_MODEL_WEIGHT,
        "weights": weights,
        "feature_names": sorted(weights.keys()),
        "trained_samples": len(dataset),
    })
    if train_torch_model_from_db:
        try:
            train_torch_model_from_db(BRAIN_DB_PATH)
        except Exception as exc:
            try:
                _log_note("torch_train_error", "", "", str(exc), {})
            except Exception:
                pass
    rebuild_metrics()
    return len(dataset)


def rebuild_metrics() -> None:
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ticker, side, label, base_probability, final_probability
            FROM brain_observations
            WHERE status='labeled' AND label IN (0,1)
            """
        ).fetchall()
        conn.execute("DELETE FROM model_metrics")

        def write(scope: str, ticker: str, side: str, items: list[sqlite3.Row]):
            samples = len(items)
            if samples <= 0:
                return
            wins = sum(1 for r in items if int(r["label"]) == 1)
            accuracy = 100.0 * wins / samples
            edges = []
            for r in items:
                p = r["final_probability"] if r["final_probability"] is not None else r["base_probability"]
                if p is None:
                    continue
                edges.append((float(p) - 50.0) * (1 if int(r["label"]) == 1 else -1))
            avg_edge = sum(edges) / len(edges) if edges else None
            conn.execute(
                """
                INSERT OR REPLACE INTO model_metrics(scope, ticker, side, samples, wins, accuracy_pct, avg_edge, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (scope, ticker, side, samples, wins, accuracy, avg_edge),
            )

        all_rows = list(rows)
        write("global", "", "", all_rows)
        tickers = sorted({str(r["ticker"] or "").upper() for r in all_rows if r["ticker"]})
        for ticker in tickers:
            t_rows = [r for r in all_rows if str(r["ticker"] or "").upper() == ticker]
            write("ticker", ticker, "", t_rows)
            for side in ("BUY", "SELL"):
                ts_rows = [r for r in t_rows if r["side"] == side]
                write("ticker_side", ticker, side, ts_rows)
        conn.commit()


def _log_note(kind: str, ticker: str, side: str, text: str, payload: dict[str, Any]) -> None:
    ensure_brain_db()
    with sqlite3.connect(BRAIN_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO brain_notes(created_at, kind, ticker, side, text, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), kind, ticker, side, text, json.dumps(payload or {}, ensure_ascii=False, default=str)),
        )
        conn.commit()
    try:
        if str(kind or "").startswith(("learning", "history", "torch")):
            print(f"[BRAIN] {kind} {ticker}: {text}")
    except Exception:
        pass


# Start learning as soon as this module is imported by the terminal.
try:
    start_brain_worker_once()
except Exception:
    pass
