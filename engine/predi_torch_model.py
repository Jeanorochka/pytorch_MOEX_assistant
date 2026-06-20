import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset
except Exception:
    torch = None
    nn = None
    DataLoader = None
    TensorDataset = None


MODEL_FILE = "predi_torch_model.pt"
MIN_TORCH_SAMPLES = 120
MAX_TRAIN_ROWS = 12000
MODEL_VERSION = 2
_CACHE: dict[str, Any] = {"loaded_at": 0.0, "mtime": 0.0, "payload": None, "model": None}


def configure_torch_runtime() -> None:
    """Enable CUDA-friendly PyTorch runtime settings when the local machine supports them."""
    if not torch_available():
        return
    try:
        if torch.cuda.is_available():
            try:
                torch.backends.cudnn.benchmark = True
            except Exception:
                pass
            try:
                torch.set_float32_matmul_precision("high")
            except Exception:
                pass
            try:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
            except Exception:
                pass
    except Exception:
        pass


def get_gpu_name() -> str:
    if not torch_available():
        return ""
    try:
        if torch.cuda.is_available():
            return str(torch.cuda.get_device_name(0))
    except Exception:
        pass
    return ""


def torch_available() -> bool:
    return torch is not None and nn is not None


def get_device() -> str:
    if not torch_available():
        return "unavailable"
    configure_torch_runtime()
    try:
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class PrediTorchNet(nn.Module):
    def __init__(self, n_features: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_features, 128),
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Dropout(0.08),
            nn.Linear(128, 64),
            nn.LayerNorm(64),
            nn.GELU(),
            nn.Dropout(0.06),
            nn.Linear(64, 32),
            nn.GELU(),
            nn.Linear(32, 1),
        )

    def forward(self, x):
        return self.net(x).squeeze(-1)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _model_path(db_path: str | Path) -> Path:
    db_path = Path(db_path)
    return db_path.parent / MODEL_FILE


