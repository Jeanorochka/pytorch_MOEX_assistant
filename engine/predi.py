import math
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from architecture import (
    BG,
    PANEL_BG,
    PANEL_BG_2,
    INPUT_BG,
    FG,
    MUTED_FG,
    GREEN,
    RED,
    YELLOW,
    BLUE,
    BORDER,
    FONT_FAMILY,
    load_chart_candles_by_ticker,
)

try:
    import architecture as _architecture_module
except Exception:
    _architecture_module = None

try:
    from .predi_brain import get_brain_snapshot, blend_probability_with_brain
except Exception:
    try:
        from predi_brain import get_brain_snapshot, blend_probability_with_brain
    except Exception:
        get_brain_snapshot = None
        blend_probability_with_brain = None

try:
    from .predi_moex import load_moex_candles_cached
except Exception:
    try:
        from predi_moex import load_moex_candles_cached
    except Exception:
        load_moex_candles_cached = None


PREDI_INTERVAL = "CANDLE_INTERVAL_5_MIN"
PREDI_MINUTES = 10080  # one calendar week for realistic local highs/lows
PREDI_PATTERN_INTERVAL = "CANDLE_INTERVAL_15_MIN"
PREDI_PATTERN_MINUTES = 3000  # enough to receive ~100 recent 15m candles even with market gaps
PREDI_REFRESH_MS = 5000
PREDI_DEBOUNCE_MS = 1000
ORDER_BOOK_DEPTH = 50
ORDER_BOOK_WALL_LIMIT = 2


def _architecture_attr(name: str):
    try:
        return getattr(_architecture_module, name, None) if _architecture_module is not None else None
    except Exception:
        return None


def _object_get(value, *keys, default=None):
    for key in keys:
        try:
            if isinstance(value, dict) and key in value:
                candidate = value.get(key)
            else:
                candidate = getattr(value, key)
        except Exception:
            candidate = None
        if candidate not in (None, ""):
            return candidate
    return default


def _quotation_to_float(value, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            if "units" in value or "nano" in value:
                units = float(value.get("units") or 0.0)
                nano = float(value.get("nano") or 0.0) / 1_000_000_000.0
                return units + nano
            for key in ("price", "value", "amount", "quotation"):
                if key in value:
                    converted = _quotation_to_float(value.get(key), default=None)
                    if converted not in (None, 0.0):
                        return float(converted)
        units = getattr(value, "units", None)
        nano = getattr(value, "nano", None)
        if units is not None or nano is not None:
            return float(units or 0.0) + (float(nano or 0.0) / 1_000_000_000.0)
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def _level_price(level) -> float:
    return _quotation_to_float(_object_get(level, "price", "p", "limit_price", "limitPrice", default=0.0), 0.0)


def _level_quantity(level) -> float:
    value = _object_get(
        level,
        "quantity",
        "qty",
        "lots",
        "volume",
        "size",
        "quantity_lots",
        "quantityLots",
        default=0.0,
    )
    try:
        return abs(float(str(value).replace(",", ".")))
    except Exception:
        return 0.0


def _extract_order_book_sides(raw_book) -> tuple[list, list]:
    if raw_book is None:
        return [], []
    # Some wrappers return {"orderbook": ...} / {"payload": ...}.
    for key in ("orderbook", "order_book", "book", "payload", "data", "result"):
        nested = _object_get(raw_book, key, default=None)
        if nested is not None and nested is not raw_book:
            bids, asks = _extract_order_book_sides(nested)
            if bids or asks:
                return bids, asks
    bids = _object_get(raw_book, "bids", "buy", "buyers", "bid", default=[])
    asks = _object_get(raw_book, "asks", "sell", "sellers", "ask", default=[])
    return list(bids or []), list(asks or [])


def _instrument_id_from_instrument(inst) -> str | None:
    if not inst:
        return None
    get_id = _architecture_attr("get_instrument_id")
    if get_id is not None:
        try:
            instrument_id = get_id(inst)
            if instrument_id:
                return str(instrument_id)
        except Exception:
            pass
    for key in ("uid", "instrument_uid", "instrumentUid", "figi", "ticker"):
        value = _object_get(inst, key, default=None)
        if value:
            return str(value)
    return None


def _resolve_order_book_instrument_id(ticker: str) -> str | None:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return None
    for name in ("find_instrument", "find_instrument_by_ticker", "get_instrument_by_ticker"):
        fn = _architecture_attr(name)
        if fn is None:
            continue
        try:
            inst = fn(ticker)
            instrument_id = _instrument_id_from_instrument(inst)
            if instrument_id:
                return instrument_id
        except Exception:
            continue
    return ticker


def _call_order_book_function(fn, ticker: str, instrument_id: str | None):
    attempts = []
    if instrument_id:
        attempts.extend([
            ((instrument_id,), {"depth": ORDER_BOOK_DEPTH}),
            ((instrument_id, ORDER_BOOK_DEPTH), {}),
            ((), {"instrument_id": instrument_id, "depth": ORDER_BOOK_DEPTH}),
            ((), {"instrument_uid": instrument_id, "depth": ORDER_BOOK_DEPTH}),
            ((), {"figi": instrument_id, "depth": ORDER_BOOK_DEPTH}),
            ((instrument_id,), {}),
        ])
    attempts.extend([
        ((ticker,), {"depth": ORDER_BOOK_DEPTH}),
        ((ticker, ORDER_BOOK_DEPTH), {}),
        ((), {"ticker": ticker, "depth": ORDER_BOOK_DEPTH}),
        ((ticker,), {}),
    ])
    last_error = None
    for args, kwargs in attempts:
        try:
            result = fn(*args, **kwargs)
            bids, asks = _extract_order_book_sides(result)
            if bids or asks:
                return result, None
        except Exception as exc:
            last_error = exc
            continue
    return None, last_error


def _load_order_book_raw(ticker: str):
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return None, "нет тикера"
    instrument_id = _resolve_order_book_instrument_id(ticker)
    for name in (
        "get_order_book",
        "get_orderbook",
        "load_order_book",
        "load_orderbook",
        "fetch_order_book",
        "fetch_orderbook",
        "get_market_order_book",
        "get_market_orderbook",
    ):
        fn = _architecture_attr(name)
        if fn is None:
            continue
        result, error = _call_order_book_function(fn, ticker, instrument_id)
        if result is not None:
            return result, name
    return None, "в architecture.py не найдена функция стакана"


def _top_order_book_walls(levels: list, side: str, limit: int = ORDER_BOOK_WALL_LIMIT) -> list[dict]:
    buckets: dict[float, float] = {}
    for level in levels or []:
        price = _level_price(level)
        qty = _level_quantity(level)
        if price <= 0 or qty <= 0:
            continue
        # Round only for bucket stability; display still uses normal formatting.
        key = round(price, 10)
        buckets[key] = buckets.get(key, 0.0) + qty
    ranked = sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:limit]
    if side == "BUY":
        ranked.sort(key=lambda item: item[0], reverse=True)
    else:
        ranked.sort(key=lambda item: item[0])
    return [{"price": price, "qty": qty} for price, qty in ranked]


def scan_order_book_walls(ticker: str) -> dict:
    raw_book, source = _load_order_book_raw(ticker)
    bids, asks = _extract_order_book_sides(raw_book)
    if not bids and not asks:
        return {
            "available": False,
            "ticker": str(ticker or "").upper().strip(),
            "source": str(source or "—"),
            "buy": [],
            "sell": [],
        }
    return {
        "available": True,
        "ticker": str(ticker or "").upper().strip(),
        "source": str(source or "order_book"),
        "buy": _top_order_book_walls(bids, "BUY"),
        "sell": _top_order_book_walls(asks, "SELL"),
    }


def _format_order_book_walls(walls: dict | None) -> str:
    walls = walls or {}
    if not walls.get("available"):
        reason = walls.get("source") or "стакан недоступен"
        return f"Плиты BUY: —\nПлиты SELL: —\nСтакан: {reason}"

    def side_text(items: list[dict]) -> str:
        if not items:
            return "—"
        parts = []
        for item in items[:ORDER_BOOK_WALL_LIMIT]:
            price = float(item.get("price") or 0.0)
            qty = float(item.get("qty") or 0.0)
            parts.append(f"{_fmt(price, 4)} × {_fmt(qty, 0)}")
        return " | ".join(parts)

    return (
        f"Плиты BUY: {side_text(walls.get('buy') or [])}\n"
        f"Плиты SELL: {side_text(walls.get('sell') or [])}"
    )


# ---------------------------- Math helpers ----------------------------

