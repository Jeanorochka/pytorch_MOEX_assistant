import json
import sqlite3
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from architecture import APP_DIR, post, money_to_decimal, q_to_decimal, fmt_dec, fmt_money

DB_DIR = APP_DIR / "db"
TRADE_DB_PATH = DB_DIR / "jtrade_trades.db"
LEGACY_TRADE_DB_PATH = APP_DIR / "jtrade_trades.db"
DEFAULT_LOOKBACK_DAYS = 3650
BROKER_SYNC_CHUNK_DAYS = 90
JOURNAL_DISPLAY_LIMIT = 5000
TRADE_OPERATION_TYPES = [
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_BUY_MARGIN",
    "OPERATION_TYPE_SELL_MARGIN",
    "OPERATION_TYPE_DELIVERY_BUY",
    "OPERATION_TYPE_DELIVERY_SELL",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Пустая дата")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        raw2 = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        raise ValueError("Дата должна быть YYYY-MM-DD или DD.MM.YYYY")


def to_api_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_operation_type(value: str) -> str:
    return str(value or "").upper().strip()


def side_from_operation_type(operation_type: str) -> str:
    op = normalize_operation_type(operation_type)
    if "SELL" in op:
        return "SELL"
    if "BUY" in op:
        return "BUY"
    return "—"


def side_text(side: str) -> str:
    return "SHORT/SELL" if side == "SELL" else "LONG/BUY" if side == "BUY" else "—"


def pick_first(data: dict, *keys, default=""):
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return data.get(key)
    return default


def parse_moneyish(value) -> Decimal:
    if isinstance(value, dict):
        return money_to_decimal(value)
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def trade_price_from_trade(trade: dict, fallback: Decimal = Decimal("0")) -> Decimal:
    for key in ("price", "pricePt", "price_pt"):
        if isinstance(trade, dict) and trade.get(key) not in (None, ""):
            return parse_moneyish(trade.get(key))
    return fallback


def trade_quantity_from_trade(trade: dict, fallback: Decimal = Decimal("0")) -> Decimal:
    for key in ("quantity", "qty", "numLots", "num_lots"):
        if isinstance(trade, dict) and trade.get(key) not in (None, ""):
            try:
                return Decimal(str(trade.get(key)))
            except Exception:
                pass
    return fallback


def operation_time(operation: dict, trade: dict | None = None) -> str:
    trade = trade or {}
    return str(
        pick_first(trade, "dateTime", "date_time", "time", default="")
        or pick_first(operation, "date", "dateTime", "date_time", "time", default="")
        or utc_now_iso()
    )


def operation_payment(operation: dict) -> Decimal:
    return parse_moneyish(pick_first(operation, "payment", "price", "amount", default={}))


def operation_currency(operation: dict) -> str:
    payment = operation.get("payment") if isinstance(operation, dict) else None
    if isinstance(payment, dict):
        return str(payment.get("currency") or "")
    return str(operation.get("currency") or operation.get("currencyName") or "")


def operation_id(operation: dict) -> str:
    return str(pick_first(operation, "id", "operationId", "operation_id", default=""))


def trade_id(trade: dict) -> str:
    return str(pick_first(trade, "tradeId", "trade_id", "id", default=""))


def operation_ticker(operation: dict) -> str:
    return str(pick_first(operation, "ticker", "instrumentTicker", "instrument_ticker", default=""))


def operation_figi(operation: dict) -> str:
    return str(pick_first(operation, "figi", "instrumentUid", "instrument_uid", "instrumentId", "instrument_id", default=""))


def ensure_trade_db_location() -> None:
    """Keep all journal/model SQLite data under project_root/db/."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    if LEGACY_TRADE_DB_PATH.exists() and not TRADE_DB_PATH.exists():
        try:
            shutil.move(str(LEGACY_TRADE_DB_PATH), str(TRADE_DB_PATH))
        except Exception:
            try:
                shutil.copy2(str(LEGACY_TRADE_DB_PATH), str(TRADE_DB_PATH))
            except Exception:
                pass


def init_trade_db() -> None:
    ensure_trade_db_location()
    with sqlite3.connect(TRADE_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                uid TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                account_id TEXT NOT NULL,
                account_label TEXT,
                operation_id TEXT,
                trade_id TEXT,
                order_id TEXT,
                time TEXT NOT NULL,
                ticker TEXT,
                figi TEXT,
                instrument_id TEXT,
                side TEXT,
                quantity REAL,
                price REAL,
                payment REAL,
                currency TEXT,
                commission REAL,
                tp_price REAL,
                sl_price REAL,
                terminal_trade_id TEXT,
                raw_json TEXT,
                synced_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_account_time ON trades(account_id, time)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_samples (
                uid TEXT PRIMARY KEY,
                trade_uid TEXT UNIQUE,
                ticker TEXT,
                side TEXT,
                entry_time TEXT,
                entry_price REAL,
                tp_price REAL,
                sl_price REAL,
                horizon_bars INTEGER DEFAULT 78,
                outcome TEXT DEFAULT 'pending',
                features_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def upsert_trade(row: dict) -> bool:
    init_trade_db()
    with sqlite3.connect(TRADE_DB_PATH) as conn:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO trades (
                uid, source, account_id, account_label, operation_id, trade_id, order_id,
                time, ticker, figi, instrument_id, side, quantity, price, payment, currency,
                commission, tp_price, sl_price, terminal_trade_id, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("uid"), row.get("source"), row.get("account_id"), row.get("account_label"),
                row.get("operation_id"), row.get("trade_id"), row.get("order_id"), row.get("time"),
                row.get("ticker"), row.get("figi"), row.get("instrument_id"), row.get("side"),
                float(row.get("quantity") or 0), float(row.get("price") or 0), float(row.get("payment") or 0),
                row.get("currency"), float(row.get("commission") or 0),
                float(row.get("tp_price") or 0) if row.get("tp_price") not in (None, "", "—") else None,
                float(row.get("sl_price") or 0) if row.get("sl_price") not in (None, "", "—") else None,
                row.get("terminal_trade_id"), row.get("raw_json"), row.get("synced_at"),
            ),
        )
        inserted = conn.total_changes > before
        if inserted:
            seed_model_sample(conn, row)
        conn.commit()
        return inserted


def seed_model_sample(conn: sqlite3.Connection, row: dict) -> None:
    side = row.get("side")
    ticker = row.get("ticker")
    price = row.get("price")
    if side not in {"BUY", "SELL"} or not ticker or not price:
        return
    trade_uid = row.get("uid")
    now = utc_now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO model_samples (
            uid, trade_uid, ticker, side, entry_time, entry_price, tp_price, sl_price,
            outcome, features_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}', ?, ?)
        """,
        (
            str(uuid.uuid4()), trade_uid, ticker, side, row.get("time"), float(price or 0),
            float(row.get("tp_price") or 0) if row.get("tp_price") not in (None, "", "—") else None,
            float(row.get("sl_price") or 0) if row.get("sl_price") not in (None, "", "—") else None,
            now, now,
        ),
    )


