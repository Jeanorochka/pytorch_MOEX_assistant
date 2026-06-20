import time
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import tkinter as tk
from tkinter import ttk, messagebox

from architecture import (
    BORDER,
    FG,
    MUTED_FG,
    GREEN,
    RED,
    YELLOW,
    BLUE,
    load_chart_candles_by_ticker,
    get_chart_live_price,
    get_chart_live_prices,
    load_state,
)


INTERVALS = {
    "1m": ("CANDLE_INTERVAL_1_MIN", 90, 60),
    "5m": ("CANDLE_INTERVAL_5_MIN", 420, 300),
    "15m": ("CANDLE_INTERVAL_15_MIN", 1440, 900),
    "1h": ("CANDLE_INTERVAL_HOUR", 4320, 3600),
    "1d": ("CANDLE_INTERVAL_DAY", 43200, 86400),
}

# REST/Tkinter cannot update literally every microsecond, but these values make the chart
# visually live: fast selected-book polling + batch prices + 60 FPS canvas redraw.
BATCH_PRICE_TICK_MS = 120
SELECTED_BOOK_TICK_MS = 90
DRAW_TICK_MS = 16
LEVEL_SYNC_MS = 250
FULL_SYNC_TICK_MS = 450
SELECTED_FULL_SYNC_SECONDS = 2.0
PASSIVE_FULL_SYNC_SECONDS = 6.0
ERROR_RETRY_SECONDS = 2.0
MAX_PARALLEL_CANDLE_LOADS = 2
MAX_VISIBLE_CANDLES = 120


