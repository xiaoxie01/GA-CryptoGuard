from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.crypto_guard.config.loader import PROJECT_ROOT


def planned_archive_path(root: str | Path, symbol: str, interval: str, year_month: str) -> Path:
    """Phase 9 归档路径规划；实际 Parquet 写入留给安装 DuckDB/pyarrow 的运行环境。"""

    return Path(root) / "klines" / "binance_um" / symbol / interval / f"{year_month}.parquet"


class ParquetKlineArchive:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else PROJECT_ROOT / "data" / "parquet" / "klines" / "binance_um"

    def path_for(self, symbol: str, interval: str, year_month: str) -> Path:
        return self.root / symbol / interval / f"{year_month}.parquet"

    def write_closed_klines(self, candles: list[dict[str, Any]], *, repo: Any | None = None) -> dict[str, Any]:
        closed = [c for c in candles if c.get("is_closed", True)]
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for candle in closed:
            symbol = str(candle["symbol"]).upper()
            interval = str(candle["interval"])
            dt = datetime.fromtimestamp(int(candle["open_time"]) / 1000, timezone.utc)
            ym = f"{dt.year:04d}-{dt.month:02d}"
            grouped.setdefault((symbol, interval, ym), []).append(_archive_row(candle))
        results = []
        for (symbol, interval, ym), rows in grouped.items():
            path = self.path_for(symbol, interval, ym)
            try:
                written = _write_parquet_dedup(path, rows)
                if repo:
                    repo.record_parquet_archive_run(symbol=symbol, interval=interval, year_month=ym, path=str(path), rows_written=written, status="success")
                results.append({"ok": True, "symbol": symbol, "interval": interval, "year_month": ym, "path": str(path), "rows_written": written})
            except Exception as exc:
                if repo:
                    repo.record_parquet_archive_run(symbol=symbol, interval=interval, year_month=ym, path=str(path), rows_written=0, status="failed", error_message=str(exc)[:500])
                results.append({"ok": False, "symbol": symbol, "interval": interval, "year_month": ym, "path": str(path), "error": str(exc)})
        return {"ok": all(r.get("ok") for r in results), "results": results, "closed_rows": len(closed)}


def archive_status(root: str | Path = "data/crypto_guard/archive") -> dict[str, Any]:
    return {
        "ok": True,
        "implemented": "path_contract_only",
        "root": str(Path(root)),
        "note": "MVP 使用 SQLite 热数据；DuckDB/Parquet 历史回放接口已预留。",
    }


def _archive_row(row: dict[str, Any]) -> dict[str, Any]:
    open_time = int(row["open_time"])
    close_time = int(row["close_time"])
    return {
        "exchange": "binance",
        "market_type": "um_futures",
        "symbol": str(row["symbol"]).upper(),
        "interval": str(row["interval"]),
        "open_time": open_time,
        "open_time_utc": datetime.fromtimestamp(open_time / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "close_time": close_time,
        "close_time_utc": datetime.fromtimestamp(close_time / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
        "quote_volume": _optional_float(row.get("quote_volume")),
        "trade_count": int(row["trade_count"]) if row.get("trade_count") not in (None, "") else None,
        "taker_buy_base_volume": _optional_float(row.get("taker_buy_volume") or row.get("taker_buy_base_volume")),
        "taker_buy_quote_volume": _optional_float(row.get("taker_buy_quote_volume")),
        "ingested_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _write_parquet_dedup(path: Path, rows: list[dict[str, Any]]) -> int:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pd.DataFrame(rows)
    if path.exists():
        existing = pd.read_parquet(path)
        frame = pd.concat([existing, incoming], ignore_index=True)
    else:
        frame = incoming
    frame = frame.drop_duplicates(subset=["symbol", "interval", "open_time"], keep="last").sort_values("open_time")
    frame.to_parquet(path, index=False)
    return int(len(frame))


def read_klines_file(path: str | Path, *, symbol: str | None = None, interval: str | None = None) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {"ok": False, "error": f"historical file not found: {source}", "rows": []}
    try:
        if source.suffix.lower() == ".parquet":
            rows = _read_parquet(source)
        elif source.suffix.lower() == ".json":
            import json

            rows = json.loads(source.read_text(encoding="utf-8"))
        elif source.suffix.lower() == ".csv":
            rows = _read_csv(source)
        else:
            return {"ok": False, "error": f"unsupported historical file type: {source.suffix}", "rows": []}
        normalized = [_normalize_row(row, symbol=symbol, interval=interval) for row in rows]
        normalized.sort(key=lambda r: int(r["open_time"]))
        return {"ok": True, "path": str(source), "count": len(normalized), "rows": normalized}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "rows": []}


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq

        return pq.read_table(path).to_pylist()
    except ImportError:
        try:
            import pandas as pd

            return pd.read_parquet(path).to_dict("records")
        except ImportError as exc:
            raise RuntimeError("reading parquet requires pyarrow or pandas") from exc


def _read_csv(path: Path) -> list[dict[str, Any]]:
    import csv

    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _normalize_row(row: dict[str, Any], *, symbol: str | None, interval: str | None) -> dict[str, Any]:
    out = dict(row)
    out["symbol"] = str(out.get("symbol") or symbol or "").upper()
    out["interval"] = str(out.get("interval") or interval or "")
    required = ["symbol", "interval", "open_time", "close_time", "open", "high", "low", "close", "volume"]
    missing = [key for key in required if out.get(key) in (None, "")]
    if missing:
        raise ValueError(f"historical kline missing fields: {missing}")
    return {
        "symbol": out["symbol"],
        "interval": out["interval"],
        "open_time": int(out["open_time"]),
        "close_time": int(out["close_time"]),
        "open": float(out["open"]),
        "high": float(out["high"]),
        "low": float(out["low"]),
        "close": float(out["close"]),
        "volume": float(out["volume"]),
        "quote_volume": _optional_float(out.get("quote_volume")),
        "taker_buy_volume": _optional_float(out.get("taker_buy_volume")),
        "taker_buy_quote_volume": _optional_float(out.get("taker_buy_quote_volume")),
        "trade_count": int(out["trade_count"]) if out.get("trade_count") not in (None, "") else None,
        "is_closed": bool(int(out.get("is_closed", 1))) if isinstance(out.get("is_closed", 1), str) else bool(out.get("is_closed", True)),
        "source": "historical_replay",
    }


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