def _read_dataset(db_path: str | Path) -> tuple[list[dict[str, float]], list[int]]:
    db_path = Path(db_path)
    if not db_path.exists():
        return [], []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT features_json, label
            FROM brain_observations
            WHERE status='labeled' AND label IN (0,1)
              AND features_json IS NOT NULL AND features_json != '{}' AND features_json != ''
            ORDER BY evaluated_at DESC
            LIMIT ?
            """,
            (MAX_TRAIN_ROWS,),
        ).fetchall()

    features_rows: list[dict[str, float]] = []
    labels: list[int] = []
    for row in rows:
        try:
            data = json.loads(row["features_json"] or "{}")
            if not isinstance(data, dict) or not data:
                continue
            clean = {str(k): max(-5.0, min(5.0, float(v))) for k, v in data.items()}
            features_rows.append(clean)
            labels.append(int(row["label"]))
        except Exception:
            continue
    return features_rows, labels


def _build_matrix(feature_rows: list[dict[str, float]], feature_names: list[str] | None = None):
    if feature_names is None:
        names = sorted({k for row in feature_rows for k in row.keys()})
    else:
        names = list(feature_names)
    if not names:
        return None, []
    matrix = []
    for row in feature_rows:
        matrix.append([_safe_float(row.get(name), 0.0) for name in names])
    return matrix, names


def _compute_validation_metrics(logits, y):
    with torch.no_grad():
        probs = torch.sigmoid(logits)
        pred = (probs >= 0.5).float()
        acc = (pred == y).float().mean().item() * 100.0
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, y).item()
    return acc, loss


def train_torch_model_from_db(db_path: str | Path, force: bool = False) -> dict[str, Any]:
    configure_torch_runtime()
    if not torch_available():
        return {"available": False, "trained": False, "reason": "torch_not_installed", "backend": "unavailable"}

    db_path = Path(db_path)
    model_path = _model_path(db_path)
    feature_rows, labels = _read_dataset(db_path)
    if len(labels) < MIN_TORCH_SAMPLES:
        return {"available": True, "trained": False, "reason": "not_enough_samples", "samples": len(labels), "backend": get_device()}

    # Avoid full retrain too frequently if the DB has not meaningfully changed.
    latest_mtime = db_path.stat().st_mtime if db_path.exists() else time.time()
    if model_path.exists() and not force:
        try:
            payload = torch.load(model_path, map_location="cpu")
            meta = payload.get("meta", {})
            if int(meta.get("samples", 0)) >= len(labels) and float(meta.get("db_mtime", 0.0)) >= latest_mtime - 0.5:
                return {"available": True, "trained": True, "reason": "already_current", **meta}
        except Exception:
            pass

    matrix, feature_names = _build_matrix(feature_rows)
    if matrix is None or not feature_names:
        return {"available": True, "trained": False, "reason": "empty_features", "samples": len(labels), "backend": get_device()}

    device_name = get_device()
    device = torch.device(device_name if device_name in {"cuda", "cpu"} else "cpu")
    x = torch.tensor(matrix, dtype=torch.float32)
    y = torch.tensor(labels, dtype=torch.float32)

    # Stable chronological-ish split: rows are newest first, so shuffle deterministically.
    g = torch.Generator()
    g.manual_seed(42)
    perm = torch.randperm(len(y), generator=g)
    x = x[perm]
    y = y[perm]

    val_size = max(16, min(int(len(y) * 0.20), 600))
    if len(y) - val_size < 80:
        val_size = max(8, int(len(y) * 0.12))
    x_val, y_val = x[:val_size], y[:val_size]
    x_train, y_train = x[val_size:], y[val_size:]

    model = PrediTorchNet(len(feature_names)).to(device)
    pos = float((y_train == 1).sum().item())
    neg = float((y_train == 0).sum().item())
    pos_weight = torch.tensor([max(0.5, min(3.0, neg / max(1.0, pos)))], dtype=torch.float32, device=device)
    criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0025, weight_decay=0.0015)

    dataset = TensorDataset(x_train, y_train)
    loader = DataLoader(
        dataset,
        batch_size=min(256, max(32, len(dataset) // 4)),
        shuffle=True,
        pin_memory=(device_name == "cuda"),
    )

    best_state = None
    best_val_loss = float("inf")
    best_val_acc = 0.0
    epochs = 22 if len(y_train) >= 600 else 14
    patience = 5
    bad = 0

    for _epoch in range(epochs):
        model.train()
        for xb, yb in loader:
            xb = xb.to(device)
            yb = yb.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.5)
            optimizer.step()

        model.eval()
        logits_val = model(x_val.to(device))
        val_acc, val_loss = _compute_validation_metrics(logits_val.cpu(), y_val)
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_val_acc = val_acc
            best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                break

    if best_state is None:
        best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}

    meta = {
        "version": MODEL_VERSION,
        "backend": device_name,
        "cuda_available": bool(device_name == "cuda"),
        "gpu_name": get_gpu_name(),
        "samples": int(len(labels)),
        "train_samples": int(len(y_train)),
        "val_samples": int(len(y_val)),
        "val_accuracy_pct": float(best_val_acc),
        "val_loss": float(best_val_loss),
        "features": feature_names,
        "db_mtime": float(latest_mtime),
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"meta": meta, "state_dict": best_state}, model_path)
    _CACHE.update({"loaded_at": 0.0, "mtime": 0.0, "payload": None, "model": None})
    return {"available": True, "trained": True, **meta}


def _load_model(db_path: str | Path):
    if not torch_available():
        return None, None
    configure_torch_runtime()
    model_path = _model_path(db_path)
    if not model_path.exists():
        return None, None
    mtime = model_path.stat().st_mtime
    device_name = get_device()
    cache_key = f"{mtime}:{device_name}"
    if _CACHE.get("payload") is not None and _CACHE.get("mtime") == cache_key:
        return _CACHE.get("model"), _CACHE.get("payload")
    try:
        payload = torch.load(model_path, map_location="cpu")
        meta = payload.get("meta", {})
        features = meta.get("features") or []
        if not features:
            return None, None
        device = torch.device(device_name if device_name in {"cuda", "cpu"} else "cpu")
        model = PrediTorchNet(len(features))
        model.load_state_dict(payload.get("state_dict") or {})
        model.to(device)
        model.eval()
        _CACHE.update({"loaded_at": time.time(), "mtime": cache_key, "payload": payload, "model": model})
        return model, payload
    except Exception:
        return None, None


def predict_torch_probability(db_path: str | Path, features: dict[str, float]) -> dict[str, Any]:
    if not torch_available():
        return {"available": False, "trained": False, "probability": None, "reason": "torch_not_installed", "backend": "unavailable"}
    configure_torch_runtime()
    model, payload = _load_model(db_path)
    if model is None or payload is None:
        train_info = train_torch_model_from_db(db_path, force=False)
        if train_info.get("trained"):
            model, payload = _load_model(db_path)
        if model is None or payload is None:
            return {
                "available": True,
                "trained": False,
                "probability": None,
                "reason": train_info.get("reason") or "model_not_trained",
                "backend": get_device(),
                "cuda_available": bool(get_device() == "cuda"),
                "gpu_name": get_gpu_name(),
                "samples": int(train_info.get("samples") or 0),
            }
    meta = payload.get("meta", {})
    names = meta.get("features") or []
    vector = [[_safe_float(features.get(name), 0.0) for name in names]]
    try:
        with torch.no_grad():
            device = next(model.parameters()).device
            x = torch.tensor(vector, dtype=torch.float32, device=device)
            logits = model(x)
            probability = float(torch.sigmoid(logits.detach().cpu())[0].item() * 100.0)
        backend_now = str(device).split(":", 1)[0]
        return {
            "available": True,
            "trained": True,
            "probability": max(0.0, min(100.0, probability)),
            "backend": "cuda" if backend_now == "cuda" else (meta.get("backend") or get_device()),
            "cuda_available": bool(get_device() == "cuda"),
            "gpu_name": get_gpu_name(),
            "samples": int(meta.get("samples") or 0),
            "val_accuracy_pct": meta.get("val_accuracy_pct"),
            "trained_at": meta.get("trained_at") or "",
            "feature_count": len(names),
            "model_file": str(_model_path(db_path)),
        }
    except Exception as exc:
        return {"available": True, "trained": False, "probability": None, "reason": str(exc), "backend": get_device(), "gpu_name": get_gpu_name()}


def get_torch_status(db_path: str | Path | None = None) -> dict[str, Any]:
    status = {
        "available": torch_available(),
        "backend": get_device(),
        "cuda_available": False,
        "gpu_name": "",
        "model_trained": False,
        "samples": 0,
        "val_accuracy_pct": None,
        "trained_at": "",
    }
    if torch_available():
        try:
            status["cuda_available"] = bool(torch.cuda.is_available())
            if status["cuda_available"]:
                status["gpu_name"] = str(torch.cuda.get_device_name(0))
        except Exception:
            pass
    if db_path:
        model_path = _model_path(db_path)
        if model_path.exists() and torch_available():
            try:
                payload = torch.load(model_path, map_location="cpu")
                meta = payload.get("meta", {})
                status.update({
                    "model_trained": True,
                    "samples": int(meta.get("samples") or 0),
                    "val_accuracy_pct": meta.get("val_accuracy_pct"),
                    "trained_at": meta.get("trained_at") or "",
                    "backend": meta.get("backend") or status["backend"],
                    "cuda_available": bool(meta.get("cuda_available") or status["cuda_available"]),
                    "gpu_name": meta.get("gpu_name") or status["gpu_name"],
                    "feature_count": len(meta.get("features") or []),
                    "model_file": str(model_path),
                })
            except Exception:
                pass
    return status