def rows_from_operation(account_id: str, account_label: str, operation: dict) -> list[dict]:
    op_type = normalize_operation_type(operation.get("operationType") or operation.get("type") or operation.get("operation_type"))
    if op_type and op_type not in TRADE_OPERATION_TYPES and not ("BUY" in op_type or "SELL" in op_type):
        return []

    side = side_from_operation_type(op_type)
    op_id = operation_id(operation)
    ticker = operation_ticker(operation)
    figi = operation_figi(operation)
    payment = operation_payment(operation)
    currency = operation_currency(operation)
    trades = operation.get("trades") or operation.get("operationItems") or []
    rows = []

    if isinstance(trades, list) and trades:
        fallback_qty = Decimal("0")
        fallback_price = Decimal("0")
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            tid = trade_id(trade) or str(len(rows) + 1)
            qty = trade_quantity_from_trade(trade, fallback_qty)
            price = trade_price_from_trade(trade, fallback_price)
            uid = f"broker:{account_id}:{op_id}:{tid}"
            rows.append({
                "uid": uid,
                "source": "broker",
                "account_id": account_id,
                "account_label": account_label,
                "operation_id": op_id,
                "trade_id": tid,
                "order_id": str(pick_first(operation, "parentOperationId", "orderId", "order_id", default="")),
                "time": operation_time(operation, trade),
                "ticker": ticker,
                "figi": figi,
                "instrument_id": figi,
                "side": side,
                "quantity": float(qty),
                "price": float(price),
                "payment": float(payment),
                "currency": currency,
                "commission": 0.0,
                "tp_price": None,
                "sl_price": None,
                "terminal_trade_id": "",
                "raw_json": json.dumps({"operation": operation, "trade": trade}, ensure_ascii=False),
                "synced_at": utc_now_iso(),
            })
    else:
        qty = Decimal("0")
        for key in ("quantity", "quantityDone", "quantity_done", "lots", "quantityRest"):
            if operation.get(key) not in (None, ""):
                try:
                    qty = Decimal(str(operation.get(key)))
                    break
                except Exception:
                    pass
        price = abs(payment / qty) if qty else Decimal("0")
        uid = f"broker:{account_id}:{op_id}:operation"
        rows.append({
            "uid": uid,
            "source": "broker",
            "account_id": account_id,
            "account_label": account_label,
            "operation_id": op_id,
            "trade_id": "operation",
            "order_id": str(pick_first(operation, "parentOperationId", "orderId", "order_id", default="")),
            "time": operation_time(operation),
            "ticker": ticker,
            "figi": figi,
            "instrument_id": figi,
            "side": side,
            "quantity": float(qty),
            "price": float(price),
            "payment": float(payment),
            "currency": currency,
            "commission": 0.0,
            "tp_price": None,
            "sl_price": None,
            "terminal_trade_id": "",
            "raw_json": json.dumps({"operation": operation}, ensure_ascii=False),
            "synced_at": utc_now_iso(),
        })
    return rows


