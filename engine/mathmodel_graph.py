import math
import threading
import tkinter as tk
from tkinter import ttk

from architecture import (
    BG, FG, MUTED_FG, GREEN, RED, YELLOW, BLUE,
    FONT_FAMILY, load_chart_candles_by_ticker,
)

from .predi_brain import get_brain_snapshot, forecast_bias_from_brain, format_accuracy

try:
    from .predi_moex import load_moex_candles_cached
except Exception:
    load_moex_candles_cached = None


GRAPH_API_MIN_INTERVAL_SECONDS = 3.0
_GRAPH_API_CACHE = {}
FORECAST_CANDLE_COUNT = 15



def _load_cached_5m_candles(ticker: str) -> list[dict]:
    import time
    ticker = str(ticker or "").upper().strip()
    now = time.time()
    cached = _GRAPH_API_CACHE.get(ticker)
    if cached and now - float(cached.get("ts", 0.0)) < GRAPH_API_MIN_INTERVAL_SECONDS:
        return list(cached.get("candles") or [])

    candles = []
    if load_moex_candles_cached:
        candles = load_moex_candles_cached(
            ticker,
            interval=5,
            minutes=5 * 180,
            limit=96,
            max_stale_seconds=GRAPH_API_MIN_INTERVAL_SECONDS,
            allow_fetch=False,
        )
    if not candles and load_chart_candles_by_ticker:
        # Emergency fallback only: normal path is MOEX DB/cache to avoid T-API limits.
        data = load_chart_candles_by_ticker(ticker, "CANDLE_INTERVAL_5_MIN", minutes=5 * 150)
        candles = (data.get("candles") or [])[-80:]
    candles = candles[-96:]
    _GRAPH_API_CACHE[ticker] = {"ts": now, "candles": candles}
    return list(candles)


def _brain_prob(brain: dict | None) -> float:
    brain = brain or {}
    # Predictor is model-first: trained torch/logistic -> raw model -> blended fallback -> base fallback.
    # That keeps the future graph tied to the AI/learning layer instead of drawing an independent TA fantasy.
    for key in ("torch_probability", "model_probability", "raw_model_probability", "final_probability", "base_probability"):
        value = brain.get(key)
        try:
            if value is not None:
                return max(0.0, min(100.0, float(value)))
        except Exception:
            pass
    return 50.0


def _build_market_brain(buy_brain: dict | None, sell_brain: dict | None) -> dict:
    buy = dict(buy_brain or {})
    sell = dict(sell_brain or {})
    buy_p = _brain_prob(buy)
    sell_p = _brain_prob(sell)
    edge = buy_p - sell_p
    direction_score = max(-1.0, min(1.0, edge / 100.0))
    dominant = buy if buy_p >= sell_p else sell
    blended = dict(dominant)

    learn_conf = max(float(buy.get("learning_confidence_pct") or 0.0), float(sell.get("learning_confidence_pct") or 0.0))
    samples = max(int(buy.get("samples") or 0), int(sell.get("samples") or 0))
    ticker_samples = max(int(buy.get("ticker_samples") or 0), int(sell.get("ticker_samples") or 0))

    backend = (
        dominant.get("model_backend")
        or dominant.get("backend")
        or (dominant.get("torch_info") or {}).get("backend")
        or "probability"
    )

    blended["buy_probability"] = buy_p
    blended["sell_probability"] = sell_p
    blended["probability_edge"] = edge
    blended["market_direction_score"] = direction_score
    blended["learning_confidence_pct"] = learn_conf
    blended["samples"] = samples
    blended["ticker_samples"] = ticker_samples
    blended["accuracy_pct"] = dominant.get("accuracy_pct")
    blended["model_backend"] = backend
    blended["patterns"] = list(dict.fromkeys((buy.get("patterns") or []) + (sell.get("patterns") or [])))[:12]

    # Use strongest orderbook signal, but keep direction signed.
    if buy.get("orderbook") or sell.get("orderbook"):
        ob_buy = buy.get("orderbook") or {}
        ob_sell = sell.get("orderbook") or {}
        blended["orderbook"] = ob_buy if abs(float(ob_buy.get("imbalance") or 0.0)) >= abs(float(ob_sell.get("imbalance") or 0.0)) else ob_sell

    if direction_score > 0.08:
        note = "bullish"
    elif direction_score < -0.08:
        note = "bearish"
    else:
        note = "neutral"
    blended["forecast_note"] = note
    blended["forecast_reason"] = (
        f"{note}: LONG {buy_p:.1f}% / SHORT {sell_p:.1f}% | "
        f"edge {edge:+.1f} п.п. | confidence {learn_conf:.1f}% | backend {backend}"
    )
    return blended


class MathModelGraphPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.ticker = ""
        self.side = "BUY"
        self.buy_brain_snapshot = {}
        self.sell_brain_snapshot = {}
        self.candles = []
        self.forecast = []
        self.brain_snapshot = {}
        self.loading = False
        self._refresh_after_id = None
        self.zoom = 1.0
        self.min_zoom = 0.55
        self.max_zoom = 4.0
        self.status_var = tk.StringVar(value="mathmodel graph: —")
        self.learning_confidence_var = tk.StringVar(value="Learning confidence: 0.0%")
        self.zoom_var = tk.StringVar(value="Zoom: 1.00x")
        self.build()
        self.schedule_refresh(700)

    def build(self):
        self.configure(style="TFrame")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        top = ttk.Frame(self, padding=(0, 0, 0, 8))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="predictor", font=(FONT_FAMILY, 13, "bold"), foreground=FG, background=BG).pack(side="left")
        ttk.Label(top, textvariable=self.status_var, style="Muted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Button(top, text="Обновить", command=self.refresh_now).pack(side="right")
        ttk.Button(top, text="+", width=3, command=lambda: self.adjust_zoom(1.18)).pack(side="right", padx=(4, 0))
        ttk.Button(top, text="-", width=3, command=lambda: self.adjust_zoom(1 / 1.18)).pack(side="right", padx=(4, 0))
        ttk.Label(top, textvariable=self.zoom_var, style="Muted.TLabel").pack(side="right", padx=(0, 10))
        ttk.Label(top, textvariable=self.learning_confidence_var, style="Muted.TLabel").pack(side="right", padx=(0, 12))

        self.canvas = tk.Canvas(self, bg="#05080C", highlightthickness=1, highlightbackground="#26384A")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.draw())
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", lambda _event: self.adjust_zoom(1.18))
        self.canvas.bind("<Button-5>", lambda _event: self.adjust_zoom(1 / 1.18))
        self.canvas.bind("<Double-Button-1>", lambda _event: self.reset_zoom())

    def adjust_zoom(self, factor: float):
        try:
            self.zoom = max(self.min_zoom, min(self.max_zoom, self.zoom * float(factor)))
            self.zoom_var.set(f"Zoom: {self.zoom:.2f}x")
            self.draw()
        except Exception:
            pass

    def reset_zoom(self):
        self.zoom = 1.0
        self.zoom_var.set("Zoom: 1.00x")
        self.draw()

    def on_mousewheel(self, event):
        try:
            if getattr(event, "delta", 0) > 0:
                self.adjust_zoom(1.18)
            else:
                self.adjust_zoom(1 / 1.18)
        except Exception:
            pass

    def set_context(self, ticker: str, side: str = "BUY"):
        ticker = str(ticker or "").strip().upper()
        side = side if side in {"BUY", "SELL"} else "BUY"
        changed = ticker != self.ticker or side != self.side
        self.ticker = ticker
        self.side = side
        if changed:
            self.refresh_now()

    def schedule_refresh(self, ms: int = 6000):
        if self._refresh_after_id:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
        self._refresh_after_id = self.after(ms, self.refresh_now)

    def refresh_now(self):
        self._refresh_after_id = None
        if self.loading:
            self.schedule_refresh(3000)
            return
        if not self.ticker:
            self.status_var.set("жду тикер")
            self.candles = []
            self.forecast = []
            self.brain_snapshot = {}
            self.draw()
            self.schedule_refresh(3000)
            return
        self.loading = True
        ticker_snapshot = self.ticker
        self.status_var.set(f"{ticker_snapshot} | 5m | обновление...")

        def worker():
            candles = _load_cached_5m_candles(ticker_snapshot)
            buy_brain = get_brain_snapshot(ticker=ticker_snapshot, side="BUY")
            sell_brain = get_brain_snapshot(ticker=ticker_snapshot, side="SELL")
            market_brain = _build_market_brain(buy_brain, sell_brain)
            forecast = build_forecast(candles, market_brain)
            return candles, forecast, market_brain, buy_brain, sell_brain

        def done(candles, forecast, brain_snapshot=None, buy_brain=None, sell_brain=None, error=None):
            self.loading = False
            if error:
                self.status_var.set(f"{ticker_snapshot}: ошибка")
                self.schedule_refresh(6000)
                return
            if ticker_snapshot == self.ticker:
                self.candles = candles
                self.forecast = forecast
                self.brain_snapshot = brain_snapshot or {}
                self.buy_brain_snapshot = buy_brain or {}
                self.sell_brain_snapshot = sell_brain or {}
                learn = float((self.brain_snapshot or {}).get("learning_confidence_pct") or 0.0)
                self.learning_confidence_var.set(f"Learning confidence: {learn:.1f}%")
                note = str((self.brain_snapshot or {}).get("forecast_note") or "neutral")
                buy_p = _safe_float((self.brain_snapshot or {}).get("buy_probability"), 50.0)
                sell_p = _safe_float((self.brain_snapshot or {}).get("sell_probability"), 50.0)
                backend = str((self.brain_snapshot or {}).get("model_backend") or "probability")
                self.status_var.set(
                    f"{ticker_snapshot} | 15 future candles | model-only | {note} | L {buy_p:.1f}% / S {sell_p:.1f}% | {backend}"
                )
                self.draw()
            self.schedule_refresh(3000)

        def run():
            try:
                candles, forecast, brain_snapshot, buy_brain, sell_brain = worker()
                self.after(0, lambda: done(candles, forecast, brain_snapshot, buy_brain, sell_brain))
            except Exception as exc:
                self.after(0, lambda e=exc: done([], [], {}, {}, {}, e))

        threading.Thread(target=run, daemon=True).start()

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        w = max(320, canvas.winfo_width())
        h = max(220, canvas.winfo_height())
        pad_l, pad_r, pad_t, pad_b = 54, 18, 22, 34
        plot_w = max(50, w - pad_l - pad_r)
        plot_h = max(50, h - pad_t - pad_b)

        history = list(self.candles or [])
        forecast = list(self.forecast or [])
        if not history and not forecast:
            canvas.create_text(w // 2, h // 2, text="жду данные", fill=MUTED_FG, font=(FONT_FAMILY, 12, "bold"))
            return

        # Zoom changes only how much past history is visible. Future always keeps all 15 model candles.
        base_history = 72
        visible_history_count = int(base_history / max(0.55, min(4.0, float(self.zoom or 1.0))))
        visible_history_count = max(16, min(len(history), visible_history_count)) if history else 0
        visible_history = history[-visible_history_count:] if visible_history_count else []
        all_candles = visible_history + forecast
        history_len = len(visible_history)

        highs = [float(c.get("high", 0) or 0) for c in all_candles]
        lows = [float(c.get("low", 0) or 0) for c in all_candles]
        hi = max(highs) if highs else 1.0
        lo = min(lows) if lows else 0.0
        if hi <= lo:
            hi += 1.0
            lo -= 1.0
        margin = (hi - lo) * 0.08
        hi += margin
        lo -= margin

        def y(price):
            return pad_t + ((hi - float(price)) / (hi - lo)) * plot_h

        total = len(all_candles)
        step = plot_w / max(1, total)
        body_w = max(3, min(15, step * 0.62))

        accuracy_text = format_accuracy(self.brain_snapshot or {}).replace("AI accuracy", "accuracy")

        for i in range(5):
            yy = pad_t + plot_h * i / 4
            canvas.create_line(pad_l, yy, w - pad_r, yy, fill="#17202A")
            price = hi - (hi - lo) * i / 4
            canvas.create_text(8, yy, text=f"{price:.2f}", anchor="w", fill="#667789", font=(FONT_FAMILY, 8))

        split_x = pad_l + step * history_len
        if forecast:
            canvas.create_line(split_x, pad_t, split_x, h - pad_b, fill=YELLOW, dash=(2, 4), width=1)
            canvas.create_text(split_x + 6, pad_t + 10, text="AI future x15", anchor="w", fill=YELLOW, font=(FONT_FAMILY, 9, "bold"))
            canvas.create_text(
                split_x + 6,
                pad_t + 25,
                text=f"{accuracy_text} | zoom={self.zoom:.2f}x",
                anchor="w",
                fill=MUTED_FG,
                font=(FONT_FAMILY, 8, "bold"),
            )

        for idx, candle in enumerate(all_candles):
            is_future = idx >= history_len
            x = pad_l + step * idx + step / 2
            o = float(candle.get("open", 0) or 0)
            c = float(candle.get("close", 0) or 0)
            hh = float(candle.get("high", 0) or 0)
            ll = float(candle.get("low", 0) or 0)
            color = GREEN if c >= o else RED
            if is_future:
                # Forecast candles are normal candles, not dashed placeholders.
                canvas.create_line(x, y(ll), x, y(hh), fill=color, width=2)
            else:
                canvas.create_line(x, y(ll), x, y(hh), fill=color, width=1)
            y1, y2 = y(o), y(c)
            top, bot = min(y1, y2), max(y1, y2)
            if bot - top < 2:
                bot = top + 2
            if is_future:
                canvas.create_rectangle(x - body_w / 2, top, x + body_w / 2, bot, outline=color, fill=color, stipple="gray50", width=1)
                conf = float(candle.get("confidence_pct", 0) or 0)
                conf_y = min(h - 12, pad_t + plot_h + 10)
                conf_color = "#9FE6B8" if conf >= 70 else ("#F6D365" if conf >= 55 else "#F29B9B")
                canvas.create_text(
                    x,
                    conf_y,
                    text=f"{conf:.0f}%",
                    anchor="n",
                    fill=conf_color,
                    font=(FONT_FAMILY, 7, "bold"),
                )
            else:
                canvas.create_rectangle(x - body_w / 2, top, x + body_w / 2, bot, outline=color, fill=color, width=1)

        reason = str((self.brain_snapshot or {}).get("forecast_reason") or "")
        if reason:
            canvas.create_text(
                pad_l,
                h - 21,
                text=reason[:145],
                anchor="sw",
                fill=MUTED_FG,
                font=(FONT_FAMILY, 8, "bold"),
            )

        if history:
            last = float(history[-1].get("close", 0) or 0)
            yy = y(last)
            canvas.create_line(pad_l, yy, w - pad_r, yy, fill="#FFFFFF", dash=(4, 3), width=1)
            canvas.create_text(
                pad_l + 4,
                yy - 8,
                text=f"NOW {last:.2f}",
                anchor="w",
                fill="#FFFFFF",
                font=(FONT_FAMILY, 9, "bold"),
            )


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sma(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    chunk = values[-period:] if len(values) >= period else values
    return sum(chunk) / max(1, len(chunk))


def _stdev(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    chunk = values[-period:] if len(values) >= period else values
    mean = sum(chunk) / max(1, len(chunk))
    var = sum((x - mean) ** 2 for x in chunk) / max(1, len(chunk))
    return math.sqrt(max(0.0, var))


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    current = values[0]
    for value in values[1:]:
        current = (value * alpha) + (current * (1.0 - alpha))
        out.append(current)
    return out


def _atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for idx in range(1, len(candles)):
        high = _safe_float(candles[idx].get("high"))
        low = _safe_float(candles[idx].get("low"))
        prev_close = _safe_float(candles[idx - 1].get("close"))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    chunk = trs[-period:] if len(trs) >= period else trs
    return sum(chunk) / max(1, len(chunk))


def _extract_pattern_bias(patterns: list[str], side: str) -> float:
    if not patterns:
        return 0.0
    bias = 0.0
    side_sign = 1.0 if side == "BUY" else -1.0
    for item in patterns[:8]:
        p = str(item).lower()
        if any(x in p for x in ("быч", "выкуп", "пробой high", "отскок", "ретест поддержки", "возврат выше")):
            bias += 0.18 * side_sign
        if any(x in p for x in ("медв", "отказ", "пробой low", "нож", "ретест сопротивления", "потеря")):
            bias -= 0.18 * side_sign
        if "ложный пробой high" in p or "закол" in p and side == "SELL":
            bias += 0.12
        if "ложный пробой low" in p or "закол" in p and side == "BUY":
            bias += 0.12
    return _clamp(bias, -0.8, 0.8)


def _market_regime(candles: list[dict]) -> dict:
    closes = [_safe_float(c.get("close")) for c in candles]
    highs = [_safe_float(c.get("high")) for c in candles]
    lows = [_safe_float(c.get("low")) for c in candles]
    opens = [_safe_float(c.get("open")) for c in candles]
    last = closes[-1]
    atr = _atr(candles, 14)
    ema_fast = _ema_series(closes, 8)
    ema_slow = _ema_series(closes, 21)
    ema_fast_now = ema_fast[-1] if ema_fast else last
    ema_slow_now = ema_slow[-1] if ema_slow else last
    ema_gap = ((ema_fast_now - ema_slow_now) / max(last, 1e-9)) if last else 0.0
    ret3 = (closes[-1] - closes[-4]) / max(atr, 1e-9) if len(closes) >= 4 and atr > 0 else 0.0
    ret8 = (closes[-1] - closes[-9]) / max(atr, 1e-9) if len(closes) >= 9 and atr > 0 else 0.0
    local_high = max(highs[-24:])
    local_low = min(lows[-24:])
    local_range = max(local_high - local_low, 1e-9)
    range_pos = _clamp((last - local_low) / local_range, 0.0, 1.0)
    bb_mid = _sma(closes, 20)
    bb_dev = _stdev(closes, 20)
    bb_width = (bb_dev * 4.0) / max(last, 1e-9)
    avg_range = sum(max(0.0, h - l) for h, l in zip(highs[-20:], lows[-20:])) / max(1, len(highs[-20:]))
    avg_body = sum(abs(c - o) for c, o in zip(closes[-20:], opens[-20:])) / max(1, len(closes[-20:]))
    trend_strength = _clamp((abs(ema_gap) * 220.0) + (abs(ret8) * 0.22), 0.0, 1.0)
    range_strength = _clamp((1.0 - trend_strength) * 0.7 + (0.08 - min(bb_width, 0.08)) / 0.08 * 0.3, 0.0, 1.0)
    breakout_up = closes[-1] > local_high - (0.12 * max(atr, avg_range, 1e-9))
    breakout_down = closes[-1] < local_low + (0.12 * max(atr, avg_range, 1e-9))
    return {
        "last": last,
        "atr": atr,
        "ema_gap": ema_gap,
        "ret3": ret3,
        "ret8": ret8,
        "local_high": local_high,
        "local_low": local_low,
        "range_pos": range_pos,
        "bb_mid": bb_mid,
        "bb_width": bb_width,
        "avg_range": avg_range,
        "avg_body": avg_body,
        "trend_strength": trend_strength,
        "range_strength": range_strength,
        "breakout_up": breakout_up,
        "breakout_down": breakout_down,
    }


def _brain_strength(brain: dict, regime: dict, probability: float, edge: float) -> float:
    learn = _clamp(_safe_float(brain.get("learning_confidence_pct"), 0.0) / 100.0, 0.0, 1.0)
    samples = _safe_float(brain.get("ticker_samples") or brain.get("samples"), 0.0)
    sample_power = _clamp(math.log10(max(1.0, samples)) / 3.0, 0.0, 1.0)
    edge_power = _clamp(abs(edge) / 32.0, 0.0, 1.0)
    model_power = _clamp(abs(probability - 50.0) / 34.0, 0.0, 1.0)
    return _clamp(0.18 + learn * 0.34 + sample_power * 0.20 + edge_power * 0.18 + model_power * 0.10, 0.12, 1.0)


def _large_move_cap(last_close: float, atr: float, confidence_strength: float) -> float:
    # Future candles may be large, but not physically absurd. Strong neural/prob signal allows wider movement.
    return max(atr * (1.8 + confidence_strength * 3.2), last_close * (0.006 + confidence_strength * 0.020))


def _history_shape(candles: list[dict]) -> dict:
    chunk = candles[-96:] if len(candles) >= 96 else candles
    if not chunk:
        return {"avg_body": 0.0, "avg_range": 0.0, "upper_ratio": 0.5, "lower_ratio": 0.5, "impulse": 0.0}
    bodies = []
    ranges = []
    uppers = []
    lowers = []
    signed_bodies = []
    for c in chunk:
        o = _safe_float(c.get("open"))
        h = _safe_float(c.get("high"))
        l = _safe_float(c.get("low"))
        cl = _safe_float(c.get("close"))
        rng = max(1e-9, h - l)
        body = abs(cl - o)
        bodies.append(body)
        ranges.append(rng)
        uppers.append(max(0.0, h - max(o, cl)) / rng)
        lowers.append(max(0.0, min(o, cl) - l) / rng)
        signed_bodies.append((cl - o) / rng)
    avg_body = sum(bodies[-32:]) / max(1, len(bodies[-32:]))
    avg_range = sum(ranges[-32:]) / max(1, len(ranges[-32:]))
    upper_ratio = sum(uppers[-32:]) / max(1, len(uppers[-32:]))
    lower_ratio = sum(lowers[-32:]) / max(1, len(lowers[-32:]))
    impulse = sum(signed_bodies[-8:]) / max(1, len(signed_bodies[-8:]))
    return {
        "avg_body": avg_body,
        "avg_range": avg_range,
        "upper_ratio": upper_ratio,
        "lower_ratio": lower_ratio,
        "impulse": _clamp(impulse, -1.0, 1.0),
    }


def _model_side_probability(brain: dict) -> float:
    for key in ("torch_probability", "model_probability", "raw_model_probability", "final_probability", "base_probability"):
        try:
            value = brain.get(key)
            if value is not None:
                return _clamp(float(value), 0.0, 100.0)
        except Exception:
            pass
    return 50.0


def _model_only_strength(brain: dict, chosen_probability: float, edge: float) -> float:
    learn = _clamp(_safe_float(brain.get("learning_confidence_pct"), 0.0) / 100.0, 0.0, 1.0)
    samples = _safe_float(brain.get("ticker_samples") or brain.get("samples"), 0.0)
    sample_power = _clamp(math.log10(max(1.0, samples)) / 3.0, 0.0, 1.0)
    try:
        acc = float(brain.get("accuracy_pct"))
        acc_power = _clamp((acc - 45.0) / 30.0, 0.10, 1.0)
    except Exception:
        acc_power = 0.35
    edge_power = _clamp(abs(edge) / 55.0, 0.0, 1.0)
    prob_power = _clamp((chosen_probability - 50.0) / 35.0, 0.0, 1.0)
    return _clamp(0.08 + learn * 0.30 + sample_power * 0.18 + acc_power * 0.14 + edge_power * 0.20 + prob_power * 0.10, 0.06, 1.0)



def _forecast_candle_confidence(
    index: int,
    horizon: int,
    chosen_probability: float,
    edge: float,
    strength: float,
    learning_confidence_pct: float,
    accuracy_pct: float,
    pattern_bias: float,
    direction_bias: float,
    samples: int,
) -> float:
    """Realistic confidence for each predicted candle.

    It is allowed to stay very high across many candles when the model truly has a strong,
    persistent pattern: high chosen_probability, strong edge, high learning confidence,
    decent historical accuracy and enough samples.
    """
    prob_term = _clamp((chosen_probability - 50.0) * 2.0, 0.0, 100.0)
    edge_term = _clamp(abs(edge) * 1.15, 0.0, 100.0)
    learn_term = _clamp(learning_confidence_pct, 0.0, 100.0)
    acc_term = _clamp(accuracy_pct if accuracy_pct > 0 else 52.0, 0.0, 100.0)
    sample_term = _clamp(math.log10(max(2, samples)) * 25.0, 0.0, 100.0)
    pattern_term = _clamp(abs(pattern_bias) * 140.0 + abs(direction_bias) * 18.0, 0.0, 100.0)

    base = (
        prob_term * 0.30
        + edge_term * 0.18
        + learn_term * 0.18
        + acc_term * 0.16
        + sample_term * 0.10
        + pattern_term * 0.08
    )

    # Strong model and clear pattern => much slower decay.
    persistence = _clamp(0.35 + strength * 0.45 + abs(pattern_bias) * 0.35 + (prob_term / 100.0) * 0.15, 0.0, 1.0)
    horizon_fraction = (index / max(1, horizon - 1))
    decay_floor = 0.52 + persistence * 0.43
    decay_curve = 1.0 - (horizon_fraction ** (1.05 + persistence * 0.9)) * (1.0 - decay_floor)
    conf = base * decay_curve

    # If the whole setup is elite, let confidence stay high even deep in the horizon.
    if chosen_probability >= 80.0 and learning_confidence_pct >= 70.0 and abs(pattern_bias) >= 0.15:
        conf = max(conf, min(96.0, base * (0.92 - horizon_fraction * 0.06)))

    return _clamp(conf, 18.0, 96.0)


def build_forecast(candles: list[dict], brain_snapshot: dict | None = None) -> list[dict]:
    if not candles:
        return []
    last_close = _safe_float(candles[-1].get("close"))
    if last_close <= 0:
        return []

    brain = brain_snapshot or {}
    regime = _market_regime(candles[-96:] if len(candles) >= 96 else candles)
    shape = _history_shape(candles)
    atr = max(regime["atr"], shape["avg_range"] * 0.85, last_close * 0.00045)
    avg_body = max(shape["avg_body"], atr * 0.18, last_close * 0.00018)

    buy_probability = _safe_float(brain.get("buy_probability"), 50.0)
    sell_probability = _safe_float(brain.get("sell_probability"), 50.0)
    edge = _safe_float(brain.get("probability_edge"), buy_probability - sell_probability)
    chosen_probability = max(buy_probability, sell_probability)
    side = "BUY" if buy_probability >= sell_probability else "SELL"
    direction = 1.0 if side == "BUY" else -1.0

    # Model-only gate: if the learned model gives no edge, future should look flat/choppy, not fake-trending.
    if chosen_probability < 52.0 and abs(edge) < 6.0:
        direction = 0.0

    learning_confidence_pct = _safe_float(brain.get("learning_confidence_pct"), 0.0)
    accuracy_pct = _safe_float(brain.get("accuracy_pct"), 0.0)
    samples = int(_safe_float(brain.get("ticker_samples") or brain.get("samples"), 0.0))
    horizon = FORECAST_CANDLE_COUNT
    direction_bias = _clamp(edge / 55.0, -1.0, 1.0)

    strength = _model_only_strength(brain, chosen_probability, edge)
    pattern_bias = _extract_pattern_bias(brain.get("patterns") or [], side)
    if direction == 0.0:
        pattern_bias *= 0.25
    else:
        # Patterns can shape/accelerate the model forecast, but they cannot flip the model side by themselves.
        pattern_bias = _clamp(pattern_bias * direction, -0.45, 0.45) * direction

    history_impulse = _clamp(shape.get("impulse", 0.0), -1.0, 1.0)
    # History is used as realism/inertia, not as an independent directional decision.
    inertia = history_impulse * 0.18 * (0.35 + strength)

    # 15 deterministic AI candles: impulse -> pullback -> continuation -> cooling.
    impulse_profile = [
        1.00, 0.82, 0.64, -0.28, 0.72,
        0.56, 0.42, -0.22, 0.46, 0.34,
        0.24, -0.16, 0.22, 0.14, 0.08,
    ]
    neutral_wave = [0.18, -0.12, 0.10, -0.16, 0.08, 0.06, -0.10, 0.12, -0.08, 0.05, -0.06, 0.04, -0.03, 0.03, -0.02]

    result = []
    prev_close = last_close

    backend = str(brain.get("model_backend") or brain.get("backend") or "").lower()
    has_torch_signal = brain.get("torch_probability") is not None or "torch" in backend or "cuda" in backend
    trained = bool(brain.get("trained", samples >= 80))
    learn_quality = _clamp(learning_confidence_pct / 100.0, 0.0, 1.0)
    sample_quality = _clamp(math.log10(max(2, samples)) / 3.0, 0.0, 1.0)
    acc_quality = _clamp((accuracy_pct - 45.0) / 35.0, 0.0, 1.0) if accuracy_pct > 0 else 0.28
    torch_bonus = 0.18 if has_torch_signal else 0.0
    learned_quality = _clamp((0.18 if trained else 0.0) + learn_quality * 0.32 + sample_quality * 0.18 + acc_quality * 0.20 + torch_bonus, 0.0, 1.0)

    # No trained/weak edge => do not invent a trend. Draw a quiet, history-shaped scenario.
    if learned_quality < 0.28 and chosen_probability < 62.0:
        direction = 0.0

    edge_norm = _clamp(edge / 100.0, -1.0, 1.0)
    model_bias = direction * _clamp((chosen_probability - 50.0) / 50.0, 0.0, 1.0) * learned_quality
    if direction == 0.0:
        model_bias = 0.0

    recent = candles[-48:] if len(candles) >= 48 else candles
    motif_rows = []
    for candle in recent[-30:]:
        o0 = _safe_float(candle.get("open"))
        h0 = _safe_float(candle.get("high"))
        l0 = _safe_float(candle.get("low"))
        c0 = _safe_float(candle.get("close"))
        rng0 = max(1e-9, h0 - l0)
        motif_rows.append({
            "body_norm": _clamp((c0 - o0) / max(atr, 1e-9), -1.25, 1.25),
            "range_norm": _clamp(rng0 / max(atr, 1e-9), 0.20, 2.20),
            "upper_ratio": _clamp((h0 - max(o0, c0)) / rng0, 0.05, 0.80),
            "lower_ratio": _clamp((min(o0, c0) - l0) / rng0, 0.05, 0.80),
        })
    if not motif_rows:
        motif_rows = [{"body_norm": 0.0, "range_norm": 0.8, "upper_ratio": 0.30, "lower_ratio": 0.30}]

    # Latest sequence is reused as market texture. The learned model only tilts it;
    # it no longer forces 15 same-direction fantasy candles.
    max_single_move = max(atr * (0.28 + 0.42 * learned_quality), last_close * (0.0010 + 0.0026 * learned_quality))
    min_body = max(last_close * 0.00006, atr * 0.035)
    overextension_limit = max(atr * (1.45 + learned_quality * 1.35), last_close * (0.004 + learned_quality * 0.009))

    for i in range(FORECAST_CANDLE_COUNT):
        open_price = prev_close
        motif = motif_rows[(len(motif_rows) - FORECAST_CANDLE_COUNT + i) % len(motif_rows)]
        horizon_fraction = i / max(1, FORECAST_CANDLE_COUNT - 1)

        candle_confidence = _forecast_candle_confidence(
            index=i,
            horizon=horizon,
            chosen_probability=chosen_probability,
            edge=edge,
            strength=strength * (0.55 + learned_quality * 0.45),
            learning_confidence_pct=learning_confidence_pct,
            accuracy_pct=accuracy_pct,
            pattern_bias=pattern_bias,
            direction_bias=direction_bias,
            samples=samples,
        )

        conf_norm = _clamp(candle_confidence / 100.0, 0.0, 1.0)
        motif_body = motif["body_norm"] * atr * (0.42 - learned_quality * 0.12)
        drift = model_bias * atr * (0.18 + 0.38 * conf_norm) * (1.0 - horizon_fraction * 0.30)
        pattern_drift = pattern_bias * atr * (0.10 + 0.12 * learned_quality) * (1.0 - horizon_fraction * 0.40)
        inertia_body = history_impulse * atr * 0.10 * (1.0 - horizon_fraction * 0.55)

        # Mean reversion guard: if forecast already travelled too far, force a pullback/chop candle.
        travelled = prev_close - last_close
        reversion = 0.0
        if abs(travelled) > overextension_limit:
            reversion = -math.copysign(atr * (0.24 + 0.18 * learned_quality), travelled)

        body = motif_body + drift + pattern_drift + inertia_body + reversion

        # Strong model can trend, but not as a painted straight line: every few bars,
        # history texture/pullback is allowed unless confidence is truly extreme.
        if direction != 0.0 and i in {3, 7, 11} and candle_confidence < 88.0:
            body -= direction * atr * (0.18 + 0.10 * (1.0 - learned_quality))

        if abs(body) < min_body:
            body = math.copysign(min_body, body if body != 0 else motif.get("body_norm", 0.01))

        body = _clamp(body, -max_single_move, max_single_move)
        close_price = max(last_close * 0.55, open_price + body)
        body_abs = abs(close_price - open_price)

        range_norm = motif.get("range_norm", 0.8)
        wick_base = max(atr * range_norm * (0.18 + 0.16 * (1.0 - learned_quality)), body_abs * 0.42, last_close * 0.00016)
        upper_ratio = _clamp(motif.get("upper_ratio", shape.get("upper_ratio", 0.28)), 0.08, 0.78)
        lower_ratio = _clamp(motif.get("lower_ratio", shape.get("lower_ratio", 0.28)), 0.08, 0.78)

        upper_wick = wick_base * (0.70 + upper_ratio)
        lower_wick = wick_base * (0.70 + lower_ratio)

        if direction > 0 and body >= 0:
            lower_wick *= 1.08 + learned_quality * 0.10
            upper_wick *= 0.94
        elif direction < 0 and body <= 0:
            upper_wick *= 1.08 + learned_quality * 0.10
            lower_wick *= 0.94

        # Counter-trend candles should have visible rejection wicks.
        if direction != 0.0 and body * direction < 0:
            upper_wick *= 1.16
            lower_wick *= 1.16

        high_price = max(open_price, close_price) + upper_wick
        low_price = max(last_close * 0.50, min(open_price, close_price) - lower_wick)

        result.append({
            "time": f"future+{i + 1}",
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": 0,
            "future": True,
            "model_only": True,
            "model_strength": strength,
            "model_edge": edge,
            "model_side": side,
            "model_probability": chosen_probability,
            "learning_quality": learned_quality,
            "torch_signal": has_torch_signal,
            "confidence_pct": candle_confidence,
        })
        prev_close = close_price

    return result

