import sys
import time
import uuid
import math
import threading
import re
import ctypes
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from architecture import *
from chart_panel import NineChartsPanel
from engine.predi import PrediPanel
from trade_journal import TradeJournalPanel, record_terminal_trade_result
from engine.mathmodel_graph import MathModelGraphPanel

try:
    from engine.predi_brain import recommend_trade_levels
except Exception:
    recommend_trade_levels = None

try:
    from engine.moex_db_manual import add_loved_tickers, run_loved_background_once
except Exception:
    add_loved_tickers = None
    run_loved_background_once = None


# Entry execution slicing.
# Market entries are split into child orders to reduce one-shot footprint.
ENTRY_SPLIT_PREFERRED_COUNTS = (15, 10, 5, 1)
ENTRY_CHILD_ORDER_DELAY_SECONDS = 0.001

# Main order-form risk/SL/TP preview is recalculated in the background once per minute.
TRADE_PREVIEW_AUTO_REFRESH_MS = 60_000

# Broker portfolio/open-position tables are refreshed frequently so the main screen stays close to real-time.
PORTFOLIO_AUTO_REFRESH_MS = 5_000

# Trade instrument metadata can be slow for futures (for example IMOEXF).
# Cache resolved instrument/full/step/point-value locally for the order form.
TRADE_INSTRUMENT_CONTEXT_TTL_SECONDS = 900

# Conservative intraday risk cap used for auto SL and recommended contracts.
INTRADAY_RISK_PERCENT_CAP = Decimal("0.20")


# ---------------------------- GUI styling ----------------------------

def set_windows_app_user_model_id() -> None:
    try:
        if sys.platform.startswith("win"):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Kaiyah.JTrade.Dark.4")
    except Exception:
        pass


def find_app_icon() -> Path | None:
    asset_dirs = [APP_DIR / "assets", APP_DIR / "assests", APP_DIR]
    names = ["app_icon.ico", "icon.ico", "jeatrade.ico", "app_icon.png", "icon.png", "jeatrade.png"]
    candidates = [folder / name for folder in asset_dirs for name in names]
    return next((p for p in candidates if p.exists()), None)


def apply_window_icon(root: tk.Tk) -> None:
    icon_path = find_app_icon()
    if not icon_path:
        return
    try:
        if icon_path.suffix.lower() == ".ico":
            root.iconbitmap(default=str(icon_path))
        if icon_path.suffix.lower() in {".png", ".gif"}:
            img = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, img)
            root._app_icon_photo = img
    except Exception:
        pass


def remove_titlebar_text(root: tk.Tk) -> None:
    try:
        root.title("")
    except Exception:
        pass


def apply_theme(root: tk.Tk) -> None:
    root.configure(bg=BG)
    root.option_add("*Font", f"{FONT_FAMILY} 10")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", font=(FONT_FAMILY, 10), background=BG, foreground=FG)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=PANEL_BG, relief="solid", borderwidth=1)
    style.configure("Header.TFrame", background=HEADER_BG)
    style.configure("Panel.TFrame", background=PANEL_BG)

    style.configure("TLabel", background=BG, foreground=FG, font=(FONT_FAMILY, 10))
    style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT_FAMILY, 20, "bold"))
    style.configure("CardTitle.TLabel", background=PANEL_BG, foreground=MUTED_FG, font=(FONT_FAMILY, 9, "bold"))
    style.configure("CardValue.TLabel", background=PANEL_BG, foreground=FG, font=(FONT_FAMILY, 15, "bold"))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED_FG, font=(FONT_FAMILY, 9))
    style.configure("PanelMuted.TLabel", background=PANEL_BG, foreground=MUTED_FG, font=(FONT_FAMILY, 9))
    style.configure("PreviewMuted.TLabel", background=PANEL_BG, foreground=MUTED_FG, font=(FONT_FAMILY, 9))
    style.configure("PreviewWhite.TLabel", background=PANEL_BG, foreground=FG, font=(FONT_FAMILY, 10, "bold"))
    style.configure("PreviewLong.TLabel", background=PANEL_BG, foreground=GREEN, font=(FONT_FAMILY, 9, "bold"))
    style.configure("PreviewShort.TLabel", background=PANEL_BG, foreground=RED, font=(FONT_FAMILY, 9, "bold"))
    style.configure("PreviewRisk.TLabel", background=PANEL_BG, foreground=RED, font=(FONT_FAMILY, 10, "bold"))
    style.configure("PreviewReward.TLabel", background=PANEL_BG, foreground=GREEN, font=(FONT_FAMILY, 10, "bold"))
    style.configure("Bold.TLabel", background=BG, foreground=FG, font=(FONT_FAMILY, 11, "bold"))
    style.configure("Green.TLabel", background=PANEL_BG, foreground=GREEN, font=(FONT_FAMILY, 15, "bold"))
    style.configure("Red.TLabel", background=PANEL_BG, foreground=RED, font=(FONT_FAMILY, 15, "bold"))

    style.configure(
        "TLabelframe",
        background=BG,
        foreground=FG,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        relief="solid",
    )
    style.configure("TLabelframe.Label", background=BG, foreground=FG, font=(FONT_FAMILY, 11, "bold"))

    style.configure(
        "TEntry",
        fieldbackground=INPUT_BG,
        foreground=INPUT_FG,
        insertcolor=INPUT_FG,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=5,
    )
    style.configure(
        "TCombobox",
        fieldbackground=INPUT_BG,
        foreground=INPUT_FG,
        background=INPUT_BG,
        arrowcolor=INPUT_FG,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=5,
    )
    style.map("TCombobox", fieldbackground=[("readonly", INPUT_BG)], foreground=[("readonly", INPUT_FG)])

    style.configure(
        "TButton",
        background=BUTTON_BG,
        foreground=FG,
        font=(FONT_FAMILY, 10, "bold"),
        borderwidth=1,
        relief="solid",
        padding=(11, 7),
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
    )
    style.map(
        "TButton",
        background=[("active", BUTTON_ACTIVE), ("pressed", BUTTON_ACTIVE)],
        foreground=[("active", FG), ("pressed", FG)],
    )

    style.configure(
        "RiskInactive.TButton",
        background=BUTTON_BG,
        foreground=FG,
        font=(FONT_FAMILY, 9, "bold"),
        borderwidth=1,
        relief="solid",
        padding=(8, 5),
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
    )
    style.map(
        "RiskInactive.TButton",
        background=[("active", BUTTON_ACTIVE), ("pressed", BUTTON_ACTIVE)],
        foreground=[("active", FG), ("pressed", FG)],
    )

    style.configure(
        "RiskActive.TButton",
        background=BLUE,
        foreground="#FFFFFF",
        font=(FONT_FAMILY, 9, "bold"),
        borderwidth=1,
        relief="solid",
        padding=(8, 5),
        bordercolor=BLUE,
        lightcolor=BLUE,
        darkcolor=BLUE,
    )
    style.map(
        "RiskActive.TButton",
        background=[("active", BLUE), ("pressed", BLUE)],
        foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")],
    )

    style.configure(
        "Buy.TButton",
        background=BUY_BUTTON_BG,
        foreground="#FFFFFF",
        font=(FONT_FAMILY, 10, "bold"),
        borderwidth=0,
        relief="flat",
        padding=(11, 7),
    )
    style.map(
        "Buy.TButton",
        background=[("active", BUY_BUTTON_ACTIVE), ("pressed", BUY_BUTTON_ACTIVE)],
        foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")],
    )

    style.configure(
        "Sell.TButton",
        background=SELL_BUTTON_BG,
        foreground="#FFFFFF",
        font=(FONT_FAMILY, 10, "bold"),
        borderwidth=0,
        relief="flat",
        padding=(11, 7),
    )
    style.map(
        "Sell.TButton",
        background=[("active", SELL_BUTTON_ACTIVE), ("pressed", SELL_BUTTON_ACTIVE)],
        foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")],
    )

    style.configure(
        "MoexDB.TButton",
        background="#B91C1C",
        foreground="#FFFFFF",
        font=(FONT_FAMILY, 10, "bold"),
        borderwidth=0,
        relief="flat",
        padding=(11, 7),
    )
    style.map(
        "MoexDB.TButton",
        background=[("active", "#DC2626"), ("pressed", "#991B1B")],
        foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")],
    )

    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=BUTTON_BG, foreground=MUTED_FG, padding=(16, 8), font=(FONT_FAMILY, 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", PANEL_BG_2)], foreground=[("selected", FG)])

    style.configure(
        "Treeview",
        background=PANEL_BG_2,
        foreground=FG,
        fieldbackground=PANEL_BG_2,
        rowheight=25,
        borderwidth=0,
        font=(FONT_FAMILY, 10),
    )
    style.configure(
        "Treeview.Heading",
        background=HEADER_BG,
        foreground=FG,
        relief="flat",
        font=(FONT_FAMILY, 10, "bold"),
    )
    style.map("Treeview", background=[("selected", TREE_SELECTED)], foreground=[("selected", FG)])
    style.map("Treeview.Heading", background=[("active", HEADER_BG)])

    style.configure("Vertical.TScrollbar", background=BUTTON_BG, troughcolor=BG, bordercolor=BORDER, arrowcolor=FG)
    style.configure("Horizontal.TScrollbar", background=BUTTON_BG, troughcolor=BG, bordercolor=BORDER, arrowcolor=FG)


def style_textbox(widget: ScrolledText) -> None:
    widget.configure(
        bg=PANEL_BG_2,
        fg=FG,
        insertbackground=FG,
        selectbackground=TREE_SELECTED,
        selectforeground=FG,
        font=(FONT_FAMILY, 9),
        relief="flat",
        borderwidth=0,
        padx=10,
        pady=8,
    )


def make_button(parent, text: str, command, width: int | None = None, style: str | None = None):
    btn = ttk.Button(parent, text=text, command=command, style=style or "TButton")
    if width:
        btn.configure(width=width)
    return btn


def make_tree(parent, columns: list[tuple[str, str, int, str]], height: int = 10) -> tuple[ttk.Treeview, ttk.Frame]:
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height)
    yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    xscroll = ttk.Scrollbar(frame, orient="horizontal")

    separator_lines: list[tk.Frame] = []

    def redraw_column_separators(event=None):
        if not tree.winfo_exists():
            return
        if not separator_lines:
            return

        total_width = sum(int(tree.column(c[0], "width")) for c in columns)
        visible_width = max(1, tree.winfo_width())
        first_x = float(tree.xview()[0]) if tree.xview() else 0.0
        x_offset = int(first_x * total_width) if total_width > visible_width else 0

        x = 0
        height_px = max(1, tree.winfo_height())
        for idx, (key, _title, _width, _anchor) in enumerate(columns[:-1]):
            x += int(tree.column(key, "width"))
            visible_x = x - x_offset
            line = separator_lines[idx]
            if 0 < visible_x < visible_width:
                line.place(x=visible_x, y=0, width=1, height=height_px)
                line.lift()
            else:
                line.place_forget()

    def on_tree_xscroll(first, last):
        xscroll.set(first, last)
        tree.after_idle(redraw_column_separators)

    def on_horizontal_scroll(*args):
        tree.xview(*args)
        tree.after_idle(redraw_column_separators)

    xscroll.configure(command=on_horizontal_scroll)
    tree.configure(yscrollcommand=yscroll.set, xscrollcommand=on_tree_xscroll)

    for key, title, width, anchor in columns:
        tree.heading(key, text=title)
        tree.column(key, width=width, minwidth=60, anchor=anchor, stretch=True)

    for _ in range(max(0, len(columns) - 1)):
        separator_lines.append(tk.Frame(tree, bg=BORDER, width=1, height=1))

    tree.tag_configure("positive", foreground=GREEN)
    tree.tag_configure("negative", foreground=RED)
    tree.tag_configure("muted", foreground=MUTED_FG)
    tree.tag_configure("warning", foreground=YELLOW)

    tree.bind("<Configure>", redraw_column_separators, add="+")
    tree.bind("<ButtonRelease-1>", redraw_column_separators, add="+")
    tree.bind("<B1-Motion>", redraw_column_separators, add="+")
    tree.bind("<<TreeviewOpen>>", redraw_column_separators, add="+")
    tree.after(150, redraw_column_separators)

    tree.grid(row=0, column=0, sticky="nsew")
    yscroll.grid(row=0, column=1, sticky="ns")
    xscroll.grid(row=1, column=0, sticky="ew")
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)
    return tree, frame