def _safe_float(value, default: float = 0.0) -> float:
    try:
        if isinstance(value, Decimal):
            return float(value)
        return float(value)
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt(value, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def ema(values: list[float], period: int) -> list[float | None]:
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = []
    current = values[0]
    for idx, value in enumerate(values):
        if idx == 0:
            current = value
        else:
            current = (value * alpha) + (current * (1.0 - alpha))
        out.append(current)
    return out


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    rolling = 0.0
    for idx, value in enumerate(values):
        rolling += value
        if idx >= period:
            rolling -= values[idx - period]
        if idx >= period - 1:
            out.append(rolling / period)
        else:
            out.append(None)
    return out


def stdev_window(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    for idx in range(len(values)):
        if idx < period - 1:
            out.append(None)
            continue
        window = values[idx - period + 1 : idx + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        out.append(math.sqrt(max(0.0, variance)))
    return out


def rsi(values: list[float], period: int) -> list[float | None]:
    if len(values) < period + 1:
        return [None] * len(values)

    out: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, period + 1):
        diff = values[idx] - values[idx - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

    for idx in range(period + 1, len(values)):
        diff = values[idx] - values[idx - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        out[idx] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    return out


def last_not_none(values: list[float | None], fallback=None):
    for value in reversed(values):
        if value is not None:
            return value
    return fallback


def calc_macd(closes: list[float], fast: int = 8, slow: int = 21, signal: int = 5) -> dict:
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line: list[float | None] = []
    for f, s in zip(fast_ema, slow_ema):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)

    clean = [x if x is not None else 0.0 for x in macd_line]
    sig = ema(clean, signal)
    hist: list[float | None] = []
    for m, s in zip(macd_line, sig):
        if m is None or s is None:
            hist.append(None)
        else:
            hist.append(m - s)

    macd_now = last_not_none(macd_line, 0.0)
    signal_now = last_not_none(sig, 0.0)
    hist_now = last_not_none(hist, 0.0)
    hist_prev = last_not_none(hist[:-1], hist_now)
    slope = hist_now - hist_prev
    return {
        "macd": macd_now,
        "signal": signal_now,
        "hist": hist_now,
        "slope": slope,
    }


def calc_cmf(candles: list[dict], period: int = 20) -> float:
    subset = candles[-period:]
    money_flow_volume = 0.0
    volume_sum = 0.0
    for candle in subset:
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))
        close = _safe_float(candle.get("close"))
        volume = max(0.0, _safe_float(candle.get("volume")))
        if high == low or volume <= 0:
            multiplier = 0.0
        else:
            multiplier = ((close - low) - (high - close)) / (high - low)
        money_flow_volume += multiplier * volume
        volume_sum += volume
    if volume_sum <= 0:
        return 0.0
    return money_flow_volume / volume_sum


def _date_key(raw_time) -> str:
    value = str(raw_time or "")
    if "T" in value:
        return value.split("T", 1)[0]
    if " " in value:
        return value.split(" ", 1)[0]
    return value[:10]


def calc_session_vwap(candles: list[dict]) -> float:
    if not candles:
        return 0.0
    last_date = _date_key(candles[-1].get("time"))
    session = [c for c in candles if _date_key(c.get("time")) == last_date]
    if len(session) < 5:
        session = candles[-78:]

    pv = 0.0
    volume_sum = 0.0
    for candle in session:
        high = _safe_float(candle.get("high"))
        low = _safe_float(candle.get("low"))
        close = _safe_float(candle.get("close"))
        volume = max(1.0, _safe_float(candle.get("volume"), 1.0))
        hlc3 = (high + low + close) / 3.0
        pv += hlc3 * volume
        volume_sum += volume
    return pv / volume_sum if volume_sum > 0 else _safe_float(candles[-1].get("close"))


def calc_bbands(closes: list[float], period: int = 20, mult: float = 2.0) -> dict:
    mid_values = sma(closes, period)
    dev_values = stdev_window(closes, period)
    mid = last_not_none(mid_values, closes[-1] if closes else 0.0)
    dev = last_not_none(dev_values, 0.0)
    upper = mid + (dev * mult)
    lower = mid - (dev * mult)
    width = (upper - lower) / mid if mid else 0.0

    widths = []
    for m, d in zip(mid_values, dev_values):
        if m and d is not None and m != 0:
            widths.append((d * mult * 2.0) / m)
    avg_width = sum(widths[-40:]) / len(widths[-40:]) if widths[-40:] else width
    return {
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "width": width,
        "avg_width": avg_width,
    }


def calc_streaks(closes: list[float]) -> list[float]:
    if not closes:
        return []
    streaks = [0.0]
    streak = 0
    for idx in range(1, len(closes)):
        if closes[idx] > closes[idx - 1]:
            streak = streak + 1 if streak > 0 else 1
        elif closes[idx] < closes[idx - 1]:
            streak = streak - 1 if streak < 0 else -1
        else:
            streak = 0
        streaks.append(float(streak))
    return streaks


def percent_rank(values: list[float], period: int = 100) -> list[float | None]:
    out: list[float | None] = []
    for idx, value in enumerate(values):
        if idx < period:
            out.append(None)
            continue
        window = values[idx - period : idx]
        below = sum(1 for item in window if item < value)
        out.append(100.0 * below / period)
    return out


def calc_crsi(closes: list[float], rsi_period: int = 3, streak_period: int = 2, rank_period: int = 100) -> dict:
    close_rsi = rsi(closes, rsi_period)
    streaks = calc_streaks(closes)
    streak_rsi = rsi(streaks, streak_period)
    roc = [0.0]
    for idx in range(1, len(closes)):
        prev = closes[idx - 1]
        roc.append(((closes[idx] - prev) / prev) * 100.0 if prev else 0.0)
    rank = percent_rank(roc, rank_period)

    a = last_not_none(close_rsi, 50.0)
    b = last_not_none(streak_rsi, 50.0)
    c = last_not_none(rank, 50.0)
    return {
        "crsi": (a + b + c) / 3.0,
        "rsi3": a,
        "streak_rsi2": b,
        "percent_rank100": c,
    }



def calc_atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    true_ranges: list[float] = []
    for idx in range(1, len(candles)):
        high = _safe_float(candles[idx].get("high"))
        low = _safe_float(candles[idx].get("low"))
        prev_close = _safe_float(candles[idx - 1].get("close"))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    subset = true_ranges[-period:]
    return sum(subset) / len(subset) if subset else 0.0


def parse_optional_price(value) -> float | None:
    raw = str(value or "").strip().replace(",", ".")
    if not raw or raw in {"—", "-", "None", "none"}:
        return None
    # Accept strings like "L 407.0", "S 414.8", "TP: 420" and plain numbers.
    token = ""
    for char in raw:
        if char.isdigit() or char in {".", "-"}:
            token += char
        elif token:
            break
    if not token or token in {"-", ".", "-."}:
        return None
    try:
        value_float = float(token)
    except Exception:
        return None
    return value_float if value_float > 0 else None


def calc_week_levels(candles: list[dict]) -> dict:
    recent = [c for c in candles if _safe_float(c.get("high")) > 0 and _safe_float(c.get("low")) > 0]
    if not recent:
        return {
            "week_high": 0.0,
            "week_low": 0.0,
            "nearest_resistance": 0.0,
            "nearest_support": 0.0,
            "range_pos": 0.5,
        }

    highs = [_safe_float(c.get("high")) for c in recent]
    lows = [_safe_float(c.get("low")) for c in recent]
    closes = [_safe_float(c.get("close")) for c in recent]
    last = closes[-1]
    week_high = max(highs)
    week_low = min(lows)
    full_range = max(1e-12, week_high - week_low)
    range_pos = _clamp((last - week_low) / full_range, 0.0, 1.0)

    local_highs: list[float] = []
    local_lows: list[float] = []
    # Pivot levels across the visible week. Width=2 is deliberately light and fast.
    for idx in range(2, len(recent) - 2):
        h = highs[idx]
        l = lows[idx]
        if h >= highs[idx - 1] and h >= highs[idx - 2] and h >= highs[idx + 1] and h >= highs[idx + 2]:
            local_highs.append(h)
        if l <= lows[idx - 1] and l <= lows[idx - 2] and l <= lows[idx + 1] and l <= lows[idx + 2]:
            local_lows.append(l)

    resistances = [x for x in local_highs if x > last]
    supports = [x for x in local_lows if x < last]

    return {
        "week_high": week_high,
        "week_low": week_low,
        "nearest_resistance": min(resistances) if resistances else week_high,
        "nearest_support": max(supports) if supports else week_low,
        "range_pos": range_pos,
    }


def calc_week_level_score(levels: dict, side: str) -> tuple[float, str]:
    pos = float(levels.get("range_pos", 0.5) or 0.5)
    week_high = float(levels.get("week_high", 0.0) or 0.0)
    week_low = float(levels.get("week_low", 0.0) or 0.0)
    support = float(levels.get("nearest_support", 0.0) or 0.0)
    resistance = float(levels.get("nearest_resistance", 0.0) or 0.0)

    # Positive score = long bias. Near weekly lows gives longs more realistic room;
    # near weekly highs gives shorts more realistic room. This is intentionally not huge:
    # trend indicators can still override it unless TP/SL geometry is impossible.
    long_bias = _clamp((0.50 - pos) / 0.50, -1.0, 1.0) * 8.0
    text = (
        f"week { _fmt(week_low, 2) }-{ _fmt(week_high, 2) } | "
        f"pos={_fmt(pos * 100.0, 1)}% | S={_fmt(support, 2)} R={_fmt(resistance, 2)}"
    )
    return long_bias, text


def calc_tp_sl_geometry(candles: list[dict], side: str, last_price: float, sl_price, tp_price, levels: dict | None = None) -> dict:
    sl = parse_optional_price(sl_price)
    tp = parse_optional_price(tp_price)
    levels = levels or calc_week_levels(candles)
    if sl is None or tp is None or last_price <= 0:
        return {
            "available": False,
            "score": 0.0,
            "probability": None,
            "hard_zero": False,
            "text": "TP/SL не заданы",
            "details": "",
        }

    week_high = float(levels.get("week_high", 0.0) or 0.0)
    week_low = float(levels.get("week_low", 0.0) or 0.0)
    nearest_resistance = float(levels.get("nearest_resistance", week_high) or week_high)
    nearest_support = float(levels.get("nearest_support", week_low) or week_low)
    range_pos = float(levels.get("range_pos", 0.5) or 0.5)

    if side == "BUY":
        sl_distance = last_price - sl
        tp_distance = tp - last_price
        impossible_tp = week_high > 0 and tp > week_high
        wrong_side = sl_distance <= 0 or tp_distance <= 0
        barrier_text = f"week_high={_fmt(week_high, 2)} nearest_R={_fmt(nearest_resistance, 2)}"
    else:
        sl_distance = sl - last_price
        tp_distance = last_price - tp
        impossible_tp = week_low > 0 and tp < week_low
        wrong_side = sl_distance <= 0 or tp_distance <= 0
        barrier_text = f"week_low={_fmt(week_low, 2)} nearest_S={_fmt(nearest_support, 2)}"

    atr14 = calc_atr(candles, 14)

    if wrong_side:
        text = f"0.0% | SL/TP не с той стороны | SL={_fmt(sl, 2)} TP={_fmt(tp, 2)}"
        return {
            "available": True,
            "score": -45.0,
            "probability": 0.0,
            "hard_zero": True,
            "text": text,
            "details": text,
            "sl_distance": sl_distance,
            "tp_distance": tp_distance,
            "rr": 0.0,
            "atr14": atr14,
        }

    if impossible_tp:
        # User-facing rule: if TP is placed beyond the realistic weekly local extreme,
        # this setup must not receive a fake high probability.
        text = f"0.0% | TP за недельным экстремумом | TP={_fmt(tp, 2)} | {barrier_text}"
        return {
            "available": True,
            "score": -45.0,
            "probability": 0.0,
            "hard_zero": True,
            "text": text,
            "details": text,
            "sl_distance": sl_distance,
            "tp_distance": tp_distance,
            "rr": 0.0,
            "atr14": atr14,
        }

    # First-touch geometry: closer TP relative to SL is easier to hit first.
    first_touch_probability = 100.0 * sl_distance / (sl_distance + tp_distance)
    rr = tp_distance / sl_distance if sl_distance > 0 else 0.0

    probability = first_touch_probability
    notes: list[str] = []

    if atr14 > 0:
        tp_atr = tp_distance / atr14
        sl_atr = sl_distance / atr14
        notes.append(f"TP={_fmt(tp_atr, 2)}ATR SL={_fmt(sl_atr, 2)}ATR")
        if tp_atr > 5.0:
            probability -= 35.0
        elif tp_atr > 3.5:
            probability -= 22.0
        elif tp_atr > 2.5:
            probability -= 12.0
        elif tp_atr < 0.45:
            probability += 4.0
        if sl_atr < 0.25:
            probability -= 6.0
        elif sl_atr > 2.5:
            probability += 3.0

    if side == "BUY":
        if nearest_resistance and tp > nearest_resistance:
            probability -= 18.0
            notes.append(f"TP выше R={_fmt(nearest_resistance, 2)}")
        if nearest_support and sl < nearest_support:
            probability += 6.0
            notes.append(f"SL за S={_fmt(nearest_support, 2)}")
        if range_pos > 0.88:
            probability -= 10.0
            notes.append("цена у недельного high")
        elif range_pos < 0.22:
            probability += 5.0
            notes.append("есть место от недельного low")
    else:
        if nearest_support and tp < nearest_support:
            probability -= 18.0
            notes.append(f"TP ниже S={_fmt(nearest_support, 2)}")
        if nearest_resistance and sl > nearest_resistance:
            probability += 6.0
            notes.append(f"SL за R={_fmt(nearest_resistance, 2)}")
        if range_pos < 0.12:
            probability -= 10.0
            notes.append("цена у недельного low")
        elif range_pos > 0.78:
            probability += 5.0
            notes.append("есть место от недельного high")

    probability = _clamp(probability, 0.0, 95.0)
    score = _clamp((probability - 50.0) / 50.0, -1.0, 1.0) * 24.0

    note_text = " | " + "; ".join(notes[:3]) if notes else ""
    text = f"{_fmt(probability, 1)}% | RR={_fmt(rr, 2)} | TPdist={_fmt(tp_distance, 2)} SLdist={_fmt(sl_distance, 2)}{note_text}"
    return {
        "available": True,
        "score": score,
        "probability": probability,
        "hard_zero": False,
        "text": text,
        "details": text,
        "sl_distance": sl_distance,
        "tp_distance": tp_distance,
        "rr": rr,
        "atr14": atr14,
    }

def _pivot_swings(candles: list[dict], left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    if len(candles) < left + right + 6:
        return highs, lows
    high_values = [_safe_float(c.get("high")) for c in candles]
    low_values = [_safe_float(c.get("low")) for c in candles]
    for idx in range(left, len(candles) - right):
        h = high_values[idx]
        l = low_values[idx]
        high_window = high_values[idx - left:idx + right + 1]
        low_window = low_values[idx - left:idx + right + 1]
        if h == max(high_window) and h > max(high_values[idx - left:idx] + high_values[idx + 1:idx + right + 1]):
            highs.append((idx, h))
        if l == min(low_window) and l < min(low_values[idx - left:idx] + low_values[idx + 1:idx + right + 1]):
            lows.append((idx, l))
    return highs[-10:], lows[-10:]


def _add_pattern(patterns: list[str], text: str) -> None:
    if text not in patterns:
        patterns.append(text)


def _volume_spike(candles: list[dict], lookback: int = 30) -> bool:
    if len(candles) < lookback + 2:
        return False
    vols = [max(0.0, _safe_float(c.get("volume"))) for c in candles[-lookback - 1:-1]]
    avg = sum(vols) / len(vols) if vols else 0.0
    last_v = max(0.0, _safe_float(candles[-1].get("volume")))
    return bool(avg > 0 and last_v > avg * 1.45)


def _advanced_ta_patterns(candles: list[dict], vwap: float, bb: dict) -> dict:
    patterns: list[str] = []
    score = 0.0
    if len(candles) < 35:
        return {"score": 0.0, "patterns": []}

    closes = [_safe_float(c.get("close")) for c in candles]
    highs = [_safe_float(c.get("high")) for c in candles]
    lows = [_safe_float(c.get("low")) for c in candles]
    opens = [_safe_float(c.get("open")) for c in candles]
    last_close = closes[-1]
    last_high = highs[-1]
    last_low = lows[-1]
    atr14 = max(calc_atr(candles, 14), last_close * 0.001 if last_close else 1e-9)
    tol = max(atr14 * 0.8, last_close * 0.003 if last_close else atr14)

    swing_highs, swing_lows = _pivot_swings(candles, 2, 2)
    prev_high = max(highs[-30:-1]) if len(highs) >= 31 else max(highs[:-1])
    prev_low = min(lows[-30:-1]) if len(lows) >= 31 else min(lows[:-1])
    spike = _volume_spike(candles)

    # Liquidity sweeps and squeeze traps.
    if last_high > prev_high and last_close < prev_high:
        score -= 13.0
        _add_pattern(patterns, "манипулятор: sweep high / ловушка лонгов")
        if spike:
            score -= 5.0
            _add_pattern(patterns, "манипулятор: long squeeze setup / выброс вверх + возврат на объёме")
    if last_low < prev_low and last_close > prev_low:
        score += 13.0
        _add_pattern(patterns, "манипулятор: sweep low / ловушка шортов")
        if spike:
            score += 5.0
            _add_pattern(patterns, "манипулятор: short squeeze setup / выброс вниз + выкуп на объёме")

    if len(swing_highs) >= 2:
        (i1, h1), (i2, h2) = swing_highs[-2], swing_highs[-1]
        valley = min(lows[i1:i2 + 1]) if i2 > i1 else min(lows[-15:])
        if abs(h1 - h2) <= tol * 1.25:
            rejected = last_close < valley + (h2 - valley) * 0.60
            score -= 11.0 + (5.0 if rejected else 0.0)
            _add_pattern(patterns, "двойная вершина")
    if len(swing_lows) >= 2:
        (i1, l1), (i2, l2) = swing_lows[-2], swing_lows[-1]
        peak = max(highs[i1:i2 + 1]) if i2 > i1 else max(highs[-15:])
        if abs(l1 - l2) <= tol * 1.25:
            reclaimed = last_close > l2 + (peak - l2) * 0.40
            score += 11.0 + (5.0 if reclaimed else 0.0)
            _add_pattern(patterns, "двойное дно")

    if len(swing_highs) >= 3:
        (a_i, a), (b_i, b), (c_i, c) = swing_highs[-3], swing_highs[-2], swing_highs[-1]
        shoulders_near = abs(a - c) <= max(tol * 1.8, last_close * 0.006)
        head_higher = b > max(a, c) + tol * 0.45
        if shoulders_near and head_higher:
            neckline = (min(lows[a_i:b_i + 1]) + min(lows[b_i:c_i + 1])) / 2.0 if c_i > b_i > a_i else min(lows[-30:])
            broken = last_close < neckline
            score -= 14.0 + (6.0 if broken else 0.0)
            _add_pattern(patterns, "голова-плечи")
    if len(swing_lows) >= 3:
        (a_i, a), (b_i, b), (c_i, c) = swing_lows[-3], swing_lows[-2], swing_lows[-1]
        shoulders_near = abs(a - c) <= max(tol * 1.8, last_close * 0.006)
        head_lower = b < min(a, c) - tol * 0.45
        if shoulders_near and head_lower:
            neckline = (max(highs[a_i:b_i + 1]) + max(highs[b_i:c_i + 1])) / 2.0 if c_i > b_i > a_i else max(highs[-30:])
            broken = last_close > neckline
            score += 14.0 + (6.0 if broken else 0.0)
            _add_pattern(patterns, "перевёрнутая голова-плечи")

    if len(swing_highs) >= 3 and len(swing_lows) >= 3:
        high_vals = [x[1] for x in swing_highs[-3:]]
        low_vals = [x[1] for x in swing_lows[-3:]]
        flat_highs = max(high_vals) - min(high_vals) <= tol * 1.45
        flat_lows = max(low_vals) - min(low_vals) <= tol * 1.45
        lows_rising = low_vals[-1] > low_vals[0] + tol * 0.30
        highs_falling = high_vals[-1] < high_vals[0] - tol * 0.30
        highs_rising = high_vals[-1] > high_vals[0] + tol * 0.30
        lows_falling = low_vals[-1] < low_vals[0] - tol * 0.30

        if flat_highs and lows_rising:
            score += 10.0
            _add_pattern(patterns, "восходящий треугольник")
        if flat_lows and highs_falling:
            score -= 10.0
            _add_pattern(patterns, "нисходящий треугольник")
        if lows_rising and highs_falling:
            bias = 5.0 if last_close >= (max(high_vals) + min(low_vals)) / 2.0 else -5.0
            score += bias
            _add_pattern(patterns, "симметричный треугольник / сжатие")
        if highs_falling and lows_falling:
            score += 5.0
            _add_pattern(patterns, "falling wedge / потенциальный выкуп")
        if highs_rising and lows_rising:
            score -= 5.0
            _add_pattern(patterns, "rising wedge / риск разгрузки")
        if highs_rising and lows_rising and high_vals[-1] > high_vals[-2] and low_vals[-1] > low_vals[-2]:
            score += 6.0
            _add_pattern(patterns, "структура HH/HL")
        if highs_falling and lows_falling and high_vals[-1] < high_vals[-2] and low_vals[-1] < low_vals[-2]:
            score -= 6.0
            _add_pattern(patterns, "структура LH/LL")

    # Volatility compression / squeezes.
    width = float(bb.get("width", 0.0) or 0.0)
    avg_width = float(bb.get("avg_width", width) or width)
    upper = float(bb.get("upper", 0.0) or 0.0)
    lower = float(bb.get("lower", 0.0) or 0.0)
    mid = float(bb.get("mid", 0.0) or 0.0)
    recent_ranges = [max(1e-9, highs[i] - lows[i]) for i in range(max(0, len(highs) - 8), len(highs))]
    nr7 = recent_ranges and recent_ranges[-1] <= min(recent_ranges)
    inside = last_high < highs[-2] and last_low > lows[-2]
    if avg_width and width < avg_width * 0.62:
        _add_pattern(patterns, "BB squeeze / волатильность сжата")
        if last_close > mid and last_close >= vwap:
            score += 5.0
            _add_pattern(patterns, "squeeze LONG bias")
        elif last_close < mid and last_close <= vwap:
            score -= 5.0
            _add_pattern(patterns, "squeeze SHORT bias")
    if nr7:
        _add_pattern(patterns, "NR7 / экстремально узкая свеча")
    if inside:
        _add_pattern(patterns, "inside bar / пружина")
    if avg_width and width < avg_width * 0.85 and upper and last_close > upper:
        score += 12.0 + (4.0 if spike else 0.0)
        _add_pattern(patterns, "squeeze breakout LONG")
        if spike:
            _add_pattern(patterns, "манипулятор: squeeze breakout на объёме")
    if avg_width and width < avg_width * 0.85 and lower and last_close < lower:
        score -= 12.0 + (4.0 if spike else 0.0)
        _add_pattern(patterns, "squeeze breakdown SHORT")
        if spike:
            _add_pattern(patterns, "манипулятор: squeeze breakdown на объёме")

    # Flag/pennant approximation after impulse.
    if len(closes) >= 18:
        impulse = closes[-9] - closes[-18]
        pullback = closes[-1] - closes[-9]
        if impulse > atr14 * 2.2 and abs(pullback) < abs(impulse) * 0.45:
            score += 5.0
            _add_pattern(patterns, "bull flag / консолидация после импульса")
        if impulse < -atr14 * 2.2 and abs(pullback) < abs(impulse) * 0.45:
            score -= 5.0
            _add_pattern(patterns, "bear flag / консолидация после импульса")

    return {"score": _clamp(score, -35.0, 35.0), "patterns": patterns}



def detect_patterns(candles: list[dict], vwap: float, bb: dict) -> dict:
    patterns: list[str] = []
    score = 0.0
    if len(candles) < 25:
        return {"score": 0.0, "patterns": ["Недостаточно свечей для паттернов"]}

    last = candles[-1]
    prev = candles[-2]
    l_open, l_high, l_low, l_close = (_safe_float(last.get(k)) for k in ("open", "high", "low", "close"))
    p_open, p_high, p_low, p_close = (_safe_float(prev.get(k)) for k in ("open", "high", "low", "close"))

    body = abs(l_close - l_open)
    full = max(1e-12, l_high - l_low)
    upper_wick = l_high - max(l_open, l_close)
    lower_wick = min(l_open, l_close) - l_low

    # Engulfing
    if p_close < p_open and l_close > l_open and l_close >= p_open and l_open <= p_close:
        score += 12.0
        patterns.append("бычье поглощение")
    if p_close > p_open and l_close < l_open and l_close <= p_open and l_open >= p_close:
        score -= 12.0
        patterns.append("медвежье поглощение")

    # Pin/rejection
    if lower_wick > body * 2.2 and lower_wick / full > 0.45 and l_close > (l_low + full * 0.55):
        score += 8.0
        patterns.append("нижний выкуп / bullish pin")
    if upper_wick > body * 2.2 and upper_wick / full > 0.45 and l_close < (l_low + full * 0.45):
        score -= 8.0
        patterns.append("верхний отказ / bearish pin")

    # 3-candle impulse
    last3 = candles[-3:]
    if all(_safe_float(c.get("close")) > _safe_float(c.get("open")) for c in last3) and all(_safe_float(last3[i].get("close")) > _safe_float(last3[i - 1].get("close")) for i in range(1, 3)):
        score += 7.0
        patterns.append("3 бычьи импульсные свечи")
    if all(_safe_float(c.get("close")) < _safe_float(c.get("open")) for c in last3) and all(_safe_float(last3[i].get("close")) < _safe_float(last3[i - 1].get("close")) for i in range(1, 3)):
        score -= 7.0
        patterns.append("3 медвежьи импульсные свечи")

    # Local breakout
    prev20 = candles[-21:-1]
    high20 = max(_safe_float(c.get("high")) for c in prev20)
    low20 = min(_safe_float(c.get("low")) for c in prev20)
    if l_close > high20:
        score += 10.0
        patterns.append("пробой high20")
    if l_close < low20:
        score -= 10.0
        patterns.append("пробой low20")

    # VWAP reclaim/reject
    if p_close < vwap <= l_close:
        score += 9.0
        patterns.append("возврат выше VWAP")
    if p_close > vwap >= l_close:
        score -= 9.0
        patterns.append("потеря VWAP")

    # BB location / squeeze breakout approximation
    upper = bb.get("upper", 0.0)
    lower = bb.get("lower", 0.0)
    width = bb.get("width", 0.0)
    avg_width = bb.get("avg_width", width)
    if avg_width and width < avg_width * 0.7:
        patterns.append("сжатие BB")
    if avg_width and width < avg_width * 0.9 and l_close > upper:
        score += 6.0
        patterns.append("BB squeeze/breakout вверх")
    if avg_width and width < avg_width * 0.9 and l_close < lower:
        score -= 6.0
        patterns.append("BB squeeze/breakout вниз")

    advanced = _advanced_ta_patterns(candles, vwap, bb)
    score += float(advanced.get("score") or 0.0)
    for item in advanced.get("patterns") or []:
        _add_pattern(patterns, item)

    if not patterns:
        patterns.append("сильный свечной паттерн не найден")
    return {"score": _clamp(score, -45.0, 45.0), "patterns": patterns}


def analyze_smart_money(candles: list[dict], side: str, sl_price=None, tp_price=None, pattern_candles_15m: list[dict] | None = None) -> dict:
    if len(candles) < 110:
        raise RuntimeError(f"Нужно минимум 110 свечей 5m, получено {len(candles)}.")

    closes = [_safe_float(c.get("close")) for c in candles]
    last_close = closes[-1]
    macd = calc_macd(closes, 8, 21, 5)
    cmf20 = calc_cmf(candles, 20)
    vwap = calc_session_vwap(candles)
    bb = calc_bbands(closes, 20, 2.0)
    crsi = calc_crsi(closes, 3, 2, 100)
    levels = calc_week_levels(candles)
    level_score, level_text = calc_week_level_score(levels, side)

    pattern_items: list[str] = []
    pattern_score_total = 0.0

    pattern_source = (pattern_candles_15m or [])[-100:]
    if len(pattern_source) >= 60:
        pattern_calc_candles = pattern_source[-60:]
        pattern_closes = [_safe_float(c.get("close")) for c in pattern_calc_candles]
        pattern_vwap = calc_session_vwap(pattern_calc_candles)
        pattern_bb = calc_bbands(pattern_closes, 20, 2.0)
        pattern_15m = detect_patterns(pattern_calc_candles, pattern_vwap, pattern_bb)
        pattern_score_total += float(pattern_15m.get("score") or 0.0)
        for item in pattern_15m.get("patterns") or []:
            _add_pattern(pattern_items, "15m: " + item)
    else:
        _add_pattern(pattern_items, "15m: нужно больше свечей")

    pattern_5m = detect_patterns(candles[-120:], vwap, bb)
    pattern_score_total += float(pattern_5m.get("score") or 0.0) * 0.65
    for item in pattern_5m.get("patterns") or []:
        _add_pattern(pattern_items, "5m: " + item)

    patterns = {"score": pattern_score_total, "patterns": pattern_items}

    components = []

    # Positive score = long; negative score = short.
    macd_score = 0.0
    hist = macd["hist"]
    slope = macd["slope"]
    if hist > 0:
        macd_score += 17.0
    elif hist < 0:
        macd_score -= 17.0
    if slope > 0:
        macd_score += 10.0
    elif slope < 0:
        macd_score -= 10.0
    components.append(("MACD 8/21/5", f"hist={_fmt(hist)}, slope={_fmt(slope)}", macd_score))

    components.append(("Week levels", level_text, level_score))

    cmf_score = _clamp(cmf20 / 0.18, -1.0, 1.0) * 18.0
    components.append(("CMF20", _fmt(cmf20), cmf_score))

    distance_vwap = ((last_close - vwap) / vwap) * 100.0 if vwap else 0.0
    vwap_score = _clamp(distance_vwap / 0.35, -1.0, 1.0) * 16.0
    components.append(("VWAP hlc3/session", f"{_fmt(vwap, 2)} | dist={_fmt(distance_vwap, 3)}%", vwap_score))

    bb_mid = bb["mid"]
    bb_pos = ((last_close - bb_mid) / (bb["upper"] - bb["lower"])) if (bb["upper"] - bb["lower"]) else 0.0
    bb_score = _clamp(bb_pos * 2.0, -1.0, 1.0) * 10.0
    components.append(("BB20 2", f"mid={_fmt(bb_mid, 2)}, pos={_fmt(bb_pos, 3)}", bb_score))

    crsi_value = crsi["crsi"]
    crsi_score = ((crsi_value - 50.0) / 50.0) * 9.0
    if crsi_value > 88.0:
        crsi_score -= 4.0
    if crsi_value < 12.0:
        crsi_score += 4.0
    components.append(("CRSI 3/2/100", _fmt(crsi_value, 2), crsi_score))

    pattern_score = _clamp(patterns["score"], -18.0, 18.0)
    components.append(("15m pattern print", ", ".join(patterns["patterns"][:4]), pattern_score))

    # Predictor/Analysis evaluates direction only.
    tp_sl = {"available": False, "hidden": True, "text": ""}

    side_multiplier = 1.0 if side == "BUY" else -1.0
    total_long_score_without_tpsl = sum(score for _name, _value, score in components)
    technical_directional_score = total_long_score_without_tpsl * side_multiplier
    technical_probability = 50.0 + 45.0 * math.tanh(technical_directional_score / 58.0)
    technical_probability = _clamp(technical_probability, 5.0, 95.0)
    probability = technical_probability

    total_long_score = sum(score for _name, _value, score in components)
    directional_score = total_long_score * side_multiplier

    if probability >= 67.0:
        verdict = "сильное совпадение"
    elif probability >= 57.0:
        verdict = "умеренное совпадение"
    elif probability >= 47.0:
        verdict = "нейтрально / слабое преимущество"
    else:
        verdict = "против направления"

    return {
        "probability": probability,
        "verdict": verdict,
        "side": side,
        "last_close": last_close,
        "components": components,
        "patterns": patterns["patterns"],
        "tp_sl": tp_sl,
        "long_score": total_long_score,
        "directional_score": directional_score,
        "technical_probability": technical_probability,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------------------- Independent probability UI ----------------------------

def _side_label(side: str) -> str:
    return "LONG" if side == "BUY" else "SHORT"


def _probability_color(probability: float, side: str, wait: bool = False):
    if wait:
        return YELLOW
    if side == "BUY":
        return GREEN if probability >= 53.0 else YELLOW
    return RED if probability >= 53.0 else YELLOW


def _extract_component_reasons(components: list[tuple[str, str, float]], side: str, limit: int = 4) -> list[str]:
    sign = 1.0 if side == "BUY" else -1.0
    ranked = []
    for name, value, score in components or []:
        try:
            directional = float(score) * sign
        except Exception:
            directional = 0.0
        if directional > 0.8:
            ranked.append((directional, f"{name}: {value} ({directional:+.1f})"))
    ranked.sort(reverse=True, key=lambda x: x[0])
    return [x[1] for x in ranked[:limit]]


def _load_probability_candles(ticker: str) -> tuple[list[dict], str]:
    ticker = str(ticker or "").strip().upper()
    candles: list[dict] = []
    if load_moex_candles_cached:
        try:
            candles = load_moex_candles_cached(
                ticker,
                interval=5,
                minutes=PREDI_MINUTES,
                limit=260,
                max_stale_seconds=60.0,
                allow_fetch=False,
            ) or []
            if len(candles) >= 64:
                return candles[-260:], "MOEX DB/cache"
        except Exception:
            candles = []
    snapshot = load_chart_candles_by_ticker(ticker, PREDI_INTERVAL, PREDI_MINUTES)
    candles = snapshot.get("candles") or []
    return candles, "terminal 5m"


def _load_pattern_candles(ticker: str) -> list[dict]:
    try:
        pattern_snapshot = load_chart_candles_by_ticker(ticker, PREDI_PATTERN_INTERVAL, PREDI_PATTERN_MINUTES)
        return pattern_snapshot.get("candles") or []
    except Exception:
        return []


def analyze_independent_probability(ticker: str) -> dict:
    ticker = str(ticker or "").strip().upper()
    candles, source = _load_probability_candles(ticker)
    if len(candles) < 40:
        raise RuntimeError(f"Недостаточно 5m свечей для {ticker}: {len(candles)}")
    pattern_candles = _load_pattern_candles(ticker)
    last_price = _safe_float(candles[-1].get("close"))
    order_book_walls = scan_order_book_walls(ticker)

    side_results: dict[str, dict] = {}
    for side in ("BUY", "SELL"):
        ta = analyze_smart_money(candles, side, None, None, pattern_candles)
        brain = {}
        blend = {"final_probability": ta.get("probability"), "impact_pp": 0.0, "model_weight": 0.0}
        if get_brain_snapshot and blend_probability_with_brain:
            try:
                brain = get_brain_snapshot(
                    ticker=ticker,
                    side=side,
                    components=ta.get("components") or [],
                    patterns=ta.get("patterns") or [],
                    base_probability=ta.get("probability"),
                    last_price=ta.get("last_close") or last_price,
                    store_observation=False,
                ) or {}
                blend = blend_probability_with_brain(float(ta.get("probability") or 50.0), brain)
                brain["impact_pp"] = blend.get("impact_pp")
                brain["final_probability"] = blend.get("final_probability")
            except Exception as exc:
                brain = {"trained": False, "error": str(exc), "learning_confidence_pct": 0.0}
        final_probability = _clamp(float(blend.get("final_probability") if blend.get("final_probability") is not None else ta.get("probability") or 50.0), 0.0, 100.0)
        side_results[side] = {
            "side": side,
            "ta": ta,
            "brain": brain,
            "probability": final_probability,
            "technical_probability": float(ta.get("technical_probability") or ta.get("probability") or final_probability),
            "base_probability": float(ta.get("probability") or final_probability),
            "patterns": ta.get("patterns") or [],
            "components": ta.get("components") or [],
        }

    long_p = side_results["BUY"]["probability"]
    short_p = side_results["SELL"]["probability"]
    edge = abs(long_p - short_p)
    best_side = "BUY" if long_p >= short_p else "SELL"
    best = side_results[best_side]
    best_probability = max(long_p, short_p)
    learning_conf = max(
        float((side_results["BUY"].get("brain") or {}).get("learning_confidence_pct") or 0.0),
        float((side_results["SELL"].get("brain") or {}).get("learning_confidence_pct") or 0.0),
    )
    wait = bool(best_probability < 53.0 or edge < 4.0)
    decision_confidence = _clamp((edge * 1.6) + ((best_probability - 50.0) * 1.25) + (learning_conf * 0.35), 0.0, 100.0)
    if wait:
        advice = "WAIT"
        verdict = "Совет: ждать / нет чистого преимущества"
    else:
        advice = best_side
        verdict = f"Совет: {_side_label(best_side)}"

    reasons = _extract_component_reasons(best.get("components") or [], best_side)
    if not reasons:
        reasons.append("нет доминирующего фактора; решение больше вероятностное, чем паттерновое")
    brain = best.get("brain") or {}
    if brain.get("trained"):
        reasons.append(f"AI слой активен: учитываю его как дополнительный вес примерно {float(brain.get('model_weight') or 0.0) * 100:.0f}% к обычной математике.")
    else:
        reasons.append("AI слой по тикеру ещё слабый/копит данные; сильнее опираюсь на 5m математику")
    return {
        "ticker": ticker,
        "timeframe": "5m",
        "source": source,
        "last_price": last_price,
        "long": side_results["BUY"],
        "short": side_results["SELL"],
        "best_side": best_side,
        "advice": advice,
        "wait": wait,
        "probability": best_probability,
        "edge": edge,
        "learning_confidence": learning_conf,
        "decision_confidence": decision_confidence,
        "verdict": verdict,
        "reasons": reasons,
        "order_book_walls": order_book_walls,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }



def _brain_num(brain: dict, key: str, default=None):
    try:
        value = (brain or {}).get(key)
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _brain_backend(brain: dict) -> str:
    brain = brain or {}
    return str(
        brain.get("model_backend")
        or brain.get("backend")
        or (brain.get("torch_info") or {}).get("backend")
        or "—"
    )


def _human_side_word(side: str) -> str:
    return "лонг" if side == "BUY" else "шорт"


def _human_component_name(name: str) -> str:
    mapping = {
        "MACD 8/21/5": "Импульс MACD",
        "Week levels": "Положение в недельном диапазоне",
        "CMF20": "Денежный поток CMF",
        "VWAP hlc3/session": "Положение относительно VWAP",
        "BB20 2": "Положение в Bollinger Bands",
        "CRSI 3/2/100": "Краткосрочная перекупленность/перепроданность",
        "15m pattern print": "Фигуры и свечные паттерны",
    }
    return mapping.get(str(name or ""), str(name or "Фактор"))


def _human_component_text(name: str, value: str, directional: float) -> str:
    title = _human_component_name(name)
    if directional > 1.5:
        effect = "поддерживает выбранное направление"
    elif directional < -1.5:
        effect = "мешает выбранному направлению"
    else:
        effect = "почти нейтрально"
    return f"{title}: {value} — {effect} ({directional:+.1f})"


def _brain_probability_text(side_result: dict) -> str:
    brain = side_result.get("brain") or {}
    final_p = _brain_num(brain, "final_probability", side_result.get("probability"))
    model_p = _brain_num(brain, "model_probability", None)
    torch_p = _brain_num(brain, "torch_probability", None)
    logistic_p = _brain_num(brain, "logistic_probability", None)
    impact = _brain_num(brain, "impact_pp", 0.0)
    backend = _brain_backend(brain)

    parts = [f"итог {float(final_p or 0.0):.1f}%"]
    if torch_p is not None:
        parts.append(f"Torch {torch_p:.1f}%")
    if logistic_p is not None:
        parts.append(f"статистика {logistic_p:.1f}%")
    elif model_p is not None:
        parts.append(f"модель {model_p:.1f}%")
    if impact:
        parts.append(f"сдвиг {float(impact or 0.0):+.1f} п.п.")
    if backend and backend != "—":
        parts.append(f"движок {backend}")
    return " | ".join(parts)


def _brain_explain(side_name: str, side_result: dict) -> list[str]:
    brain = side_result.get("brain") or {}
    lines: list[str] = []
    side_text = "LONG" if str(side_name).upper() in {"LONG", "BUY"} else "SHORT"
    backend = _brain_backend(brain)
    trained = bool(brain.get("trained"))
    model_weight = _brain_num(brain, "model_weight", 0.0)
    impact = _brain_num(brain, "impact_pp", 0.0)
    torch_p = _brain_num(brain, "torch_probability", None)
    logistic_p = _brain_num(brain, "logistic_probability", None)
    final_p = _brain_num(brain, "final_probability", side_result.get("probability"))
    learning = _brain_num(brain, "learning_confidence_pct", 0.0)
    accuracy = _brain_accuracy(brain)
    samples = int(_brain_num(brain, "samples", 0) or 0)
    ticker_samples = int(_brain_num(brain, "ticker_samples", 0) or 0)

    if not trained:
        msg = f"{side_text}: нейрослой пока слабый по этому тикеру"
        if brain.get("error"):
            msg += f" ({brain.get('error')})"
        lines.append(msg)
        return lines

    lines.append(
        f"{side_text}: AI даёт {float(final_p or 0.0):.1f}%. "
        f"Вес AI в итоговом решении около {float(model_weight or 0.0) * 100:.0f}%, "
        f"сдвиг к базовой математике {float(impact or 0.0):+.1f} п.п."
    )
    if torch_p is not None:
        lines.append(f"{side_text}: Torch-сеть отдельно оценивает это направление в {torch_p:.1f}%.")
    if logistic_p is not None:
        lines.append(f"{side_text}: простая статистическая модель даёт {logistic_p:.1f}%.")
    if accuracy is not None:
        lines.append(f"{side_text}: последняя проверочная точность модели около {accuracy:.1f}%.")
    if samples or ticker_samples:
        lines.append(f"{side_text}: база обучения — всего {samples} наблюдений, по тикеру {ticker_samples}.")
    if backend and backend != "—":
        lines.append(f"{side_text}: активный AI-движок: {backend}.")
    return lines


def _brain_accuracy(brain: dict) -> float | None:
    brain = brain or {}
    for key in ("accuracy_pct", "val_accuracy_pct"):
        value = _brain_num(brain, key, None)
        if value is not None:
            return value
    torch_info = brain.get("torch_info") if isinstance(brain.get("torch_info"), dict) else {}
    for key in ("accuracy_pct", "val_accuracy_pct"):
        try:
            value = torch_info.get(key)
            if value is not None:
                return float(value)
        except Exception:
            pass
    return None


def _torch_eval_text(long_r: dict, short_r: dict) -> str:
    lb = long_r.get("brain") or {}
    sb = short_r.get("brain") or {}
    long_t = _brain_num(lb, "torch_probability", _brain_num(lb, "model_probability", long_r.get("probability")))
    short_t = _brain_num(sb, "torch_probability", _brain_num(sb, "model_probability", short_r.get("probability")))
    acc_values = [x for x in (_brain_accuracy(lb), _brain_accuracy(sb)) if x is not None]
    acc = max(acc_values) if acc_values else None
    backend = _brain_backend(lb) if _brain_backend(lb) != "—" else _brain_backend(sb)

    long_t = float(long_t or 0.0)
    short_t = float(short_t or 0.0)
    edge = abs(long_t - short_t)
    if edge < 3.0:
        lean = "явного перекоса нет"
    elif long_t > short_t:
        lean = f"нейросеть больше склоняется к LONG на {edge:.1f} п.п."
    else:
        lean = f"нейросеть больше склоняется к SHORT на {edge:.1f} п.п."

    parts = [f"Torch: LONG {long_t:.1f}% / SHORT {short_t:.1f}% — {lean}"]
    if acc is not None:
        parts.append(f"проверочная точность модели около {acc:.1f}%")
    if backend and backend != "—":
        parts.append(f"движок: {backend}")
    return " | ".join(parts)


def _logistic_eval_text(long_r: dict, short_r: dict) -> str:
    lb = long_r.get("brain") or {}
    sb = short_r.get("brain") or {}
    long_l = _brain_num(lb, "logistic_probability", _brain_num(lb, "base_probability", long_r.get("base_probability")))
    short_l = _brain_num(sb, "logistic_probability", _brain_num(sb, "base_probability", short_r.get("base_probability")))
    long_l = float(long_l or 0.0)
    short_l = float(short_l or 0.0)
    edge = abs(long_l - short_l)
    if edge < 3.0:
        lean = "статистически почти ровно"
    elif long_l > short_l:
        lean = f"базовая статистика за LONG на {edge:.1f} п.п."
    else:
        lean = f"базовая статистика за SHORT на {edge:.1f} п.п."
    return f"Базовая статистика: LONG {long_l:.1f}% / SHORT {short_l:.1f}% — {lean}"


def _final_thought_text(result: dict) -> str:
    long_p = float((result.get("long") or {}).get("probability") or 0.0)
    short_p = float((result.get("short") or {}).get("probability") or 0.0)
    best_side = result.get("best_side") or "BUY"
    wait = bool(result.get("wait"))
    edge = abs(long_p - short_p)
    if wait:
        return f"Итоговая мысль: лучше ждать. Разница всего {edge:.1f} п.п., чистого преимущества нет."
    return f"Итоговая мысль: перевес за {_side_label(best_side)} на {edge:.1f} п.п. Размер позиции всё равно держать аккуратно."


class PrediPanel(ttk.Frame):
    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app = app_context
        self.ticker = ""
        self._refresh_after_id = None
        self._debounce_after_id = None
        self._running = False
        self._last_seq = 0

        self.ticker_var = tk.StringVar(value="Тикер: —")
        self.side_var = tk.StringVar(value="Режим: независимый 5m")
        self._manipulator_active = False
        self._manipulator_blink_on = False
        self._manipulator_blink_after_id = None
        self._manipulator_marquee_after_id = None
        self._manipulator_auto_hide_after_id = None
        self._manipulator_signal_latched = False
        self._manipulator_marquee_text = "   МАНИПУЛЯТОР НА РЫНКЕ! Будьте бдительны.   "
        self._manipulator_marquee_pos = 0

        self.probability_var = tk.StringVar(value="—")
        self.verdict_var = tk.StringVar(value="Жду тикер из вкладки «Сделка»")
        self.score_var = tk.StringVar(value="LONG — | SHORT —")
        self.tp_sl_var = tk.StringVar(value="")
        self.ai_var = tk.StringVar(value="AI: —")
        self.updated_var = tk.StringVar(value="Обновление: —")

        self._build()
        self.after(800, self._periodic_refresh)

    def _build(self):
        self.configure(style="TFrame")
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        # Static title intentionally removed: the panel speaks through the live AI analysis below.

        info = ttk.Frame(self, padding=(12, 0, 12, 8))
        info.pack(fill="x")
        ttk.Label(info, textvariable=self.ticker_var, foreground=FG).pack(side="left", padx=(0, 18))
        ttk.Label(info, textvariable=self.side_var, foreground=FG).pack(side="left", padx=(0, 18))
        ttk.Label(info, textvariable=self.updated_var, foreground=MUTED_FG).pack(side="left")

        card = ttk.Frame(self, style="Card.TFrame", padding=16)
        card.pack(fill="x", padx=12, pady=(0, 10))
        self.prob_label = ttk.Label(card, textvariable=self.probability_var, font=(FONT_FAMILY, 34, "bold"), foreground=BLUE, background=PANEL_BG)
        self.prob_label.pack(anchor="w", pady=(4, 0))
        ttk.Label(card, textvariable=self.verdict_var, style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(card, textvariable=self.score_var, style="PanelMuted.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(card, textvariable=self.ai_var, style="PanelMuted.TLabel").pack(anchor="w", pady=(2, 0))

        self.cage_icon_label = ttk.Label(card, background=PANEL_BG)
        self.cage_icon_label.place(relx=1.0, y=10, x=-10, anchor="ne", width=86, height=86)
        self._load_cage_icon()

        table_frame = ttk.LabelFrame(self, text="Вероятности и факторы", padding=8)
        table_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_frame, columns=["name", "value", "score"], show="headings", height=9)
        self.tree.heading("name", text="Фактор")
        self.tree.heading("value", text="Значение")
        self.tree.heading("score", text="Вклад")
        self.tree.column("name", width=170, anchor="w")
        self.tree.column("value", width=300, anchor="w")
        self.tree.column("score", width=80, anchor="e")
        self.tree.tag_configure("positive", foreground=GREEN)
        self.tree.tag_configure("negative", foreground=RED)
        self.tree.tag_configure("neutral", foreground=MUTED_FG)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        details_frame = ttk.Frame(self, padding=8)
        details_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        details_frame.grid_rowconfigure(0, weight=1)
        details_frame.grid_columnconfigure(0, weight=1)

        self.analysis_notebook = ttk.Notebook(details_frame)
        self.analysis_notebook.grid(row=0, column=0, sticky="nsew")

        analysis_tab = ttk.Frame(self.analysis_notebook, padding=0)
        positions_tab = ttk.Frame(self.analysis_notebook, padding=0)
        today_tab = ttk.Frame(self.analysis_notebook, padding=0)
        self.analysis_notebook.add(analysis_tab, text="Анализ")
        self.analysis_notebook.add(positions_tab, text="Позиции")
        self.analysis_notebook.add(today_tab, text="Мысли по тикеру на сегодня")

        self.details = ScrolledText(analysis_tab, height=8, bg=PANEL_BG_2, fg=FG, insertbackground=FG, relief="flat", borderwidth=0, padx=10, pady=8, font=(FONT_FAMILY, 10))
        self.details.pack(fill="both", expand=True)

        self.positions_review = ScrolledText(positions_tab, height=8, bg=PANEL_BG_2, fg=FG, insertbackground=FG, relief="flat", borderwidth=0, padx=10, pady=8, font=(FONT_FAMILY, 10))
        self.positions_review.pack(fill="both", expand=True)

        self.today_thoughts = ScrolledText(today_tab, height=8, bg=PANEL_BG_2, fg=FG, insertbackground=FG, relief="flat", borderwidth=0, padx=10, pady=8, font=(FONT_FAMILY, 10))
        self.today_thoughts.pack(fill="both", expand=True)

        self.manipulator_var = tk.StringVar(value="")
        self.manipulator_label = tk.Label(
            self,
            textvariable=self.manipulator_var,
            bg=PANEL_BG,
            fg=RED,
            font=(FONT_FAMILY, 12, "bold"),
            padx=10,
            pady=6,
        )
        self.manipulator_label.pack(fill="x", padx=12, pady=(0, 8))
        self.manipulator_label.pack_forget()

        self._set_details("Пока нет тикера. Введи тикер во вкладке «Сделка».\n")
        self._set_positions_review("Пока нет тикера. После анализа здесь будет ревью текущих позиций по этому инструменту.\n")
        self._set_today_thoughts("Пока нет тикера. Здесь будут короткие мысли по тикеру на сегодня.\n")

    def _find_cage_icon_path(self) -> Path | None:
        names = ("cage.png", "cage.gif")
        folders = []
        try:
            current = Path(__file__).resolve()
            folders.extend([
                current.parent,
                current.parent / "assets",
                current.parent / "assests",
                current.parent.parent / "assets",
                current.parent.parent / "assests",
                current.parent.parent,
            ])
        except Exception:
            folders.append(Path.cwd())
        for folder in folders:
            for name in names:
                path = folder / name
                if path.exists():
                    return path
        return None

    def _load_cage_icon(self):
        try:
            path = self._find_cage_icon_path()
            if not path:
                return
            img = tk.PhotoImage(file=str(path))
            # Keep it approximately square 86x86.
            try:
                w, h = img.width(), img.height()
                if w > 86 or h > 86:
                    factor = max(1, int(max(w, h) / 86))
                    img = img.subsample(factor, factor)
            except Exception:
                pass
            self._cage_photo = img
            self.cage_icon_label.configure(image=img)
        except Exception:
            pass

    def _row_value(self, row: dict, *keys, default="—"):
        for key in keys:
            try:
                value = row.get(key)
            except Exception:
                value = None
            if value not in (None, ""):
                return value
        return default

    def _normalize_position_ticker(self, value: str) -> str:
        raw = str(value or "").upper().strip().replace(" ", "")
        if "@" in raw:
            raw = raw.split("@", 1)[0]
        return raw

    def _row_has_closed_state(self, row: dict) -> bool:
        text_parts = []
        for key in (
            "status", "state", "status_text", "state_text", "condition",
            "trade_status", "position_state", "closed", "is_closed", "is_active",
        ):
            try:
                value = row.get(key)
            except Exception:
                value = None
            if value in (None, ""):
                continue
            # Boolean fields are common in cached rows.
            if key in {"closed", "is_closed"} and bool(value):
                return True
            if key == "is_active" and value is False:
                return True
            text_parts.append(str(value).lower())
        text = " ".join(text_parts)
        closed_markers = (
            "closed", "close", "закры", "flat", "inactive",
            "cancel", "cancelled", "canceled", "отмен", "done", "filled_exit",
        )
        return any(marker in text for marker in closed_markers)

    def _row_quantity_abs(self, row: dict) -> float | None:
        for key in (
            "qty", "qty_lots", "lots", "balance", "quantity", "quantity_lots",
            "position", "position_lots", "available_qty", "current_qty",
        ):
            try:
                value = row.get(key)
            except Exception:
                value = None
            if value in (None, ""):
                continue
            try:
                return abs(float(str(value).replace(" ", "").replace(",", ".")))
            except Exception:
                continue
        return None

    def _is_live_position_row(self, row: dict, source_name: str = "") -> bool:
        # Analysis must switch back to ordinary analyst mode after the broker position is flat.
        # Therefore cached/managed trade rows are not enough: a row is treated as an open
        # position only when it has non-zero quantity and is not marked closed/cancelled.
        if not isinstance(row, dict):
            return False
        if self._row_has_closed_state(row):
            return False
        qty_abs = self._row_quantity_abs(row)
        if qty_abs is not None:
            return qty_abs > 1e-12
        # open_positions_rows is the only source trusted without explicit qty.
        return source_name == "open_positions"

    def _position_rows_for_ticker(self, ticker: str) -> list[dict]:
        ticker = self._normalize_position_ticker(ticker)
        rows: list[dict] = []
        sources = [
            ("open_positions", getattr(self.app, "open_positions_rows", {})),
            ("portfolio", getattr(self.app, "portfolio_rows", {})),
            # Deliberately do not use active_trade_rows to decide whether a position is open.
            # Managed protection records can survive briefly after exit; broker position rows are the source of truth.
        ]
        seen = set()
        for source_name, source in sources:
            try:
                iterable = source.values() if isinstance(source, dict) else source
                for row in iterable or []:
                    if not isinstance(row, dict):
                        continue
                    row_ticker = self._normalize_position_ticker(row.get("ticker") or row.get("symbol") or "")
                    if row_ticker != ticker:
                        continue
                    if not self._is_live_position_row(row, source_name=source_name):
                        continue
                    key = (
                        source_name,
                        row.get("account_id"),
                        row.get("instrument_id"),
                        row.get("figi"),
                        row.get("side"),
                        row.get("qty"),
                        row.get("qty_lots"),
                        row.get("lots"),
                        row.get("avg"),
                        row.get("entry_price"),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    enriched = dict(row)
                    enriched["_position_source"] = source_name
                    rows.append(enriched)
            except Exception:
                continue
        return rows

    def _position_side(self, row: dict) -> str:
        raw = str(row.get("side") or row.get("direction") or row.get("side_text") or "").upper()
        if "SELL" in raw or "SHORT" in raw or "ШОРТ" in raw or "ПРОД" in raw:
            return "SELL"
        if "BUY" in raw or "LONG" in raw or "ЛОНГ" in raw or "КУП" in raw:
            return "BUY"
        try:
            qty = float(str(row.get("qty") or row.get("qty_lots") or "0").replace(",", "."))
            if qty < 0:
                return "SELL"
        except Exception:
            pass
        return "BUY"


    def _position_float(self, row: dict, *keys, default: float = 0.0) -> float:
        for key in keys:
            try:
                value = row.get(key)
            except Exception:
                value = None
            if value in (None, ""):
                continue
            try:
                return float(str(value).replace(" ", "").replace(",", "."))
            except Exception:
                continue
        return default

    def _primary_position_row(self, rows: list[dict]) -> dict | None:
        if not rows:
            return None
        def weight(row: dict) -> float:
            qty = self._position_float(row, "qty", "qty_lots", "lots", "balance", default=0.0)
            pnl_abs = abs(self._position_float(row, "pnl", "pnl_value", "expected_yield", default=0.0))
            return abs(qty) * 1000000.0 + pnl_abs
        try:
            return sorted(rows, key=weight, reverse=True)[0]
        except Exception:
            return rows[0]

    def _position_context(self, result: dict | None) -> dict:
        if not result:
            return {"active": False}
        ticker = str(result.get("ticker") or self.ticker or "").upper().strip()
        rows = self._position_rows_for_ticker(ticker)
        row = self._primary_position_row(rows)
        if not row:
            return {"active": False, "rows": []}

        long_r = result.get("long") or {}
        short_r = result.get("short") or {}
        long_p = float(long_r.get("probability") or 0.0)
        short_p = float(short_r.get("probability") or 0.0)
        side = self._position_side(row)
        side_label = "LONG" if side == "BUY" else "SHORT"
        opposite_label = "SHORT" if side == "BUY" else "LONG"
        position_p = long_p if side == "BUY" else short_p
        opposite_p = short_p if side == "BUY" else long_p
        support = position_p - opposite_p
        edge = abs(long_p - short_p)
        wait = bool(result.get("wait"))

        if support >= 10.0 and position_p >= 57.0:
            action = "HOLD"
            title = "ДЕРЖАТЬ"
            verdict = "Открытая позиция подтверждается моделью. Не открываем новый сценарий, сопровождаем текущий вход."
            tag = "positive"
        elif support >= 4.0 and position_p >= 53.0:
            action = "HOLD_LIGHT"
            title = "ДЕРЖАТЬ / НЕ УСИЛИВАТЬ"
            verdict = "Позиция всё ещё по модели, но перевес умеренный. Логика — сопровождать, не добирать автоматически."
            tag = "positive"
        elif support <= -10.0 and opposite_p >= 57.0:
            action = "DANGER"
            title = "ОПАСНО / ПРОТИВ ПОЗИЦИИ"
            verdict = "Модель уже явно против открытой позиции. Это не новый вход в обратную сторону, а сигнал проверить защиту/выход."
            tag = "negative"
        elif support <= -4.0:
            action = "CAUTION"
            title = "ОСЛАБЛО / ВНИМАНИЕ"
            verdict = "Позиция начала идти против текущей математики. Не переворачиваться вслепую; сопровождать по SL/структуре."
            tag = "warning"
        elif wait or edge < 4.0:
            action = "WATCH"
            title = "НАБЛЮДАТЬ"
            verdict = "Чистого преимущества сейчас нет. Для открытой позиции это не приказ закрываться, а режим наблюдения."
            tag = "warning"
        else:
            action = "NEUTRAL"
            title = "НЕЙТРАЛЬНО"
            verdict = "Сигнал смешанный. Открытую позицию оцениваем отдельно от новых входов."
            tag = "neutral"

        qty = self._row_value(row, "qty", "qty_lots", "lots", default="—")
        avg = self._row_value(row, "avg", "avg_price", "entry_price", default="—")
        last = self._row_value(row, "last", "last_price", "current_price", default=result.get("last_price") or "—")
        pnl = self._row_value(row, "pnl", "pnl_value", "expected_yield", default="—")
        account = self._row_value(row, "account_label", "account", "account_id", default="—")

        return {
            "active": True,
            "ticker": ticker,
            "rows": rows,
            "row": row,
            "side": side,
            "side_label": side_label,
            "opposite_label": opposite_label,
            "position_probability": position_p,
            "opposite_probability": opposite_p,
            "support": support,
            "edge": edge,
            "wait": wait,
            "action": action,
            "title": title,
            "verdict": verdict,
            "tag": tag,
            "qty": qty,
            "avg": avg,
            "last": last,
            "pnl": pnl,
            "account": account,
        }

    def _position_context_line(self, result: dict | None) -> str:
        ctx = self._position_context(result)
        if not ctx.get("active"):
            return "Открытая позиция: не найдена"
        return (
            f"Открытая позиция: {ctx.get('side_label')} | {ctx.get('title')} | "
            f"сторона позиции {float(ctx.get('position_probability') or 0.0):.1f}% против "
            f"{ctx.get('opposite_label')} {float(ctx.get('opposite_probability') or 0.0):.1f}% | "
            f"support={float(ctx.get('support') or 0.0):+.1f} п.п. | "
            f"qty={ctx.get('qty')} | avg={ctx.get('avg')} | pnl={ctx.get('pnl')} | "
            f"source={(ctx.get('row') or {}).get('_position_source', '—')}"
        )

    def _build_positions_review(self, result: dict | None) -> str:
        if not result:
            return "Нет анализа для ревью позиций.\n"
        ticker = str(result.get("ticker") or self.ticker or "").upper().strip()
        rows = self._position_rows_for_ticker(ticker)
        long_r = result.get("long") or {}
        short_r = result.get("short") or {}
        long_p = float(long_r.get("probability") or 0.0)
        short_p = float(short_r.get("probability") or 0.0)
        best_side = result.get("best_side") or ("BUY" if long_p >= short_p else "SELL")
        best_label = "LONG" if best_side == "BUY" else "SHORT"
        wait = bool(result.get("wait"))
        edge = float(result.get("edge") or abs(long_p - short_p) or 0.0)
        learning = float(result.get("learning_confidence") or 0.0)
        decision_conf = float(result.get("decision_confidence") or 0.0)

        pos_ctx = self._position_context(result)
        header = [
            f"Тикер: {ticker} | режим: {'сопровождение открытой позиции' if pos_ctx.get('active') else 'поиск нового направления'}",
            f"LONG={long_p:.1f}% | SHORT={short_p:.1f}% | edge={edge:.1f} п.п. | learning={learning:.1f}% | confidence={decision_conf:.1f}%",
        ]
        if pos_ctx.get("active"):
            header.append(self._position_context_line(result))

        if not rows:
            return "\n".join(header + [
                "",
                "Открытых позиций по этому тикеру в таблицах приложения не вижу.",
                "AI-ревью: позиции нет, поэтому это только оценка направления, без сопровождения конкретного входа.",
            ]) + "\n"

        out = header + ["", f"Найдено позиций/строк: {len(rows)}"]
        for idx, row in enumerate(rows, start=1):
            side = self._position_side(row)
            side_label = "LONG" if side == "BUY" else "SHORT"
            position_p = long_p if side == "BUY" else short_p
            opposite_p = short_p if side == "BUY" else long_p
            support = position_p - opposite_p
            brain = (long_r if side == "BUY" else short_r).get("brain") or {}

            qty = self._row_value(row, "qty", "qty_lots", "lots", default="—")
            avg = self._row_value(row, "avg", "avg_price", "entry_price", default="—")
            last = self._row_value(row, "last", "last_price", "current_price", default=result.get("last_price") or "—")
            pnl = self._row_value(row, "pnl", "pnl_value", "expected_yield", default="—")
            account = self._row_value(row, "account_label", "account", "account_id", default="—")

            if support >= 10.0 and position_p >= 57.0:
                verdict = "ДЕРЖАТЬ"
                advice = "модель подтверждает именно сторону уже открытой позиции"
            elif support >= 4.0 and position_p >= 53.0:
                verdict = "ДЕРЖАТЬ / НЕ УСИЛИВАТЬ"
                advice = "позиция ещё по модели, но перевес не максимальный"
            elif support <= -10.0 and opposite_p >= 57.0:
                verdict = "ОПАСНО / ПРОТИВ ПОЗИЦИИ"
                advice = "это сигнал проверить защиту, а не автоматический переворот"
            elif support <= -4.0:
                verdict = "ОСЛАБЛО / ВНИМАНИЕ"
                advice = "сопровождать по SL/структуре, без добора"
            elif wait:
                verdict = "НАБЛЮДАТЬ"
                advice = "чистого преимущества нет; открытая сделка не обязана закрываться от шума"
            else:
                verdict = "НЕЙТРАЛЬНО"
                advice = "сигнал смешанный"

            line = f"#{idx} {side_label} | qty={qty} | avg={avg} | last={last} | pnl={pnl} | {verdict}"
            out.extend(["", line])
            out.append(f"Вероятность стороны позиции: {position_p:.1f}% против {opposite_p:.1f}% у противоположной стороны. Support={support:+.1f} п.п.")
            out.append(f"Логика: {advice}.")

        return "\n".join(out) + "\n"

    def _has_manipulator_signal(self, result: dict) -> bool:
        keywords = (
            "манипулятор",
            "sweep high",
            "sweep low",
            "ловушка лонгов",
            "ловушка шортов",
            "long squeeze setup",
            "short squeeze setup",
            "снятие ликвидности",
            "выброс вверх + возврат",
            "выброс вниз + выкуп",
        )
        texts: list[str] = []
        for side_key in ("long", "short"):
            side_result = result.get(side_key) or {}
            texts.extend(str(x).lower() for x in side_result.get("patterns") or [])
            for name, value, _score in side_result.get("components") or []:
                texts.append(str(name).lower())
                texts.append(str(value).lower())
        return any(any(key in text for key in keywords) for text in texts)

    def _set_manipulator_warning(self, active: bool):
        active = bool(active)
        if not hasattr(self, "_manipulator_active"):
            self._manipulator_active = False
        if not hasattr(self, "_manipulator_blink_on"):
            self._manipulator_blink_on = False
        if not hasattr(self, "_manipulator_blink_after_id"):
            self._manipulator_blink_after_id = None
        if not hasattr(self, "_manipulator_marquee_after_id"):
            self._manipulator_marquee_after_id = None
        if not hasattr(self, "_manipulator_auto_hide_after_id"):
            self._manipulator_auto_hide_after_id = None
        if not hasattr(self, "_manipulator_signal_latched"):
            self._manipulator_signal_latched = False
        if not hasattr(self, "_manipulator_marquee_text"):
            self._manipulator_marquee_text = "   МАНИПУЛЯТОР НА РЫНКЕ! Будьте бдительны.   "
        if not hasattr(self, "_manipulator_marquee_pos"):
            self._manipulator_marquee_pos = 0

        # No signal: fully reset the temporary warning so the next real signal
        # can flash again. The persistent red marker remains in pattern text.
        if not active:
            self._manipulator_signal_latched = False
            self._hide_manipulator_warning(reset_latch=False)
            return

        # Signal is still present and the user has already seen the 5-second flash.
        # Do not restart the banner every refresh cycle; keep it only in pattern text.
        if self._manipulator_signal_latched:
            self._hide_manipulator_warning(reset_latch=False)
            return

        self._manipulator_signal_latched = True
        self._manipulator_active = True
        try:
            if not hasattr(self, "manipulator_var"):
                self.manipulator_var = tk.StringVar(value="")
            if not hasattr(self, "manipulator_label"):
                self.manipulator_label = tk.Label(
                    self,
                    textvariable=self.manipulator_var,
                    bg=PANEL_BG,
                    fg=RED,
                    font=(FONT_FAMILY, 12, "bold"),
                    padx=10,
                    pady=6,
                )
            self.manipulator_label.pack(fill="x", padx=12, pady=(0, 8))
        except Exception:
            pass

        self._manipulator_marquee_pos = 0
        self._blink_manipulator_warning()
        self._scroll_manipulator_warning()

        if self._manipulator_auto_hide_after_id:
            try:
                self.after_cancel(self._manipulator_auto_hide_after_id)
            except Exception:
                pass
        self._manipulator_auto_hide_after_id = self.after(5000, self._auto_hide_manipulator_warning)

    def _auto_hide_manipulator_warning(self):
        self._manipulator_auto_hide_after_id = None
        self._hide_manipulator_warning(reset_latch=False)

    def _hide_manipulator_warning(self, reset_latch: bool = False):
        if reset_latch:
            self._manipulator_signal_latched = False
        self._manipulator_active = False
        for attr in ("_manipulator_blink_after_id", "_manipulator_marquee_after_id", "_manipulator_auto_hide_after_id"):
            after_id = getattr(self, attr, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self.manipulator_var.set("")
            self.manipulator_label.configure(bg=PANEL_BG, fg=RED)
            self.manipulator_label.pack_forget()
        except Exception:
            pass

    def _blink_manipulator_warning(self):
        if not self._manipulator_active:
            return
        self._manipulator_blink_on = not self._manipulator_blink_on
        try:
            if self._manipulator_blink_on:
                self.manipulator_label.configure(bg="#7F0000", fg="#FFFFFF")
            else:
                self.manipulator_label.configure(bg=PANEL_BG, fg=RED)
        except Exception:
            pass
        self._manipulator_blink_after_id = self.after(420, self._blink_manipulator_warning)

    def _scroll_manipulator_warning(self):
        if not self._manipulator_active:
            return
        base = self._manipulator_marquee_text
        line = (base * 8)
        pos = self._manipulator_marquee_pos % max(1, len(base))
        try:
            self.manipulator_var.set(line[pos:pos + 160])
        except Exception:
            pass
        self._manipulator_marquee_pos += 1
        self._manipulator_marquee_after_id = self.after(115, self._scroll_manipulator_warning)


    def _pattern_lines_with_manipulator_marker(self, patterns: list[str], has_manipulator: bool, limit: int = 8) -> str:
        lines: list[str] = []
        if has_manipulator:
            lines.append("• РЫНОЧНЫЕ МАНИПУЛЯЦИИ")
        for item in (patterns or [])[:limit]:
            lines.append(f"• {item}")
        if not lines:
            lines.append("• явный паттерн не найден")
        return "\n".join(lines)

    def _tag_market_manipulations(self, widget):
        try:
            widget.tag_configure("market_manipulations", foreground=RED, font=(FONT_FAMILY, 10, "bold"))
            start = "1.0"
            needle = "РЫНОЧНЫЕ МАНИПУЛЯЦИИ"
            while True:
                pos = widget.search(needle, start, stopindex="end")
                if not pos:
                    break
                end = f"{pos}+{len(needle)}c"
                widget.tag_add("market_manipulations", pos, end)
                start = end
        except Exception:
            pass


    def _build_today_thoughts(self, result: dict) -> str:
        long_r = result.get("long") or {}
        short_r = result.get("short") or {}
        chosen = long_r if result.get("best_side") == "BUY" else short_r
        best_side = result.get("best_side") or "BUY"
        best_label = "WAIT" if result.get("wait") else _side_label(best_side)
        long_p = float(long_r.get("probability") or 0.0)
        short_p = float(short_r.get("probability") or 0.0)
        patterns = chosen.get("patterns") or []

        components = []
        sign = 1.0 if best_side == "BUY" else -1.0
        for name, value, score in chosen.get("components") or []:
            try:
                directional = float(score) * sign
            except Exception:
                directional = 0.0
            if abs(directional) >= 3.0:
                components.append((abs(directional), f"{name}: {value}"))
        components.sort(reverse=True, key=lambda x: x[0])

        out = [
            f"{result.get('ticker')} / мысли на сегодня",
            _torch_eval_text(long_r, short_r),
            _logistic_eval_text(long_r, short_r),
            f"Сигнал для нового входа: {best_label} | LONG {long_p:.1f}% | SHORT {short_p:.1f}% | разница {float(result.get('edge') or 0.0):.1f} п.п.",
        ]

        pos_ctx = self._position_context(result)
        if pos_ctx.get("active"):
            out.append(self._position_context_line(result))
            out.append("Режим: сделка уже открыта, поэтому главный смысл — сопровождение позиции, а не постоянный переворот по каждой свече.")

        has_manipulator = self._has_manipulator_signal(result)
        if has_manipulator:
            out.append("Внимание: есть признаки выноса/ловушки. Не гнаться за импульсом.")

        out.append("")
        out.append("Стакан / плиты:")
        out.append(_format_order_book_walls(result.get("order_book_walls")))

        out.append("")
        out.append("Что видно по технике:")
        out.append(self._pattern_lines_with_manipulator_marker(patterns, has_manipulator, limit=10))

        out.append("")
        out.append("Главные факторы:")
        if components:
            out.extend(f"• {x[1]}" for x in components[:5])
        else:
            out.append("• доминирующего фактора нет")

        out.append("")
        out.append(_final_thought_text(result))
        return "\n".join(out) + "\n"

    def _set_today_thoughts(self, text: str):
        if not hasattr(self, "today_thoughts"):
            return
        self.today_thoughts.configure(state="normal")
        self.today_thoughts.delete("1.0", "end")
        self.today_thoughts.insert("end", text)
        self._tag_market_manipulations(self.today_thoughts)
        self.today_thoughts.configure(state="disabled")


    def set_context(self, ticker: str | None = None, side: str | None = None, sl_price=None, tp_price=None):
        # Intentional: this panel is independent from the currently prepared trade side.
        new_ticker = str(ticker or "").strip().upper()
        changed = new_ticker != self.ticker
        self.ticker = new_ticker
        self.ticker_var.set(f"Тикер: {self.ticker or '—'}")
        self.side_var.set("Режим: независимый 5m")
        if changed:
            self._schedule_refresh(1000)

    def _schedule_refresh(self, delay_ms: int = PREDI_DEBOUNCE_MS):
        if self._debounce_after_id:
            try:
                self.after_cancel(self._debounce_after_id)
            except Exception:
                pass
        self._debounce_after_id = self.after(delay_ms, self.refresh_async)

    def _periodic_refresh(self):
        self.refresh_async()
        self._refresh_after_id = self.after(PREDI_REFRESH_MS, self._periodic_refresh)

    def refresh_async(self):
        if self._running:
            return
        ticker = self.ticker.strip().upper()
        if not ticker:
            self.probability_var.set("—")
            self.verdict_var.set("Жду тикер из вкладки «Сделка»")
            self.updated_var.set("Обновление: —")
            self.score_var.set("LONG — | SHORT —")
            self.ai_var.set("AI: —")
            self._set_today_thoughts("Пока нет тикера.\n")
            self._set_manipulator_warning(False)
            return

        self._running = True
        self._last_seq += 1
        seq = self._last_seq
        self.verdict_var.set("Считаю независимый 5m сценарий...")

        def worker():
            try:
                result = analyze_independent_probability(ticker)
                self.after(0, lambda: self._render_result(seq, result, None))
            except Exception as exc:
                self.after(0, lambda e=exc: self._render_result(seq, None, e))

        threading.Thread(target=worker, daemon=True).start()

    def _wall_items_inline(self, items: list[dict]) -> str:
        if not items:
            return "—"
        parts = []
        for item in items[:ORDER_BOOK_WALL_LIMIT]:
            try:
                price = float(item.get("price") or 0.0)
                qty = float(item.get("qty") or 0.0)
                parts.append(f"{_fmt(price, 4)} × {_fmt(qty, 0)}")
            except Exception:
                continue
        return " | ".join(parts) if parts else "—"


    def _render_result(self, seq: int, result: dict | None, error: Exception | None):
        self._running = False
        if seq != self._last_seq:
            return
        if error:
            self.probability_var.set("—")
            self.verdict_var.set(str(error))
            self.score_var.set("LONG — | SHORT —")
            self.ai_var.set("AI: ошибка / нет расчёта")
            self.updated_var.set(f"Ошибка: {datetime.now().strftime('%H:%M:%S')}")
            self._set_details(f"Ошибка расчёта: {error}\n")
            self._set_positions_review(f"Ошибка расчёта: {error}\n")
            self._set_today_thoughts(f"Ошибка расчёта: {error}\n")
            self._set_manipulator_warning(False)
            self._clear_tree()
            return
        if not result:
            return

        long_r = result.get("long") or {}
        short_r = result.get("short") or {}
        long_p = float(long_r.get("probability") or 0.0)
        short_p = float(short_r.get("probability") or 0.0)
        best_side = result.get("best_side") or "BUY"
        wait = bool(result.get("wait"))
        best_label = "WAIT" if wait else _side_label(best_side)
        probability = float(result.get("probability") or 0.0)
        pos_ctx = self._position_context(result)

        if pos_ctx.get("active"):
            position_label = str(pos_ctx.get("side_label") or "POSITION")
            position_probability = float(pos_ctx.get("position_probability") or 0.0)
            self.probability_var.set(f"{pos_ctx.get('title')} {position_label} {position_probability:.1f}%")
            self.verdict_var.set(str(pos_ctx.get("verdict") or "—"))
            self.side_var.set(f"Режим: сопровождение {position_label} позиции")
        else:
            self.probability_var.set(f"{best_label} {probability:.1f}%")
            self.verdict_var.set(str(result.get("verdict") or "—"))
            self.side_var.set("Режим: независимый 5m")

        self.score_var.set(
            f"LONG {long_p:.1f}% | SHORT {short_p:.1f}% | edge={float(result.get('edge') or 0.0):.1f} п.п. | "
            f"уверенность={float(result.get('decision_confidence') or 0.0):.1f}% | learning={float(result.get('learning_confidence') or 0.0):.1f}%"
        )

        long_brain = long_r.get("brain") or {}
        short_brain = short_r.get("brain") or {}
        chosen = long_r if best_side == "BUY" else short_r
        chosen_brain = chosen.get("brain") or {}
        self.ai_var.set(_torch_eval_text(long_r, short_r))
        self.tp_sl_var.set("")
        self.updated_var.set(f"Обновление: {result.get('updated_at')} | {result.get('source')} | TF 5m")

        try:
            if pos_ctx.get("active"):
                tag = str(pos_ctx.get("tag") or "neutral")
                if tag == "positive":
                    self.prob_label.configure(foreground=GREEN)
                elif tag == "negative":
                    self.prob_label.configure(foreground=RED)
                elif tag == "warning":
                    self.prob_label.configure(foreground=YELLOW)
                else:
                    self.prob_label.configure(foreground=_probability_color(probability, best_side, wait=wait))
            else:
                self.prob_label.configure(foreground=_probability_color(probability, best_side, wait=wait))
        except Exception:
            pass

        self._clear_tree()
        if pos_ctx.get("active"):
            self.tree.insert(
                "",
                "end",
                values=[
                    "Открытая позиция",
                    f"{pos_ctx.get('side_label')} | {pos_ctx.get('title')} | support={float(pos_ctx.get('support') or 0.0):+.1f} п.п.",
                    f"{float(pos_ctx.get('position_probability') or 0.0) - 50.0:+.1f}",
                ],
                tags=(str(pos_ctx.get("tag") or "neutral"),),
            )
        self.tree.insert("", "end", values=["LONG", f"{long_p:.1f}% | TA={long_r.get('technical_probability', long_p):.1f}%", f"{long_p - 50:+.1f}"], tags=("positive" if long_p >= short_p else "neutral",))
        self.tree.insert("", "end", values=["SHORT", f"{short_p:.1f}% | TA={short_r.get('technical_probability', short_p):.1f}%", f"{short_p - 50:+.1f}"], tags=("negative" if short_p > long_p else "neutral",))
        self.tree.insert("", "end", values=["Нейросеть LONG", _brain_probability_text(long_r), f"{_brain_num(long_brain, 'impact_pp', 0.0):+.1f}"], tags=("positive" if _brain_num(long_brain, 'impact_pp', 0.0) > 0 else "negative" if _brain_num(long_brain, 'impact_pp', 0.0) < 0 else "neutral",))
        self.tree.insert("", "end", values=["Нейросеть SHORT", _brain_probability_text(short_r), f"{_brain_num(short_brain, 'impact_pp', 0.0):+.1f}"], tags=("positive" if _brain_num(short_brain, 'impact_pp', 0.0) > 0 else "negative" if _brain_num(short_brain, 'impact_pp', 0.0) < 0 else "neutral",))
        walls = result.get("order_book_walls") or {}
        if walls.get("available"):
            self.tree.insert("", "end", values=["Плиты BUY", self._wall_items_inline(walls.get("buy") or []), ""], tags=("positive",))
            self.tree.insert("", "end", values=["Плиты SELL", self._wall_items_inline(walls.get("sell") or []), ""], tags=("negative",))
        side_sign = 1.0 if best_side == "BUY" else -1.0
        ranked_components = []
        for name, value, score in chosen.get("components") or []:
            try:
                directional = float(score) * side_sign
            except Exception:
                directional = 0.0
            ranked_components.append((abs(directional), directional, name, value, score))
        ranked_components.sort(reverse=True, key=lambda x: x[0])
        for _abs_score, directional, name, value, raw_score in ranked_components[:8]:
            tag = "positive" if directional > 0.8 else "negative" if directional < -0.8 else "neutral"
            self.tree.insert("", "end", values=[name, value, f"{directional:+.2f}"], tags=(tag,))

        reasons = "\n".join(f"• {item}" for item in result.get("reasons", []))
        patterns = chosen.get("patterns") or []
        has_manipulator = self._has_manipulator_signal(result)
        pattern_text = self._pattern_lines_with_manipulator_marker(patterns, has_manipulator, limit=8)

        ai_text = _torch_eval_text(long_r, short_r) + "\n" + _logistic_eval_text(long_r, short_r)

        chosen_components = chosen.get("components") or []
        component_lines = []
        side_sign = 1.0 if best_side == "BUY" else -1.0
        for name, value, score in sorted(
            chosen_components,
            key=lambda item: abs(float(item[2]) if len(item) > 2 else 0.0),
            reverse=True,
        )[:10]:
            try:
                directional = float(score) * side_sign
            except Exception:
                directional = 0.0
            component_lines.append("• " + _human_component_text(name, value, directional))
        component_text = "\n".join(component_lines[:5]) if component_lines else "• компонентов мало / нет данных"

        self._set_details(
            f"{result.get('ticker')} | цена={float(result.get('last_price') or 0.0):.4f} | анализ 5m/15m\n"
            f"{ai_text}\n"
            f"Итог для нового входа: LONG {long_p:.1f}% | SHORT {short_p:.1f}% | разница {float(result.get('edge') or 0.0):.1f} п.п. | сигнал {best_label}\n"
            f"{self._position_context_line(result)}\n\n"
            f"Стакан / плиты:\n{_format_order_book_walls(result.get('order_book_walls'))}\n\n"
            f"Главные факторы:\n{component_text}\n\n"
            f"Паттерны:\n{pattern_text}\n\n"
            f"{_final_thought_text(result)}\n"
        )
        self._set_positions_review(self._build_positions_review(result))
        self._set_today_thoughts(self._build_today_thoughts(result))
        self._set_manipulator_warning(self._has_manipulator_signal(result))

    def _clear_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _set_details(self, text: str):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self._tag_market_manipulations(self.details)
        self.details.configure(state="disabled")

    def _set_positions_review(self, text: str):
        if not hasattr(self, "positions_review"):
            return
        self.positions_review.configure(state="normal")
        self.positions_review.delete("1.0", "end")
        self.positions_review.insert("end", text)
        self.positions_review.configure(state="disabled")