def _fetch_operations_by_cursor_range(account_id: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    cursor = ""
    operations: list[dict] = []
    while True:
        payload = {
            "accountId": account_id,
            "from": to_api_time(start_dt),
            "to": to_api_time(end_dt),
            "limit": 1000,
            "state": "OPERATION_STATE_EXECUTED",
            "withoutCommissions": False,
            "withoutTrades": False,
            "withoutOvernights": True,
            "operationTypes": TRADE_OPERATION_TYPES,
        }
        if cursor:
            payload["cursor"] = cursor
        data = post("OperationsService/GetOperationsByCursor", payload)
        chunk = data.get("items") or data.get("operations") or []
        if isinstance(chunk, list):
            operations.extend([x for x in chunk if isinstance(x, dict)])
        cursor = str(data.get("nextCursor") or data.get("next_cursor") or "")
        has_next = bool(data.get("hasNext") or data.get("has_next"))
        if not cursor or not has_next:
            break
    return operations


def iter_time_chunks(start_dt: datetime, end_dt: datetime, chunk_days: int = BROKER_SYNC_CHUNK_DAYS):
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    start_dt = start_dt.astimezone(timezone.utc)
    end_dt = end_dt.astimezone(timezone.utc)
    if end_dt <= start_dt:
        return

    cursor_dt = start_dt
    step = timedelta(days=max(1, int(chunk_days)))
    while cursor_dt < end_dt:
        next_dt = min(cursor_dt + step, end_dt)
        yield cursor_dt, next_dt
        cursor_dt = next_dt


def fetch_operations_by_cursor(account_id: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    # Large ranges are split into chunks: this gives the journal enough historical data
    # while keeping every broker request light and less likely to time out.
    operations: list[dict] = []
    seen_operation_ids: set[str] = set()

    for chunk_start, chunk_end in iter_time_chunks(start_dt, end_dt):
        for operation in _fetch_operations_by_cursor_range(account_id, chunk_start, chunk_end):
            uid = operation_id(operation) or f"{operation_time(operation)}:{operation_ticker(operation)}:{operation_payment(operation)}"
            if uid in seen_operation_ids:
                continue
            seen_operation_ids.add(uid)
            operations.append(operation)
    return operations


def sync_broker_trades(account_ids: list[str], label_for_account, start_dt: datetime, end_dt: datetime) -> dict:
    init_trade_db()
    inserted = 0
    scanned = 0
    errors = []
    for account_id in account_ids:
        try:
            label = label_for_account(account_id)
            operations = fetch_operations_by_cursor(account_id, start_dt, end_dt)
            for operation in operations:
                for row in rows_from_operation(account_id, label, operation):
                    scanned += 1
                    if upsert_trade(row):
                        inserted += 1
        except Exception as exc:
            errors.append(f"{label_for_account(account_id)}: {exc}")
    return {"inserted": inserted, "scanned": scanned, "errors": errors}


def record_terminal_trade_result(plan: dict, result: dict) -> int:
    init_trade_db()
    inserted = 0
    now = utc_now_iso()
    for item in result.get("results", []):
        if item.get("status") != "ok":
            continue
        uid = f"terminal:{item.get('account_id')}:{item.get('order_id')}:{item.get('qty')}"
        row = {
            "uid": uid,
            "source": "terminal",
            "account_id": item.get("account_id") or "",
            "account_label": item.get("account_label") or "",
            "operation_id": "",
            "trade_id": "",
            "order_id": item.get("order_id") or "",
            "time": now,
            "ticker": plan.get("ticker") or result.get("ticker") or "",
            "figi": plan.get("instrument_id") or "",
            "instrument_id": plan.get("instrument_id") or "",
            "side": plan.get("side") or "",
            "quantity": float(item.get("qty") or 0),
            "price": float(item.get("entry") or result.get("entry") or 0),
            "payment": 0.0,
            "currency": "RUB",
            "commission": 0.0,
            "tp_price": None if plan.get("tp") in (None, "", "—") else float(plan.get("tp")),
            "sl_price": None if item.get("sl") in (None, "", "—") else float(item.get("sl")),
            "terminal_trade_id": "",
            "raw_json": json.dumps({"plan": _json_safe(plan), "result_item": _json_safe(item)}, ensure_ascii=False),
            "synced_at": now,
        }
        if upsert_trade(row):
            inserted += 1
    return inserted


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def load_trade_rows(limit: int = 500) -> list[dict]:
    init_trade_db()
    with sqlite3.connect(TRADE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM trades
            ORDER BY time DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]


class TradeJournalPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        init_trade_db()
        self.sync_status_var = tk.StringVar(value="Журнал: db/jtrade_trades.db")
        today = datetime.now(timezone.utc).date()
        self.from_var = tk.StringVar(value=str(today - timedelta(days=DEFAULT_LOOKBACK_DAYS)))
        self.to_var = tk.StringVar(value=str(today + timedelta(days=1)))
        self.build()
        self.refresh_rows()

    def build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        controls = ttk.Frame(self, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="C", style="Muted.TLabel").pack(side="left", padx=(0, 4))
        ttk.Entry(controls, textvariable=self.from_var, width=12).pack(side="left", padx=(0, 8))
        ttk.Label(controls, text="По", style="Muted.TLabel").pack(side="left", padx=(0, 4))
        ttk.Entry(controls, textvariable=self.to_var, width=12).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Синхронизировать", command=self.sync_selected_period).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Обновить", command=self.refresh_rows).pack(side="left", padx=(0, 8))
        ttk.Label(controls, textvariable=self.sync_status_var, style="Muted.TLabel").pack(side="left", padx=(8, 0))

        columns = [
            ("time", "Время", 155, "w"),
            ("source", "Источник", 85, "center"),
            ("account", "Счёт", 120, "w"),
            ("ticker", "Тикер", 90, "w"),
            ("side", "Сторона", 90, "center"),
            ("qty", "Кол-во", 90, "e"),
            ("price", "Цена", 100, "e"),
            ("tp", "TP", 90, "e"),
            ("sl", "SL", 90, "e"),
            ("payment", "Сумма", 120, "e"),
            ("uid", "UID", 220, "w"),
        ]
        frame = ttk.Frame(self)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=22)
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        for key, title, width, anchor in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=60, anchor=anchor, stretch=True)
        self.tree.tag_configure("buy", foreground="#3DDC97")
        self.tree.tag_configure("sell", foreground="#FF5B6A")
        self.tree.tag_configure("muted", foreground="#AEB8C2")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

    def set_max_history_period(self):
        today = datetime.now(timezone.utc).date()
        self.from_var.set(str(today - timedelta(days=DEFAULT_LOOKBACK_DAYS)))
        self.to_var.set(str(today + timedelta(days=1)))

    def auto_sync_on_startup(self):
        self.sync_selected_period(silent=True)

    def sync_selected_period(self, silent: bool = False):
        try:
            start_dt = parse_dt(self.from_var.get())
            end_dt = parse_dt(self.to_var.get())
        except Exception as exc:
            if not silent:
                messagebox.showerror("Дата", str(exc))
            return
        account_ids = self.app.get_selected_account_ids()
        if not account_ids:
            return
        self.sync_status_var.set("Синхронизация...")

        def task():
            return sync_broker_trades(account_ids, self.app.account_short_label, start_dt, end_dt)

        def done(result, error):
            if error:
                self.sync_status_var.set("Ошибка синхронизации")
                self.app.log(f"Журнал сделок: {error}")
                if not silent:
                    messagebox.showerror("Журнал сделок", str(error))
                return
            result = result or {}
            inserted = result.get("inserted", 0)
            scanned = result.get("scanned", 0)
            errors = result.get("errors") or []
            self.sync_status_var.set(f"Синхронизировано: +{inserted}, просмотрено: {scanned}")
            if errors:
                self.app.log("Журнал сделок: " + "; ".join(errors))
            self.refresh_rows()
        self.app.run_async(task, done)

    def refresh_rows(self):
        rows = load_trade_rows(JOURNAL_DISPLAY_LIMIT)
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for row in rows:
            side = row.get("side") or "—"
            tag = "buy" if side == "BUY" else "sell" if side == "SELL" else "muted"
            self.tree.insert("", "end", values=(
                row.get("time") or "",
                row.get("source") or "",
                row.get("account_label") or row.get("account_id") or "",
                row.get("ticker") or "",
                side_text(side),
                fmt_dec(row.get("quantity") or 0, 0),
                fmt_dec(row.get("price") or 0, 4),
                fmt_dec(row.get("tp_price"), 4) if row.get("tp_price") not in (None, "") else "—",
                fmt_dec(row.get("sl_price"), 4) if row.get("sl_price") not in (None, "") else "—",
                fmt_dec(row.get("payment") or 0, 2),
                row.get("uid") or "",
            ), tags=(tag,))