def _decimal_from_possible_quotation(value, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        if isinstance(value, dict):
            # T-Invest Quotation/MoneyValue style: {"units": "...", "nano": ...}
            if "units" in value or "nano" in value:
                units = Decimal(str(value.get("units", "0") or "0"))
                nano = Decimal(str(value.get("nano", 0) or 0)) / Decimal("1000000000")
                result = units + nano
                return result if result > 0 else default
            # Sometimes price can be wrapped deeper.
            for nested_key in ("price", "value", "amount"):
                nested = value.get(nested_key)
                nested_result = _decimal_from_possible_quotation(nested, default=None)
                if nested_result is not None and nested_result > 0:
                    return nested_result
            return default
        result = Decimal(str(value).replace(",", "."))
        return result if result > 0 else default
    except Exception:
        return default


def _walk_first_price_by_keys(node, wanted_keys: set[str]) -> Decimal | None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_norm = str(key).lower()
            if key_norm in wanted_keys:
                result = _decimal_from_possible_quotation(value, default=None)
                if result is not None and result > 0:
                    return result
        for value in node.values():
            result = _walk_first_price_by_keys(value, wanted_keys)
            if result is not None and result > 0:
                return result
    elif isinstance(node, list):
        for item in node:
            result = _walk_first_price_by_keys(item, wanted_keys)
            if result is not None and result > 0:
                return result
    return None


def extract_execution_price_from_state(order_state: dict, fallback_price) -> Decimal:
    """Best-effort average fill price extraction from T-Invest order state.

    If the API response shape changes or the state has no usable execution price,
    this function returns the planned entry price instead of breaking SL/TP placement.
    """
    fallback = _decimal_from_possible_quotation(fallback_price, default=Decimal("0")) or Decimal("0")
    if not isinstance(order_state, dict) or not order_state:
        return fallback

    # Prefer explicit executed/average fields.
    wanted = {
        "averagepositionprice",
        "average_position_price",
        "executedorderprice",
        "executed_order_price",
        "averageorderprice",
        "average_order_price",
        "avgprice",
        "avg_price",
        "priceaverage",
        "price_average",
    }
    direct = _walk_first_price_by_keys(order_state, wanted)
    if direct is not None and direct > 0:
        return direct

    # Then try weighted average from execution stages/trades.
    stages = order_state.get("stages") or order_state.get("orderStages") or order_state.get("order_stages") or []
    weighted_sum = Decimal("0")
    total_qty = Decimal("0")
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            price = (
                _decimal_from_possible_quotation(stage.get("price"), default=None)
                or _decimal_from_possible_quotation(stage.get("executionPrice"), default=None)
                or _decimal_from_possible_quotation(stage.get("execution_price"), default=None)
            )
            qty_raw = (
                stage.get("quantity")
                or stage.get("lots")
                or stage.get("lotsExecuted")
                or stage.get("lots_executed")
                or 1
            )
            try:
                qty = Decimal(str(qty_raw))
            except Exception:
                qty = Decimal("1")
            if price is not None and price > 0 and qty > 0:
                weighted_sum += price * qty
                total_qty += qty
    if total_qty > 0:
        return weighted_sum / total_qty

    # Last safe fallback: planned entry price. This prevents an opened position
    # from being left without protection because of a non-critical parsing field.
    return fallback


# ---------------------------- App ----------------------------

class JTradeDarkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        apply_theme(root)
        remove_titlebar_text(root)
        self.root.geometry("1380x800")
        self.root.minsize(1160, 680)

        self.accounts: list[dict] = []
        self.account_id: str | None = None
        self.account_label = ""
        self.selected_account_ids: list[str] = []
        self.account_labels_by_id: dict[str, str] = {}
        self.add_account_var = tk.StringVar()
        self.remove_account_var = tk.StringVar()
        self.risk_model_var = tk.StringVar(value="position")
        self.selected_accounts_var = tk.StringVar(value="Счета: —")

        self.oco_started = False
        self.operations_stream = None
        self._operations_stream_refresh_after_id = None
        self._operations_stream_status_seen: set[str] = set()
        self.charts_window = None
        self.charts_panel = None
        self._charts_window_built = False
        self.math_window = None
        self.math_panel = None
        self._math_window_built = False
        self.mathmodel_window = None
        self.mathmodel_panel = None
        self.instrument_cache: dict[str, dict] = {}
        self.trade_instrument_context_cache: dict[str, tuple[float, dict]] = {}
        self.portfolio_rows: dict[str, dict] = {}
        self.open_positions_rows: dict[str, dict] = {}
        self.active_trade_rows: dict[str, dict] = {}
        self.stop_order_rows: dict[str, dict] = {}

        self.total_var = tk.StringVar(value="—")
        self.expected_var = tk.StringVar(value="—")
        self.cash_var = tk.StringVar(value="—")
        self.blocked_var = tk.StringVar(value="—")
        self.var_margin_var = tk.StringVar(value="—")
        self.risk_var = tk.StringVar(value="—")
        self.account_var = tk.StringVar()
        self._refresh_all_seq = 0
        self._last_margin_base_skipped: list[str] = []
        self._trade_context_after_id = None
        self._trade_preview_periodic_after_id = None
        self._portfolio_periodic_after_id = None
        self._refresh_all_running = False
        self._loved_moex_bg_after_id = None
        self._loved_moex_bg_running = False

        self.build_account_window()
        self.load_accounts_async()

    def run_async(self, target, on_done=None):
        def wrapper():
            try:
                result = target()
                if on_done:
                    self.root.after(0, lambda: on_done(result, None))
            except Exception as exc:
                if on_done:
                    self.root.after(0, lambda e=exc: on_done(None, e))
                else:
                    self.root.after(0, lambda e=exc: messagebox.showerror("Ошибка", str(e)))
        threading.Thread(target=wrapper, daemon=True).start()

    def log(self, text: str):
        if not hasattr(self, "log_box"):
            print(text)
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def bring_main_to_front(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            # Short topmost pulse: enough to put main above old tool windows,
            # but it will not stay permanently pinned over everything.
            self.root.attributes("-topmost", True)
            self.root.after(650, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    # ---------------------------- Account selection ----------------------------

    def account_short_label(self, account_id: str | None) -> str:
        if not account_id:
            return "—"
        if account_id in self.account_labels_by_id:
            return self.account_labels_by_id[account_id]
        for acc in self.accounts:
            if acc.get("id") == account_id:
                return self.make_account_label(acc, short=True)
        return str(account_id)

    def make_account_label(self, acc: dict, short: bool = False) -> str:
        name = acc.get("name") or "Счёт"
        account_id = acc.get("id") or ""
        if short:
            return f"{name} | {str(account_id)[-6:]}"
        return f"{name} | id={account_id} | type={acc.get('type')} | status={acc.get('status')} | access={acc.get('accessLevel')}"

    def is_account_eligible_for_aggregation(self, acc: dict) -> bool:
        if not acc or not acc.get("id"):
            return False
        status = str(acc.get("status") or "").upper()
        access = str(acc.get("accessLevel") or "").upper()
        if "CLOSED" in status or "UNSPECIFIED" in status:
            return False
        if "NO_ACCESS" in access:
            return False
        return True

    def get_selected_account_ids(self) -> list[str]:
        result = []
        for account_id in self.selected_account_ids:
            if account_id and account_id not in result:
                result.append(account_id)
        return result

    def build_account_window(self):
        for widget in self.root.winfo_children():
            widget.destroy()

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)

        card = ttk.Frame(outer, style="Card.TFrame", padding=18)
        card.pack(fill="x", padx=35, pady=(26, 16))

        self.account_quick_buttons_frame = ttk.Frame(card, style="Panel.TFrame")
        self.account_quick_buttons_frame.pack(fill="x")

        self.status_label = ttk.Label(outer, text="Получаю список брокерских счетов...", style="Muted.TLabel")
        self.status_label.pack(anchor="center", pady=(4, 8))

        log_frame = ttk.LabelFrame(outer, text="Лог", padding=10)
        log_frame.pack(fill="both", expand=True, padx=90)
        self.log_box = ScrolledText(log_frame, state="disabled", height=18)
        style_textbox(self.log_box)
        self.log_box.pack(fill="both", expand=True)

    def load_accounts_async(self):
        if hasattr(self, "status_label"):
            self.status_label.configure(text="Получаю список брокерских счетов...")
        self.log("Запрос UsersService/GetAccounts...")

        def done(result, error):
            if error:
                if hasattr(self, "status_label"):
                    self.status_label.configure(text="Ошибка получения счетов")
                self.log(str(error))
                messagebox.showerror("Ошибка", str(error))
                return

            self.accounts = result or []
            self.account_labels_by_id = {
                acc.get("id"): self.make_account_label(acc, short=True)
                for acc in self.accounts
                if acc.get("id")
            }
            if not self.accounts:
                self.status_label.configure(text="Счета не найдены")
                self.log("Счета не найдены.")
                return

            values = [f"{i}. {self.make_account_label(acc)}" for i, acc in enumerate(self.accounts, start=1)]
            if hasattr(self, "account_combo"):
                self.account_combo.configure(values=values)
                self.account_combo.current(0)
            if hasattr(self, "status_label"):
                self.status_label.configure(text=f"Найдено счетов: {len(values)}")
            self.log(f"Найдено счетов: {len(values)}")
            self.render_account_quick_buttons()
            if hasattr(self, "additional_account_combo"):
                self.sync_account_header_controls()

        self.run_async(get_accounts, done)

    def render_account_quick_buttons(self):
        frame = getattr(self, "account_quick_buttons_frame", None)
        if frame is None:
            return
        for widget in frame.winfo_children():
            try:
                widget.destroy()
            except Exception:
                pass
        if not self.accounts:
            return

        columns_per_row = 5
        for col in range(columns_per_row):
            frame.grid_columnconfigure(col, weight=1, uniform="account_quick_buttons")

        for idx, acc in enumerate(self.accounts):
            account_id = acc.get("id") or ""
            name = str(acc.get("name") or "Счёт").strip() or "Счёт"
            label = name
            if account_id:
                label = f"{name} · {str(account_id)[-6:]}"
            row_idx = 1 + (idx // columns_per_row)
            col_idx = idx % columns_per_row
            btn = make_button(frame, label, lambda i=idx: self.select_account_by_index(i))
            btn.grid(row=row_idx, column=col_idx, sticky="ew", padx=4, pady=4)

    def open_account_record(self, acc: dict):
        self.account_id = acc.get("id")
        self.account_label = self.make_account_label(acc, short=True)
        if not self.account_id:
            messagebox.showerror("Ошибка", "У выбранного счёта нет id.")
            return
        self.selected_account_ids = [self.account_id]
        self.build_main_window()
        self.bring_main_to_front()
        self.refresh_all_async()
        self.start_oco_monitor()
        self.restart_operations_stream()

    def select_account_by_index(self, idx: int):
        if idx < 0 or idx >= len(self.accounts):
            messagebox.showwarning("Счёт", "Выбери счёт из списка.")
            return
        self.open_account_record(self.accounts[idx])

    def select_account(self):
        idx = self.account_combo.current()
        if idx < 0 or idx >= len(self.accounts):
            messagebox.showwarning("Счёт", "Выбери счёт из списка.")
            return
        self.open_account_record(self.accounts[idx])

    def sync_account_header_controls(self):
        selected_ids = self.get_selected_account_ids()
        labels = [self.account_short_label(account_id) for account_id in selected_ids]
        if labels:
            self.selected_accounts_var.set(f"Счета ({len(labels)}): " + "  +  ".join(labels))
        else:
            self.selected_accounts_var.set("Счета: —")

        if not hasattr(self, "additional_account_combo"):
            return

        add_values = []
        self.additional_account_value_to_id = {}
        for acc in self.accounts:
            account_id = acc.get("id")
            if not self.is_account_eligible_for_aggregation(acc) or account_id in selected_ids:
                continue
            label = self.make_account_label(acc, short=True)
            value = f"{label}"
            add_values.append(value)
            self.additional_account_value_to_id[value] = account_id
        self.additional_account_combo.configure(values=add_values)
        if add_values:
            self.additional_account_combo.set(add_values[0])
        else:
            self.additional_account_combo.set("")

        if hasattr(self, "remove_account_combo"):
            remove_values = []
            self.remove_account_value_to_id = {}
            for account_id in selected_ids:
                value = self.account_short_label(account_id)
                remove_values.append(value)
                self.remove_account_value_to_id[value] = account_id
            self.remove_account_combo.configure(values=remove_values)
            if remove_values:
                self.remove_account_combo.set(remove_values[0])
            else:
                self.remove_account_combo.set("")

    def add_second_account_async(self):
        value = self.add_account_var.get().strip()
        account_id = getattr(self, "additional_account_value_to_id", {}).get(value)
        if not account_id:
            messagebox.showwarning("Счёт", "Нет доступного счёта для добавления.")
            return
        if account_id not in self.selected_account_ids:
            self.selected_account_ids.append(account_id)
        self.sync_account_header_controls()
        self.log(f"Добавлен счёт в агрегацию: {self.account_short_label(account_id)}")
        self.restart_operations_stream()
        self.refresh_all_async()

    def add_all_accounts_async(self):
        added = 0
        selected_now = set(self.get_selected_account_ids())
        for acc in self.accounts:
            account_id = acc.get("id")
            if not self.is_account_eligible_for_aggregation(acc):
                continue
            if account_id and account_id not in selected_now:
                self.selected_account_ids.append(account_id)
                selected_now.add(account_id)
                added += 1
        self.sync_account_header_controls()
        if added:
            self.log(f"Добавлены все счета в агрегацию: +{added}")
        self.restart_operations_stream()
        self.refresh_all_async()

    def remove_selected_account_async(self):
        value = self.remove_account_var.get().strip()
        account_id = getattr(self, "remove_account_value_to_id", {}).get(value)
        if not account_id:
            messagebox.showwarning("Счёт", "Выбери счёт для удаления.")
            return

        selected_ids = self.get_selected_account_ids()
        if account_id not in selected_ids:
            return
        if len(selected_ids) <= 1:
            messagebox.showwarning("Счёт", "Нельзя убрать последний выбранный счёт.")
            return

        removed_label = self.account_short_label(account_id)
        self.selected_account_ids = [x for x in self.selected_account_ids if x != account_id]

        # If the removed account was the current primary account, promote the first remaining selected account.
        # Opening positions is still distributed across all selected accounts; this only keeps logs/default labels sane.
        if account_id == self.account_id:
            remaining_ids = self.get_selected_account_ids()
            self.account_id = remaining_ids[0] if remaining_ids else None
            self.account_label = self.account_short_label(self.account_id)

        self.sync_account_header_controls()
        self.log(f"Счёт убран из агрегации: {removed_label}")
        self.restart_operations_stream()
        self.refresh_all_async()

    # ---------------------------- Main window layout ----------------------------

    def build_main_window(self):
        self.stop_periodic_trade_preview_refresh()
        self.stop_periodic_portfolio_refresh()
        for widget in self.root.winfo_children():
            widget.destroy()
        self.root.geometry("1380x800")

        root_frame = ttk.Frame(self.root, padding=12)
        root_frame.pack(fill="both", expand=True)

        header = ttk.Frame(root_frame, style="Header.TFrame", padding=(12, 10))
        header.pack(fill="x")
        header.grid_columnconfigure(0, weight=1)

        left = ttk.Frame(header, style="Header.TFrame")
        left.grid(row=0, column=0, sticky="ew")
        ttk.Label(left, textvariable=self.selected_accounts_var, style="Muted.TLabel").pack(side="left", padx=(0, 12))

        right = ttk.Frame(header, style="Header.TFrame")
        right.grid(row=0, column=1, sticky="e")
        ttk.Label(right, text="Добавить счёт:", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.additional_account_combo = ttk.Combobox(right, textvariable=self.add_account_var, state="readonly", width=25)
        self.additional_account_combo.pack(side="left", padx=(0, 6))
        make_button(right, "Добавить", self.add_second_account_async).pack(side="left", padx=4)
        make_button(right, "Добавить все", self.add_all_accounts_async).pack(side="left", padx=4)

        ttk.Label(right, text="Убрать счёт:", style="Muted.TLabel").pack(side="left", padx=(12, 6))
        self.remove_account_combo = ttk.Combobox(right, textvariable=self.remove_account_var, state="readonly", width=25)
        self.remove_account_combo.pack(side="left", padx=(0, 6))
        make_button(right, "Убрать выбранный", self.remove_selected_account_async).pack(side="left", padx=4)

        make_button(right, "График", self.open_charts_window).pack(side="left", padx=4)
        make_button(right, "Predictor", self.open_mathmodel_window).pack(side="left", padx=4)
        make_button(right, "Analysis", self.open_math_window).pack(side="left", padx=4)
        make_button(right, "Обновить всё", self.refresh_all_async).pack(side="left", padx=4)
        self.sync_account_header_controls()

        cards = ttk.Frame(root_frame)
        cards.pack(fill="x", pady=(12, 10))
        self.make_card(cards, "Стоимость портфеля", self.total_var, 0)
        self.make_card(cards, "Прибыль портфеля ₽", self.expected_var, 1)
        self.make_card(cards, "Свободные деньги", self.cash_var, 2)
        self.make_card(cards, "Заблокировано", self.blocked_var, 3)
        self.make_card(cards, "Ожид. вармаржа", self.var_margin_var, 4)
        self.make_card(cards, "Риск текущий, % капитала", self.risk_var, 5)

        self.notebook = ttk.Notebook(root_frame)
        self.notebook.pack(fill="both", expand=True)

        self.trade_tab = ttk.Frame(self.notebook, padding=10)
        self.portfolio_tab = ttk.Frame(self.notebook, padding=10)
        self.positions_tab = ttk.Frame(self.notebook, padding=10)
        self.stops_tab = ttk.Frame(self.notebook, padding=10)
        self.journal_tab = ttk.Frame(self.notebook, padding=10)
        self.log_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.trade_tab, text="Сделка")
        self.notebook.add(self.portfolio_tab, text="Портфель")
        self.notebook.add(self.positions_tab, text="Открытые позиции")
        self.notebook.add(self.stops_tab, text="Заявки защиты")
        self.notebook.add(self.journal_tab, text="Дневник сделок")
        self.notebook.add(self.log_tab, text="Лог")

        self.build_trade_tab()
        self.build_portfolio_tab()
        self.build_positions_tab()
        self.build_stops_tab()
        self.build_trade_journal_tab()
        self.build_log_tab()

        self.log(f"Основной счёт для новых сделок: {self.account_label}")
        self.log("Дневник сделок: SQLite db/jtrade_trades.db")
        # Secondary tool windows are opened only by user request:
        self.root.after(120, self.bring_main_to_front)
        self.root.after(1100, self.auto_sync_trade_journal)
        self.root.after(1600, self.start_periodic_portfolio_refresh)
        self.root.after(2500, self.start_periodic_trade_preview_refresh)
        self.root.after(3500, self.schedule_loved_moex_background)

    def make_card(self, parent, label: str, variable: tk.StringVar, col: int):
        parent.grid_columnconfigure(col, weight=1, uniform="cards")
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        card.grid(row=0, column=col, sticky="ew", padx=5)
        ttk.Label(card, text=label, style="CardTitle.TLabel").pack(anchor="w")
        value_label = ttk.Label(card, textvariable=variable, style="CardValue.TLabel")
        value_label.pack(anchor="w", pady=(4, 0))
        if not hasattr(self, "card_value_labels"):
            self.card_value_labels = {}
        self.card_value_labels[label] = value_label

    def build_trade_tab(self):
        layout = ttk.Frame(self.trade_tab)
        layout.pack(fill="both", expand=True)
        layout.grid_columnconfigure(0, weight=0)
        layout.grid_columnconfigure(1, weight=1)
        layout.grid_rowconfigure(1, weight=1)

        form = ttk.LabelFrame(layout, text="Открытие позиции", padding=14)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=(0, 10))

        self.ticker_var = tk.StringVar()
        self.qty_var = tk.StringVar(value="1")
        self.sl_price_var = tk.StringVar(value="авто")
        self.tp_manual_var = tk.StringVar(value="авто")
        self._auto_sl_value = ""
        self._auto_tp_value = ""
        self._auto_qty_value = ""
        self.entry_split_enabled_var = tk.BooleanVar(value=True)
        self.entry_split_count_var = tk.StringVar(value="auto")
        self._auto_level_update = False
        self.available_preview_var = tk.StringVar(value="Доступно: —")
        self.available_long_var = tk.StringVar(value="L —")
        self.available_short_var = tk.StringVar(value="S —")
        self.available_selected_var = tk.StringVar(value="Доступно: —")
        self.recommended_qty_var = tk.StringVar(value="Рекомендуемое количество: —")
        self.available_note_var = tk.StringVar(value="")
        self.position_value_preview_var = tk.StringVar(value="Позиция: —")
        self.lot_price_var = tk.StringVar(value="Цена за лот: —")
        self.position_long_var = tk.StringVar(value="L —")
        self.position_short_var = tk.StringVar(value="S —")
        self.position_selected_var = tk.StringVar(value="Позиция: —")
        self.sl_preview_var = tk.StringVar(value="SL: —")
        self.sl_long_var = tk.StringVar(value="L —")
        self.sl_short_var = tk.StringVar(value="S —")
        self.sl_selected_var = tk.StringVar(value="SL: —")
        self.tp_preview_var = tk.StringVar(value="TP: —")
        self.sl_note_var = tk.StringVar(value="")
        self.risk_money_var = tk.StringVar(value="risk: —")
        self.ev_money_var = tk.StringVar(value="ev: —")
        self.trade_side_var = tk.StringVar(value="BUY")
        self.moex_db_status_var = tk.StringVar(value="moex // db: фон ждёт любимые тикеры")
        self._trade_preview_after_id = None
        self._trade_preview_seq = 0

        ttk.Label(form, text="Тикер").grid(row=0, column=0, sticky="w", pady=5, padx=(0, 8))
        ttk.Entry(form, textvariable=self.ticker_var, width=22).grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(form, text="Лоты/контракты").grid(row=1, column=0, sticky="w", pady=5, padx=(0, 8))
        ttk.Entry(form, textvariable=self.qty_var, width=22).grid(row=1, column=1, sticky="ew", pady=5)
        position_frame = ttk.Frame(form, style="Panel.TFrame")
        position_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(position_frame, textvariable=self.position_selected_var, style="PreviewWhite.TLabel").pack(side="left")

        lot_price_frame = ttk.Frame(form, style="Panel.TFrame")
        lot_price_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(lot_price_frame, textvariable=self.lot_price_var, style="PreviewWhite.TLabel").pack(side="left")

        ttk.Label(form, text="SL").grid(row=4, column=0, sticky="w", pady=5, padx=(0, 8))
        ttk.Entry(form, textvariable=self.sl_price_var, width=22).grid(row=4, column=1, sticky="ew", pady=5)
        sl_frame = ttk.Frame(form, style="Panel.TFrame")
        sl_frame.grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(sl_frame, textvariable=self.sl_selected_var, style="PreviewWhite.TLabel").pack(side="left")
        ttk.Label(sl_frame, textvariable=self.sl_note_var, style="PreviewMuted.TLabel").pack(side="left", padx=(6, 0))
        ttk.Label(sl_frame, textvariable=self.risk_money_var, style="PreviewRisk.TLabel").pack(side="left", padx=(8, 0))

        ttk.Label(form, text="TP").grid(row=6, column=0, sticky="w", pady=5, padx=(0, 8))
        ttk.Entry(form, textvariable=self.tp_manual_var, width=22).grid(row=6, column=1, sticky="ew", pady=5)
        tp_frame = ttk.Frame(form, style="Panel.TFrame")
        tp_frame.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(tp_frame, textvariable=self.tp_preview_var, style="PreviewWhite.TLabel").pack(side="left")
        ttk.Label(tp_frame, textvariable=self.ev_money_var, style="PreviewReward.TLabel").pack(side="left", padx=(8, 0))

        available_frame = ttk.Frame(form, style="Panel.TFrame")
        available_frame.grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 4))
        ttk.Label(available_frame, textvariable=self.available_selected_var, style="PreviewWhite.TLabel").pack(side="left")
        ttk.Label(available_frame, textvariable=self.available_note_var, style="PreviewMuted.TLabel").pack(side="left", padx=(6, 0))

        recommended_frame = ttk.Frame(form, style="Panel.TFrame")
        recommended_frame.grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(recommended_frame, textvariable=self.recommended_qty_var, style="PreviewWhite.TLabel").pack(side="left")

        split_frame = ttk.Frame(form, style="Panel.TFrame")
        split_frame.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(
            split_frame,
            text="Дробить вход",
            variable=self.entry_split_enabled_var,
            command=self.schedule_trade_preview_refresh,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(split_frame, text="частей:", style="PreviewMuted.TLabel").pack(side="left", padx=(0, 4))
        self.entry_split_count_combo = ttk.Combobox(
            split_frame,
            textvariable=self.entry_split_count_var,
            values=["auto", "2", "3", "5", "10", "15"],
            state="readonly",
            width=7,
        )
        self.entry_split_count_combo.pack(side="left")
        self.entry_split_count_combo.bind("<<ComboboxSelected>>", lambda _event: self.schedule_trade_preview_refresh())

        risk_frame = ttk.Frame(form, style="Panel.TFrame")
        risk_frame.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(2, 8))
        risk_frame.grid_columnconfigure(0, weight=1)
        risk_frame.grid_columnconfigure(1, weight=1)
        self.risk_portfolio_button = make_button(risk_frame, "R от портфеля", lambda: self.set_risk_model("portfolio"), style="RiskInactive.TButton")
        self.risk_portfolio_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.risk_position_button = make_button(risk_frame, "R по позиции", lambda: self.set_risk_model("position"), style="RiskInactive.TButton")
        self.risk_position_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.refresh_risk_mode_buttons()

        self.ticker_var.trace_add("write", self.schedule_trade_preview_refresh)
        self.ticker_var.trace_add("write", self.on_trade_context_changed)
        self.qty_var.trace_add("write", self.schedule_trade_preview_refresh)
        self.sl_price_var.trace_add("write", self.schedule_trade_preview_refresh)
        self.sl_price_var.trace_add("write", self.on_trade_context_changed)
        self.tp_manual_var.trace_add("write", self.schedule_trade_preview_refresh)
        self.tp_manual_var.trace_add("write", self.on_trade_context_changed)

        side_frame = ttk.Frame(form, style="Panel.TFrame")
        side_frame.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(10, 5))
        side_frame.grid_columnconfigure(0, weight=1)
        side_frame.grid_columnconfigure(1, weight=1)
        self.buy_side_button = make_button(side_frame, "✓ Купить", lambda: self.set_trade_side("BUY"), style="Buy.TButton")
        self.buy_side_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.sell_side_button = make_button(side_frame, "Продать", lambda: self.set_trade_side("SELL"), style="TButton")
        self.sell_side_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        make_button(form, "Открыть сделку", self.open_selected_trade_async, width=22).grid(row=13, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Label(form, textvariable=self.moex_db_status_var, style="PreviewMuted.TLabel", wraplength=320, justify="left").grid(row=14, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        make_button(form, "Обновить позицию по тикеру", self.refresh_current_ticker_position_async, width=22).grid(row=15, column=0, columnspan=2, sticky="ew", pady=(12, 5))

        self.current_ticker_status_var = tk.StringVar(value="Позиция по тикеру: —")
        status = ttk.LabelFrame(layout, text="Статус выбранного тикера", padding=12)
        status.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        ttk.Label(status, textvariable=self.current_ticker_status_var, wraplength=320, justify="left").pack(fill="x")

        quick = ttk.LabelFrame(layout, text="Позиции счёта", padding=10)
        quick.grid(row=0, column=1, rowspan=2, sticky="nsew")
        quick.grid_rowconfigure(0, weight=1)
        quick.grid_columnconfigure(0, weight=1)

        columns = [
            ("account", "Счёт", 120, "w"),
            ("ticker", "Тикер", 100, "w"),
            ("side", "Сторона", 80, "center"),
            ("qty", "Кол-во", 95, "e"),
            ("lots", "Лоты", 85, "e"),
            ("avg", "Средняя", 100, "e"),
            ("last", "Текущая", 100, "e"),
            ("pnl", "Прибыль ₽", 105, "e"),
            ("pnl_pct", "PnL %", 80, "e"),
            ("var_margin", "Вармаржа", 105, "e"),
            ("risk_pct", "Риск %", 115, "e"),
            ("tp", "TP", 85, "e"),
            ("sl", "SL", 85, "e"),
        ]
        self.trade_positions_tree, frame = make_tree(quick, columns, height=18)
        frame.grid(row=0, column=0, sticky="nsew")
        self.trade_positions_tree.bind("<<TreeviewSelect>>", self.on_any_position_selected)


    def normalize_moex_train_ticker(self, ticker: str) -> str:
        raw = str(ticker or "").upper().strip().replace(" ", "")
        if "@" in raw:
            raw = raw.split("@", 1)[0]
        raw = re.sub(r"[^A-Z0-9_\-]", "", raw)
        return raw

    def collect_moex_db_tickers(self) -> list[str]:
        ignored = {"TMON", "LQDT", "S"}
        tickers: list[str] = []

        def add(value):
            ticker = self.normalize_moex_train_ticker(value)
            if not ticker or ticker in ignored or len(ticker) < 2:
                return
            if ticker not in tickers:
                tickers.append(ticker)

        try:
            add(self.ticker_var.get())
        except Exception:
            pass

        for source in (
            getattr(self, "portfolio_rows", {}),
            getattr(self, "open_positions_rows", {}),
            getattr(self, "active_trade_rows", {}),
            getattr(self, "stop_order_rows", {}),
        ):
            try:
                for row in (source or {}).values():
                    if isinstance(row, dict):
                        for key in ("ticker", "symbol", "instrument_ticker"):
                            add(row.get(key))
            except Exception:
                pass

        for tree_name in ("trade_positions_tree", "positions_tree", "portfolio_tree", "stops_tree"):
            tree = getattr(self, tree_name, None)
            if tree is None:
                continue
            try:
                columns = list(tree["columns"])
                ticker_idx = columns.index("ticker") if "ticker" in columns else -1
                if ticker_idx < 0:
                    continue
                for item_id in tree.get_children(""):
                    values = tree.item(item_id, "values") or []
                    if ticker_idx < len(values):
                        add(values[ticker_idx])
            except Exception:
                continue

        return tickers

    def schedule_loved_moex_background(self, delay_ms: int = 60000):
        if not hasattr(self, "root"):
            return
        if getattr(self, "_loved_moex_bg_after_id", None):
            try:
                self.root.after_cancel(self._loved_moex_bg_after_id)
            except Exception:
                pass
        self._loved_moex_bg_after_id = self.root.after(delay_ms, self.run_loved_moex_background_async)

    def run_loved_moex_background_async(self):
        self._loved_moex_bg_after_id = None
        if run_loved_background_once is None:
            self.schedule_loved_moex_background(90000)
            return
        if getattr(self, "_loved_moex_bg_running", False):
            self.schedule_loved_moex_background(45000)
            return

        self._loved_moex_bg_running = True
        self.log("engine: фоновая докачка любимых тикеров стартовала")

        def progress(message: str):
            text = str(message or "")
            def apply():
                self.log(text)
                try:
                    if hasattr(self, "moex_db_status_var"):
                        short = text.replace("moex // db: ", "")
                        if len(short) > 140:
                            short = short[:137] + "..."
                        self.moex_db_status_var.set(f"engine bg: {short}")
                except Exception:
                    pass
            try:
                self.root.after(0, apply)
            except Exception:
                pass

        def target():
            return run_loved_background_once(
                log_callback=progress,
                max_tickers=10,
                minutes=43200,
            )

        def done(result, error):
            self._loved_moex_bg_running = False
            if error:
                self.log(f"moex // db: фоновая докачка ошибка: {error}")
                self.schedule_loved_moex_background(90000)
                return
            result = result or {}
            self.log(
                "engine: фоновая докачка завершена. "
                f"downloaded={result.get('downloaded', 0)}, "
                f"history={result.get('history_inserted', 0)}, "
                f"training_rows={result.get('training_rows', 0)}"
            )
            self.schedule_loved_moex_background(60000)

        self.run_async(target, done)

    def open_charts_window(self):
        if self.charts_window is not None:
            try:
                if self.charts_window.winfo_exists():
                    self.charts_window.deiconify()
                    self.charts_window.lift()
                    self.charts_window.focus_force()
                    return
            except Exception:
                self.charts_window = None
                self.charts_panel = None

        window = tk.Toplevel(self.root)
        window.title("График")
        window.geometry("1240x760+80+70")
        window.minsize(900, 560)
        window.configure(bg=BG)
        try:
            apply_window_icon(window)
        except Exception:
            pass

        container = ttk.Frame(window, padding=10)
        container.pack(fill="both", expand=True)

        self.charts_window = window
        self.charts_panel = NineChartsPanel(container, self)
        self.charts_panel.pack(fill="both", expand=True)

        window.protocol("WM_DELETE_WINDOW", self.hide_charts_window)
        self.log("Окно «График» открыто.")

    def hide_charts_window(self):
        if self.charts_window is None:
            return
        try:
            if self.charts_window.winfo_exists():
                self.charts_window.withdraw()
                self.log("Окно «График» скрыто. Кнопка вернёт его обратно.")
        except Exception:
            self.charts_window = None
            self.charts_panel = None

    def open_math_window(self):
        if self.math_window is not None:
            try:
                if self.math_window.winfo_exists():
                    self.math_window.deiconify()
                    self.math_window.lift()
                    self.math_window.focus_force()
                    self.sync_math_window_context()
                    return
            except Exception:
                self.math_window = None
                self.math_panel = None

        window = tk.Toplevel(self.root)
        window.title("Analysis")
        window.configure(bg=BG)
        window.geometry("660x760")
        window.minsize(560, 620)
        apply_window_icon(window)

        container = ttk.Frame(window, padding=8)
        container.pack(fill="both", expand=True)
        self.math_window = window
        self.math_panel = PrediPanel(container, self)
        self.math_panel.pack(fill="both", expand=True)

        window.protocol("WM_DELETE_WINDOW", self.hide_math_window)
        self.sync_math_window_context()
        self.log("Окно «Analysis» открыто.")

    def hide_math_window(self):
        if self.math_window is None:
            return
        try:
            if self.math_window.winfo_exists():
                self.math_window.withdraw()
                self.log("Окно «Analysis» скрыто. Кнопка вернёт его обратно.")
        except Exception:
            self.math_window = None
            self.math_panel = None


    def open_mathmodel_window(self):
        if self.mathmodel_window is not None:
            try:
                if self.mathmodel_window.winfo_exists():
                    self.mathmodel_window.deiconify()
                    self.mathmodel_window.lift()
                    self.mathmodel_window.focus_force()
                    self.sync_mathmodel_window_context()
                    return
            except Exception:
                self.mathmodel_window = None
                self.mathmodel_panel = None

        window = tk.Toplevel(self.root)
        window.title("Predictor")
        window.configure(bg=BG)
        window.geometry("900x520+760+100")
        window.minsize(620, 380)
        apply_window_icon(window)

        container = ttk.Frame(window, padding=8)
        container.pack(fill="both", expand=True)
        self.mathmodel_window = window
        self.mathmodel_panel = MathModelGraphPanel(container, self)
        self.mathmodel_panel.pack(fill="both", expand=True)

        window.protocol("WM_DELETE_WINDOW", self.hide_mathmodel_window)
        self.sync_mathmodel_window_context()
        self.log("Окно «Predictor» открыто.")

    def hide_mathmodel_window(self):
        if self.mathmodel_window is None:
            return
        try:
            if self.mathmodel_window.winfo_exists():
                self.mathmodel_window.withdraw()
        except Exception:
            self.mathmodel_window = None
            self.mathmodel_panel = None

    def sync_mathmodel_window_context(self):
        if not getattr(self, "mathmodel_panel", None):
            return
        ticker = self.ticker_var.get().strip().upper() if hasattr(self, "ticker_var") else ""
        side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        self.mathmodel_panel.set_context(ticker=ticker, side=side)

    def on_trade_context_changed(self, *_args):
        if not hasattr(self, "root"):
            return
        if getattr(self, "_trade_context_after_id", None):
            try:
                self.root.after_cancel(self._trade_context_after_id)
            except Exception:
                pass
        self._trade_context_after_id = self.root.after(1000, self.perform_trade_context_update)

    def perform_trade_context_update(self):
        self._trade_context_after_id = None
        ticker = self.ticker_var.get().strip().upper() if hasattr(self, "ticker_var") else ""
        ticker = self.normalize_moex_train_ticker(ticker) if hasattr(self, "normalize_moex_train_ticker") else ticker
        if ticker and add_loved_tickers:
            try:
                added = add_loved_tickers([ticker])
                if added:
                    self.log(f"moex // db: тикер добавлен в любимые для фоновой докачки: {ticker}")
            except Exception as exc:
                self.log(f"moex // db: не смог добавить тикер в любимые: {exc}")
        self.sync_math_window_context()
        self.sync_mathmodel_window_context()

    def extract_price_from_preview_text(self, text: str):
        raw = str(text or "").replace(",", ".")
        token = ""
        for char in raw:
            if char.isdigit() or char in {".", "-"}:
                token += char
            elif token:
                break
        if not token or token in {"-", ".", "-."}:
            return None
        try:
            value = float(token)
        except Exception:
            return None
        return value if value > 0 else None

    def get_selected_side_sl_for_math(self):
        side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        raw_sl = self.sl_price_var.get().strip() if hasattr(self, "sl_price_var") else ""
        if raw_sl and not is_risk_based_sl_value(raw_sl):
            return raw_sl
        if side == "BUY" and hasattr(self, "sl_long_var"):
            return self.extract_price_from_preview_text(self.sl_long_var.get())
        if side == "SELL" and hasattr(self, "sl_short_var"):
            return self.extract_price_from_preview_text(self.sl_short_var.get())
        return None

    def sync_math_window_context(self):
        if not getattr(self, "math_panel", None):
            return
        ticker = self.ticker_var.get().strip().upper() if hasattr(self, "ticker_var") else ""
        # Probability mathematic is independent from the currently prepared position.
        self.math_panel.set_context(ticker=ticker)

    def set_trade_side(self, side: str):
        side = side if side in {"BUY", "SELL"} else "BUY"
        if not hasattr(self, "trade_side_var"):
            self.trade_side_var = tk.StringVar(value=side)
        old_side = self.trade_side_var.get()
        if old_side != side:
            # If SL/TP fields still contain values that were auto-filled for the previous side,
            # turn them back into auto before clearing the auto markers. Otherwise a LONG auto
            # bracket can become a "manual" SHORT bracket and distort the preview.
            self.reset_autofilled_price_fields_to_auto()
        self.trade_side_var.set(side)
        if old_side != side:
            self._auto_sl_value = ""
            self._auto_tp_value = ""
            self._auto_level_context_key = None
            self.set_available_preview()
            self.set_recommended_qty_preview()
            self.set_position_value_preview()
            self.set_sl_preview_pair()
            self.set_tp_preview()
            self.set_trade_risk_reward_preview()
        self.refresh_trade_side_buttons()
        self.schedule_trade_preview_refresh()
        self.sync_math_window_context()
        self.sync_mathmodel_window_context()

    def refresh_trade_side_buttons(self):
        if not hasattr(self, "buy_side_button") or not hasattr(self, "sell_side_button"):
            return
        side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        if side == "BUY":
            self.buy_side_button.configure(text="✓ Купить", style="Buy.TButton")
            self.sell_side_button.configure(text="Продать", style="TButton")
        else:
            self.buy_side_button.configure(text="Купить", style="TButton")
            self.sell_side_button.configure(text="✓ Продать", style="Sell.TButton")

    def open_selected_trade_async(self):
        side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        if side not in {"BUY", "SELL"}:
            messagebox.showwarning("Сделка", "Сначала выбери LONG или SHORT.")
            return
        self.open_trade_async(side)

    def build_portfolio_tab(self):
        self.portfolio_tab.grid_columnconfigure(0, weight=1)
        self.portfolio_tab.grid_rowconfigure(0, weight=1)
        columns = [
            ("account", "Счёт", 120, "w"),
            ("section", "Раздел", 105, "w"),
            ("ticker", "Тикер", 100, "w"),
            ("name", "Название", 220, "w"),
            ("qty", "Кол-во", 100, "e"),
            ("lots", "Лоты", 85, "e"),
            ("avg", "Средняя", 105, "e"),
            ("last", "Текущая", 105, "e"),
            ("value", "Стоимость", 115, "e"),
            ("pnl", "Прибыль ₽", 105, "e"),
            ("pnl_pct", "Доход %", 85, "e"),
            ("var_margin", "Вармаржа", 105, "e"),
            ("risk_pct", "Риск %", 115, "e"),
            ("figi", "FIGI/UID", 150, "w"),
        ]
        self.portfolio_tree, frame = make_tree(self.portfolio_tab, columns, height=21)
        frame.grid(row=0, column=0, sticky="nsew")

    def build_positions_tab(self):
        self.positions_tab.grid_columnconfigure(0, weight=1)
        self.positions_tab.grid_rowconfigure(0, weight=1)
        self.positions_tab.grid_rowconfigure(1, weight=0)

        columns = [
            ("account", "Счёт", 120, "w"),
            ("ticker", "Тикер", 100, "w"),
            ("side", "Сторона", 80, "center"),
            ("qty", "Кол-во", 95, "e"),
            ("lots", "Лоты", 85, "e"),
            ("avg", "Средняя", 105, "e"),
            ("last", "Текущая", 105, "e"),
            ("pnl", "Прибыль ₽", 105, "e"),
            ("pnl_pct", "PnL %", 85, "e"),
            ("var_margin", "Вармаржа", 105, "e"),
            ("risk_pct", "Риск %", 115, "e"),
            ("tp", "TP", 85, "e"),
            ("sl", "SL", 85, "e"),
            ("state", "Состояние", 160, "w"),
        ]
        self.open_positions_tree, frame = make_tree(self.positions_tab, columns, height=19)
        frame.grid(row=0, column=0, sticky="nsew")
        self.open_positions_tree.bind("<<TreeviewSelect>>", self.on_any_position_selected)

        controls = ttk.LabelFrame(self.positions_tab, text="Управление выбранной позицией", padding=10)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        self.manage_selected_var = tk.StringVar(value="Выбрано: —")
        ttk.Label(controls, textvariable=self.manage_selected_var, style="Bold.TLabel").grid(row=0, column=0, columnspan=10, sticky="w", pady=(0, 8))

        ttk.Label(controls, text="Новый SL").grid(row=1, column=0, sticky="w", padx=(0, 6))
        self.manage_sl_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.manage_sl_var, width=12).grid(row=1, column=1, sticky="w", padx=(0, 12))

        ttk.Label(controls, text="Новый TP").grid(row=1, column=2, sticky="w", padx=(0, 6))
        self.manage_tp_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.manage_tp_var, width=12).grid(row=1, column=3, sticky="w", padx=(0, 12))

        make_button(controls, "Заменить TP/SL", self.replace_selected_protection_async).grid(row=1, column=4, sticky="ew", padx=4)
        make_button(controls, "Снять TP/SL", self.cancel_selected_protection_async).grid(row=1, column=5, sticky="ew", padx=4)
        make_button(controls, "Закрыть 25%", lambda: self.close_selected_position_async(Decimal("0.25"))).grid(row=1, column=6, sticky="ew", padx=4)
        make_button(controls, "Закрыть 50%", lambda: self.close_selected_position_async(Decimal("0.50"))).grid(row=1, column=7, sticky="ew", padx=4)
        make_button(controls, "Закрыть 100%", lambda: self.close_selected_position_async(Decimal("1"))).grid(row=1, column=8, sticky="ew", padx=4)
        make_button(controls, "Обновить всё", self.refresh_all_async).grid(row=1, column=9, sticky="ew", padx=4)

        for col in range(10):
            controls.grid_columnconfigure(col, weight=1 if col >= 4 else 0)

    def build_stops_tab(self):
        self.stops_tab.grid_columnconfigure(0, weight=1)
        self.stops_tab.grid_rowconfigure(0, weight=1)
        self.stops_tab.grid_rowconfigure(1, weight=1)

        active_frame = ttk.LabelFrame(self.stops_tab, text="Сделки, которыми управляет приложение", padding=8)
        active_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        active_frame.grid_columnconfigure(0, weight=1)
        active_frame.grid_rowconfigure(0, weight=1)

        active_cols = [
            ("account", "Счёт", 120, "w"),
            ("created", "Создано", 145, "w"),
            ("ticker", "Тикер", 100, "w"),
            ("side", "Сторона", 80, "center"),
            ("qty", "Кол-во", 80, "e"),
            ("entry", "Вход", 100, "e"),
            ("tp", "TP", 100, "e"),
            ("sl", "SL", 100, "e"),
            ("tp_id", "TP id", 170, "w"),
            ("sl_id", "SL id", 170, "w"),
        ]
        self.active_trades_tree, frame1 = make_tree(active_frame, active_cols, height=8)
        frame1.grid(row=0, column=0, sticky="nsew")

        stops_frame = ttk.LabelFrame(self.stops_tab, text="Активные стоп-заявки у брокера", padding=8)
        stops_frame.grid(row=1, column=0, sticky="nsew")
        stops_frame.grid_columnconfigure(0, weight=1)
        stops_frame.grid_rowconfigure(0, weight=1)

        stop_cols = [
            ("account", "Счёт", 120, "w"),
            ("ticker", "Тикер", 110, "w"),
            ("type", "Тип", 170, "w"),
            ("direction", "Направление", 140, "w"),
            ("qty", "Кол-во", 90, "e"),
            ("price", "Цена", 100, "e"),
            ("stop", "Стоп цена", 100, "e"),
            ("id", "stopOrderId", 230, "w"),
        ]
        self.stop_orders_tree, frame2 = make_tree(stops_frame, stop_cols, height=9)
        frame2.grid(row=0, column=0, sticky="nsew")


    def build_trade_journal_tab(self):
        self.journal_tab.grid_columnconfigure(0, weight=1)
        self.journal_tab.grid_rowconfigure(0, weight=0)
        self.journal_tab.grid_rowconfigure(1, weight=1)

        stats_frame = ttk.LabelFrame(self.journal_tab, text="Чистый результат", padding=10)
        stats_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        stats_frame.grid_columnconfigure(5, weight=1)

        self.trade_stats_period_var = tk.StringVar(value="7 дней")
        self.trade_stats_summary_var = tk.StringVar(value="Чистый итог: —")
        self.trade_stats_detail_var = tk.StringVar(value="")

        ttk.Label(stats_frame, text="Период:", style="PanelMuted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
        period_combo = ttk.Combobox(
            stats_frame,
            textvariable=self.trade_stats_period_var,
            values=["Сегодня", "7 дней", "30 дней", "90 дней", "Всё"],
            state="readonly",
            width=12,
        )
        period_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        period_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_trade_stats_async())
        make_button(stats_frame, "Обновить статистику", self.refresh_trade_stats_async).grid(row=0, column=2, sticky="w", padx=(0, 10))
        ttk.Label(stats_frame, textvariable=self.trade_stats_summary_var, style="PreviewWhite.TLabel").grid(row=0, column=3, sticky="w", padx=(0, 10))
        ttk.Label(stats_frame, textvariable=self.trade_stats_detail_var, style="PreviewMuted.TLabel").grid(row=0, column=4, sticky="w")

        self.trade_journal_panel = TradeJournalPanel(self.journal_tab, self)
        self.trade_journal_panel.grid(row=1, column=0, sticky="nsew")
        self.root.after(300, self.refresh_trade_stats_async)

    def auto_sync_trade_journal(self):
        panel = getattr(self, "trade_journal_panel", None)
        if panel:
            panel.auto_sync_on_startup()
        self.refresh_trade_stats_async()

    def build_log_tab(self):
        self.log_box = ScrolledText(self.log_tab, state="disabled")
        style_textbox(self.log_box)
        self.log_box.pack(fill="both", expand=True)


    # ---------------------------- Operations stream bridge ----------------------------

    def restart_operations_stream(self):
        account_ids = self.get_selected_account_ids()
        try:
            if self.operations_stream:
                self.operations_stream.stop()
        except Exception:
            pass
        self.operations_stream = None
        self._operations_stream_status_seen = set()
        if not account_ids:
            return
        try:
            self.operations_stream = LightOperationsStream(
                account_ids,
                on_event=self.on_operations_stream_event,
                on_status=self.on_operations_stream_status,
            )
            self.operations_stream.start()
            self.log(f"Operations stream: подписка на {len(account_ids)} сч.")
        except Exception as exc:
            self.log(f"Operations stream не запустился: {exc}")

    def on_operations_stream_status(self, text: str):
        def emit():
            if not str(text).strip():
                return
            # Avoid flooding the log with the same ping/reconnect status.
            key = str(text)[:180]
            if key in self._operations_stream_status_seen:
                return
            if len(self._operations_stream_status_seen) > 30:
                self._operations_stream_status_seen.clear()
            self._operations_stream_status_seen.add(key)
            self.log(str(text))
        try:
            self.root.after(0, emit)
        except Exception:
            pass

    def on_operations_stream_event(self, kind: str, payload: dict):
        try:
            self.root.after(0, lambda: self.schedule_stream_refresh(kind, payload))
        except Exception:
            pass

    def schedule_stream_refresh(self, kind: str, payload: dict | None = None):
        # Pings/subscription responses only prove the connection is alive; do not refresh tables for them.
        payload = payload or {}
        text = str(payload)
        if "ping" in payload and len(payload) <= 1:
            return
        if "subscriptions" in payload and not any(k in payload for k in ("portfolio", "position", "orderTrades")):
            return

        if self._operations_stream_refresh_after_id:
            try:
                self.root.after_cancel(self._operations_stream_refresh_after_id)
            except Exception:
                pass
        self._operations_stream_refresh_after_id = self.root.after(450, self._run_stream_refresh)

    def _run_stream_refresh(self):
        self._operations_stream_refresh_after_id = None
        self.log("Stream event → лёгкое обновление портфеля/позиций")
        self.refresh_all_async()

    # ---------------------------- Data refresh ----------------------------

    def refresh_all_async(self, quiet: bool = False):
        account_ids = self.get_selected_account_ids()
        if not account_ids:
            return
        if quiet and getattr(self, "_refresh_all_running", False):
            return

        self._refresh_all_running = True
        self._refresh_all_seq += 1
        refresh_seq = self._refresh_all_seq
        selected_snapshot = list(account_ids)
        if not quiet:
            self.log(f"Обновляю агрегированный портфель, позиции и стоп-заявки ({len(selected_snapshot)} сч.)...")

        def task():
            account_payloads = []
            all_rows = []
            all_stops = []
            skipped_accounts = []
            total = Decimal("0")
            expected = Decimal("0")
            cash = Decimal("0")
            blocked = Decimal("0")
            var_margin = Decimal("0")

            # First pass: get all accounts and total selected capital.
            # Risk is calculated from current drawdown against this total capital,
            # not from SL distance and not from a single account only.
            for account_id in selected_snapshot:
                try:
                    portfolio = get_portfolio(account_id)
                    positions = get_positions(account_id)
                    try:
                        withdraw_limits = get_withdraw_limits(account_id)
                    except Exception:
                        withdraw_limits = {}
                    try:
                        stops = self.enrich_stop_orders(get_stop_orders(account_id), account_id)
                    except Exception:
                        stops = []
                    account_total = money_to_decimal(portfolio.get("totalAmountPortfolio"))
                    account_expected = money_to_decimal(portfolio.get("expectedYield"))
                    _cash_rub, account_blocked = self.extract_cash(positions, withdraw_limits)
                    account_cash = max(account_total - account_blocked, Decimal("0"))
                    account_var_margin = self.extract_var_margin(portfolio)
                    total += account_total
                    expected += account_expected
                    cash += account_cash
                    blocked += account_blocked
                    var_margin += account_var_margin
                    all_stops.extend(stops)
                    account_payloads.append({
                        "account_id": account_id,
                        "portfolio": portfolio,
                        "stops": stops,
                        "account_total": account_total,
                        "account_label": self.account_short_label(account_id),
                    })
                except Exception as exc:
                    skipped_accounts.append(f"{self.account_short_label(account_id)}: {exc}")

            # Second pass: build rows after the aggregated capital is known.
            for payload in account_payloads:
                all_rows.extend(self.build_portfolio_rows(
                    payload["portfolio"],
                    payload["account_id"],
                    payload["account_label"],
                    payload["account_total"],
                    payload["stops"],
                    total,
                ))

            aggregated_rows = self.aggregate_position_rows(all_rows, total)
            risk_total = sum((row.get("risk_raw") or Decimal("0")) for row in all_rows)
            risk_pct = (risk_total / total * Decimal("100")) if total else Decimal("0")
            return {
                "rows": aggregated_rows,
                "raw_rows": all_rows,
                "stops": all_stops,
                "total": total,
                "expected": expected,
                "cash": cash,
                "blocked": blocked,
                "var_margin": var_margin,
                "risk_total": risk_total,
                "risk_pct": risk_pct,
                "skipped_accounts": skipped_accounts,
                "refresh_seq": refresh_seq,
            }

        def done(result, error):
            self._refresh_all_running = False
            if refresh_seq != self._refresh_all_seq:
                if not quiet:
                    self.log("Старое обновление проигнорировано.")
                return
            if error:
                self.log(f"Ошибка обновления: {error}")
                return
            self.render_summary(result["total"], result["expected"], result["cash"], result["blocked"], result["var_margin"], result["risk_pct"])
            self.render_portfolio(result["rows"])
            self.render_open_positions(result["rows"])
            self.render_active_trades()
            self.render_stop_orders(result["stops"])
            self.sync_account_header_controls()
            skipped = result.get("skipped_accounts") or []
            if skipped:
                self.log("Некоторые счета пропущены: " + " | ".join(skipped[:3]))
            if not quiet:
                self.log(f"Данные обновлены: {len(selected_snapshot) - len(skipped)}/{len(selected_snapshot)} сч.")

        self.run_async(task, done)

    def refresh_current_ticker_position_async(self):
        ticker = self.ticker_var.get().strip().upper()
        if not ticker:
            self.current_ticker_status_var.set("Позиция по тикеру: введи тикер")
            return

        account_ids = self.get_selected_account_ids()
        if not account_ids:
            return

        def task():
            account_payloads = []
            total = Decimal("0")
            for account_id in account_ids:
                portfolio = get_portfolio(account_id)
                stops = self.enrich_stop_orders(get_stop_orders(account_id), account_id)
                account_total = money_to_decimal(portfolio.get("totalAmountPortfolio"))
                total += account_total
                account_payloads.append((account_id, portfolio, stops, account_total))

            all_rows = []
            for account_id, portfolio, stops, account_total in account_payloads:
                all_rows.extend(self.build_portfolio_rows(
                    portfolio,
                    account_id,
                    self.account_short_label(account_id),
                    account_total,
                    stops,
                    total,
                ))
            rows = self.aggregate_position_rows(all_rows, total)
            for row in rows:
                if row["ticker"].upper() == ticker:
                    return row
            return None

        def done(row, error):
            if error:
                self.current_ticker_status_var.set(f"Ошибка: {error}")
                return
            if not row:
                self.current_ticker_status_var.set(f"Позиция {ticker}: нет открытой позиции")
                return
            self.current_ticker_status_var.set(
                f"Позиция {row['ticker']}: {row['side_text']} | счёт={row['account_label']} | "
                f"qty={row['qty']} | lots={row['qty_lots']} | avg={row['avg']} | "
                f"current={row['last']} | прибыль={row['pnl']} ({row['pnl_pct']}) | риск={row['risk_pct']}"
            )

        self.run_async(task, done)

    def get_cached_instrument_for_position(self, position: dict) -> dict:
        uid = position.get("instrumentUid") or position.get("instrumentId") or position.get("positionUid") or ""
        figi = position.get("figi") or ""
        key = uid or figi
        if key and key in self.instrument_cache:
            return self.instrument_cache[key]

        inst = {}
        try:
            if uid:
                inst = get_instrument_full_by_uid(uid)
            elif figi:
                inst = get_instrument_full_by_figi(figi)
        except Exception:
            inst = {}
        if key:
            self.instrument_cache[key] = inst or {}
        return inst or {}

    def section_for_type(self, instrument_type: str) -> str:
        t = str(instrument_type).lower()
        if "currency" in t:
            return "Валюта"
        if "future" in t:
            return "Фьючерсы"
        if "option" in t:
            return "Опционы"
        if "bond" in t:
            return "Облигации"
        if "share" in t or "stock" in t:
            return "Акции"
        if "etf" in t:
            return "Фонды"
        return instrument_type or "Другое"

    def decimal_from_any_money(self, value) -> Decimal:
        if isinstance(value, dict):
            return money_to_decimal(value)
        if isinstance(value, (int, float, str, Decimal)):
            try:
                return Decimal(str(value))
            except Exception:
                return Decimal("0")
        return Decimal("0")

    def decimal_from_instrument_fields(self, primary: dict, secondary: dict, names: list[str]) -> Decimal:
        """Read Decimal/MoneyValue fields from T-Invest instrument payloads."""
        for container in (primary or {}, secondary or {}):
            if not isinstance(container, dict):
                continue
            for name in names:
                if name in container and container.get(name) not in (None, ""):
                    value = self.decimal_from_any_money(container.get(name))
                    if value > 0:
                        return value
        return Decimal("0")

    def is_futures_context(self, inst: dict, full: dict) -> bool:
        class_code = str((inst or {}).get("classCode") or (full or {}).get("classCode") or "").upper()
        instrument_type = str((inst or {}).get("instrumentType") or (full or {}).get("instrumentType") or "").lower()
        return class_code == "SPBFUT" or "future" in instrument_type or "futures" in instrument_type

    def get_futures_margin_per_contract(self, inst: dict, full: dict, side: str) -> Decimal:
        """Best-effort initial margin per futures contract.

        The ruble PnL/risk still comes from point_value * price_distance * contracts.
        Margin is used for futures exposure/leverage display and position-based risk budget.
        """
        side = str(side or "BUY").upper()
        if side == "SELL":
            names = [
                "initialMarginOnSell", "initial_margin_on_sell",
                "shortMargin", "short_margin",
                "dshortMin", "dshort_min",
                "dshort", "dshortMinValue",
                "sellMargin", "sell_margin",
                "initialMargin", "initial_margin",
            ]
        else:
            names = [
                "initialMarginOnBuy", "initial_margin_on_buy",
                "longMargin", "long_margin",
                "dlongMin", "dlong_min",
                "dlong", "dlongMinValue",
                "buyMargin", "buy_margin",
                "initialMargin", "initial_margin",
            ]
        margin = self.decimal_from_instrument_fields(full, inst, names)
        return margin if margin > 0 else Decimal("0")

    def futures_leverage_info(
        self,
        entry: Decimal,
        qty: int,
        point_value: Decimal,
        margin_per_contract: Decimal | None,
    ) -> dict:
        """Return notional only. ГО/leverage is deliberately not used by SL/TP logic."""
        notional = calc_position_value(entry, qty, point_value)
        return {
            "notional_value": notional,
            "margin_value": Decimal("0"),
            "margin_per_contract": Decimal("0"),
            "leverage": Decimal("0"),
        }

    def get_trade_instrument_context(self, ticker: str) -> dict:
        """Resolve instrument metadata once and reuse it.

        Futures like IMOEXF can make FindInstrument/GetInstrumentBy feel slow if we call
        them on every preview tick. This cache keeps the order form responsive while
        still refreshing periodically.
        """
        raw = str(ticker or "").strip().upper()
        if not raw:
            die("Тикер пустой.")
        key = raw
        now = time.time()
        cached = getattr(self, "trade_instrument_context_cache", {}).get(key)
        if cached:
            cached_at, payload = cached
            if now - float(cached_at or 0.0) <= TRADE_INSTRUMENT_CONTEXT_TTL_SECONDS:
                return dict(payload)

        inst = find_instrument(raw)
        instrument_id = get_instrument_id(inst)
        full = get_instrument_full_by_uid(instrument_id)
        step = get_min_step(inst, full)
        point_value = get_price_point_value(inst, full, step)
        class_code = inst.get("classCode", "") or full.get("classCode", "")
        is_futures = self.is_futures_context(inst, full)
        # Do not use broker ГО/leverage in order-form risk math. Futures PnL is
        # calculated through minPriceIncrement/minPriceIncrementAmount => point_value.
        buy_margin_per_contract = Decimal("0")
        sell_margin_per_contract = Decimal("0")
        tick_value = point_value * step if point_value > 0 and step > 0 else Decimal("0")

        payload = {
            "ticker": inst.get("ticker", raw),
            "inst": inst,
            "full": full,
            "instrument_id": instrument_id,
            "class_code": class_code,
            "step": step,
            "point_value": point_value,
            "tick_value": tick_value,
            "is_futures": is_futures,
            "buy_margin_per_contract": buy_margin_per_contract,
            "sell_margin_per_contract": sell_margin_per_contract,
        }
        self.trade_instrument_context_cache[key] = (now, dict(payload))
        return payload

    def is_plausible_protection_price(self, price, reference_price) -> bool:
        try:
            p = Decimal(str(price))
            ref = Decimal(str(reference_price))
        except Exception:
            return False
        if p <= 0 or ref <= 0:
            return False
        # Filters out broker/API parsing mistakes such as showing qty=1 as SL on an index future.
        ratio = p / ref
        return Decimal("0.05") <= ratio <= Decimal("20")

    def clean_protection_prices(self, side: str, reference_price: Decimal, tp_price, sl_price) -> tuple:
        side = str(side or "").upper()
        ref = Decimal(str(reference_price or "0"))
        tp = tp_price if isinstance(tp_price, Decimal) else None
        sl = sl_price if isinstance(sl_price, Decimal) else None

        if tp is not None:
            if not self.is_plausible_protection_price(tp, ref):
                tp = None
            elif side == "BUY" and tp <= ref:
                tp = None
            elif side == "SELL" and tp >= ref:
                tp = None

        if sl is not None:
            if not self.is_plausible_protection_price(sl, ref):
                sl = None
            elif side == "BUY" and sl >= ref:
                sl = None
            elif side == "SELL" and sl <= ref:
                sl = None

        return tp, sl

    def resolve_leg_instrument_id_for_order(self, leg: dict) -> str:
        """Prefer real instrument uid over position uid when posting protection orders."""
        instrument_uid = str(leg.get("instrument_uid") or "").strip()
        if instrument_uid:
            return instrument_uid
        uid = str(leg.get("uid") or "").strip()
        position_uid = str(leg.get("position_uid") or "").strip()
        if uid and uid != position_uid:
            return uid
        figi = str(leg.get("figi") or "").strip()
        if figi:
            return figi
        return str(leg.get("instrument_id") or "").strip()

    def sum_money_items(self, items) -> Decimal:
        total = Decimal("0")
        if not items:
            return total
        if isinstance(items, dict):
            return self.decimal_from_any_money(items)
        if isinstance(items, list):
            for item in items:
                total += self.decimal_from_any_money(item)
        return total

    def extract_var_margin(self, portfolio: dict) -> Decimal:
        direct = self.decimal_from_any_money(portfolio.get("varMargin") or portfolio.get("var_margin"))
        if direct:
            return direct
        total = Decimal("0")
        for position in portfolio.get("positions", []):
            total += self.decimal_from_any_money(position.get("varMargin") or position.get("var_margin"))
        return total

    def stop_field(self, stop: dict, *names, default=None):
        for name in names:
            if name in stop and stop.get(name) not in (None, ""):
                return stop.get(name)
        return default

    def normalize_stop_order(self, stop: dict, account_id: str | None = None) -> dict:
        normalized = dict(stop)
        if account_id:
            normalized["_account_id"] = account_id
            normalized["_account_label"] = self.account_short_label(account_id)

        instrument_uid = self.stop_field(normalized, "instrumentUid", "instrumentId", "instrument_uid", "uid", default="")
        figi = self.stop_field(normalized, "figi", default="")
        ticker = str(self.stop_field(normalized, "ticker", default="") or "").upper()

        inst = {}
        cache_key = str(instrument_uid or figi or "")
        if cache_key and cache_key in self.instrument_cache:
            inst = self.instrument_cache.get(cache_key) or {}
        else:
            try:
                if instrument_uid:
                    inst = get_instrument_full_by_uid(str(instrument_uid))
                elif figi:
                    inst = get_instrument_full_by_figi(str(figi))
            except Exception:
                inst = {}
            if cache_key:
                self.instrument_cache[cache_key] = inst or {}

        if not ticker:
            ticker = str(inst.get("ticker") or "").upper()
        if not figi:
            figi = inst.get("figi") or ""
        if not instrument_uid:
            instrument_uid = inst.get("uid") or inst.get("instrumentUid") or ""

        normalized["_ticker"] = ticker
        normalized["_figi"] = str(figi or "")
        normalized["_instrument_uid"] = str(instrument_uid or "")
        normalized["_stop_id"] = self.stop_field(normalized, "stopOrderId", "stop_order_id", "orderId", "order_id", default="") or ""
        normalized["_stop_type"] = str(self.stop_field(normalized, "stopOrderType", "stop_order_type", default="") or "").upper()
        normalized["_direction"] = str(self.stop_field(normalized, "direction", default="") or "").upper()
        normalized["_stop_price"] = self.stop_price_value(normalized)
        return normalized

    def enrich_stop_orders(self, stops: list[dict], account_id: str) -> list[dict]:
        return [self.normalize_stop_order(stop, account_id) for stop in (stops or [])]

    def stop_price_value(self, stop: dict) -> Decimal:
        raw = self.stop_field(stop, "stopPrice", "stop_price", "activationPrice", "activation_price", "price")
        return q_to_decimal(raw) if isinstance(raw, dict) else self.decimal_from_any_money(raw)

    def stop_matches_position(self, stop: dict, row_ids: set[str], ticker: str) -> bool:
        stop_ids = {
            str(stop.get("_instrument_uid") or ""),
            str(stop.get("_figi") or ""),
            str(stop.get("instrumentUid") or ""),
            str(stop.get("instrumentId") or ""),
            str(stop.get("positionUid") or ""),
            str(stop.get("figi") or ""),
            str(stop.get("uid") or ""),
        }
        stop_ids = {x for x in stop_ids if x and x != "None"}
        clean_row_ids = {str(x) for x in row_ids if x and str(x) != "None"}
        if clean_row_ids and stop_ids and clean_row_ids.intersection(stop_ids):
            return True
        stop_ticker = str(stop.get("_ticker") or stop.get("ticker") or "").upper()
        return bool(stop_ticker and ticker and stop_ticker == ticker.upper())

    def classify_stop_kind(self, stop: dict, side: str, reference_price: Decimal) -> str | None:
        stop_type = str(stop.get("_stop_type") or stop.get("stopOrderType") or stop.get("stop_order_type") or "").upper()
        if "TAKE_PROFIT" in stop_type:
            return "tp"
        if "STOP_LOSS" in stop_type or "STOP_LIMIT" in stop_type or stop_type.endswith("STOP"):
            return "sl"

        # Fallback for broker responses that omit/rename stopOrderType:
        # classify by stop location relative to current/average price.
        price = stop.get("_stop_price") if isinstance(stop.get("_stop_price"), Decimal) else self.stop_price_value(stop)
        if price <= 0 or reference_price <= 0:
            return None
        if side == "BUY":
            return "tp" if price > reference_price else "sl"
        if side == "SELL":
            return "tp" if price < reference_price else "sl"
        return None

    def protection_from_broker_stops(self, account_id: str, ticker: str, side: str, row_ids: set[str], stops: list[dict], reference_price: Decimal) -> dict:
        result = {
            "tp_price": None,
            "sl_price": None,
            "tp_stop_id": "",
            "sl_stop_id": "",
            "source": "",
        }
        tp_candidates = []
        sl_candidates = []

        for raw_stop in stops or []:
            stop = raw_stop if raw_stop.get("_stop_price") is not None else self.normalize_stop_order(raw_stop, account_id)
            if stop.get("_account_id") and stop.get("_account_id") != account_id:
                continue
            if not self.stop_matches_position(stop, row_ids, ticker):
                continue
            price = stop.get("_stop_price") if isinstance(stop.get("_stop_price"), Decimal) else self.stop_price_value(stop)
            if price <= 0:
                continue
            stop_id = str(stop.get("_stop_id") or stop.get("stopOrderId") or stop.get("stop_order_id") or "")
            kind = self.classify_stop_kind(stop, side, reference_price)
            if kind == "tp":
                tp_candidates.append((price, stop_id))
            elif kind == "sl":
                sl_candidates.append((price, stop_id))

        if tp_candidates:
            # nearest TP to the current price is the one that matters first
            tp_candidates.sort(key=lambda item: abs(item[0] - reference_price))
            result["tp_price"], result["tp_stop_id"] = tp_candidates[0]
            result["source"] = "broker"
        if sl_candidates:
            # nearest SL to the current price is the one that matters first
            sl_candidates.sort(key=lambda item: abs(item[0] - reference_price))
            result["sl_price"], result["sl_stop_id"] = sl_candidates[0]
            result["source"] = "broker"
        return result

    def merge_protection(self, active_trade: dict | None, broker_protection: dict) -> dict:
        result = dict(broker_protection or {})
        if active_trade:
            try:
                if active_trade.get("tp_price") and active_trade.get("tp_price") != "—":
                    result["tp_price"] = Decimal(str(active_trade.get("tp_price")))
                    result["tp_stop_id"] = active_trade.get("tp_stop_id", "")
                    result["source"] = "app"
            except Exception:
                pass
            try:
                if active_trade.get("sl_price") and active_trade.get("sl_price") != "—":
                    result["sl_price"] = Decimal(str(active_trade.get("sl_price")))
                    result["sl_stop_id"] = active_trade.get("sl_stop_id", "")
                    result["source"] = "app"
            except Exception:
                pass
        return result

    def calc_current_drawdown_risk(self, pnl: Decimal, selected_capital_total: Decimal) -> tuple[Decimal, Decimal]:
        try:
            drawdown = -pnl if pnl < 0 else Decimal("0")
            risk_pct = (drawdown / selected_capital_total * Decimal("100")) if selected_capital_total else Decimal("0")
            return drawdown, risk_pct
        except Exception:
            return Decimal("0"), Decimal("0")

    def build_portfolio_rows(self, portfolio: dict, account_id: str, account_label: str, account_total: Decimal, stops: list[dict] | None = None, selected_capital_total: Decimal | None = None) -> list[dict]:
        rows = []
        for position in portfolio.get("positions", []):
            qty = q_to_decimal(position.get("quantity"))
            if qty == 0:
                continue

            inst = self.get_cached_instrument_for_position(position)
            figi = position.get("figi", "")
            instrument_uid = position.get("instrumentUid") or position.get("instrumentId") or inst.get("uid", "")
            position_uid = position.get("positionUid") or ""
            uid = instrument_uid or position_uid or ""
            ticker = inst.get("ticker") or figi or uid or "—"
            name = inst.get("name") or inst.get("title") or "—"
            class_code = inst.get("classCode") or position.get("classCode", "")
            instrument_type = position.get("instrumentType") or inst.get("instrumentType") or ""
            qty_lots = q_to_decimal(position.get("quantityLots")) if position.get("quantityLots") else qty
            avg = money_to_decimal(position.get("averagePositionPrice"))
            last = money_to_decimal(position.get("currentPrice"))
            pnl = money_to_decimal(position.get("expectedYield"))
            var_margin = self.decimal_from_any_money(position.get("varMargin") or position.get("var_margin"))
            value = last * qty
            base = abs(avg * qty) if avg and qty else Decimal("0")
            pnl_pct = (pnl / base * Decimal("100")) if base else Decimal("0")
            side = position_side_from_qty(qty)
            row_ids = {str(x) for x in (instrument_uid, position_uid, figi, uid) if x}
            active_trade = find_active_trade(account_id, ticker=ticker, side=None, instrument_id=uid) or find_active_trade(account_id, ticker=ticker, side=None, instrument_id=instrument_uid)
            reference_price = last if last > 0 else avg
            risk_capital = selected_capital_total if selected_capital_total is not None else account_total
            broker_protection = self.protection_from_broker_stops(account_id, ticker, side, row_ids, stops or [], reference_price)
            protection = self.merge_protection(active_trade, broker_protection)
            tp_price = protection.get("tp_price")
            sl_price = protection.get("sl_price")
            tp_price, sl_price = self.clean_protection_prices(side, reference_price, tp_price, sl_price)
            risk, risk_pct = self.calc_current_drawdown_risk(pnl, risk_capital)

            row = {
                "account_id": account_id,
                "account_label": account_label,
                "account_total_raw": account_total,
                "section": self.section_for_type(instrument_type),
                "instrument_type": instrument_type,
                "ticker": ticker,
                "name": name,
                "qty": qty,
                "qty_lots": qty_lots,
                "avg_raw": avg,
                "last_raw": last,
                "value_raw": value,
                "pnl_raw": pnl,
                "pnl_pct_raw": pnl_pct,
                "var_margin_raw": var_margin,
                "risk_raw": risk,
                "risk_pct_raw": risk_pct,
                "side": side,
                "side_text": side_to_text(side) if side in {"BUY", "SELL"} else "FLAT",
                "avg": fmt_dec(avg),
                "last": fmt_dec(last),
                "value": fmt_money(value),
                "pnl": signed_text(pnl),
                "pnl_pct": fmt_percent(pnl_pct),
                "var_margin": signed_text(var_margin),
                "risk": "",
                "risk_pct": fmt_percent(risk_pct),
                "figi": figi,
                "uid": uid,
                "instrument_uid": instrument_uid,
                "position_uid": position_uid,
                "instrument_id": uid or figi,
                "class_code": class_code,
                "tp": fmt_dec(tp_price) if isinstance(tp_price, Decimal) and tp_price > 0 else "—",
                "sl": fmt_dec(sl_price) if isinstance(sl_price, Decimal) and sl_price > 0 else "—",
                "tp_stop_id": protection.get("tp_stop_id", ""),
                "sl_stop_id": protection.get("sl_stop_id", ""),
                "protection_source": protection.get("source", ""),
                "active_trade": active_trade,
                "legs": [],
            }
            row["legs"] = [row]
            rows.append(row)
        return rows

    def aggregate_position_rows(self, rows: list[dict], selected_capital_total: Decimal | None = None) -> list[dict]:
        grouped: dict[tuple[str, str], list[dict]] = {}
        passthrough = []
        for row in rows:
            key = (row.get("instrument_id") or row.get("ticker"), row.get("side"))
            if row.get("side") in {"BUY", "SELL"}:
                grouped.setdefault(key, []).append(row)
            else:
                passthrough.append(row)

        result = []
        for legs in grouped.values():
            result.append(self.aggregate_leg_group(legs, selected_capital_total))
        result.extend(passthrough)
        result.sort(key=lambda r: (r.get("section", ""), r.get("ticker", ""), r.get("account_label", "")))
        return result

    def aggregate_leg_group(self, legs: list[dict], selected_capital_total: Decimal | None = None) -> dict:
        if len(legs) == 1:
            row = dict(legs[0])
            row["legs"] = legs
            return row

        first = legs[0]
        qty = sum((leg["qty"] for leg in legs), Decimal("0"))
        qty_lots = sum((leg["qty_lots"] for leg in legs), Decimal("0"))
        value = sum((leg["value_raw"] for leg in legs), Decimal("0"))
        pnl = sum((leg["pnl_raw"] for leg in legs), Decimal("0"))
        weight = sum((abs(leg["qty"]) for leg in legs), Decimal("0"))
        avg = (sum((leg["avg_raw"] * abs(leg["qty"]) for leg in legs), Decimal("0")) / weight) if weight else Decimal("0")
        last = (sum((leg["last_raw"] * abs(leg["qty"]) for leg in legs), Decimal("0")) / weight) if weight else Decimal("0")
        base = sum((abs(leg["avg_raw"] * leg["qty"]) for leg in legs), Decimal("0"))
        pnl_pct = (pnl / base * Decimal("100")) if base else Decimal("0")
        var_margin = sum(((leg.get("var_margin_raw") or Decimal("0")) for leg in legs), Decimal("0"))
        risk = sum(((leg.get("risk_raw") or Decimal("0")) for leg in legs), Decimal("0"))
        account_total = selected_capital_total if selected_capital_total is not None else sum({leg["account_id"]: leg.get("account_total_raw", Decimal("0")) for leg in legs}.values(), Decimal("0"))
        risk_pct = (risk / account_total * Decimal("100")) if account_total else Decimal("0")
        account_labels = []
        for leg in legs:
            if leg["account_label"] not in account_labels:
                account_labels.append(leg["account_label"])

        tp_values = {str(leg.get("tp", "—")) for leg in legs}
        sl_values = {str(leg.get("sl", "—")) for leg in legs}
        tp = next(iter(tp_values)) if len(tp_values) == 1 else "mixed"
        sl = next(iter(sl_values)) if len(sl_values) == 1 else "mixed"
        tp_stop_ids = [leg.get("tp_stop_id", "") for leg in legs if leg.get("tp_stop_id")]
        sl_stop_ids = [leg.get("sl_stop_id", "") for leg in legs if leg.get("sl_stop_id")]

        row = dict(first)
        row.update({
            "account_id": "AGGREGATED",
            "account_label": " + ".join(account_labels),
            "account_total_raw": account_total,
            "qty": qty,
            "qty_lots": qty_lots,
            "avg_raw": avg,
            "last_raw": last,
            "value_raw": value,
            "pnl_raw": pnl,
            "pnl_pct_raw": pnl_pct,
            "var_margin_raw": var_margin,
            "risk_raw": risk if risk else None,
            "risk_pct_raw": risk_pct,
            "avg": fmt_dec(avg),
            "last": fmt_dec(last),
            "value": fmt_money(value),
            "pnl": signed_text(pnl),
            "pnl_pct": fmt_percent(pnl_pct),
            "var_margin": signed_text(var_margin),
            "risk": "",
            "risk_pct": fmt_percent(risk_pct),
            "tp": tp,
            "sl": sl,
            "tp_stop_id": ",".join(tp_stop_ids),
            "sl_stop_id": ",".join(sl_stop_ids),
            "protection_source": "mixed" if tp_stop_ids or sl_stop_ids else "",
            "active_trade": None,
            "legs": legs,
        })
        return row

    def extract_cash(self, positions: dict, withdraw_limits: dict | None = None) -> tuple[Decimal, Decimal]:
        withdraw_limits = withdraw_limits or {}
        cash = self.sum_money_items(positions.get("money"))
        blocked = Decimal("0")
        blocked += self.sum_money_items(positions.get("blocked"))
        blocked += self.sum_money_items(withdraw_limits.get("blocked"))
        blocked += self.sum_money_items(withdraw_limits.get("blockedGuarantee"))
        blocked += self.sum_money_items(withdraw_limits.get("blocked_guarantee"))
        return cash, blocked

    def get_account_margin_base(self, account_id: str) -> Decimal:
        portfolio = get_portfolio(account_id)
        positions = get_positions(account_id)
        try:
            withdraw_limits = get_withdraw_limits(account_id)
        except Exception:
            withdraw_limits = {}
        total = money_to_decimal(portfolio.get("totalAmountPortfolio"))
        _cash_rub, blocked = self.extract_cash(positions, withdraw_limits)
        return max(total - blocked, Decimal("0"))

    def get_selected_margin_base(self, account_ids: list[str]) -> Decimal:
        total = Decimal("0")
        skipped = []
        for account_id in account_ids:
            try:
                total += self.get_account_margin_base(account_id)
            except Exception as exc:
                skipped.append(f"{self.account_short_label(account_id)}: {exc}")
        self._last_margin_base_skipped = skipped
        return total

    def get_available_lots_for_accounts(self, account_ids: list[str], instrument_id: str, price: Decimal, side: str) -> tuple[int | None, dict[str, int | None]]:
        total = Decimal("0")
        details: dict[str, int | None] = {}
        found_any = False
        for account_id in account_ids:
            try:
                data = get_max_lots(account_id, instrument_id, price)
                value = get_available_lots_from_max_lots(data, side)
            except Exception:
                value = None
            details[account_id] = value
            if value is not None:
                total += Decimal(value)
                found_any = True
        return (int(total) if found_any else None), details

    def set_card_value_style(self, label: str, style_name: str) -> None:
        try:
            widget = getattr(self, "card_value_labels", {}).get(label)
            if widget is not None:
                widget.configure(style=style_name)
        except Exception:
            pass

    def row_profit_risk_tag(self, row: dict) -> str:
        try:
            risk_pct = row.get("risk_pct_raw") or Decimal("0")
            if risk_pct > 0:
                return "negative"
        except Exception:
            pass
        try:
            pnl = row.get("pnl_raw") or Decimal("0")
            if pnl > 0:
                return "positive"
            if pnl < 0:
                return "negative"
        except Exception:
            pass
        return "muted"

    def render_summary(self, total: Decimal, expected: Decimal, cash: Decimal, blocked: Decimal, var_margin: Decimal, risk_pct: Decimal):
        self.total_var.set(fmt_money(total))
        self.expected_var.set(signed_text(expected))
        self.cash_var.set(fmt_money(cash))
        self.blocked_var.set(fmt_money(blocked))
        self.var_margin_var.set(signed_text(var_margin))
        self.risk_var.set(fmt_percent(risk_pct))

        # The portfolio PnL card is the current ruble profit/loss from the broker.
        if expected > 0:
            self.set_card_value_style("Прибыль портфеля ₽", "Green.TLabel")
        elif expected < 0:
            self.set_card_value_style("Прибыль портфеля ₽", "Red.TLabel")
        else:
            self.set_card_value_style("Прибыль портфеля ₽", "CardValue.TLabel")

        # Current risk in this table means current drawdown: max(-PnL, 0) / selected capital.
        # If it is above zero, highlight the risk card in red.
        if risk_pct > 0:
            self.set_card_value_style("Риск текущий, % капитала", "Red.TLabel")
        else:
            self.set_card_value_style("Риск текущий, % капитала", "CardValue.TLabel")

    def clear_tree(self, tree: ttk.Treeview):
        for item in tree.get_children():
            tree.delete(item)

    def row_state_text(self, row: dict) -> str:
        legs = row.get("legs") or [row]
        protected = 0
        for leg in legs:
            has_ids = bool(leg.get("tp_stop_id") or leg.get("sl_stop_id"))
            trade = leg.get("active_trade") or find_active_trade(leg["account_id"], ticker=leg["ticker"], side=leg["side"], instrument_id=leg["instrument_id"])
            if has_ids or (trade and (trade.get("tp_stop_id") or trade.get("sl_stop_id"))):
                protected += 1
        if protected == len(legs):
            return "TP/SL есть"
        if protected == 0:
            return "без TP/SL"
        return "частично защищено"

    def render_portfolio(self, rows: list[dict]):
        self.portfolio_rows.clear()
        self.clear_tree(self.portfolio_tree)
        for idx, row in enumerate(rows):
            iid = f"portfolio_{idx}"
            tag = self.row_profit_risk_tag(row)
            self.portfolio_rows[iid] = row
            self.portfolio_tree.insert("", "end", iid=iid, values=(
                row["account_label"], row["section"], row["ticker"], row["name"], fmt_dec(row["qty"], 4), fmt_dec(row["qty_lots"], 4),
                row["avg"], row["last"], row["value"], row["pnl"], row["pnl_pct"], row["var_margin"], row["risk_pct"], row["figi"] or row["uid"],
            ), tags=(tag,))

    def render_open_positions(self, rows: list[dict]):
        self.open_positions_rows.clear()
        self.clear_tree(self.open_positions_tree)
        self.clear_tree(self.trade_positions_tree)

        open_rows = [r for r in rows if r["side"] in {"BUY", "SELL"} and str(r["instrument_type"]).lower() != "currency"]
        for idx, row in enumerate(open_rows):
            iid = f"open_{idx}"
            state = self.row_state_text(row)
            tag = self.row_profit_risk_tag(row)
            values = (
                row["account_label"], row["ticker"], row["side_text"], fmt_dec(row["qty"], 4), fmt_dec(row["qty_lots"], 4),
                row["avg"], row["last"], row["pnl"], row["pnl_pct"], row["var_margin"], row["risk_pct"], row["tp"], row["sl"], state,
            )
            self.open_positions_rows[iid] = row
            self.open_positions_tree.insert("", "end", iid=iid, values=values, tags=(tag,))
            self.trade_positions_tree.insert("", "end", iid=iid, values=values[:-1], tags=(tag,))

    def render_active_trades(self):
        self.active_trade_rows.clear()
        self.clear_tree(self.active_trades_tree)
        selected_ids = set(self.get_selected_account_ids())
        state = load_state()
        for idx, trade in enumerate(state.get("active_trades", [])):
            if trade.get("account_id") not in selected_ids:
                continue
            iid = f"active_{idx}"
            self.active_trade_rows[iid] = trade
            self.active_trades_tree.insert("", "end", iid=iid, values=(
                self.account_short_label(trade.get("account_id")),
                trade.get("created_at", "—"),
                trade.get("ticker", "—"),
                side_to_text(trade.get("side", "BUY")),
                trade.get("qty", "—"),
                trade.get("entry_price", "—"),
                trade.get("tp_price", "—"),
                trade.get("sl_price", "—"),
                trade.get("tp_stop_id", "—"),
                trade.get("sl_stop_id", "—"),
            ))

    def render_stop_orders(self, stops: list[dict]):
        self.stop_order_rows.clear()
        self.clear_tree(self.stop_orders_tree)
        for idx, stop in enumerate(stops):
            iid = f"stop_{idx}"
            self.stop_order_rows[iid] = stop
            self.stop_orders_tree.insert("", "end", iid=iid, values=(
                stop.get("_account_label", "—"),
                stop.get("_ticker") or stop.get("ticker") or stop.get("figi") or stop.get("instrumentUid") or "—",
                stop.get("_stop_type") or stop.get("stopOrderType", "—"),
                stop.get("_direction") or stop.get("direction", "—"),
                stop.get("lotsRequested") or stop.get("quantity") or "—",
                fmt_dec(q_to_decimal(stop.get("price"))),
                fmt_dec(stop.get("_stop_price") if isinstance(stop.get("_stop_price"), Decimal) else self.stop_price_value(stop)),
                stop.get("_stop_id") or stop.get("stopOrderId", "—"),
            ))

    # ---------------------------- Selection and management ----------------------------

    def on_any_position_selected(self, event=None):
        row = self.get_selected_position_row()
        if not row:
            self.manage_selected_var.set("Выбрано: —")
            return
        self.manage_selected_var.set(
            f"Выбрано: {row['ticker']} {row['side_text']} | счёт={row['account_label']} | "
            f"lots={fmt_dec(row['qty_lots'], 4)} | avg={row['avg']} | PnL={row['pnl']} | риск={row['risk_pct']}"
        )
        if row.get("sl") and row["sl"] not in {"—", "mixed"}:
            self.manage_sl_var.set(str(row["sl"]))
        if row.get("tp") and row["tp"] not in {"—", "mixed"}:
            self.manage_tp_var.set(str(row["tp"]))

    def get_selected_position_row(self) -> dict | None:
        if hasattr(self, "open_positions_tree"):
            selected = self.open_positions_tree.selection()
            if selected and selected[0] in self.open_positions_rows:
                return self.open_positions_rows[selected[0]]
        if hasattr(self, "trade_positions_tree"):
            selected = self.trade_positions_tree.selection()
            if selected and selected[0] in self.open_positions_rows:
                return self.open_positions_rows[selected[0]]
        return None

    def set_risk_model(self, model: str):
        if model not in {"portfolio", "position"}:
            return
        self.risk_model_var.set(model)
        self.refresh_risk_mode_buttons()
        self.schedule_trade_preview_refresh()

    def refresh_risk_mode_buttons(self):
        model = self.get_risk_model()
        if hasattr(self, "risk_portfolio_button"):
            self.risk_portfolio_button.configure(style="RiskActive.TButton" if model == "portfolio" else "RiskInactive.TButton")
        if hasattr(self, "risk_position_button"):
            self.risk_position_button.configure(style="RiskActive.TButton" if model == "position" else "RiskInactive.TButton")

    def get_risk_model(self) -> str:
        try:
            model = self.risk_model_var.get().strip().lower()
        except Exception:
            model = "position"
        return model if model in {"portfolio", "position"} else "position"

    def get_risk_model_label(self, model: str | None = None) -> str:
        model = model or self.get_risk_model()
        return "портфеля" if model == "portfolio" else "позиции"

    def effective_intraday_risk_percent(self, risk_percent: Decimal | None = None) -> Decimal:
        """Use a hard intraday cap for auto SL and recommended quantity."""
        try:
            requested = Decimal(str(risk_percent if risk_percent is not None else INTRADAY_RISK_PERCENT_CAP))
        except Exception:
            requested = INTRADAY_RISK_PERCENT_CAP
        if requested <= 0:
            requested = INTRADAY_RISK_PERCENT_CAP
        return min(requested, INTRADAY_RISK_PERCENT_CAP)

    def calculate_risk_budget(
        self,
        entry: Decimal,
        qty: int,
        point_value: Decimal,
        selected_capital: Decimal,
        risk_percent: Decimal | None = None,
        margin_per_contract: Decimal | None = None,
    ) -> tuple[Decimal, Decimal, str, str]:
        """Calculate money risk budget without using futures ГО/leverage.

        For futures the protective bracket must be based on the real PnL formula:
        price distance * contracts * point_value. ГО/leverage is broker/margin
        metadata and can be absent, stale, or parsed differently. Using it to
        build SL/TP makes protection unstable, so the optional margin argument is
        intentionally ignored here.
        """
        position_value = calc_position_value(entry, qty, point_value)

        model = self.get_risk_model()
        percent = self.effective_intraday_risk_percent(risk_percent)
        risk_fraction = percent / Decimal("100")
        if model == "portfolio":
            risk_budget = selected_capital * risk_fraction
            if risk_budget <= 0:
                die("Не смог рассчитать риск от портфеля: маржинальная база выбранных счетов нулевая.")
        else:
            risk_budget = position_value * risk_fraction
            if risk_budget <= 0:
                die("Не смог рассчитать риск от суммы позиции.")
        return position_value, risk_budget, model, self.get_risk_model_label(model)

    def current_trade_side_label(self) -> str:
        side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        return "LONG" if side == "BUY" else "SHORT"

    def _selected_side_text(self, long_text: str, short_text: str) -> str:
        side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        return long_text if side == "BUY" else short_text

    def set_available_preview(self, long_text: str = "L —", short_text: str = "S —", note: str = ""):
        if hasattr(self, "available_long_var"):
            self.available_long_var.set(long_text)
        if hasattr(self, "available_short_var"):
            self.available_short_var.set(short_text)
        selected = self._selected_side_text(long_text, short_text)
        if hasattr(self, "available_selected_var"):
            label = self.current_trade_side_label()
            clean = selected.replace("L ", "").replace("S ", "")
            self.available_selected_var.set(f"Доступно {label}: {clean}")
        if hasattr(self, "available_note_var"):
            self.available_note_var.set(note)
        if hasattr(self, "available_preview_var"):
            self.available_preview_var.set(f"Доступно: {selected} {note}".strip())

    def set_recommended_qty_preview(self, text: str = "Рекомендуемое количество: —"):
        if hasattr(self, "recommended_qty_var"):
            self.recommended_qty_var.set(text)

    def set_position_value_preview(self, long_text: str = "L —", short_text: str = "S —"):
        if hasattr(self, "position_long_var"):
            self.position_long_var.set(long_text)
        if hasattr(self, "position_short_var"):
            self.position_short_var.set(short_text)
        selected = self._selected_side_text(long_text, short_text)
        if hasattr(self, "position_selected_var"):
            label = self.current_trade_side_label()
            clean = selected.replace("L ", "").replace("S ", "")
            self.position_selected_var.set(f"Позиция {label}: {clean}")
        if hasattr(self, "position_value_preview_var"):
            self.position_value_preview_var.set(f"Позиция: {selected}")

    def set_lot_price_preview(self, text: str = "Цена за лот: —"):
        if hasattr(self, "lot_price_var"):
            self.lot_price_var.set(text)

    def set_sl_preview_pair(self, long_text: str = "L —", short_text: str = "S —", note: str = ""):
        if hasattr(self, "sl_long_var"):
            self.sl_long_var.set(long_text)
        if hasattr(self, "sl_short_var"):
            self.sl_short_var.set(short_text)
        selected = self._selected_side_text(long_text, short_text)
        if hasattr(self, "sl_selected_var"):
            label = self.current_trade_side_label()
            clean = selected.replace("L ", "").replace("S ", "")
            self.sl_selected_var.set(f"SL {label}: {clean}")
        if hasattr(self, "sl_note_var"):
            self.sl_note_var.set(note)
        if hasattr(self, "sl_preview_var"):
            self.sl_preview_var.set(f"{selected} {note}".strip())

    def set_tp_preview(self, text: str = "TP: —"):
        if hasattr(self, "tp_preview_var"):
            self.tp_preview_var.set(text)

    def set_trade_risk_reward_preview(self, risk_amount=None, reward_amount=None):
        if hasattr(self, "risk_money_var"):
            if risk_amount is None:
                self.risk_money_var.set("risk: —")
            else:
                try:
                    self.risk_money_var.set(f"risk: -{fmt_money(abs(Decimal(str(risk_amount))))}")
                except Exception:
                    self.risk_money_var.set("risk: —")
        if hasattr(self, "ev_money_var"):
            if reward_amount is None:
                self.ev_money_var.set("ev: —")
            else:
                try:
                    self.ev_money_var.set(f"ev: +{fmt_money(abs(Decimal(str(reward_amount))))}")
                except Exception:
                    self.ev_money_var.set("ev: —")

    def format_available_pair(self, result: dict) -> tuple[str, str, str]:
        if len(self.get_selected_account_ids()) > 1:
            long_text = f"L осн. {result['primary_long_available_text']} / выбр. {result['long_available_text']}"
            short_text = f"S осн. {result['primary_short_available_text']} / выбр. {result['short_available_text']}"
            note = ""
        else:
            long_text = f"L {result['long_available_text']}"
            short_text = f"S {result['short_available_text']}"
            note = ""
        return long_text, short_text, note

    def get_entry_split_settings(self) -> tuple[bool, int | None, str]:
        try:
            enabled = bool(self.entry_split_enabled_var.get())
        except Exception:
            enabled = True

        if not enabled:
            return False, None, "без дробления"

        raw = "auto"
        try:
            raw = str(self.entry_split_count_var.get() or "auto").strip().lower()
        except Exception:
            raw = "auto"

        if raw in {"", "auto", "авто"}:
            return True, None, "auto"

        try:
            count = int(raw)
        except Exception:
            return True, None, "auto"

        if count <= 1:
            return False, None, "без дробления"
        return True, count, f"{count} частей"

    def choose_entry_split_count(self, qty: int, split_enabled: bool | None = None, split_count: int | None = None) -> int:
        """Pick child market order count depending on user split settings and total quantity."""
        try:
            qty_int = int(qty)
        except Exception:
            return 1
        if qty_int <= 1:
            return 1
        if split_enabled is False:
            return 1
        if split_count is not None:
            try:
                count = int(split_count)
            except Exception:
                count = 1
            return max(1, min(qty_int, count))
        for count in ENTRY_SPLIT_PREFERRED_COUNTS:
            if qty_int >= count:
                return int(count)
        return 1

    def split_entry_order_quantities(self, qty: int, split_enabled: bool | None = None, split_count: int | None = None) -> list[int]:
        """Balanced child-order quantities.

        If splitting is disabled, return one child order with the full quantity.
        If a manual split count is selected, use that count capped by total quantity.
        """
        try:
            qty_int = int(qty)
        except Exception:
            return []
        if qty_int <= 0:
            return []
        count = self.choose_entry_split_count(qty_int, split_enabled=split_enabled, split_count=split_count)
        if count <= 1:
            return [qty_int]

        base = qty_int // count
        remainder = qty_int % count
        chunks = []
        for idx in range(count):
            child_qty = base + (1 if idx < remainder else 0)
            if child_qty > 0:
                chunks.append(child_qty)
        return chunks or [qty_int]

    def format_entry_split_quantities(self, quantities: list[int]) -> str:
        values = [int(x) for x in quantities if int(x) > 0]
        if not values:
            return "—"
        groups = []
        for qty in sorted(set(values), reverse=True):
            count = sum(1 for x in values if x == qty)
            groups.append(f"{count}×{qty}")
        return " + ".join(groups)

    def describe_entry_split_for_legs(self, legs: list[dict], split_enabled: bool | None = None, split_count: int | None = None) -> str:
        parts = []
        total_orders = 0
        for leg in legs or []:
            try:
                qty = int(leg.get("qty") or 0)
            except Exception:
                qty = 0
            if qty <= 0:
                continue
            chunks = self.split_entry_order_quantities(qty, split_enabled=split_enabled, split_count=split_count)
            total_orders += len(chunks)
            parts.append(f"{leg.get('account_label', 'счёт')}: {len(chunks)} заявок ({self.format_entry_split_quantities(chunks)})")
        if not parts:
            return "Дробление входа: —\n"
        if split_enabled is False:
            return f"Дробление входа: выключено; {total_orders} заявка(и); " + "; ".join(parts) + "\n"
        delay_ms = max(0.0, ENTRY_CHILD_ORDER_DELAY_SECONDS * 1000.0)
        return f"Дробление входа: {total_orders} заявок, пауза ≈{delay_ms:.1f} мс; " + "; ".join(parts) + "\n"

    def allocate_lots_across_accounts(self, qty: int, availability_by_account: dict[str, int | None]) -> list[dict]:
        remaining = int(qty)
        legs = []
        for account_id in self.get_selected_account_ids():
            available = availability_by_account.get(account_id)
            if available is None:
                continue
            try:
                available_int = int(available)
            except Exception:
                continue
            if available_int <= 0:
                continue
            leg_qty = min(remaining, available_int)
            if leg_qty > 0:
                legs.append({
                    "account_id": account_id,
                    "account_label": self.account_short_label(account_id),
                    "qty": leg_qty,
                    "available": available_int,
                })
                remaining -= leg_qty
            if remaining <= 0:
                break

        if remaining > 0:
            visible_total = sum(int(v) for v in availability_by_account.values() if v is not None and int(v) > 0)
            die(f"Недостаточно доступных контрактов на выбранных счетах: хочешь {qty}, доступно {visible_total}.")
        return legs

    def get_ticker_qty_inputs(self) -> tuple[str, int]:
        ticker = self.ticker_var.get().strip().upper()
        if not ticker:
            die("Введи тикер.")
        try:
            qty = int(self.qty_var.get().strip())
        except ValueError:
            die("Количество должно быть целым числом.")
        if qty <= 0:
            die("Количество должно быть больше нуля.")
        return ticker, qty

    # ---------------------------- Periodic portfolio refresh ----------------------------

    def stop_periodic_portfolio_refresh(self):
        after_id = getattr(self, "_portfolio_periodic_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._portfolio_periodic_after_id = None

    def start_periodic_portfolio_refresh(self, initial_delay_ms: int = PORTFOLIO_AUTO_REFRESH_MS):
        self.stop_periodic_portfolio_refresh()
        self.schedule_periodic_portfolio_refresh(initial_delay_ms)

    def schedule_periodic_portfolio_refresh(self, delay_ms: int = PORTFOLIO_AUTO_REFRESH_MS):
        if not hasattr(self, "root"):
            return
        self._portfolio_periodic_after_id = self.root.after(
            max(3000, int(delay_ms)),
            self.run_periodic_portfolio_refresh,
        )

    def run_periodic_portfolio_refresh(self):
        self._portfolio_periodic_after_id = None
        try:
            if getattr(self, "account_id", None) and self.get_selected_account_ids():
                self.refresh_all_async(quiet=True)
        finally:
            if getattr(self, "account_id", None):
                self.schedule_periodic_portfolio_refresh()

    # ---------------------------- Trade journal statistics ----------------------------

    def trade_journal_db_candidates(self) -> list[Path]:
        candidates = []
        try:
            candidates.append(APP_DIR / "db" / "jtrade_trades.db")
        except Exception:
            pass
        candidates.extend([
            Path.cwd() / "db" / "jtrade_trades.db",
            Path("db") / "jtrade_trades.db",
            Path("jtrade_trades.db"),
        ])
        unique = []
        for path in candidates:
            try:
                resolved = Path(path)
            except Exception:
                continue
            if resolved not in unique:
                unique.append(resolved)
        return unique

    def find_trade_journal_db_path(self) -> Path | None:
        for path in self.trade_journal_db_candidates():
            try:
                if path.exists() and path.is_file():
                    return path
            except Exception:
                continue
        return None

    def trade_stats_period_start(self, period: str) -> datetime | None:
        now = datetime.now()
        raw = str(period or "").strip().lower()
        if raw in {"сегодня", "today"}:
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if "7" in raw:
            return now - timedelta(days=7)
        if "30" in raw:
            return now - timedelta(days=30)
        if "90" in raw:
            return now - timedelta(days=90)
        return None

    def parse_trade_datetime(self, value) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            try:
                number = float(value)
                if number <= 0:
                    return None
                if number > 10_000_000_000:
                    number = number / 1000.0
                return datetime.fromtimestamp(number)
            except Exception:
                return None
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        for candidate in (text, text.replace("T", " ")):
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                return parsed
            except Exception:
                pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y",
        ):
            try:
                return datetime.strptime(text.split(".", 1)[0] if fmt.startswith("%Y") and "." in text else text, fmt)
            except Exception:
                pass
        return None

    def decimal_from_db_value(self, value) -> Decimal | None:
        if value in (None, ""):
            return None
        if isinstance(value, Decimal):
            return value
        try:
            if isinstance(value, (int, float)):
                return Decimal(str(value))
            text = str(value).strip()
            if not text:
                return None
            text = text.replace("\u00a0", " ").replace("₽", "").replace("руб", "").replace("RUB", "")
            text = text.replace(" ", "").replace(",", ".")
            match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
            if not match:
                return None
            return Decimal(match.group(0))
        except Exception:
            return None

    def pick_first_existing_column(self, columns: set[str], names: list[str]) -> str | None:
        lower_to_original = {c.lower(): c for c in columns}
        for name in names:
            found = lower_to_original.get(name.lower())
            if found:
                return found
        return None

    def score_trade_stats_table(self, column_names: list[str]) -> int:
        lowered = {c.lower() for c in column_names}
        score = 0
        # Real JTrade broker operation log. This must beat model_samples,
        # because model_samples has ticker/side/time-like columns but is not real PnL.
        if {"side", "quantity", "payment"}.issubset(lowered):
            score += 100
        if "raw_json" in lowered:
            score += 10
        if "source" in lowered:
            score += 5
        if any(c in lowered for c in {"realized_pnl", "pnl", "profit", "result", "net_pnl", "pnl_net"}):
            score += 5
        if any("pnl" in c or "profit" in c or "result" in c for c in lowered):
            score += 3
        if any(c in lowered for c in {"closed_at", "close_time", "exit_time", "updated_at", "created_at", "opened_at", "timestamp", "time"}):
            score += 3
        if any("commission" in c or "fee" in c for c in lowered):
            score += 2
        if any(c in lowered for c in {"status", "state", "ticker", "symbol", "side"}):
            score += 1
        return score

    def select_trade_stats_table(self, conn: sqlite3.Connection) -> tuple[str, list[str]] | None:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        best = None
        for row in rows:
            table = row[0]
            try:
                info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            except Exception:
                continue
            columns = [item[1] for item in info]
            score = self.score_trade_stats_table(columns)
            if best is None or score > best[0]:
                best = (score, table, columns)
        if not best or best[0] <= 0:
            return None
        return best[1], best[2]

    def row_is_closed_trade(self, row: sqlite3.Row, columns: set[str]) -> bool:
        status_col = self.pick_first_existing_column(columns, ["status", "state", "trade_status"])
        if not status_col:
            return True
        status = str(row[status_col] or "").strip().lower()
        if not status:
            return True
        return status not in {"open", "opened", "active", "running", "pending", "new", "created", "working"}

    def extract_trade_stats_amounts(self, row: sqlite3.Row, columns: set[str]) -> tuple[Decimal | None, Decimal, Decimal | None]:
        net_candidates = [
            "net_pnl", "pnl_net", "realized_pnl_net", "net_profit", "profit_net",
            "result_net", "financial_result_net", "pnl_after_commission", "profit_after_commission",
        ]
        gross_candidates = [
            "realized_pnl", "pnl", "profit", "result", "financial_result", "money_result",
            "gross_pnl", "gross_profit", "realized_profit", "closed_pnl",
        ]

        net_col = self.pick_first_existing_column(columns, net_candidates)
        gross_col = self.pick_first_existing_column(columns, gross_candidates)

        net = self.decimal_from_db_value(row[net_col]) if net_col else None
        gross = self.decimal_from_db_value(row[gross_col]) if gross_col else None

        fee_total = Decimal("0")
        for column in columns:
            low = column.lower()
            if "percent" in low or low.endswith("_pct") or low.endswith("pct"):
                continue
            if "commission" in low or "fee" in low:
                value = self.decimal_from_db_value(row[column])
                if value is not None:
                    fee_total += abs(value)

        if net is None and gross is not None:
            net = gross - fee_total
        if gross is None and net is not None:
            gross = net + fee_total
        return gross, fee_total, net


    def json_money_to_decimal(self, value) -> Decimal | None:
        if not isinstance(value, dict):
            return None
        try:
            units = Decimal(str(value.get("units", "0") or "0"))
            nano = Decimal(str(value.get("nano", 0) or 0)) / Decimal("1000000000")
            return units + nano
        except Exception:
            return None

    def extract_trade_fee_from_raw_json(self, raw_json) -> Decimal:
        """Extract broker commission/fees from raw_json when the flat DB column is empty.

        T-Invest operations often keep the fee in operation.commission and duplicate it
        as a child operation payment. Prefer operation.commission to avoid double counting.
        """
        try:
            import json
            payload = json.loads(raw_json or "{}")
        except Exception:
            return Decimal("0")
        if not isinstance(payload, dict):
            return Decimal("0")
        operation = payload.get("operation")
        if not isinstance(operation, dict):
            return Decimal("0")

        commission = self.json_money_to_decimal(operation.get("commission"))
        if commission is not None and commission != 0:
            return abs(commission)

        total = Decimal("0")
        for child in operation.get("childOperations") or operation.get("child_operations") or []:
            if not isinstance(child, dict):
                continue
            payment = self.json_money_to_decimal(child.get("payment"))
            if payment is not None:
                total += abs(payment)
        return total

    def row_decimal_value(self, row: sqlite3.Row, column: str | None, default: Decimal | None = None) -> Decimal | None:
        if not column:
            return default
        try:
            value = row[column]
        except Exception:
            return default
        parsed = self.decimal_from_db_value(value)
        return parsed if parsed is not None else default

    def row_text_value(self, row: sqlite3.Row, column: str | None, default: str = "") -> str:
        if not column:
            return default
        try:
            value = row[column]
        except Exception:
            return default
        if value is None:
            return default
        return str(value)

    def calculate_fifo_payment_trade_stats(self, conn: sqlite3.Connection, table: str, column_list: list[str], start: datetime | None, period: str) -> dict | None:
        """Calculate real closed-trade stats from BUY/SELL cashflows.

        This is used for jtrade_trades.db where broker rows have payment/price/quantity
        but do not have a ready realized_pnl column. The calculation is FIFO:
        an opposite-side row closes existing lots, fees are allocated per unit, and
        stats are counted by the closing operation time.
        """
        columns = set(column_list)
        required = {"side", "quantity", "payment"}
        if not required.issubset({c.lower() for c in columns}):
            return None

        source_col = self.pick_first_existing_column(columns, ["source"])
        account_col = self.pick_first_existing_column(columns, ["account_id", "account", "broker_account_id"])
        ticker_col = self.pick_first_existing_column(columns, ["ticker", "symbol"])
        instrument_col = self.pick_first_existing_column(columns, ["instrument_id", "instrument_uid", "figi", "uid"])
        currency_col = self.pick_first_existing_column(columns, ["currency"])
        side_col = self.pick_first_existing_column(columns, ["side", "direction"])
        qty_col = self.pick_first_existing_column(columns, ["quantity", "qty", "lots"])
        price_col = self.pick_first_existing_column(columns, ["price", "avg_price", "entry_price"])
        payment_col = self.pick_first_existing_column(columns, ["payment", "amount", "cashflow", "cash_flow"])
        commission_col = self.pick_first_existing_column(columns, ["commission", "fee", "fees"])
        raw_json_col = self.pick_first_existing_column(columns, ["raw_json", "raw"])
        time_col = self.pick_first_existing_column(columns, ["time", "date", "created_at", "updated_at", "timestamp"])

        if not side_col or not qty_col or not payment_col:
            return None

        rows = conn.execute(f'SELECT rowid AS _rowid_, * FROM "{table}"').fetchall()
        rows = sorted(rows, key=lambda row: (self.parse_trade_datetime(row[time_col]) or datetime.min) if time_col else datetime.min)

        positions: dict[tuple[str, str, str], list[dict]] = {}
        closed_trades: list[dict] = []
        skipped_rows = 0

        for row in rows:
            source = self.row_text_value(row, source_col).lower()
            # Terminal rows are usually planned/opening records, not closed broker cashflows.
            if source and source != "broker":
                continue

            side = self.row_text_value(row, side_col).upper().strip()
            if side not in {"BUY", "SELL"}:
                skipped_rows += 1
                continue

            qty = self.row_decimal_value(row, qty_col, Decimal("0")) or Decimal("0")
            if qty <= 0:
                skipped_rows += 1
                continue

            payment = self.row_decimal_value(row, payment_col, Decimal("0")) or Decimal("0")
            price = self.row_decimal_value(row, price_col, None)
            if payment == 0 and price is not None and price > 0:
                payment = price * qty * (Decimal("1") if side == "SELL" else Decimal("-1"))
            if payment == 0:
                skipped_rows += 1
                continue

            fee = abs(self.row_decimal_value(row, commission_col, Decimal("0")) or Decimal("0"))
            if fee == 0 and raw_json_col:
                fee = self.extract_trade_fee_from_raw_json(row[raw_json_col])

            account = self.row_text_value(row, account_col, "—")
            ticker = self.row_text_value(row, ticker_col, "—").upper().strip() or "—"
            instrument = self.row_text_value(row, instrument_col, ticker).upper().strip() or ticker
            currency = self.row_text_value(row, currency_col, "RUB").upper().strip() or "RUB"
            key = (account, instrument, currency)
            lots = positions.setdefault(key, [])

            trade_time = self.parse_trade_datetime(row[time_col]) if time_col else None
            payment_per_qty = payment / qty
            fee_per_qty = fee / qty
            remaining = qty

            close_gross = Decimal("0")
            close_fee = Decimal("0")
            close_qty = Decimal("0")

            while remaining > 0 and lots and lots[0].get("side") != side:
                open_lot = lots[0]
                matched_qty = min(remaining, open_lot["qty"])
                close_gross += (open_lot["payment_per_qty"] + payment_per_qty) * matched_qty
                close_fee += (open_lot["fee_per_qty"] + fee_per_qty) * matched_qty
                close_qty += matched_qty
                open_lot["qty"] -= matched_qty
                remaining -= matched_qty
                if open_lot["qty"] <= 0:
                    lots.pop(0)

            if close_qty > 0:
                net = close_gross - close_fee
                if start is None or (trade_time is not None and trade_time >= start):
                    closed_trades.append({
                        "time": trade_time,
                        "ticker": ticker,
                        "currency": currency,
                        "qty": close_qty,
                        "gross": close_gross,
                        "fees": close_fee,
                        "net": net,
                    })

            if remaining > 0:
                lots.append({
                    "side": side,
                    "qty": remaining,
                    "payment_per_qty": payment_per_qty,
                    "fee_per_qty": fee_per_qty,
                })

        total = len(closed_trades)
        wins = sum(1 for item in closed_trades if item["net"] > 0)
        losses = sum(1 for item in closed_trades if item["net"] < 0)
        flats = total - wins - losses
        gross_total = sum((item["gross"] for item in closed_trades), Decimal("0"))
        fee_total = sum((item["fees"] for item in closed_trades), Decimal("0"))
        profit_total = sum((item["net"] for item in closed_trades if item["net"] > 0), Decimal("0"))
        loss_total = sum((-item["net"] for item in closed_trades if item["net"] < 0), Decimal("0"))
        net_total = profit_total - loss_total
        biggest_win = max((item["net"] for item in closed_trades), default=None)
        biggest_loss = min((item["net"] for item in closed_trades), default=None)
        decided = wins + losses
        winrate = (Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
        avg_net = (net_total / Decimal(total)) if total else Decimal("0")

        return {
            "available": True,
            "method": "FIFO cashflow",
            "table": table,
            "period": period,
            "date_col": time_col or "—",
            "total": total,
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "winrate": winrate,
            "gross_total": gross_total,
            "fee_total": fee_total,
            "profit_total": profit_total,
            "loss_total": loss_total,
            "net_total": net_total,
            "avg_net": avg_net,
            "biggest_win": biggest_win,
            "biggest_loss": biggest_loss,
            "skipped_rows": skipped_rows,
        }

    def calculate_trade_stats(self) -> dict:
        db_path = self.find_trade_journal_db_path()
        if not db_path:
            return {"available": False, "error": "db/jtrade_trades.db не найден"}

        period = self.trade_stats_period_var.get() if hasattr(self, "trade_stats_period_var") else "7 дней"
        start = self.trade_stats_period_start(period)
        date_candidates = [
            "closed_at", "close_time", "closed_time", "exit_time", "exit_at", "finished_at",
            "updated_at", "created_at", "opened_at", "open_time", "timestamp", "date", "time",
        ]

        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            selected = self.select_trade_stats_table(conn)
            if not selected:
                return {"available": False, "error": "не нашёл таблицу сделок в SQLite"}
            table, column_list = selected
            columns = set(column_list)

            fifo_stats = self.calculate_fifo_payment_trade_stats(conn, table, column_list, start, period)
            if fifo_stats is not None:
                fifo_stats["db_path"] = str(db_path)
                return fifo_stats

            date_col = self.pick_first_existing_column(columns, date_candidates)
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()

            total = wins = losses = flats = 0
            gross_total = Decimal("0")
            fee_total = Decimal("0")
            profit_total = Decimal("0")
            loss_total = Decimal("0")
            net_total = Decimal("0")
            biggest_win = None
            biggest_loss = None

            for row in rows:
                if not self.row_is_closed_trade(row, columns):
                    continue
                if start is not None and date_col:
                    trade_dt = self.parse_trade_datetime(row[date_col])
                    if trade_dt is None or trade_dt < start:
                        continue

                gross, fees, net = self.extract_trade_stats_amounts(row, columns)
                if net is None:
                    continue

                total += 1
                gross_total += gross if gross is not None else net
                fee_total += fees
                net_total += net
                biggest_win = net if biggest_win is None else max(biggest_win, net)
                biggest_loss = net if biggest_loss is None else min(biggest_loss, net)
                if net > 0:
                    wins += 1
                    profit_total += net
                elif net < 0:
                    losses += 1
                    loss_total += abs(net)
                else:
                    flats += 1

            decided = wins + losses
            winrate = (Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
            net_total = profit_total - loss_total
            avg_net = (net_total / Decimal(total)) if total else Decimal("0")
            return {
                "available": True,
                "db_path": str(db_path),
                "table": table,
                "period": period,
                "date_col": date_col or "—",
                "total": total,
                "wins": wins,
                "losses": losses,
                "flats": flats,
                "winrate": winrate,
                "gross_total": gross_total,
                "fee_total": fee_total,
                "profit_total": profit_total,
                "loss_total": loss_total,
                "net_total": net_total,
                "avg_net": avg_net,
                "biggest_win": biggest_win,
                "biggest_loss": biggest_loss,
            }
        finally:
            conn.close()

    def refresh_trade_stats_async(self):
        if not hasattr(self, "trade_stats_summary_var"):
            return

        def task():
            return self.calculate_trade_stats()

        def done(result, error):
            if error:
                self.trade_stats_summary_var.set("Статистика: ошибка")
                self.trade_stats_detail_var.set(str(error))
                return
            result = result or {}
            if not result.get("available"):
                self.trade_stats_summary_var.set("Статистика: —")
                self.trade_stats_detail_var.set(result.get("error", "нет данных"))
                return
            winrate = result.get("winrate")
            winrate_text = f"{winrate:.2f}%" if winrate is not None else "—"
            profit_total = result.get("profit_total")
            loss_total = result.get("loss_total")
            if profit_total is not None and loss_total is not None:
                net = Decimal(str(profit_total)) - Decimal(str(loss_total))
            else:
                net = result.get("net_total", Decimal("0"))
            sign = "+" if net > 0 else ""
            self.trade_stats_summary_var.set(
                f"{result.get('period')}: Winrate {winrate_text} | Чистый итог {sign}{fmt_money(net)}"
            )
            self.trade_stats_detail_var.set("")

        self.run_async(task, done)

    def reset_autofilled_price_fields_to_auto(self) -> None:
        """Convert still-auto numeric SL/TP fields back to auto/авто.

        The order form writes recommended numeric levels into the entry cells for convenience.
        If the ticker or side changes, those numbers must not become manual levels accidentally,
        especially for SHORT where bracket geometry is inverted versus LONG.
        """
        if not hasattr(self, "sl_price_var") or not hasattr(self, "tp_manual_var"):
            return

        old_flag = getattr(self, "_auto_level_update", False)
        self._auto_level_update = True
        try:
            if self._same_as_previous_auto_value(self.sl_price_var.get(), getattr(self, "_auto_sl_value", "")):
                self.sl_price_var.set("авто")
            if self._same_as_previous_auto_value(self.tp_manual_var.get(), getattr(self, "_auto_tp_value", "")):
                self.tp_manual_var.set("авто")
        finally:
            self._auto_level_update = old_flag

    def reset_autofilled_price_fields_if_context_changed(self) -> None:
        """Reset auto-filled SL/TP numbers when ticker or side changes.

        Manual user values are preserved; only values equal to the last auto-filled SL/TP are reset.
        """
        if not hasattr(self, "ticker_var") or not hasattr(self, "trade_side_var"):
            return
        current_key = (
            str(self.ticker_var.get() or "").strip().upper(),
            str(self.trade_side_var.get() or "BUY").strip().upper(),
        )
        previous_key = getattr(self, "_auto_level_context_key", None)
        if previous_key and previous_key != current_key:
            self.reset_autofilled_price_fields_to_auto()
            self._auto_sl_value = ""
            self._auto_tp_value = ""
            self._auto_level_context_key = None

    def is_auto_tp_value(self, value: str) -> bool:
        raw = str(value or "").strip().lower().replace(",", ".")
        return raw in {"auto", "авто", "a", "а", "tp auto", "тп авто", "take auto"}

    def _same_as_previous_auto_value(self, value: str, previous_auto_value: str) -> bool:
        raw = str(value or "").strip().replace(",", ".")
        prev = str(previous_auto_value or "").strip().replace(",", ".")
        if not raw or not prev:
            return False
        if raw == prev:
            return True
        try:
            return Decimal(raw) == Decimal(prev)
        except Exception:
            return False

    def is_effective_auto_tp_value(self, value: str) -> bool:
        # If the TP field still contains our previous auto-filled number, keep treating it as auto.
        # This prevents stale SHORT targets when price/entry moves after an auto-fill.
        return self.is_auto_tp_value(value) or self._same_as_previous_auto_value(value, getattr(self, "_auto_tp_value", ""))

    def is_effective_risk_based_sl_value(self, value: str) -> bool:
        # If the SL field still contains our previous auto-filled risk SL, keep treating it as auto/risk-based.
        # If the user edits the number, it becomes a real manual SL.
        return is_risk_based_sl_value(value) or self._same_as_previous_auto_value(value, getattr(self, "_auto_sl_value", ""))

    def parse_optional_tp(self, value: str) -> Decimal | None:
        raw = str(value).strip().replace(",", ".")
        if not raw:
            return None
        if self.is_effective_auto_tp_value(raw):
            return None
        return parse_decimal(raw, "TP цена")

    def schedule_trade_preview_refresh(self, *_args):
        if not hasattr(self, "root") or not hasattr(self, "ticker_var"):
            return
        # Do not start a new preview cycle when we ourselves write auto SL/TP fields.
        # Otherwise recommended levels constantly re-trigger their own recalculation.
        if getattr(self, "_auto_level_update", False):
            return
        self.reset_autofilled_price_fields_if_context_changed()
        if getattr(self, "_trade_preview_after_id", None):
            try:
                self.root.after_cancel(self._trade_preview_after_id)
            except Exception:
                pass
        # Slower debounce: the broker/API + AI level engine should not jump on every keystroke.
        self._trade_preview_after_id = self.root.after(2200, self.refresh_trade_risk_preview_async)

    def stop_periodic_trade_preview_refresh(self):
        after_id = getattr(self, "_trade_preview_periodic_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._trade_preview_periodic_after_id = None

    def start_periodic_trade_preview_refresh(self, initial_delay_ms: int = TRADE_PREVIEW_AUTO_REFRESH_MS):
        self.stop_periodic_trade_preview_refresh()
        self.schedule_periodic_trade_preview_refresh(initial_delay_ms)

    def schedule_periodic_trade_preview_refresh(self, delay_ms: int = TRADE_PREVIEW_AUTO_REFRESH_MS):
        if not hasattr(self, "root"):
            return
        self._trade_preview_periodic_after_id = self.root.after(
            max(1000, int(delay_ms)),
            self.run_periodic_trade_preview_refresh,
        )

    def run_periodic_trade_preview_refresh(self):
        self._trade_preview_periodic_after_id = None
        try:
            if not getattr(self, "account_id", None):
                return
            if not hasattr(self, "ticker_var") or not hasattr(self, "qty_var"):
                return

            ticker = self.ticker_var.get().strip().upper()
            if not ticker:
                return

            # Recalculate the whole main order-form preview once per minute:
            # available lots, recommended size, risk SL, AI/stabilized levels, and auto TP.
            # The normal debounce is reused, so manual typing and periodic refresh cannot race hard.
            self.schedule_trade_preview_refresh()
        finally:
            if getattr(self, "account_id", None) and hasattr(self, "ticker_var"):
                self.schedule_periodic_trade_preview_refresh()

    def should_autofill_level_field(self, current_value: str, previous_auto_value: str, allow_risk_words: bool = False) -> bool:
        raw = str(current_value or "").strip().replace(",", ".")
        prev = str(previous_auto_value or "").strip().replace(",", ".")
        if not raw:
            return True
        if prev and raw == prev:
            return True
        if allow_risk_words and is_risk_based_sl_value(raw):
            return True
        return False

    def stabilize_recommended_levels(self, rec: dict, side: str, entry_price: Decimal, step: Decimal) -> dict:
        """Keep AI levels realistic and non-crazy for the trade form.

        The probability/AI layer may suggest wide contextual levels. For the actual order form
        we need practical protective levels: near enough to be tradable, but not absurdly tight.
        SL is only used when the user left SL empty; if SL field is auto/авто we keep risk-based SL.
        """
        if not rec or not rec.get("available"):
            return rec or {"available": False}

        try:
            entry = Decimal(str(entry_price))
            step_dec = Decimal(str(step or "0"))
            sl = Decimal(str(rec.get("sl")))
            tp = Decimal(str(rec.get("tp")))
        except Exception:
            return {"available": False, "reason": "AI levels parse error"}

        if entry <= 0:
            return {"available": False, "reason": "entry <= 0"}

        if side == "BUY":
            if sl >= entry or tp <= entry:
                return {"available": False, "reason": "AI levels are on wrong side"}
            raw_sl_dist = entry - sl
            raw_tp_dist = tp - entry
        else:
            if sl <= entry or tp >= entry:
                return {"available": False, "reason": "AI levels are on wrong side"}
            raw_sl_dist = sl - entry
            raw_tp_dist = entry - tp

        # Practical cap: contextual levels can be very far; order form should use a closer tactical bracket.
        # 0.16%..0.65% of entry, with step-aware minimum.
        min_dist = max(step_dec * Decimal("2"), entry * Decimal("0.0016"))
        max_dist = max(step_dec * Decimal("5"), entry * Decimal("0.0065"))
        sl_dist = min(max(raw_sl_dist, min_dist), max_dist)

        # TP should not be thrown to the moon: target RR roughly 1.15..1.70 from stabilized SL.
        min_tp_dist = sl_dist * Decimal("1.15")
        max_tp_dist = sl_dist * Decimal("1.70")
        tp_dist = min(max(raw_tp_dist, min_tp_dist), max_tp_dist)

        if side == "BUY":
            fixed_sl = round_to_step(entry - sl_dist, step_dec)
            fixed_tp = round_to_step(entry + tp_dist, step_dec)
            if fixed_sl >= entry and step_dec > 0:
                fixed_sl = entry - step_dec
            if fixed_tp <= entry and step_dec > 0:
                fixed_tp = entry + step_dec
        else:
            fixed_sl = round_to_step(entry + sl_dist, step_dec)
            fixed_tp = round_to_step(entry - tp_dist, step_dec)
            if fixed_sl <= entry and step_dec > 0:
                fixed_sl = entry + step_dec
            if fixed_tp >= entry and step_dec > 0:
                fixed_tp = entry - step_dec

        fixed_sl_dist = abs(entry - fixed_sl)
        fixed_tp_dist = abs(fixed_tp - entry)
        rr = (fixed_tp_dist / fixed_sl_dist) if fixed_sl_dist > 0 else Decimal("0")

        out = dict(rec)
        out["raw_sl"] = rec.get("sl")
        out["raw_tp"] = rec.get("tp")
        out["sl"] = fixed_sl
        out["tp"] = fixed_tp
        out["rr"] = float(rr)
        raw_reason = str(rec.get("reason") or "").strip()
        out["reason"] = (raw_reason + " | " if raw_reason else "") + "stabilized tactical bracket"
        return out

    def directionally_round_level(self, entry_price: Decimal, side: str, step: Decimal, price: Decimal, role: str) -> Decimal:
        """Round a price while preserving the correct bracket side.

        BUY:  SL below entry, TP above entry.
        SELL: SL above entry, TP below entry.
        This fixes SHORT brackets after broker tick-size rounding.
        """
        entry = Decimal(str(entry_price))
        price_dec = Decimal(str(price))
        step_dec = Decimal(str(step or "0"))
        role = str(role or "").upper()
        side = str(side or "BUY").upper()

        if step_dec > 0:
            rounded = round_to_step(price_dec, step_dec)
            min_step = step_dec
        else:
            rounded = price_dec
            min_step = max(abs(entry) * Decimal("0.0001"), Decimal("0.01"))

        if side == "BUY":
            if role == "SL" and rounded >= entry:
                rounded = entry - min_step
            elif role == "TP" and rounded <= entry:
                rounded = entry + min_step
        else:
            if role == "SL" and rounded <= entry:
                rounded = entry + min_step
            elif role == "TP" and rounded >= entry:
                rounded = entry - min_step

        return round_to_step(rounded, step_dec) if step_dec > 0 else rounded

    def validate_trade_bracket(self, entry_price: Decimal, side: str, sl_price: Decimal, tp_price: Decimal | None = None) -> None:
        entry = Decimal(str(entry_price))
        sl = Decimal(str(sl_price))
        tp = Decimal(str(tp_price)) if tp_price is not None else None
        side = str(side or "BUY").upper()
        if side == "BUY":
            if sl >= entry:
                die(f"SL для LONG должен быть ниже входа. entry={entry}, SL={sl}")
            if tp is not None and tp <= entry:
                die(f"TP для LONG должен быть выше входа. entry={entry}, TP={tp}")
        else:
            if sl <= entry:
                die(f"SL для SHORT должен быть выше входа. entry={entry}, SL={sl}")
            if tp is not None and tp >= entry:
                die(f"TP для SHORT должен быть ниже входа. entry={entry}, TP={tp}")

    def calc_auto_tp_from_sl(self, entry_price: Decimal, side: str, step: Decimal, sl_price: Decimal, rec: dict | None = None) -> tuple[Decimal | None, Decimal]:
        """Build practical TP from the actual SL distance.

        SL/risk decides the distance; TP auto is then placed by controlled RR.
        For SHORT this always returns TP below entry and keeps SL above entry.
        """
        try:
            entry = Decimal(str(entry_price))
            sl = Decimal(str(sl_price))
            step_dec = Decimal(str(step or "0"))
        except Exception:
            return None, Decimal("0")
        if entry <= 0 or sl <= 0:
            return None, Decimal("0")

        # Preserve bracket geometry before calculating the TP distance.
        sl = self.directionally_round_level(entry, side, step_dec, sl, "SL")
        try:
            self.validate_trade_bracket(entry, side, sl, None)
        except Exception:
            return None, Decimal("0")

        distance = abs(entry - sl)
        if distance <= 0:
            return None, Decimal("0")

        rr = Decimal("1.35")
        try:
            if rec and rec.get("rr") is not None:
                rr = Decimal(str(rec.get("rr")))
        except Exception:
            rr = Decimal("1.35")
        rr = max(Decimal("1.15"), min(Decimal("1.70"), rr))

        if str(side or "BUY").upper() == "BUY":
            raw_tp = entry + (distance * rr)
        else:
            raw_tp = entry - (distance * rr)
        tp = self.directionally_round_level(entry, side, step_dec, raw_tp, "TP")
        try:
            self.validate_trade_bracket(entry, side, sl, tp)
        except Exception:
            return None, Decimal("0")
        return tp, rr

    def should_autofill_qty_field(self, current_value: str, previous_auto_value: str) -> bool:
        raw = str(current_value or "").strip().replace(",", ".")
        prev = str(previous_auto_value or "").strip().replace(",", ".")
        if not raw:
            return True
        if prev and raw == prev:
            return True
        # The default order-form value is 1; treat it as replaceable until the user edits it.
        return raw == "1"

    def apply_recommended_order_fields_if_free(self, result: dict) -> bool:
        """Prefill Qty, SL and TP with current recommendations without overwriting manual edits."""
        changed = False
        rec = result.get("recommended_levels") or {}
        recommended_qty = result.get("selected_recommended_qty")

        replace_qty = (
            recommended_qty is not None
            and int(recommended_qty or 0) > 0
            and self.should_autofill_qty_field(self.qty_var.get(), getattr(self, "_auto_qty_value", ""))
        )

        replace_sl = False
        replace_tp = False
        sl_text = tp_text = ""

        # Fill the order cells from the already validated selected-side preview.
        # This is safer for SHORT than writing raw AI levels directly into the cells.
        selected_sl = result.get("selected_sl")
        selected_tp = result.get("selected_tp")
        if selected_sl is not None:
            sl_text = str(selected_sl)
            replace_sl = self.should_autofill_level_field(
                self.sl_price_var.get(),
                getattr(self, "_auto_sl_value", ""),
                allow_risk_words=True,
            )
        if selected_tp is not None:
            tp_text = str(selected_tp)
            replace_tp = self.should_autofill_level_field(
                self.tp_manual_var.get(),
                getattr(self, "_auto_tp_value", ""),
                allow_risk_words=True,
            ) or self.is_auto_tp_value(self.tp_manual_var.get())

        if not any((replace_qty, replace_sl, replace_tp)):
            return False

        self._auto_level_update = True
        try:
            if replace_qty:
                qty_text = str(int(recommended_qty))
                self._auto_qty_value = qty_text
                if str(self.qty_var.get()).strip() != qty_text:
                    self.qty_var.set(qty_text)
                    changed = True
            if replace_sl:
                self._auto_sl_value = sl_text
                if str(self.sl_price_var.get()).strip() != sl_text:
                    self.sl_price_var.set(sl_text)
                    changed = True
            if replace_tp:
                self._auto_tp_value = tp_text
                if str(self.tp_manual_var.get()).strip() != tp_text:
                    self.tp_manual_var.set(tp_text)
                    changed = True
            if replace_sl or replace_tp:
                self._auto_level_context_key = (
                    str(result.get("ticker") or self.ticker_var.get() or "").strip().upper(),
                    str(result.get("selected_side") or self.trade_side_var.get() or "BUY").strip().upper(),
                )
        finally:
            self._auto_level_update = False

        if changed:
            parts = []
            if replace_qty:
                parts.append(f"Qty={int(recommended_qty)}")
            if replace_sl:
                parts.append(f"SL={sl_text}")
            if replace_tp:
                parts.append(f"TP={tp_text}")
            rr = result.get("selected_tp_rr") or rec.get("rr")
            rr_text = f", RR={float(rr):.2f}" if rr is not None else ""
            self.log("Предзаполнены рекомендации: " + ", ".join(parts) + rr_text)
        return changed

    # Backward-compatible name: old code called this from the preview refresh.
    def apply_recommended_levels_if_free(self, result: dict):
        return self.apply_recommended_order_fields_if_free(result)

    def refresh_trade_risk_preview_async(self):
        self._trade_preview_after_id = None
        if not self.account_id:
            return

        ticker = self.ticker_var.get().strip().upper()
        raw_qty = self.qty_var.get().strip()
        sl_raw = self.sl_price_var.get().strip()
        if not ticker:
            self.set_available_preview()
            self.set_recommended_qty_preview()
            self.set_position_value_preview()
            self.set_lot_price_preview()
            self.set_sl_preview_pair()
            self.set_tp_preview()
            self.set_trade_risk_reward_preview()
            self.sync_math_window_context()
            return

        try:
            qty = int(raw_qty)
            if qty <= 0:
                raise ValueError
        except Exception:
            self.set_available_preview("L —", "S —", "введи целое количество")
            self.set_recommended_qty_preview()
            self.set_position_value_preview()
            self.set_lot_price_preview()
            self.set_sl_preview_pair()
            self.set_tp_preview()
            self.set_trade_risk_reward_preview()
            self.sync_math_window_context()
            return

        self._trade_preview_seq += 1
        seq = self._trade_preview_seq
        self.set_available_preview("L считаю...", "S считаю...")
        self.set_recommended_qty_preview("Рекомендуемое количество: считаю...")
        self.set_sl_preview_pair("L считаю...", "S считаю...")
        self.set_tp_preview(f"TP {self.current_trade_side_label()}: считаю...")

        def task():
            tp_raw = self.tp_manual_var.get().strip()
            tp_manual = self.parse_optional_tp(tp_raw)
            return self.build_trade_risk_preview(ticker, qty, sl_raw, tp_manual, tp_raw)

        def done(result, error):
            if seq != getattr(self, "_trade_preview_seq", seq):
                return
            if error:
                self.set_available_preview()
                self.set_recommended_qty_preview()
                self.set_position_value_preview()
                self.set_lot_price_preview()
                self.set_sl_preview_pair("L —", "S —", f"{error}")
                self.set_trade_risk_reward_preview()
                self.sync_math_window_context()
                return

            side = result.get("selected_side") or (self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY")
            side_label = "LONG" if side == "BUY" else "SHORT"

            self.set_position_value_preview(
                f"L {fmt_money(result['buy_position_value'])}",
                f"S {fmt_money(result['sell_position_value'])}",
            )
            if result.get("is_futures"):
                self.set_lot_price_preview(
                    f"Фьючерс {side_label}: шаг {result.get('step')} = "
                    f"{fmt_money(result.get('tick_value'))}; 1 пункт = {fmt_money(result.get('point_value'))}"
                )
            else:
                self.set_lot_price_preview(
                    f"Цена за лот {side_label}: {fmt_money(result.get('selected_lot_value'))}"
                )
            autofilled = self.apply_recommended_order_fields_if_free(result)

            long_text, short_text, note = self.format_available_pair(result)
            self.set_available_preview(long_text, short_text, note)
            self.set_recommended_qty_preview(result.get("selected_recommended_qty_text") or "Рекомендуемое количество: —")

            selected_sl = result.get("selected_sl")
            selected_risk = result.get("selected_risk")
            selected_reward = result.get("selected_reward")
            if selected_sl is None or result.get("selected_sl_error"):
                self.set_sl_preview_pair("L —", "S —", str(result.get("selected_sl_error") or "SL не рассчитан"))
                self.set_trade_risk_reward_preview()
            else:
                if side == "BUY":
                    self.set_sl_preview_pair(f"L {selected_sl}", "S —", "")
                else:
                    self.set_sl_preview_pair("L —", f"S {selected_sl}", "")

            selected_tp = result.get("selected_tp")
            if selected_tp is not None:
                auto_mark = " auto" if result.get("selected_tp_auto") else ""
                rr = result.get("selected_tp_rr")
                rr_text = f" | RR≈{float(rr):.2f}" if rr else ""
                self.set_tp_preview(f"TP {side_label}{auto_mark}: {selected_tp}{rr_text}")
                if selected_sl is not None and not result.get("selected_sl_error"):
                    self.set_trade_risk_reward_preview(selected_risk, selected_reward)
            else:
                self.set_tp_preview(f"TP {side_label}: не ставится")
                if selected_sl is not None and not result.get("selected_sl_error"):
                    self.set_trade_risk_reward_preview(selected_risk, None)
            self.sync_math_window_context()
            if autofilled and hasattr(self, "root"):
                # Run one more pass using the freshly written Qty/SL/TP values.
                self.root.after(250, self.schedule_trade_preview_refresh)

        self.run_async(task, done)

    def calculate_recommended_contracts(
        self,
        entry: Decimal,
        sl_price: Decimal | None,
        point_value: Decimal,
        selected_capital: Decimal,
        risk_percent: Decimal | None,
        available_lots,
    ) -> dict:
        """5m risk-budget sizing: allowed money risk first, contracts second."""
        try:
            if sl_price is None:
                return {"qty": None, "text": "Рекомендуемое количество: —"}
            entry_dec = Decimal(str(entry))
            sl_dec = Decimal(str(sl_price))
            point_dec = Decimal(str(point_value))
            capital_dec = Decimal(str(selected_capital))
            percent_dec = self.effective_intraday_risk_percent(risk_percent)
        except Exception:
            return {"qty": None, "text": "Рекомендуемое количество: —"}

        if entry_dec <= 0 or sl_dec <= 0 or point_dec <= 0 or capital_dec <= 0:
            return {"qty": None, "text": "Рекомендуемое количество: —"}

        distance = abs(entry_dec - sl_dec)
        one_contract_risk = distance * point_dec
        if one_contract_risk <= 0:
            return {"qty": None, "text": "Рекомендуемое количество: —"}

        risk_budget = capital_dec * (percent_dec / Decimal("100"))
        raw_qty = int(risk_budget // one_contract_risk)

        try:
            available_int = int(available_lots) if available_lots is not None else None
        except Exception:
            available_int = None

        qty = max(0, raw_qty)
        cap_note = ""
        if available_int is not None and qty > available_int:
            qty = max(0, available_int)
            cap_note = " | ограничено доступными"

        if qty <= 0:
            text = "Рекомендуемое количество: 0 контр."
        else:
            text = f"Рекомендуемое количество: {qty} контр."

        return {
            "qty": qty,
            "raw_qty": raw_qty,
            "risk_budget": risk_budget,
            "one_contract_risk": one_contract_risk,
            "distance": distance,
            "text": text,
        }

    def build_trade_risk_preview(self, ticker: str, qty: int, sl_raw: str, tp_manual: Decimal | None, tp_raw: str = "") -> dict:
        ctx = self.get_trade_instrument_context(ticker)
        inst = ctx["inst"]
        instrument_id = ctx["instrument_id"]
        full = ctx["full"]
        step = ctx["step"]
        point_value = ctx["point_value"]
        is_futures = bool(ctx.get("is_futures"))
        buy_margin_per_contract = Decimal(str(ctx.get("buy_margin_per_contract") or "0"))
        sell_margin_per_contract = Decimal(str(ctx.get("sell_margin_per_contract") or "0"))

        buy_entry = round_to_step(get_best_entry_price(instrument_id, "BUY"), step)
        sell_entry = round_to_step(get_best_entry_price(instrument_id, "SELL"), step)
        buy_exposure = self.futures_leverage_info(buy_entry, qty, point_value, buy_margin_per_contract)
        sell_exposure = self.futures_leverage_info(sell_entry, qty, point_value, sell_margin_per_contract)
        buy_lot_value = calc_position_value(buy_entry, 1, point_value)
        sell_lot_value = calc_position_value(sell_entry, 1, point_value)
        tick_value = Decimal(str(ctx.get("tick_value") or "0"))
        selected_side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"
        selected_entry = buy_entry if selected_side == "BUY" else sell_entry
        recommended_levels = {"available": False}
        if recommend_trade_levels is not None:
            try:
                recommended_levels = recommend_trade_levels(ticker, selected_side, selected_entry, step, None)
                recommended_levels = self.stabilize_recommended_levels(recommended_levels, selected_side, selected_entry, step)
            except Exception as exc:
                recommended_levels = {"available": False, "reason": str(exc)}

        selected_capital = self.get_selected_margin_base(self.get_selected_account_ids())
        risk_percent = parse_sl_risk_percent(sl_raw)
        effective_risk_percent = self.effective_intraday_risk_percent(risk_percent)
        buy_position_value, buy_risk_budget, risk_model, risk_model_label = self.calculate_risk_budget(
            buy_entry, qty, point_value, selected_capital, risk_percent, buy_margin_per_contract
        )
        sell_position_value, sell_risk_budget, _risk_model2, _risk_model_label2 = self.calculate_risk_budget(
            sell_entry, qty, point_value, selected_capital, risk_percent, sell_margin_per_contract
        )

        risk_based_sl = self.is_effective_risk_based_sl_value(sl_raw)
        manual_sl = None
        if risk_based_sl:
            buy_sl, _buy_distance = calc_sl_by_risk(buy_entry, "BUY", step, qty, buy_risk_budget, point_value)
            sell_sl, _sell_distance = calc_sl_by_risk(sell_entry, "SELL", step, qty, sell_risk_budget, point_value)
            buy_sl = self.directionally_round_level(buy_entry, "BUY", step, buy_sl, "SL")
            sell_sl = self.directionally_round_level(sell_entry, "SELL", step, sell_sl, "SL")
            buy_distance = abs(buy_entry - buy_sl)
            sell_distance = abs(sell_entry - sell_sl)
            buy_risk = calc_position_risk(buy_distance, qty, point_value)
            sell_risk = calc_position_risk(sell_distance, qty, point_value)
            buy_sl_error = ""
            sell_sl_error = ""
        else:
            manual_sl = parse_decimal(sl_raw, "SL")
            selected_side = self.trade_side_var.get() if hasattr(self, "trade_side_var") else "BUY"

            def calc_manual_preview(entry_price: Decimal, preview_side: str):
                try:
                    _tp, sl_value = calc_sl_optional_tp(entry_price, preview_side, step, manual_sl, tp_manual)
                    distance = abs(entry_price - sl_value)
                    risk_value = calc_position_risk(distance, qty, point_value)
                    return sl_value, distance, risk_value, ""
                except Exception as exc:
                    # Preview validates the selected direction strictly, but does not break SHORT preview
                    # just because the same manual SL/TP would be invalid for LONG, and vice versa.
                    if selected_side == preview_side:
                        raise
                    return None, Decimal("0"), Decimal("0"), str(exc)

            buy_sl, buy_distance, buy_risk, buy_sl_error = calc_manual_preview(buy_entry, "BUY")
            sell_sl, sell_distance, sell_risk, sell_sl_error = calc_manual_preview(sell_entry, "SELL")

        selected_account_ids = self.get_selected_account_ids()
        long_available, long_available_by_account = self.get_available_lots_for_accounts(selected_account_ids, instrument_id, buy_entry, "BUY")
        short_available, short_available_by_account = self.get_available_lots_for_accounts(selected_account_ids, instrument_id, sell_entry, "SELL")

        primary_long_available = long_available_by_account.get(self.account_id)
        primary_short_available = short_available_by_account.get(self.account_id)

        selected_sl = buy_sl if selected_side == "BUY" else sell_sl
        selected_sl_error = buy_sl_error if selected_side == "BUY" else sell_sl_error
        selected_tp_auto = self.is_effective_auto_tp_value(tp_raw)
        selected_tp = None if selected_tp_auto else tp_manual
        selected_tp_rr = Decimal("0")
        if selected_tp_auto and not selected_sl_error and selected_sl is not None:
            selected_tp, selected_tp_rr = self.calc_auto_tp_from_sl(selected_entry, selected_side, step, selected_sl, recommended_levels)

        # Final selected-side guard. This is especially important for SHORT:
        # SL must be above entry, TP must be below entry.
        if selected_sl is not None and not selected_sl_error:
            try:
                selected_sl = self.directionally_round_level(selected_entry, selected_side, step, selected_sl, "SL")
                if selected_tp is not None:
                    if selected_tp_auto:
                        selected_tp = self.directionally_round_level(selected_entry, selected_side, step, selected_tp, "TP")
                    else:
                        selected_tp = round_to_step(Decimal(str(selected_tp)), step)
                self.validate_trade_bracket(selected_entry, selected_side, selected_sl, selected_tp if selected_tp is not None else None)
            except Exception as exc:
                selected_sl_error = str(exc)
                selected_tp = None

        selected_risk = Decimal("0")
        selected_reward = None
        if selected_sl is not None and not selected_sl_error:
            selected_risk = calc_position_risk(abs(selected_entry - selected_sl), qty, point_value)
            if selected_tp is not None:
                selected_reward = calc_position_risk(abs(selected_entry - selected_tp), qty, point_value)

        selected_available = long_available if selected_side == "BUY" else short_available
        selected_recommended_qty = self.calculate_recommended_contracts(
            selected_entry,
            selected_sl if not selected_sl_error else None,
            point_value,
            selected_capital,
            risk_percent,
            selected_available,
        )

        return {
            "ticker": inst.get("ticker", ticker),
            "point_value": point_value,
            "tick_value": tick_value,
            "step": step,
            "is_futures": is_futures,
            "buy_margin_per_contract": buy_margin_per_contract,
            "sell_margin_per_contract": sell_margin_per_contract,
            "buy_notional_value": buy_exposure.get("notional_value"),
            "sell_notional_value": sell_exposure.get("notional_value"),
            "buy_margin_value": buy_exposure.get("margin_value"),
            "sell_margin_value": sell_exposure.get("margin_value"),
            "buy_leverage": buy_exposure.get("leverage"),
            "sell_leverage": sell_exposure.get("leverage"),
            "buy_entry": buy_entry,
            "sell_entry": sell_entry,
            "buy_position_value": buy_position_value,
            "sell_position_value": sell_position_value,
            "buy_lot_value": buy_lot_value,
            "sell_lot_value": sell_lot_value,
            "recommended_levels": recommended_levels,
            "selected_capital": selected_capital,
            "margin_base_skipped": list(getattr(self, "_last_margin_base_skipped", [])),
            "risk_model": risk_model,
            "risk_model_label": risk_model_label,
            "buy_risk_budget": buy_risk_budget,
            "sell_risk_budget": sell_risk_budget,
            "buy_sl": buy_sl,
            "sell_sl": sell_sl,
            "buy_distance": buy_distance,
            "sell_distance": sell_distance,
            "buy_risk": buy_risk,
            "sell_risk": sell_risk,
            "buy_sl_error": buy_sl_error,
            "sell_sl_error": sell_sl_error,
            "risk_based_sl": risk_based_sl,
            "auto_sl": risk_based_sl,
            "manual_sl": manual_sl,
            "risk_percent": effective_risk_percent,
            "long_available": long_available,
            "short_available": short_available,
            "primary_long_available": primary_long_available,
            "primary_short_available": primary_short_available,
            "long_available_by_account": long_available_by_account,
            "short_available_by_account": short_available_by_account,
            "long_available_text": str(long_available) if long_available is not None else "—",
            "short_available_text": str(short_available) if short_available is not None else "—",
            "primary_long_available_text": str(primary_long_available) if primary_long_available is not None else "—",
            "primary_short_available_text": str(primary_short_available) if primary_short_available is not None else "—",
            "selected_side": selected_side,
            "selected_entry": selected_entry,
            "selected_lot_value": buy_lot_value if selected_side == "BUY" else sell_lot_value,
            "selected_margin_per_contract": buy_margin_per_contract if selected_side == "BUY" else sell_margin_per_contract,
            "selected_leverage": buy_exposure.get("leverage") if selected_side == "BUY" else sell_exposure.get("leverage"),
            "selected_notional_value": buy_exposure.get("notional_value") if selected_side == "BUY" else sell_exposure.get("notional_value"),
            "selected_margin_value": buy_exposure.get("margin_value") if selected_side == "BUY" else sell_exposure.get("margin_value"),
            "selected_position_value": buy_position_value if selected_side == "BUY" else sell_position_value,
            "selected_sl": selected_sl,
            "selected_sl_error": selected_sl_error,
            "selected_risk": selected_risk,
            "selected_reward": selected_reward,
            "selected_available": selected_available,
            "selected_primary_available": primary_long_available if selected_side == "BUY" else primary_short_available,
            "selected_recommended_qty": selected_recommended_qty.get("qty"),
            "selected_recommended_qty_text": selected_recommended_qty.get("text"),
            "selected_recommended_risk_budget": selected_recommended_qty.get("risk_budget"),
            "selected_recommended_one_contract_risk": selected_recommended_qty.get("one_contract_risk"),
            "selected_tp": selected_tp,
            "selected_tp_auto": selected_tp_auto,
            "selected_tp_rr": selected_tp_rr,
        }

    def build_open_trade_plan(self, ticker: str, qty: int, side: str, sl_raw: str, tp_manual: Decimal | None, tp_raw: str = "") -> dict:
        account_ids = self.get_selected_account_ids()
        selected_capital = self.get_selected_margin_base(account_ids)

        ctx = self.get_trade_instrument_context(ticker)
        inst = ctx["inst"]
        instrument_id = ctx["instrument_id"]
        class_code = ctx.get("class_code") or inst.get("classCode", "")
        full = ctx["full"]
        step = ctx["step"]
        point_value = ctx["point_value"]
        tick_value = Decimal(str(ctx.get("tick_value") or "0"))
        is_futures = bool(ctx.get("is_futures"))
        margin_per_contract = Decimal("0")
        entry = round_to_step(get_best_entry_price(instrument_id, side), step)

        exposure = self.futures_leverage_info(entry, qty, point_value, margin_per_contract)
        risk_percent = parse_sl_risk_percent(sl_raw)
        effective_risk_percent = self.effective_intraday_risk_percent(risk_percent)
        position_value, risk_budget, risk_model, risk_model_label = self.calculate_risk_budget(
            entry, qty, point_value, selected_capital, risk_percent, margin_per_contract
        )

        risk_based_sl = self.is_effective_risk_based_sl_value(sl_raw)
        if risk_based_sl:
            sl_price, _sl_distance = calc_sl_by_risk(entry, side, step, qty, risk_budget, point_value)
            sl_price = self.directionally_round_level(entry, side, step, sl_price, "SL")
            sl_distance = abs(entry - sl_price)
        else:
            sl_price = parse_decimal(sl_raw, "SL")
            sl_distance = abs(entry - round_to_step(sl_price, step))

        tp_auto = self.is_effective_auto_tp_value(tp_raw)
        if tp_auto:
            tp_manual = None
        tp, sl = calc_sl_optional_tp(entry, side, step, sl_price, tp_manual)
        if sl is None or sl <= 0:
            die("SL не рассчитался. Проверь тикер, количество и SL.")
        sl = self.directionally_round_level(entry, side, step, sl, "SL")
        self.validate_trade_bracket(entry, side, sl, None)

        auto_tp_rr = Decimal("0")
        auto_tp_rec = {"available": False}
        if tp_auto:
            if recommend_trade_levels is not None:
                try:
                    auto_tp_rec = recommend_trade_levels(ticker, side, entry, step, None)
                    auto_tp_rec = self.stabilize_recommended_levels(auto_tp_rec, side, entry, step)
                except Exception:
                    auto_tp_rec = {"available": False}
            tp, auto_tp_rr = self.calc_auto_tp_from_sl(entry, side, step, sl, auto_tp_rec)
            if tp is None or tp <= 0:
                die("TP auto не рассчитался. Проверь тикер, количество и SL.")

        if tp is not None:
            tp = self.directionally_round_level(entry, side, step, tp, "TP")
        self.validate_trade_bracket(entry, side, sl, tp if tp is not None else None)

        actual_risk = calc_position_risk(abs(entry - sl), qty, point_value)
        reward_amount = calc_position_risk(abs(entry - tp), qty, point_value) if tp is not None else None

        selected_available_lots, available_by_account = self.get_available_lots_for_accounts(account_ids, instrument_id, entry, side)
        if selected_available_lots is None:
            die("Не вижу доступные контракты по выбранным счетам. Проверь GetMaxLots/API.")
        if qty > selected_available_lots:
            die(f"Недостаточно доступных контрактов на выбранных счетах: хочешь {qty}, доступно {selected_available_lots}.")
        legs = self.allocate_lots_across_accounts(qty, available_by_account)
        if not legs:
            die("Не удалось распределить количество по выбранным счетам.")
        available_lots = available_by_account.get(self.account_id)

        return {
            "account_id": self.account_id,
            "account_label": " + ".join(leg["account_label"] for leg in legs),
            "primary_account_label": self.account_label,
            "legs": legs,
            "ticker_input": ticker,
            "ticker": inst.get("ticker", ticker),
            "inst": inst,
            "instrument_id": instrument_id,
            "class_code": class_code,
            "side": side,
            "side_name": side_to_text(side),
            "qty": qty,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "sl_distance": abs(entry - sl),
            "step": step,
            "tp_manual": tp if tp_auto else tp_manual,
            "tp_enabled": tp is not None,
            "tp_auto": tp_auto,
            "tp_rr": auto_tp_rr,
            "selected_capital": selected_capital,
            "margin_base_skipped": list(getattr(self, "_last_margin_base_skipped", [])),
            "position_value": position_value,
            "risk_amount": actual_risk,
            "reward_amount": reward_amount,
            "risk_budget": risk_budget,
            "risk_model": risk_model,
            "risk_model_label": risk_model_label,
            "risk_percent": effective_risk_percent,
            "auto_sl": risk_based_sl,
            "risk_based_sl": risk_based_sl,
            "point_value": point_value,
            "tick_value": tick_value,
            "is_futures": is_futures,
            "margin_per_contract": margin_per_contract,
            "notional_value": exposure.get("notional_value"),
            "margin_value": exposure.get("margin_value"),
            "futures_leverage": exposure.get("leverage"),
            "available_lots": available_lots,
            "selected_available_lots": selected_available_lots,
            "available_lots_by_account": available_by_account,
        }

    def open_trade_async(self, side: str):
        if not self.account_id:
            messagebox.showwarning("Счёт", "Сначала выбери основной счёт.")
            return
        try:
            ticker, qty = self.get_ticker_qty_inputs()
            sl_raw = self.sl_price_var.get().strip()
            if not sl_raw:
                die("SL пустой. Введи цену или auto/авто.")
            tp_raw = self.tp_manual_var.get().strip()
            tp_manual = self.parse_optional_tp(tp_raw)
            entry_split_enabled, entry_split_count, entry_split_label = self.get_entry_split_settings()
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        side_name = side_to_text(side)
        self.log(
            f"Готовлю сделку {side_name}: {ticker}, qty={qty}, SL={sl_raw}, "
            f"account={self.account_label}, split={entry_split_label}"
        )

        def prepare_task():
            return self.build_open_trade_plan(ticker, qty, side, sl_raw, tp_manual, tp_raw)

        def prepared(plan, error):
            if error:
                self.log(f"Ошибка подготовки сделки: {error}")
                messagebox.showerror("Ошибка", str(error))
                return

            plan["entry_split_enabled"] = entry_split_enabled
            plan["entry_split_count"] = entry_split_count
            plan["entry_split_label"] = entry_split_label

            tp_text = str(plan["tp_manual"]) if plan["tp_manual"] is not None else "не ставится"
            ev_text = "+" + fmt_money(plan.get("reward_amount")) if plan.get("reward_amount") is not None else "—"
            recommended_qty = self.calculate_recommended_contracts(
                plan["entry"],
                plan["sl"],
                plan["point_value"],
                plan["selected_capital"],
                plan.get("risk_percent"),
                plan.get("selected_available_lots"),
            )
            recommended_line = (recommended_qty.get("text") or "Рекомендуемое количество: —") + "\n"
            available_text = str(plan["available_lots"]) if plan["available_lots"] is not None else "—"
            selected_available_text = str(plan.get("selected_available_lots")) if plan.get("selected_available_lots") is not None else "—"
            selected_accounts_count = len(self.get_selected_account_ids())
            if selected_accounts_count > 1:
                available_line = f"Доступно: основной {available_text}; выбранные суммарно {selected_available_text}\n"
            else:
                available_line = f"Доступно по расчёту брокера: {available_text}\n"

            futures_line = ""
            if plan.get("is_futures"):
                futures_line = (
                    f"Фьючерс: шаг {plan.get('step')} = {fmt_money(plan.get('tick_value'))}; "
                    f"1 пункт = {fmt_money(plan.get('point_value'))}; "
                    f"номинал {fmt_money(plan.get('notional_value'))}\n"
                )
            legs_line = "; ".join(f"{leg['account_label']}: {leg['qty']}" for leg in plan.get("legs", []))
            if legs_line:
                legs_line = f"Распределение: {legs_line}\n"
            split_line = self.describe_entry_split_for_legs(
                plan.get("legs", []),
                split_enabled=plan.get("entry_split_enabled"),
                split_count=plan.get("entry_split_count"),
            )
            if not messagebox.askyesno(
                "Подтверждение",
                f"Открыть {plan['side_name']} {plan['ticker']}, количество {plan['qty']}?\n"
                f"Счета: {plan['account_label']}\n"
                f"{legs_line}"
                f"{split_line}"
                f"{available_line}"
                f"{recommended_line}"
                f"{futures_line}"
                f"Entry: {plan['entry']}\n"
                f"Позиция: {fmt_money(plan['position_value'])}\n"
                f"Маржинальная база: {fmt_money(plan['selected_capital'])}\n"
                f"Риск: {fmt_money(plan['risk_amount'])} ({plan['risk_percent']}% от {plan['risk_model_label']})\n"
                f"SL: {plan['sl']} | {plan['sl_distance']} п.\n"
                f"TP: {tp_text}\n"
                f"ev: {ev_text}",
            ):
                return
            self.execute_open_trade_plan_async(plan)

        self.run_async(prepare_task, prepared)

    def execute_open_trade_plan_async(self, plan: dict):
        legs_text = "; ".join(f"{leg['account_label']}={leg['qty']}" for leg in plan.get("legs", []))
        split_text = self.describe_entry_split_for_legs(
            plan.get("legs", []),
            split_enabled=plan.get("entry_split_enabled"),
            split_count=plan.get("entry_split_count"),
        ).strip()
        self.log(
            f"Открываю {plan['side_name']}: {plan['ticker']}, total_qty={plan['qty']}, "
            f"legs=[{legs_text}], {split_text}, entry={plan['entry']}, SL={plan['sl']}, "
            f"TP={fmt_optional_price(plan['tp'])}, risk={fmt_money(plan['risk_amount'])}"
        )

        def task():
            results = []
            errors = []
            total_filled = 0
            total_risk = Decimal("0")
            total_entry_weighted_sum = Decimal("0")
            posted_entry_orders = 0
            protection_summaries = []

            for leg in plan.get("legs", []):
                account_id = leg["account_id"]
                account_label = leg["account_label"]
                parent_leg_qty = int(leg["qty"])
                if parent_leg_qty <= 0:
                    continue

                try:
                    try:
                        before_portfolio = get_total_portfolio_value(account_id)
                    except Exception:
                        before_portfolio = Decimal("0")

                    entry_direction = "ORDER_DIRECTION_BUY" if plan["side"] == "BUY" else "ORDER_DIRECTION_SELL"
                    child_quantities = self.split_entry_order_quantities(
                        parent_leg_qty,
                        split_enabled=plan.get("entry_split_enabled"),
                        split_count=plan.get("entry_split_count"),
                    )
                    child_count = len(child_quantities)
                    posted_orders = []

                    # Entry burst stays sliced, but protective orders are no longer created per child order.
                    # After all child fills are known, one aggregated SL and one aggregated TP are posted per account leg.
                    for child_index, child_qty in enumerate(child_quantities, start=1):
                        if child_qty <= 0:
                            continue
                        try:
                            order_id = post_market_order(
                                account_id,
                                plan["instrument_id"],
                                child_qty,
                                entry_direction,
                                plan["class_code"],
                                confirm_margin=(plan["side"] == "SELL"),
                            )
                            posted_entry_orders += 1
                            posted_orders.append({
                                "order_id": order_id,
                                "requested_qty": child_qty,
                                "child_index": child_index,
                                "child_count": child_count,
                                "parent_leg_qty": parent_leg_qty,
                            })
                            if child_index < child_count and ENTRY_CHILD_ORDER_DELAY_SECONDS > 0:
                                time.sleep(ENTRY_CHILD_ORDER_DELAY_SECONDS)
                        except Exception as exc:
                            errors.append(f"{account_label}: вход {child_index}/{child_count} не отправлен: {exc}")

                    if not posted_orders:
                        results.append({
                            "status": "not_posted",
                            "account_id": account_id,
                            "account_label": account_label,
                            "requested_qty": parent_leg_qty,
                            "qty": 0,
                            "order_id": "",
                            "entry_orders": 0,
                            "child_index": 0,
                            "child_count": child_count,
                            "child_fills": [],
                            "child_fills_text": "нет отправленных входов",
                        })
                        continue

                    leg_filled_qty = 0
                    leg_entry_weighted_sum = Decimal("0")
                    child_fills = []
                    child_order_ids = []

                    for child in posted_orders:
                        order_id = child["order_id"]
                        child_requested_qty = int(child["requested_qty"])
                        child_index = int(child["child_index"])
                        child_count = int(child["child_count"])
                        child_order_ids.append(order_id)

                        filled_qty = wait_fill(account_id, order_id, ENTRY_WAIT_SECONDS)
                        try:
                            filled_qty = int(filled_qty)
                        except Exception:
                            filled_qty = 0
                        if filled_qty <= 0:
                            child_fills.append({
                                "order_id": order_id,
                                "requested_qty": child_requested_qty,
                                "qty": 0,
                                "entry": "",
                                "child_index": child_index,
                                "child_count": child_count,
                                "status": "not_filled",
                            })
                            continue

                        try:
                            order_state = get_order_state(account_id, order_id)
                        except Exception:
                            order_state = {}
                        child_entry = extract_execution_price_from_state(order_state, plan["entry"])

                        leg_filled_qty += filled_qty
                        leg_entry_weighted_sum += child_entry * Decimal(filled_qty)
                        total_filled += filled_qty
                        total_entry_weighted_sum += child_entry * Decimal(filled_qty)
                        child_fills.append({
                            "order_id": order_id,
                            "requested_qty": child_requested_qty,
                            "qty": filled_qty,
                            "entry": str(child_entry),
                            "child_index": child_index,
                            "child_count": child_count,
                            "status": "ok",
                        })

                    if leg_filled_qty <= 0:
                        results.append({
                            "status": "not_filled",
                            "account_id": account_id,
                            "account_label": account_label,
                            "requested_qty": parent_leg_qty,
                            "qty": 0,
                            "order_id": ",".join(child_order_ids),
                            "entry_orders": len(posted_orders),
                            "child_index": 0,
                            "child_count": child_count,
                            "child_fills": child_fills,
                            "child_fills_text": ", ".join(f"#{x['child_index']} {x['qty']}/{x['requested_qty']}" for x in child_fills),
                        })
                        continue

                    leg_entry = (leg_entry_weighted_sum / Decimal(leg_filled_qty)) if leg_entry_weighted_sum > 0 else plan["entry"]

                    # For risk-based SL, keep the intended point distance but anchor it to the aggregated average fill.
                    # For manual SL, preserve the exact manual trigger.
                    if plan.get("risk_based_sl"):
                        if plan["side"] == "BUY":
                            leg_sl_raw = leg_entry - plan["sl_distance"]
                        else:
                            leg_sl_raw = leg_entry + plan["sl_distance"]
                        leg_sl = self.directionally_round_level(leg_entry, plan["side"], plan.get("step", Decimal("0")), leg_sl_raw, "SL")
                    else:
                        leg_sl = plan["sl"]

                    leg_sl_distance = abs(leg_entry - leg_sl)

                    leg_tp = plan["tp"]
                    if plan.get("tp_auto") and leg_tp is not None:
                        rr = plan.get("tp_rr") or Decimal("1.35")
                        if plan["side"] == "BUY":
                            leg_tp_raw = leg_entry + (leg_sl_distance * rr)
                        else:
                            leg_tp_raw = leg_entry - (leg_sl_distance * rr)
                        leg_tp = self.directionally_round_level(leg_entry, plan["side"], plan.get("step", Decimal("0")), leg_tp_raw, "TP")

                    try:
                        self.validate_trade_bracket(leg_entry, plan["side"], leg_sl, leg_tp if leg_tp is not None else None)
                    except Exception as exc:
                        errors.append(f"{account_label}: агрегированная защита отклонена: {exc}")
                        continue

                    # Put one aggregated SL first. If TP fails, the full filled leg is still protected.
                    tp_id = ""
                    sl_id = ""
                    try:
                        sl_id = post_stop(
                            account_id,
                            plan["instrument_id"],
                            leg_filled_qty,
                            leg_sl,
                            plan["side"],
                            "STOP_ORDER_TYPE_STOP_LOSS",
                            plan["class_code"],
                        )
                    except Exception as exc:
                        errors.append(f"{account_label}: агрегированный SL не выставлен на {leg_filled_qty}: {exc}")

                    if leg_tp is not None:
                        try:
                            tp_id = post_stop(
                                account_id,
                                plan["instrument_id"],
                                leg_filled_qty,
                                leg_tp,
                                plan["side"],
                                "STOP_ORDER_TYPE_TAKE_PROFIT",
                                plan["class_code"],
                            )
                        except Exception as exc:
                            errors.append(f"{account_label}: агрегированный TP не выставлен на {leg_filled_qty}: {exc}")

                    leg_risk = calc_position_risk(leg_sl_distance, leg_filled_qty, plan["point_value"])
                    total_risk += leg_risk

                    child_fills_text = ", ".join(
                        f"#{x['child_index']} {x['qty']}/{x['requested_qty']}"
                        for x in child_fills[:15]
                    )
                    if len(child_fills) > 15:
                        child_fills_text += f", ... +{len(child_fills) - 15}"

                    trade = {
                        "trade_id": str(uuid.uuid4()),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "account_id": account_id,
                        "ticker": plan["ticker"],
                        "class_code": plan["class_code"],
                        "instrument_id": plan["instrument_id"],
                        "side": plan["side"],
                        "qty": leg_filled_qty,
                        "entry_price": str(leg_entry),
                        "tp_price": fmt_optional_price(leg_tp),
                        "sl_price": str(leg_sl),
                        "sl_distance_points": str(leg_sl_distance),
                        "risk_percent": str(plan["risk_percent"]),
                        "risk_amount": str(leg_risk),
                        "risk_model": plan["risk_model"],
                        "selected_capital_at_entry": str(plan["selected_capital"]),
                        "position_value_at_entry": str(calc_position_value(leg_entry, leg_filled_qty, plan["point_value"])),
                        "point_value": str(plan["point_value"]),
                        "rr": str(plan.get("tp_rr") or DEFAULT_RR),
                        "tp_enabled": bool(plan.get("tp_enabled")),
                        "tp_stop_id": tp_id,
                        "sl_stop_id": sl_id,
                        "entry_portfolio": str(before_portfolio),
                        "entry_slice": f"aggregated/{len(posted_orders)}",
                        "entry_split_enabled": bool(plan.get("entry_split_enabled", True)),
                        "entry_split_count": plan.get("entry_split_count"),
                        "entry_split_mode": plan.get("entry_split_label") or "auto",
                        "entry_parent_qty": parent_leg_qty,
                        "entry_child_requested_qty": parent_leg_qty,
                        "entry_order_ids": ",".join(child_order_ids),
                        "managed_external": False,
                    }
                    add_active_trade(trade)

                    protection_summaries.append({
                        "account_id": account_id,
                        "account_label": account_label,
                        "qty": leg_filled_qty,
                        "entry": leg_entry,
                        "tp": leg_tp,
                        "sl": leg_sl,
                        "tp_stop_id": tp_id,
                        "sl_stop_id": sl_id,
                        "entry_orders": len(posted_orders),
                    })
                    results.append({
                        "status": "ok",
                        "account_id": account_id,
                        "account_label": account_label,
                        "requested_qty": parent_leg_qty,
                        "parent_leg_qty": parent_leg_qty,
                        "qty": leg_filled_qty,
                        "order_id": ",".join(child_order_ids),
                        "entry_orders": len(posted_orders),
                        "child_index": 0,
                        "child_count": child_count,
                        "child_fills": child_fills,
                        "child_fills_text": child_fills_text,
                        "tp_stop_id": tp_id,
                        "sl_stop_id": sl_id,
                        "entry": leg_entry,
                        "sl": leg_sl,
                        "tp": leg_tp,
                        "risk_amount": leg_risk,
                    })
                except Exception as exc:
                    errors.append(f"{account_label}: {exc}")

            protection_text = "; ".join(
                f"{item['account_label']} qty={item['qty']} entry={item['entry']} TP={fmt_optional_price(item['tp'])} SL={item['sl']}"
                for item in protection_summaries
            )
            overall_entry = (total_entry_weighted_sum / Decimal(total_filled)) if total_filled > 0 and total_entry_weighted_sum > 0 else plan["entry"]
            if len(protection_summaries) == 1:
                result_tp = protection_summaries[0]["tp"]
                result_sl = protection_summaries[0]["sl"]
                result_sl_distance = abs(protection_summaries[0]["entry"] - result_sl)
            else:
                result_tp = plan["tp"]
                result_sl = plan["sl"]
                result_sl_distance = plan["sl_distance"]

            if total_filled <= 0:
                return {
                    "status": "failed",
                    "ticker": plan["ticker"],
                    "side_name": plan["side_name"],
                    "requested_qty": plan["qty"],
                    "qty": 0,
                    "entry": plan["entry"],
                    "tp": plan["tp"],
                    "sl": plan["sl"],
                    "risk_amount": total_risk,
                    "sl_distance": plan["sl_distance"],
                    "results": results,
                    "errors": errors,
                    "entry_orders": posted_entry_orders,
                    "protection_summaries": protection_summaries,
                    "protection_text": protection_text,
                }

            return {
                "status": "ok",
                "ticker": plan["ticker"],
                "side_name": plan["side_name"],
                "requested_qty": plan["qty"],
                "qty": total_filled,
                "entry": overall_entry,
                "tp": result_tp,
                "sl": result_sl,
                "risk_amount": total_risk,
                "sl_distance": result_sl_distance,
                "results": results,
                "errors": errors,
                "entry_orders": posted_entry_orders,
                "protection_summaries": protection_summaries,
                "protection_text": protection_text,
            }

        def done(result, error):
            if error:
                self.log(f"Ошибка открытия: {error}")
                messagebox.showerror("Ошибка открытия", str(error))
                return
            if result["status"] == "failed":
                error_text = "\n".join(result.get("errors") or []) or "Входные заявки не исполнились."
                self.log(f"Вход не исполнен ни на одном счёте: {result['ticker']} по {result['entry']}. {error_text}")
                messagebox.showinfo("Не исполнено", f"Ни одна заявка не открылась.\n{error_text}")
                self.refresh_all_async()
                return

            ok_results = [item for item in result.get("results", []) if item.get("status") == "ok"]
            fills_text = "; ".join(
                f"{item['account_label']}: {item['qty']}/{item['requested_qty']} "
                f"({item.get('entry_orders', 0)} входн.; {item.get('child_fills_text') or 'fills —'})"
                for item in ok_results[:8]
            )
            if len(ok_results) > 8:
                fills_text += f"; ... +{len(ok_results) - 8} сч."
            protection_text = result.get("protection_text") or f"TP={fmt_optional_price(result['tp'])} SL={result['sl']}"
            errors_text = "\n".join(result.get("errors") or [])
            self.log(
                f"Открыт {result['side_name']} {result['ticker']} total_qty={result['qty']}/{result['requested_qty']} "
                f"entry_orders={result.get('entry_orders', 0)} "
                f"avg_entry={result['entry']} protection=[{protection_text}] "
                f"risk={fmt_money(result['risk_amount'])} distance={result['sl_distance']}п. [{fills_text}]"
            )
            if errors_text:
                self.log(f"Часть защиты/входов с ошибками: {errors_text}")
            try:
                inserted_journal = record_terminal_trade_result(plan, result)
                if inserted_journal:
                    self.log(f"Дневник сделок: добавлено из терминала {inserted_journal}")
                if hasattr(self, "trade_journal_panel"):
                    self.trade_journal_panel.refresh_rows()
                self.refresh_trade_stats_async()
            except Exception as exc:
                self.log(f"Дневник сделок: не записал сделку: {exc}")
            messagebox.showinfo(
                "Готово",
                f"Открыто: {result['side_name']} {result['ticker']}\n"
                f"Контракты: {result['qty']} из {result['requested_qty']}\n"
                f"Входных заявок: {result.get('entry_orders', 0)}\n"
                f"Исполнения: {fills_text}\n"
                f"Защита: {protection_text}\n"
                f"Риск: {fmt_money(result['risk_amount'])}"
                + (f"\n\nОшибки:\n{errors_text}" if errors_text else ""),
            )
            self.refresh_all_async()

        self.run_async(task, done)

    def row_legs(self, row: dict) -> list[dict]:
        legs = row.get("legs") or [row]
        return [leg for leg in legs if leg.get("account_id") and leg.get("account_id") != "AGGREGATED"]

    def leg_stop_ids(self, leg: dict, trade: dict | None = None) -> list[str]:
        ids = []
        for raw in (leg.get("tp_stop_id"), leg.get("sl_stop_id")):
            for stop_id in str(raw or "").split(","):
                stop_id = stop_id.strip()
                if stop_id and stop_id not in ids:
                    ids.append(stop_id)
        if trade:
            for raw in (trade.get("tp_stop_id"), trade.get("sl_stop_id")):
                for stop_id in str(raw or "").split(","):
                    stop_id = stop_id.strip()
                    if stop_id and stop_id not in ids:
                        ids.append(stop_id)
        return ids

    def selected_close_lots(self, row: dict, fraction: Decimal) -> int:
        lots = abs(row.get("qty_lots", Decimal("0")))
        if lots <= 0:
            die("Не вижу лоты по выбранной позиции.")
        qty = int(math.floor(float(lots * fraction)))
        return max(1, min(int(lots), qty))

    def close_selected_position_async(self, fraction: Decimal):
        row = self.get_selected_position_row()
        if not row:
            messagebox.showwarning("Позиция", "Выбери позицию в таблице открытых позиций.")
            return
        legs = self.row_legs(row)
        try:
            plan = [(leg, self.selected_close_lots(leg, fraction)) for leg in legs]
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        total_lots = sum(qty for _leg, qty in plan)
        side = row["side"]
        close_direction = "ORDER_DIRECTION_SELL" if side == "BUY" else "ORDER_DIRECTION_BUY"
        percent_text = fmt_percent(fraction * Decimal("100"))
        if not messagebox.askyesno(
            "Подтверждение",
            f"Закрыть {percent_text} позиции {row['ticker']} {row['side_text']} по рынку?\n"
            f"Счёт/счета: {row['account_label']}\nВсего к закрытию: {total_lots} лот(ов).",
        ):
            return

        self.log(f"Закрываю {row['ticker']} {row['side_text']} по рынку: accounts={row['account_label']}, lots={total_lots}")

        def task():
            results = []
            for leg, close_qty in plan:
                account_id = leg["account_id"]
                trade = find_active_trade(account_id, ticker=leg["ticker"], side=leg["side"], instrument_id=leg["instrument_id"])
                if fraction >= Decimal("1"):
                    for stop_id in self.leg_stop_ids(leg, trade):
                        try:
                            cancel_stop_order(account_id, stop_id)
                        except Exception:
                            pass

                order_id = post_market_order(
                    account_id,
                    leg["instrument_id"],
                    close_qty,
                    close_direction,
                    leg.get("class_code", ""),
                    confirm_margin=False,
                )
                filled = wait_fill(account_id, order_id, timeout_sec=20)
                after_portfolio = get_total_portfolio_value(account_id)
                pnl = None
                if trade and trade.get("entry_portfolio"):
                    pnl = after_portfolio - Decimal(str(trade["entry_portfolio"]))
                    if fraction >= Decimal("1"):
                        remove_active_trade(trade["trade_id"])

                # Excel trades_diary.xlsx logging is disabled.
                # SQLite trade journal sync handles terminal and external trades.
                results.append((account_id, filled, after_portfolio, pnl))
            return results

        def done(result, error):
            if error:
                self.log(f"Ошибка закрытия: {error}")
                messagebox.showerror("Ошибка закрытия", str(error))
                return
            filled_total = sum(item[1] for item in result)
            self.log(f"Закрытие {row['ticker']}: исполнено суммарно={filled_total} лот(ов).")
            self.refresh_all_async()

        self.run_async(task, done)

    def cancel_selected_protection_async(self):
        row = self.get_selected_position_row()
        if not row:
            messagebox.showwarning("Позиция", "Выбери позицию в таблице открытых позиций.")
            return
        legs = self.row_legs(row)

        cancel_plan = []
        for leg in legs:
            trade = find_active_trade(leg["account_id"], ticker=leg["ticker"], side=leg["side"], instrument_id=leg["instrument_id"])
            stop_ids = self.leg_stop_ids(leg, trade)
            if stop_ids:
                cancel_plan.append((leg, trade, stop_ids))

        if not cancel_plan:
            messagebox.showinfo("TP/SL", "Для выбранной позиции нет активных TP/SL.")
            return
        if not messagebox.askyesno("Подтверждение", f"Снять TP/SL по {row['ticker']} на {len(cancel_plan)} счёте/ноге?"):
            return

        def task():
            for leg, trade, stop_ids in cancel_plan:
                for stop_id in stop_ids:
                    try:
                        cancel_stop_order(leg["account_id"], stop_id)
                    except Exception:
                        pass
                if trade:
                    update_active_trade(trade["trade_id"], {"tp_stop_id": "", "sl_stop_id": "", "tp_price": "—", "sl_price": "—"})
            return True

        def done(result, error):
            if error:
                self.log(f"Ошибка снятия TP/SL: {error}")
                messagebox.showerror("Ошибка", str(error))
                return
            self.log(f"TP/SL по {row['ticker']} сняты.")
            self.refresh_all_async()

        self.run_async(task, done)

    def replace_selected_protection_async(self):
        row = self.get_selected_position_row()
        if not row:
            messagebox.showwarning("Позиция", "Выбери позицию в таблице открытых позиций.")
            return
        try:
            sl = parse_decimal(self.manage_sl_var.get(), "Новый SL")
            tp_manual = self.parse_optional_tp(self.manage_tp_var.get())
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        legs = self.row_legs(row)
        tp_text = str(tp_manual) if tp_manual is not None else "не ставится"
        if not messagebox.askyesno(
            "Подтверждение",
            f"Заменить защиту по {row['ticker']}?\nСчёт/счета: {row['account_label']}\nSL: {sl}\nTP: {tp_text}",
        ):
            return

        def task():
            updates = []
            for leg in legs:
                side = leg["side"]
                entry = leg["avg_raw"] if leg["avg_raw"] > 0 else leg["last_raw"]
                if entry <= 0:
                    die(f"Не вижу среднюю/текущую цену позиции {leg['ticker']} на счёте {self.account_short_label(leg['account_id'])}.")
                leg_instrument_id = self.resolve_leg_instrument_id_for_order(leg)
                if not leg_instrument_id:
                    die(f"Не вижу instrument id для защитной заявки {leg['ticker']}.")
                try:
                    full = get_instrument_full_by_uid(leg_instrument_id)
                except Exception:
                    try:
                        full = get_instrument_full_by_figi(leg.get("figi") or "")
                    except Exception:
                        full = {}
                step = get_min_step(leg, full)
                tp, sl_rounded = calc_sl_optional_tp(entry, side, step, sl, tp_manual)
                sl_rounded = self.directionally_round_level(entry, side, step, sl_rounded, "SL")
                if tp is not None:
                    tp = self.directionally_round_level(entry, side, step, tp, "TP")
                self.validate_trade_bracket(entry, side, sl_rounded, tp)
                if not self.is_plausible_protection_price(sl_rounded, entry):
                    die(f"SL выглядит как не цена, а ошибочное число: entry={entry}, SL={sl_rounded}.")
                if tp is not None and not self.is_plausible_protection_price(tp, entry):
                    die(f"TP выглядит как не цена, а ошибочное число: entry={entry}, TP={tp}.")
                qty = int(abs(leg["qty_lots"]))
                if qty <= 0:
                    die("Не вижу количество лотов для защитной заявки.")

                old_trade = find_active_trade(leg["account_id"], ticker=leg["ticker"], side=side, instrument_id=leg["instrument_id"])
                for stop_id in self.leg_stop_ids(leg, old_trade):
                    try:
                        cancel_stop_order(leg["account_id"], stop_id)
                    except Exception:
                        pass

                tp_id = ""
                if tp is not None:
                    tp_id = post_stop(leg["account_id"], leg_instrument_id, qty, tp, side, "STOP_ORDER_TYPE_TAKE_PROFIT", leg.get("class_code", ""))
                sl_id = post_stop(leg["account_id"], leg_instrument_id, qty, sl_rounded, side, "STOP_ORDER_TYPE_STOP_LOSS", leg.get("class_code", ""))

                if old_trade:
                    update_active_trade(old_trade["trade_id"], {
                        "qty": qty,
                        "entry_price": str(entry),
                        "tp_price": fmt_optional_price(tp),
                        "sl_price": str(sl_rounded),
                        "tp_stop_id": tp_id,
                        "sl_stop_id": sl_id,
                    })
                else:
                    before_portfolio = get_total_portfolio_value(leg["account_id"])
                    add_active_trade({
                        "trade_id": str(uuid.uuid4()),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "account_id": leg["account_id"],
                        "ticker": leg["ticker"],
                        "class_code": leg.get("class_code", ""),
                        "instrument_id": leg_instrument_id,
                        "side": side,
                        "qty": qty,
                        "entry_price": str(entry),
                        "tp_price": fmt_optional_price(tp),
                        "sl_price": str(sl_rounded),
                        "rr": str(DEFAULT_RR),
                        "tp_stop_id": tp_id,
                        "sl_stop_id": sl_id,
                        "entry_portfolio": str(before_portfolio),
                        "managed_external": True,
                    })
                updates.append((leg["account_id"], tp, sl_rounded))
            return updates

        def done(result, error):
            if error:
                self.log(f"Ошибка замены TP/SL: {error}")
                messagebox.showerror("Ошибка TP/SL", str(error))
                return
            details = "; ".join(f"{self.account_short_label(account_id)} TP={fmt_optional_price(tp)} SL={sl}" for account_id, tp, sl in result)
            self.log(f"Защита по {row['ticker']} заменена: {details}")
            self.refresh_all_async()

        self.run_async(task, done)

    # ---------------------------- OCO monitor ----------------------------

    def start_oco_monitor(self):
        if self.oco_started:
            return
        self.oco_started = True

        def loop():
            while True:
                try:
                    for account_id in self.get_selected_account_ids():
                        self.check_oco_once_for_account(account_id)
                except Exception as exc:
                    self.root.after(0, lambda e=exc: self.log(f"OCO-monitor: {e}"))
                time.sleep(OCO_CHECK_SECONDS)

        threading.Thread(target=loop, daemon=True).start()

    def check_oco_once_for_account(self, account_id: str):
        state = load_state()
        trades = state.get("active_trades", [])
        if not trades or not account_id:
            return

        active_stops = get_stop_orders(account_id)
        active_ids = {x.get("stopOrderId") for x in active_stops}

        changed = False
        for trade in list(trades):
            if trade.get("account_id") != account_id:
                continue

            tp_id = trade.get("tp_stop_id")
            sl_id = trade.get("sl_stop_id")
            if not tp_id and not sl_id:
                continue

            tp_active = tp_id in active_ids if tp_id else False
            sl_active = sl_id in active_ids if sl_id else False

            # If only one protective order was intentionally placed, keep the trade
            # in state while that single order is still active. This is important
            # for SL-only trades where the TP field was left blank.
            if tp_id and sl_id:
                if tp_active and sl_active:
                    continue
                if not tp_active and sl_active:
                    try:
                        cancel_stop_order(account_id, sl_id)
                    except Exception:
                        pass
                    self.root.after(0, lambda t=trade: self.log(f"OCO: TP по {t.get('ticker')} исчез/сработал, SL снят."))
                elif not sl_active and tp_active:
                    try:
                        cancel_stop_order(account_id, tp_id)
                    except Exception:
                        pass
                    self.root.after(0, lambda t=trade: self.log(f"OCO: SL по {t.get('ticker')} исчез/сработал, TP снят."))
                else:
                    self.root.after(0, lambda t=trade: self.log(f"OCO: обе защитные заявки по {t.get('ticker')} отсутствуют."))
            else:
                single_active = tp_active or sl_active
                if single_active:
                    continue
                self.root.after(0, lambda t=trade: self.log(f"Защитная заявка по {t.get('ticker')} исчезла/сработала."))

            # SQLite trade journal sync handles terminal and external trades.

            trades.remove(trade)
            changed = True

        if changed:
            state["active_trades"] = trades
            save_state(state)
            self.root.after(0, self.refresh_all_async)


def main():
    set_windows_app_user_model_id()
    root = tk.Tk()
    apply_window_icon(root)
    app = JTradeDarkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
