"""Single-stock leveraged / inverse ETF map.

This module owns the JSON-backed lookup used by the scanner's ``ETF:``
indicator. Two access patterns:

* **Forward** — ``get_etfs_for(symbol)`` — given an underlying like ``TSLA``
  returns the list of leveraged / inverse ETFs that track it
  (``[{"ticker": "TSLL", "issuer": "Direxion", "mult": 1.5}, ...]``),
  sorted by absolute multiple descending (highest leverage first), with
  bull-before-bear as a tiebreaker.
* **Reverse** — ``get_underlying_for(symbol)`` — given an ETF ticker like
  ``TSLL`` returns the single underlying it tracks
  (``{"underlying": "TSLA", "issuer": "Direxion", "mult": 1.5}``) or
  ``None`` if the symbol isn't a known leveraged-stock ETF.

Path resolution at runtime:

1. If the user set a custom path via Settings (passed in as
   ``custom_path``), use that.
2. Else use ``next_to_exe / "single_stock_etfs.json"``.
3. If that file doesn't exist yet, fall back to the read-only baseline
   shipped inside the exe at ``_MEIPASS / "single_stock_etfs.json"``
   (or alongside the .py file when running from source).

The bundled baseline is also copied out to the writable location on
first launch when it doesn't exist there yet, so subsequent ``Refresh``
runs have something to overwrite.

Atomic writes use temp-file + ``os.replace`` so a crash mid-write can't
leave a half-truncated JSON on disk.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Match the BASE_DIR pattern in scan_sec.py — writable location is next
# to the running exe (or next to the .py file in dev).
if getattr(sys, "frozen", False):
    _RUNTIME_DIR = Path(sys.executable).parent
    _BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", _RUNTIME_DIR))
else:
    _RUNTIME_DIR = Path(__file__).parent
    _BUNDLED_DIR = _RUNTIME_DIR

DEFAULT_FILE_NAME = "single_stock_etfs.json"
DEFAULT_WRITABLE_PATH = _RUNTIME_DIR / DEFAULT_FILE_NAME
BUNDLED_BASELINE_PATH = _BUNDLED_DIR / DEFAULT_FILE_NAME


class EtfMap:
    """Thread-safe in-memory ETF lookup, backed by a JSON file."""

    def __init__(self, path: Optional[Path] = None):
        self._lock = threading.RLock()
        self._path = Path(path) if path else DEFAULT_WRITABLE_PATH
        self._refreshed_at: Optional[str] = None
        self._issuers_scraped: list[str] = []
        self._errors: list[str] = []
        # Forward: {UNDERLYING: [{ticker, issuer, mult}, ...]}
        self._forward: dict[str, list[dict]] = {}
        # Reverse: {ETF_TICKER: {underlying, issuer, mult}}
        self._reverse: dict[str, dict] = {}
        self._seed_writable_from_bundle()
        self.reload()

    # ---- public API ------------------------------------------------------

    @property
    def path(self) -> Path:
        with self._lock:
            return self._path

    def set_path(self, new_path: Path) -> None:
        """Switch which JSON file backs the map. Triggers a reload.

        Use this when the user picks a custom path in Settings. Does
        nothing if the path is unchanged.
        """
        new_path = Path(new_path) if new_path else DEFAULT_WRITABLE_PATH
        with self._lock:
            if new_path == self._path:
                return
            self._path = new_path
            self.reload()

    def reload(self) -> None:
        """Re-read the JSON from disk and rebuild both indexes."""
        with self._lock:
            data = self._read_json(self._path)
            if data is None and self._path != BUNDLED_BASELINE_PATH:
                # Custom path doesn't exist or is unreadable — warn so
                # the user knows their custom file vanished, then fall
                # back to the bundled baseline so the indicator still
                # has data to show. Without the warning, a moved/
                # deleted custom file silently downgraded the app to
                # baseline coverage with no visible signal.
                logger.warning(
                    "ETF map: custom path %s unreadable; "
                    "falling back to bundled baseline", self._path,
                )
                data = self._read_json(BUNDLED_BASELINE_PATH)
            self._apply(data or {})

    def get_etfs_for(self, symbol: str) -> list[dict]:
        """Return ETFs that track ``symbol``, sorted by abs(mult) desc.

        Bull (positive mult) sorts before bear (negative) at equal
        magnitude. Returns ``[]`` when the symbol has no matches.
        """
        if not symbol:
            return []
        with self._lock:
            return list(self._forward.get(symbol.upper().strip(), []))

    def snapshot_forward(self) -> dict:
        """Return a shallow-per-entry copy of the whole forward map under
        the lock — for a consumer (e.g. the refresh worker's
        preserve-on-failure baseline) that needs every tracked underlying
        without reaching into the private ``_forward`` past the lock."""
        with self._lock:
            return {k: list(v) for k, v in self._forward.items()}

    def get_underlying_for(self, symbol: str) -> Optional[dict]:
        """Return ``{underlying, issuer, mult}`` if ``symbol`` IS a known
        leveraged-stock ETF, else ``None``."""
        if not symbol:
            return None
        with self._lock:
            entry = self._reverse.get(symbol.upper().strip())
            return dict(entry) if entry else None

    def health(self) -> dict:
        """Snapshot of the current state — for the Health popup."""
        with self._lock:
            per_issuer: dict[str, int] = {}
            for etfs in self._forward.values():
                for e in etfs:
                    iss = e.get("issuer") or "Unknown"
                    per_issuer[iss] = per_issuer.get(iss, 0) + 1
            top5 = sorted(
                self._forward.items(),
                key=lambda kv: len(kv[1]), reverse=True,
            )[:5]
            return {
                "path": str(self._path),
                "exists": self._path.exists(),
                "refreshed_at": self._refreshed_at,
                "issuers_scraped": list(self._issuers_scraped),
                "errors": list(self._errors),
                "total_underlyings": len(self._forward),
                "total_etfs": sum(len(v) for v in self._forward.values()),
                "per_issuer": per_issuer,
                "top_underlyings": [
                    (k, len(v)) for k, v in top5
                ],
            }

    def replace(
        self,
        forward: dict[str, list[dict]],
        *,
        issuers_scraped: list[str],
        errors: list[str],
    ) -> None:
        """Atomically replace the map (called by the scraper) and persist."""
        normalized = self._normalize_forward(forward)
        payload = {
            "schema_version": 1,
            "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issuers_scraped": list(issuers_scraped),
            "errors": list(errors),
            "underlyings": normalized,
        }
        with self._lock:
            # Empty-payload floor: a degenerate refresh where nothing
            # usable was scraped AND errors were reported must NOT cleanly
            # overwrite the user's accumulated map with {}. Preserve the
            # existing on-disk + in-memory data instead.
            if not normalized and errors:
                return
            # Back up the current good file before overwriting. The atomic
            # write can't *corrupt*, but it can cleanly replace good data
            # with a worse (smaller) scrape; the .bak makes that reversible.
            self._backup_existing(self._path)
            self._write_atomic(self._path, payload)
            self._apply(payload)

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _backup_existing(path: Path) -> None:
        """Best-effort copy of the current file to a sibling ``.bak`` so a
        degenerate refresh that overwrites good data stays recoverable."""
        try:
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass

    def _seed_writable_from_bundle(self) -> None:
        """First-launch copy: if the writable JSON doesn't exist but the
        bundled baseline does, copy it out so refresh has something to
        update later. Failures are silent — the bundled file is still
        usable read-only via the reload() fallback."""
        try:
            if (
                self._path == DEFAULT_WRITABLE_PATH
                and not self._path.exists()
                and BUNDLED_BASELINE_PATH.exists()
                and BUNDLED_BASELINE_PATH != self._path
            ):
                self._path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BUNDLED_BASELINE_PATH, self._path)
        except OSError:
            pass

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _write_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".etfmap_", suffix=".json", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _normalize_forward(
        forward: dict[str, list[dict]],
    ) -> dict[str, list[dict]]:
        """Clean, dedupe, and sort the forward map.

        - Underlying keys uppercased + stripped.
        - ETF rows deduped by (ticker, issuer) — last occurrence wins.
        - Each underlying's list sorted by abs(mult) desc, ties broken
          bull-before-bear.
        """
        out: dict[str, list[dict]] = {}
        for raw_und, etfs in (forward or {}).items():
            if not isinstance(raw_und, str):
                continue
            und = raw_und.upper().strip()
            if not und or not isinstance(etfs, list):
                continue
            seen: dict[tuple, dict] = {}
            for e in etfs:
                if not isinstance(e, dict):
                    continue
                tk = str(e.get("ticker") or "").upper().strip()
                if not tk:
                    continue
                try:
                    m = float(e.get("mult"))
                except (TypeError, ValueError):
                    continue
                # Reject clearly-bogus multiples from a mis-parsed scrape
                # (NaN/inf, no-leverage 0, or a number far outside the real
                # ±5x single-stock-ETF range — e.g. a mis-read year). The
                # live data is all within ±2x, so nothing valid is lost.
                if m != m or m in (float("inf"), float("-inf")) \
                        or m == 0 or abs(m) > 5.0:
                    continue
                iss = str(e.get("issuer") or "Unknown").strip() or "Unknown"
                seen[(tk, iss)] = {"ticker": tk, "issuer": iss, "mult": m}
            if not seen:
                continue
            rows = sorted(
                seen.values(),
                key=lambda r: (-abs(r["mult"]), -r["mult"], r["ticker"]),
            )
            out[und] = rows
        return out

    def _apply(self, payload: dict) -> None:
        """Replace in-memory state from a payload dict (locked by caller)."""
        forward_raw = payload.get("underlyings") or {}
        self._forward = self._normalize_forward(forward_raw)
        self._reverse = {}
        for und, rows in self._forward.items():
            for r in rows:
                # If a ticker appears under multiple underlyings (shouldn't
                # happen for single-stock products, but guard anyway),
                # the first sorted-key occurrence wins deterministically.
                self._reverse.setdefault(
                    r["ticker"],
                    {"underlying": und, "issuer": r["issuer"], "mult": r["mult"]},
                )
        self._refreshed_at = payload.get("refreshed_at")
        iss = payload.get("issuers_scraped") or []
        self._issuers_scraped = [str(x) for x in iss if isinstance(x, str)]
        errs = payload.get("errors") or []
        self._errors = [str(x) for x in errs if isinstance(x, str)]


def format_mult(mult: float) -> str:
    """Render a multiple for UI display: 1.5x, -1x, 2x, -2x."""
    try:
        m = float(mult)
    except (TypeError, ValueError):
        return "?"
    # Show no trailing .0 for clean integer leverages (TSLT = "2x")
    # but keep .5 for fractional ones (TSLL = "1.5x"). Negatives carry
    # the sign so users can read direction at a glance.
    if abs(m - round(m)) < 1e-9:
        return f"{int(round(m))}x"
    return f"{m:g}x"