class ChartTile(ttk.Frame):
    def __init__(self, parent, index: int, on_select):
        super().__init__(parent)
        self.index = index
        self.on_select = on_select
        self.ticker = ""
        self.interval_key = "1m"
        self.snapshot = None
        self.levels = {}
        self.error = ""
        self.selected = False
        self.loading_candles = False
        self.loading_price = False
        self.last_full_sync = 0.0
        self.last_price_sync = 0.0
        self.last_book_sync = 0.0
        self.last_ok_sync = ""
        self.seq = 0
        self.dirty = True
        self.zoom = 1.0
        self._last_draw = 0.0
        self.tool = "none"
        self.drawings: list[list[tuple[float, float]]] = []
        self._active_drawing: list[tuple[float, float]] | None = None
        self._last_draw_point: tuple[float, float] | None = None

        self.canvas = tk.Canvas(self, bg="#000000", highlightthickness=1, highlightbackground=BORDER, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._clicked)
        self.canvas.bind("<B1-Motion>", self._drag_draw)
        self.canvas.bind("<ButtonRelease-1>", self._finish_draw)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda _event: self.zoom_in())
        self.canvas.bind("<Button-5>", lambda _event: self.zoom_out())
        self.canvas.bind("<Configure>", lambda _event: self.mark_dirty())

    def _clicked(self, event=None):
        self.on_select(self.index)
        if self.tool == "pencil" and event is not None:
            self._start_pencil(event)
        elif self.tool == "eraser" and event is not None:
            self._erase_at(event.x, event.y)

    def set_tool(self, tool: str):
        self.tool = tool if tool in {"none", "pencil", "eraser"} else "none"
        self._active_drawing = None
        self._last_draw_point = None
        self.mark_dirty()

    def _norm_point(self, x: float, y: float) -> tuple[float, float]:
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        return (max(0.0, min(1.0, x / w)), max(0.0, min(1.0, y / h)))

    def _screen_point(self, point: tuple[float, float]) -> tuple[float, float]:
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        return (point[0] * w, point[1] * h)

    def _start_pencil(self, event):
        point = self._norm_point(event.x, event.y)
        self._active_drawing = [point]
        self._last_draw_point = point
        self.mark_dirty()

    def _drag_draw(self, event):
        if self.tool == "pencil":
            if self._active_drawing is None:
                self._start_pencil(event)
                return
            point = self._norm_point(event.x, event.y)
            if self._last_draw_point:
                lx, ly = self._screen_point(self._last_draw_point)
                if abs(event.x - lx) + abs(event.y - ly) < 3:
                    return
            self._active_drawing.append(point)
            self._last_draw_point = point
            self.mark_dirty()
        elif self.tool == "eraser":
            self._erase_at(event.x, event.y)

    def _finish_draw(self, _event=None):
        if self.tool == "pencil" and self._active_drawing:
            if len(self._active_drawing) >= 2:
                self.drawings.append(self._active_drawing)
            self._active_drawing = None
            self._last_draw_point = None
            self.mark_dirty()

    def _erase_at(self, x: float, y: float):
        if not self.drawings:
            return
        radius = 14.0
        radius_sq = radius * radius
        kept = []
        changed = False
        for stroke in self.drawings:
            erase = False
            for point in stroke:
                sx, sy = self._screen_point(point)
                if (sx - x) * (sx - x) + (sy - y) * (sy - y) <= radius_sq:
                    erase = True
                    break
            if erase:
                changed = True
            else:
                kept.append(stroke)
        if changed:
            self.drawings = kept
            self.mark_dirty()

    def _draw_drawings(self, canvas: tk.Canvas):
        strokes = list(self.drawings)
        if self._active_drawing:
            strokes.append(self._active_drawing)
        for stroke in strokes:
            if len(stroke) < 2:
                continue
            coords = []
            for point in stroke:
                sx, sy = self._screen_point(point)
                coords.extend([sx, sy])
            canvas.create_line(*coords, fill="#EDE7D0", width=2, smooth=True, capstyle="round", joinstyle="round")

    def _wheel(self, event):
        if getattr(event, "delta", 0) > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def zoom_in(self):
        self.zoom = min(8.0, self.zoom * 1.25)
        self.mark_dirty()

    def zoom_out(self):
        self.zoom = max(0.5, self.zoom / 1.25)
        self.mark_dirty()

    def zoom_reset(self):
        self.zoom = 1.0
        self.mark_dirty()

    def set_selected(self, selected: bool):
        self.selected = selected
        self.canvas.configure(highlightbackground=BLUE if selected else BORDER, highlightthickness=2 if selected else 1)
        self.mark_dirty()

    def mark_dirty(self):
        self.dirty = True

    def clear(self):
        self.ticker = ""
        self.interval_key = "1m"
        self.snapshot = None
        self.levels = {}
        self.drawings = []
        self._active_drawing = None
        self.error = ""
        self.loading_candles = False
        self.loading_price = False
        self.last_full_sync = 0.0
        self.last_price_sync = 0.0
        self.last_book_sync = 0.0
        self.last_ok_sync = ""
        self.seq += 1
        self.mark_dirty()
        self.redraw(force=True)

    def set_loading(self, ticker: str, interval_key: str):
        self.ticker = ticker.upper().strip()
        self.interval_key = interval_key or "1m"
        self.error = "Загрузка..."
        self.snapshot = None
        self.levels = {}
        self.drawings = []
        self._active_drawing = None
        self.loading_candles = True
        self.mark_dirty()
        self.redraw(force=True)

    def mark_silent_loading(self):
        self.loading_candles = True
        self.mark_dirty()

    def set_error(self, ticker: str, error: str, keep_existing: bool = False):
        self.ticker = ticker.upper().strip()
        self.error = str(error)
        self.loading_candles = False
        self.loading_price = False
        if not keep_existing:
            self.snapshot = None
            self.levels = {}
        self.mark_dirty()
        self.redraw(force=not keep_existing)

    def set_data(self, snapshot: dict, levels: dict):
        self.ticker = str(snapshot.get("ticker") or self.ticker).upper()
        self.snapshot = snapshot
        self.levels = levels or {}
        self.error = ""
        self.loading_candles = False
        self.loading_price = False
        self.last_full_sync = time.monotonic()
        self.last_ok_sync = time.strftime("%H:%M:%S")
        self.mark_dirty()
        self.redraw(force=True)

    def set_levels(self, levels: dict):
        self.levels = levels or {}
        self.mark_dirty()

    def interval_seconds(self) -> int:
        return int(INTERVALS.get(self.interval_key, INTERVALS["1m"])[2])

    def _bucket_from_epoch(self, epoch_seconds: float | None = None) -> int:
        seconds = max(1, self.interval_seconds())
        if epoch_seconds is None:
            epoch_seconds = time.time()
        return int(epoch_seconds // seconds)

    def _bucket_from_candle_time(self, value) -> int | None:
        if not value:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            raw = str(value)
            if raw.startswith("live:"):
                try:
                    return int(raw.split(":", 1)[1])
                except Exception:
                    return None
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return self._bucket_from_epoch(dt.timestamp())

    def _ensure_live_candle(self, price: Decimal, epoch_seconds: float | None = None) -> dict | None:
        if not self.snapshot or price is None or price <= 0:
            return None
        candles = self.snapshot.get("candles") or []
        if not candles:
            return None

        now_bucket = self._bucket_from_epoch(epoch_seconds)
        last = candles[-1]
        last_bucket = last.get("live_bucket")
        if last_bucket is None:
            last_bucket = self._bucket_from_candle_time(last.get("time"))

        if last_bucket is not None and now_bucket > int(last_bucket):
            previous_close = Decimal(str(last.get("close", price) or price))
            new_candle = {
                "time": f"live:{now_bucket}",
                "live_bucket": now_bucket,
                "open": previous_close,
                "high": max(previous_close, price),
                "low": min(previous_close, price),
                "close": price,
                "volume": 0,
            }
            candles.append(new_candle)
            if len(candles) > MAX_VISIBLE_CANDLES + 80:
                del candles[: len(candles) - (MAX_VISIBLE_CANDLES + 80)]
            return new_candle

        last["live_bucket"] = now_bucket if last_bucket is None else last_bucket
        return last

    def apply_live_price(self, price: Decimal, source: str = "last"):
        if not self.snapshot or price is None or price <= 0:
            return
        candle = self._ensure_live_candle(price)
        if not candle:
            return

        previous_close = Decimal(str(candle.get("close", price) or price))
        high = Decimal(str(candle.get("high", price) or price))
        low = Decimal(str(candle.get("low", price) or price))
        high = max(high, price, previous_close)
        low = min(low, price, previous_close)
        candle["close"] = price
        candle["high"] = high
        candle["low"] = low
        self.levels["current"] = price
        self.last_price_sync = time.monotonic()
        if source == "book":
            self.last_book_sync = self.last_price_sync
        self.last_ok_sync = time.strftime("%H:%M:%S")
        self.error = ""
        self.loading_price = False
        self.mark_dirty()

    def redraw(self, force: bool = False):
        now = time.monotonic()
        if not force and not self.dirty:
            return
        if not force and now - self._last_draw < 0.025:
            return
        self.dirty = False
        self._last_draw = now

        canvas = self.canvas
        canvas.delete("all")
        w = max(canvas.winfo_width(), 10)
        h = max(canvas.winfo_height(), 10)
        canvas.create_rectangle(0, 0, w, h, fill="#000000", outline="")

        if not self.snapshot:
            title = self.ticker or f"Chart {self.index + 1}"
            canvas.create_text(8, 8, anchor="nw", text=title, fill=FG, font=("Calibri", 9, "bold"))
            if self.error:
                canvas.create_text(8, 28, anchor="nw", text=self.error[:90], fill=MUTED_FG, font=("Calibri", 8), width=w - 16)
            else:
                canvas.create_text(8, 28, anchor="nw", text="Выбери тикер", fill=MUTED_FG, font=("Calibri", 8))
            return

        candles = self.snapshot.get("candles") or []
        if not candles:
            canvas.create_text(8, 8, anchor="nw", text=self.ticker, fill=FG, font=("Calibri", 9, "bold"))
            canvas.create_text(8, 28, anchor="nw", text="Нет свечей", fill=MUTED_FG, font=("Calibri", 8))
            return

        visible_count = int(MAX_VISIBLE_CANDLES / max(0.5, min(self.zoom, 8.0)))
        visible_count = max(18, min(len(candles), visible_count))
        candles = candles[-visible_count:]
        left, right, top, bottom = 8, 44, 22, 18
        x0, y0 = left, top
        x1, y1 = w - right, h - bottom
        if x1 <= x0 + 5 or y1 <= y0 + 5:
            return

        prices = []
        for candle in candles:
            prices.extend([float(candle["high"]), float(candle["low"]), float(candle["close"])])
        for key in ("avg", "sl", "tp", "current"):
            value = self.levels.get(key)
            if value is not None:
                try:
                    prices.append(float(value))
                except Exception:
                    pass

        p_min = min(prices)
        p_max = max(prices)
        if p_max == p_min:
            p_max += 1
            p_min -= 1
        pad = (p_max - p_min) * 0.08
        p_max += pad
        p_min -= pad

        def x_at(i: int) -> float:
            if len(candles) <= 1:
                return (x0 + x1) / 2
            return x0 + (x1 - x0) * i / (len(candles) - 1)

        def y_at(price) -> float:
            value = float(price)
            return y1 - (value - p_min) / (p_max - p_min) * (y1 - y0)

        for part in (0.25, 0.5, 0.75):
            gy = y0 + (y1 - y0) * part
            canvas.create_line(x0, gy, x1, gy, fill="#151515")

        step_px = max(1.0, (x1 - x0) / max(len(candles), 1))
        body_w = max(1, min(5, int(step_px * 0.55)))
        for i, candle in enumerate(candles):
            x = x_at(i)
            open_y = y_at(candle["open"])
            close_y = y_at(candle["close"])
            high_y = y_at(candle["high"])
            low_y = y_at(candle["low"])
            up = candle["close"] >= candle["open"]
            color = "#24B47E" if up else "#CF3F4B"
            canvas.create_line(x, high_y, x, low_y, fill=color)
            canvas.create_rectangle(
                x - body_w / 2,
                min(open_y, close_y),
                x + body_w / 2,
                max(open_y, close_y) + 1,
                outline=color,
                fill=color,
            )

        # No oscillator/close-line overlay: candles only, plus SL/TP/AVG/NOW levels.

        def draw_level(key: str, label: str, color: str, dash=None):
            value = self.levels.get(key)
            if value is None:
                return
            try:
                y = y_at(value)
            except Exception:
                return
            canvas.create_line(x0, y, x1, y, fill=color, width=1, dash=dash)
            canvas.create_text(x1 + 4, y, anchor="w", text=label, fill=color, font=("Calibri", 7, "bold"))

        draw_level("sl", "SL", RED, (3, 2))
        draw_level("tp", "TP", GREEN, (3, 2))
        draw_level("avg", "AVG", YELLOW, (2, 2))
        draw_level("current", "NOW", BLUE, None)
        self._draw_drawings(canvas)

        last = candles[-1]["close"]
        canvas.create_text(8, 7, anchor="nw", text=f"{self.ticker} {self.interval_key}  x{self.zoom:.1f}", fill=FG, font=("Calibri", 9, "bold"))
        canvas.create_text(w - 8, 7, anchor="ne", text=str(last), fill=BLUE, font=("Calibri", 8, "bold"))

        if self.loading_candles or self.loading_price:
            canvas.create_oval(w - 18, h - 15, w - 10, h - 7, outline=BLUE, fill=BLUE)
        elif self.last_ok_sync:
            canvas.create_text(8, h - 9, anchor="sw", text=self.last_ok_sync, fill="#444444", font=("Calibri", 7))
        if self.error and self.snapshot:
            canvas.create_text(w - 8, h - 9, anchor="se", text="sync err", fill=RED, font=("Calibri", 7, "bold"))


class NineChartsPanel(ttk.Frame):
    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app = app_context
        self.selected_tile = 0
        self.ticker_var = tk.StringVar()
        self.interval_var = tk.StringVar(value="1m")
        self.status_var = tk.StringVar(value="Выбери тикер и нажми Открыть")
        self.paused = False
        self.active_tool = "none"
        self.tiles: list[ChartTile] = []
        self._batch_price_pending = False
        self._selected_book_pending = False
        self._candle_pending = 0
        self._full_sync_cursor = 0
        self._build()
        self.after(250, self._batch_price_tick)
        self.after(280, self._selected_book_tick)
        self.after(120, self._draw_tick)
        self.after(600, self._levels_tick)
        self.after(1000, self._full_sync_tick)

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="Тикер").pack(side="left", padx=(0, 6))
        entry = ttk.Entry(top, textvariable=self.ticker_var, width=18)
        entry.pack(side="left", padx=(0, 8))
        entry.bind("<Return>", lambda _e: self.open_chart())

        ttk.Label(top, text="TF").pack(side="left", padx=(0, 6))
        interval = ttk.Combobox(top, textvariable=self.interval_var, state="readonly", width=7, values=list(INTERVALS.keys()))
        interval.pack(side="left", padx=(0, 8))

        ttk.Button(top, text="Открыть", command=self.open_chart).pack(side="left", padx=4)
        self.pause_button = ttk.Button(top, text="Пауза", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=4)
        ttk.Button(top, text="Синхр", command=self.refresh_visible).pack(side="left", padx=4)
        ttk.Button(top, text="Очистить", command=self.clear_selected).pack(side="left", padx=4)
        ttk.Label(top, textvariable=self.status_var, foreground=MUTED_FG).pack(side="left", padx=(14, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        tools = ttk.Frame(body)
        tools.pack(side="left", fill="y", padx=(0, 8))
        self.pencil_button = ttk.Button(tools, text="Карандаш", command=lambda: self.set_tool("pencil"))
        self.pencil_button.pack(fill="x", pady=(0, 6))
        self.eraser_button = ttk.Button(tools, text="Ластик", command=lambda: self.set_tool("eraser"))
        self.eraser_button.pack(fill="x", pady=(0, 6))

        grid = ttk.Frame(body)
        grid.pack(side="left", fill="both", expand=True)
        grid.grid_rowconfigure(0, weight=1)
        grid.grid_columnconfigure(0, weight=1)

        tile = ChartTile(grid, 0, self.select_tile)
        tile.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.tiles.append(tile)
        self.select_tile(0)
        self._refresh_tool_buttons()


    def current_tile(self):
        if not self.tiles:
            return None
        index = max(0, min(self.selected_tile, len(self.tiles) - 1))
        return self.tiles[index]

    def toggle_pause(self):
        self.paused = not self.paused
        if hasattr(self, "pause_button"):
            self.pause_button.configure(text="Продолжить" if self.paused else "Пауза")
        self.status_var.set("График на паузе" if self.paused else "Обновления графика включены")
        for tile in self.tiles:
            tile.loading_price = False
            tile.loading_candles = False if self.paused else tile.loading_candles
            tile.mark_dirty()

    def set_tool(self, tool: str):
        if self.active_tool == tool:
            tool = "none"
        self.active_tool = tool if tool in {"none", "pencil", "eraser"} else "none"
        for tile in self.tiles:
            tile.set_tool(self.active_tool)
        self._refresh_tool_buttons()

    def _refresh_tool_buttons(self):
        active_style = "RiskActive.TButton"
        inactive_style = "RiskInactive.TButton"
        if hasattr(self, "pencil_button"):
            self.pencil_button.configure(style=active_style if self.active_tool == "pencil" else inactive_style)
        if hasattr(self, "eraser_button"):
            self.eraser_button.configure(style=active_style if self.active_tool == "eraser" else inactive_style)

    def zoom_in(self):
        tile = self.current_tile()
        if tile:
            tile.zoom_in()

    def zoom_out(self):
        tile = self.current_tile()
        if tile:
            tile.zoom_out()

    def zoom_reset(self):
        tile = self.current_tile()
        if tile:
            tile.zoom_reset()

    def select_tile(self, index: int):
        self.selected_tile = index
        for i, tile in enumerate(self.tiles):
            tile.set_selected(i == index)

    def clear_selected(self):
        tile = self.current_tile()
        if tile:
            tile.clear()

    def open_chart(self):
        ticker = self.ticker_var.get().strip().upper()
        if not ticker:
            messagebox.showwarning("График", "Введи тикер.")
            return
        target = self.selected_tile
        interval_key = self.interval_var.get() or "1m"
        self._load_tile(target, ticker, interval_key=interval_key, silent=False, force=True)

    def refresh_visible(self):
        for index, tile in enumerate(self.tiles):
            if tile.ticker:
                self._load_tile(index, tile.ticker, interval_key=tile.interval_key, silent=True, force=True)
        self.status_var.set("Принудительная синхронизация свечей запущена")

    def _load_tile(self, index: int, ticker: str, interval_key: str | None = None, silent: bool = False, force: bool = False):
        if index < 0 or index >= len(self.tiles):
            return
        tile = self.tiles[index]
        if tile.loading_candles and not force:
            return
        if self._candle_pending >= MAX_PARALLEL_CANDLE_LOADS and not force:
            return

        interval_key = interval_key or tile.interval_key or self.interval_var.get() or "1m"
        interval, minutes, _seconds = INTERVALS.get(interval_key, INTERVALS["1m"])
        tile.seq += 1
        seq = tile.seq
        tile.ticker = ticker.upper().strip()
        tile.interval_key = interval_key
        tile.loading_candles = True
        tile.last_full_sync = time.monotonic()
        self._candle_pending += 1

        if not silent:
            tile.set_loading(ticker, interval_key)
            self.status_var.set(f"Загружаю {ticker}...")
        else:
            tile.mark_silent_loading()

        def run():
            try:
                snapshot = load_chart_candles_by_ticker(ticker, interval, minutes)
                levels = self._levels_for_ticker(snapshot.get("ticker") or ticker, snapshot)
                self.after(0, lambda: self._loaded(index, seq, snapshot, levels, None, silent))
            except Exception as exc:
                self.after(0, lambda e=exc: self._loaded(index, seq, {"ticker": ticker}, {}, e, silent))

        threading.Thread(target=run, daemon=True).start()

    def _loaded(self, index: int, seq: int, snapshot: dict, levels: dict, error, silent: bool):
        self._candle_pending = max(0, self._candle_pending - 1)
        if index < 0 or index >= len(self.tiles):
            return
        tile = self.tiles[index]
        if seq != tile.seq:
            return
        if error:
            tile.set_error(snapshot.get("ticker", tile.ticker), error, keep_existing=silent)
            if not silent:
                self.status_var.set(f"Ошибка графика: {error}")
            return
        old_current = tile.levels.get("current") if tile.levels else None
        tile.set_data(snapshot, levels)
        if old_current and old_current > 0:
            tile.apply_live_price(old_current)
        if not silent:
            self.status_var.set(f"График открыт: {snapshot.get('ticker')}")

    def _active_tiles(self):
        return [tile for tile in self.tiles if tile.ticker and tile.snapshot and tile.snapshot.get("instrument_id")]

    def _batch_price_tick(self):
        try:
            if self.paused:
                return
            if self._batch_price_pending:
                return
            tiles = self._active_tiles()
            if not tiles:
                return
            instrument_ids = []
            for tile in tiles:
                instrument_id = tile.snapshot.get("instrument_id")
                if instrument_id and instrument_id not in instrument_ids:
                    instrument_ids.append(instrument_id)
            if not instrument_ids:
                return
            self._batch_price_pending = True
            for tile in tiles:
                tile.loading_price = True
                tile.mark_dirty()

            def run():
                try:
                    prices = get_chart_live_prices(instrument_ids)
                    self.after(0, lambda: self._batch_prices_loaded(prices, None))
                except Exception as exc:
                    self.after(0, lambda e=exc: self._batch_prices_loaded({}, e))

            threading.Thread(target=run, daemon=True).start()
        finally:
            if self.winfo_exists():
                self.after(BATCH_PRICE_TICK_MS, self._batch_price_tick)

    def _batch_prices_loaded(self, prices: dict[str, Decimal], error):
        self._batch_price_pending = False
        if self.paused:
            return
        if error:
            for tile in self._active_tiles():
                tile.loading_price = False
                tile.error = str(error)
                tile.mark_dirty()
            return

        updated = 0
        now = time.monotonic()
        for tile in self._active_tiles():
            tile.loading_price = False
            instrument_id = tile.snapshot.get("instrument_id")
            price = prices.get(instrument_id)
            if price is not None and price > 0:
                tile.apply_live_price(price, source="last")
                tile.last_price_sync = now
                updated += 1
        loaded_count = sum(1 for tile in self.tiles if tile.ticker)
        if loaded_count and not self.paused:
            self.status_var.set(f"Live: tick {updated}/{loaded_count} | candles {self._candle_pending}")

    def _selected_book_tick(self):
        try:
            if self.paused:
                return
            if self._selected_book_pending:
                return
            if self.selected_tile < 0 or self.selected_tile >= len(self.tiles):
                return
            tile = self.tiles[self.selected_tile]
            if not tile.ticker or not tile.snapshot:
                return
            instrument_id = tile.snapshot.get("instrument_id")
            if not instrument_id:
                return
            self._selected_book_pending = True

            def run():
                try:
                    price = get_chart_live_price(instrument_id)
                    self.after(0, lambda: self._selected_book_loaded(self.selected_tile, price, None))
                except Exception as exc:
                    self.after(0, lambda e=exc: self._selected_book_loaded(self.selected_tile, Decimal("0"), e))

            threading.Thread(target=run, daemon=True).start()
        finally:
            if self.winfo_exists():
                self.after(SELECTED_BOOK_TICK_MS, self._selected_book_tick)

    def _selected_book_loaded(self, index: int, price: Decimal, error):
        self._selected_book_pending = False
        if self.paused:
            return
        if index < 0 or index >= len(self.tiles):
            return
        tile = self.tiles[index]
        if error:
            tile.error = str(error)
            tile.mark_dirty()
            return
        if price and price > 0:
            tile.apply_live_price(price, source="book")

    def _draw_tick(self):
        try:
            for tile in self.tiles:
                tile.redraw(force=False)
        finally:
            if self.winfo_exists():
                self.after(DRAW_TICK_MS, self._draw_tick)

    def _levels_tick(self):
        try:
            if self.paused:
                return
            for tile in self.tiles:
                if not tile.ticker or not tile.snapshot:
                    continue
                levels = self._levels_for_ticker(tile.ticker, tile.snapshot)
                current = tile.levels.get("current")
                if current is not None:
                    levels["current"] = current
                tile.set_levels(levels)
        finally:
            if self.winfo_exists():
                self.after(LEVEL_SYNC_MS, self._levels_tick)

    def _full_sync_tick(self):
        try:
            if self.paused:
                return
            now = time.monotonic()
            active_indices = [i for i, tile in enumerate(self.tiles) if tile.ticker]
            if active_indices and self._candle_pending < MAX_PARALLEL_CANDLE_LOADS:
                if self._full_sync_cursor >= len(active_indices):
                    self._full_sync_cursor = 0
                ordered = active_indices[self._full_sync_cursor:] + active_indices[:self._full_sync_cursor]
                for index in ordered:
                    tile = self.tiles[index]
                    if tile.loading_candles:
                        continue
                    interval_seconds = SELECTED_FULL_SYNC_SECONDS if index == self.selected_tile else PASSIVE_FULL_SYNC_SECONDS
                    if tile.error and not tile.snapshot:
                        interval_seconds = ERROR_RETRY_SECONDS
                    if now - tile.last_full_sync >= interval_seconds:
                        self._full_sync_cursor = (active_indices.index(index) + 1) % max(1, len(active_indices))
                        self._load_tile(index, tile.ticker, interval_key=tile.interval_key, silent=True, force=False)
                        break
        finally:
            if self.winfo_exists():
                self.after(FULL_SYNC_TICK_MS, self._full_sync_tick)

    def _levels_for_ticker(self, ticker: str, snapshot: dict) -> dict:
        ticker_upper = str(ticker or "").upper()
        selected_ids = set(self.app.get_selected_account_ids()) if hasattr(self.app, "get_selected_account_ids") else set()
        trades = []
        try:
            state = load_state()
            for trade in state.get("active_trades", []):
                if selected_ids and trade.get("account_id") not in selected_ids:
                    continue
                if str(trade.get("ticker", "")).upper() == ticker_upper:
                    trades.append(trade)
        except Exception:
            trades = []

        levels = {}
        candles = snapshot.get("candles") or []
        if candles:
            levels["current"] = candles[-1]["close"]

        total_qty = Decimal("0")
        total_entry = Decimal("0")
        sl_values = []
        tp_values = []
        for trade in trades:
            try:
                qty = Decimal(str(trade.get("qty", "0") or "0"))
                entry = Decimal(str(trade.get("entry_price", "0") or "0"))
                if qty > 0 and entry > 0:
                    total_qty += qty
                    total_entry += qty * entry
            except Exception:
                pass

            for source_key, target in (("sl_price", sl_values), ("tp_price", tp_values)):
                raw = str(trade.get(source_key, "") or "").strip()
                if not raw or raw == "—":
                    continue
                try:
                    target.append(Decimal(raw.replace(",", ".")))
                except (InvalidOperation, ValueError):
                    pass

        if total_qty > 0:
            levels["avg"] = total_entry / total_qty
        if sl_values:
            levels["sl"] = sl_values[-1]
        if tp_values:
            levels["tp"] = tp_values[-1]
        return levels
