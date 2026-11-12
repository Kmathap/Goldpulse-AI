"""Signal journal: records every live setup of the active strategy and its outcome.

* Runs inside the server (background thread), checking about once a minute.
* At every NEW closed H1 candle it takes the same engine snapshot the strategy
  lab uses, then replays the lab's exact trade logic (backend.strategy_lab.simulate)
  over all candles since the journal started. Live results are therefore
  directly comparable with the backtest.
* Only setups AFTER the journal's start time are recorded – nothing is back-filled.
* Saved in the journal/ folder: meta.json, snapshots.jsonl, trades.json.

Outcomes are measured on the same chart series the dashboard uses (free feed:
delayed, possibly futures-based). No orders are ever placed.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from backend.backtest import ORDER_VALID_BARS, WARMUP_H1
from backend.data.base import DataUnavailableError
from backend.strategy_lab import ACTIVE_KEY, get_variant, simulate, snapshot_at, stats
from backend.timeutils import iso, ist_label


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class Journal:
    def __init__(self, provider, folder: Path, clock: Callable[[], float] = time.time,
                 variant_key: str = ACTIVE_KEY, start_at: datetime | None = None):
        self._provider = provider
        self._folder = Path(folder)
        self._folder.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._variant = get_variant(variant_key)
        self._lock = threading.Lock()
        self._snaps: dict[str, dict] = {}
        self._trades: dict[str, dict] = {}
        self._status: dict[str, Any] = {"state": "STARTING"}
        self.last_error: str | None = None
        self._load(start_at)

    # ------------------------------------------------------------------ persistence
    def _load(self, start_at: datetime | None) -> None:
        meta_f = self._folder / "meta.json"
        if meta_f.exists():
            self._meta = json.loads(meta_f.read_text(encoding="utf-8"))
        else:
            start = start_at or datetime.fromtimestamp(self._clock(), tz=timezone.utc)
            self._meta = {"started_at": start.isoformat(), "variant": self._variant.key, "variant_name": self._variant.name}
            meta_f.write_text(json.dumps(self._meta, indent=2), encoding="utf-8")
        snaps_f = self._folder / "snapshots.jsonl"
        if snaps_f.exists():
            for line in snaps_f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._snaps[rec["t"]] = rec["snap"]
        trades_f = self._folder / "trades.json"
        if trades_f.exists():
            for t in json.loads(trades_f.read_text(encoding="utf-8")):
                self._trades[t["signal_time"]] = t

    def _save_trades(self) -> None:
        rows = sorted(self._trades.values(), key=lambda t: t["signal_time"])
        tmp = self._folder / "trades.json.tmp"
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        tmp.replace(self._folder / "trades.json")

    # ------------------------------------------------------------------ main step
    def update(self) -> dict:
        with self._lock:
            try:
                h1cs = self._provider.get_candles("H1")
                d1 = self._provider.get_candles("D1").df
            except DataUnavailableError as exc:
                self.last_error = str(exc)
                return self.status()
            h1 = h1cs.df
            ref = pd.Timestamp(h1cs.last_point_utc) if h1cs.last_point_utc else pd.Timestamp(self._clock(), unit="s", tz="UTC")
            ref = min(ref, pd.Timestamp(self._clock(), unit="s", tz="UTC"))
            h1 = h1[h1.index + pd.Timedelta(hours=1) <= ref]            # closed candles only
            start = _ts(self._meta["started_at"])

            new_lines = []
            snaps: list[dict | None] = [None] * len(h1)
            for i, t in enumerate(h1.index):
                if i < WARMUP_H1 or t + pd.Timedelta(hours=1) <= start:
                    continue
                key = t.isoformat()
                if key not in self._snaps:
                    self._snaps[key] = snapshot_at(h1, d1, i)
                    new_lines.append(json.dumps({"t": key, "snap": self._snaps[key]}))
                snaps[i] = self._snaps[key]
            if new_lines:
                with open(self._folder / "snapshots.jsonl", "a", encoding="utf-8") as f:
                    f.write("\n".join(new_lines) + "\n")

            state: dict = {}
            done = simulate(h1, snaps, self._variant, state_out=state)
            changed = False
            for t in done:
                k = _ts(t["signal_time"]).isoformat()
                if k not in self._trades:
                    self._trades[k] = {
                        "signal_time": k,
                        "signal_ist": ist_label(_ts(t["signal_time"]).to_pydatetime()),
                        "exit_time": _ts(t["exit_time"]).isoformat(),
                        "exit_ist": ist_label(_ts(t["exit_time"]).to_pydatetime()),
                        "side": t["side"], "entry": t["entry"], "stop": t["stop"], "target": t["target"],
                        "result": t["result"], "r": t["r"],
                    }
                    changed = True
            if changed:
                self._save_trades()

            self.last_error = None
            self._status = self._describe(state, h1)
            return self.status()

    def _describe(self, state: dict, h1: pd.DataFrame) -> dict:
        p, o = state.get("pending"), state.get("open")
        last = h1.index[-1] if len(h1) else None
        if o:
            return {"state": "OPEN", "side": o["side"], "entry": round(o["entry"], 2), "stop": round(o["stop"], 2),
                    "target": round(o["target"], 2), "opened_ist": ist_label(h1.index[o["fill_i"]].to_pydatetime() + pd.Timedelta(hours=1)),
                    "last_candle_ist": ist_label((last + pd.Timedelta(hours=1)).to_pydatetime()) if last is not None else None}
        if p:
            sig_t = h1.index[p["signal_i"]]
            expires = sig_t + pd.Timedelta(hours=1 + ORDER_VALID_BARS)
            return {"state": "PENDING", "side": p["side"], "zone": [round(z, 2) for z in p["zone"]], "stop": round(p["stop"], 2),
                    "touched": bool(p.get("touched")), "signal_ist": ist_label((sig_t + pd.Timedelta(hours=1)).to_pydatetime()),
                    "expires_ist": ist_label(expires.to_pydatetime()),
                    "last_candle_ist": ist_label((last + pd.Timedelta(hours=1)).to_pydatetime()) if last is not None else None}
        return {"state": "WAITING", "last_candle_ist": ist_label((last + pd.Timedelta(hours=1)).to_pydatetime()) if last is not None else None}

    # ------------------------------------------------------------------ read
    def status(self) -> dict:
        rows = sorted(self._trades.values(), key=lambda t: t["signal_time"])
        st = stats([{"r": t["r"]} for t in rows])
        return {
            "strategy": self._meta["variant_name"],
            "started_at_utc": self._meta["started_at"],
            "started_ist": ist_label(_ts(self._meta["started_at"]).to_pydatetime()),
            "checked_ist": ist_label(datetime.fromtimestamp(self._clock(), tz=timezone.utc)),
            "current": self._status,
            "stats": st,
            "trades": rows[-50:][::-1],
            "error": self.last_error,
            "goal": "Collect 20–30 completed live trades before judging. No real money until then.",
            "note": "Outcomes measured on the free chart series (about 11 min delayed). The journal never places orders.",
        }


def start_background(journal: Journal, interval: float = 60.0) -> threading.Event:
    """Run journal.update() every `interval` seconds in a daemon thread. Returns a stop event."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                journal.update()
            except Exception as exc:  # keep the server alive; show the error on the dashboard
                journal.last_error = f"{type(exc).__name__}: {exc}"
            stop.wait(interval)

    threading.Thread(target=loop, name="signal-journal", daemon=True).start()
    return stop
