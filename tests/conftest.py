"""Shared fixtures for the pipeline regression tests.

Everything here is offline. The fetchers are exercised through
`fetch_data.FixtureTransport`, which reads one file per URL from a temporary
directory that these helpers build, so the tests never touch mempool.space,
bitnodes.io, Kraken or Coinbase.

`_utcnow` is frozen for every test: the pipeline drops the in-progress UTC day,
so "today" decides which candles are closed and has to be deterministic.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_data  # noqa: E402  (import after sys.path setup)

# A fixed instant: 2026-11-06 06:30 UTC. Candles dated 2026-11-06 are still open
# and must be dropped; 2026-11-05 is the newest closed daily bar.
NOW = datetime(2026, 11, 6, 6, 30, tzinfo=timezone.utc)
TODAY = "2026-11-06"
LAST_CLOSED = "2026-11-05"

TIP_HEIGHT = 974_852


def day(offset: int, anchor: str = TODAY) -> str:
    """ISO date `offset` days from `anchor` (negative = earlier)."""
    base = datetime.strptime(anchor, "%Y-%m-%d")
    return (base + timedelta(days=offset)).strftime("%Y-%m-%d")


def make_candle(date: str, index: int, source: str = "kraken:XBTUSD") -> dict:
    """A deterministic, in-range daily candle."""
    base = 60_000.0 + index * 137.0
    return {
        "date": date,
        "open": base,
        "high": base + 500.0,
        "low": base - 400.0,
        "close": base + 120.0,
        "volume": 100.0 + index,
        "source": source,
    }


def make_candles(dates: list[str]) -> list[dict]:
    return [make_candle(date, index) for index, date in enumerate(dates)]


def _kraken_row(candle: dict) -> list:
    # [time, open, high, low, close, vwap, volume, count] -- Kraken's OHLC shape.
    stamp = datetime.strptime(candle["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [
        int(stamp.timestamp()),
        candle["open"],
        candle["high"],
        candle["low"],
        candle["close"],
        candle["close"],
        candle["volume"],
        42,
    ]


def write_fixtures(directory: Path, candles: list[dict], tip_height: int = TIP_HEIGHT) -> Path:
    """Write one fixture file per source URL into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        fetch_data.MEMPOOL_URL: json.dumps(
            {"count": 54_253, "vsize": 25_600_000, "total_fee": 1_234_567}
        ),
        # mempool.space returns a bare integer here, not JSON.
        fetch_data.TIP_HEIGHT_URL: str(tip_height),
        fetch_data.FEES_URL: json.dumps(
            {"fastestFee": 3, "halfHourFee": 2, "hourFee": 2, "economyFee": 1, "minimumFee": 1}
        ),
        fetch_data.KRAKEN_URL: json.dumps(
            {
                "error": [],
                "result": {
                    "XXBTZUSD": [_kraken_row(candle) for candle in candles],
                    "last": 1_760_000_000,
                },
            }
        ),
    }
    for url, body in payloads.items():
        (directory / fetch_data.url_slug(url)).write_text(body, encoding="utf-8")
    return directory


def seed_dataset(data_dir: Path, dates: list[str]) -> list[dict]:
    """Write an existing committed dataset with one candle per date."""
    data_dir.mkdir(parents=True, exist_ok=True)
    rows = make_candles(dates)
    (data_dir / fetch_data.MARKET_CSV).write_text(fetch_data.render_csv(rows), encoding="utf-8")
    return rows


def read_dataset(data_dir: Path) -> list[dict]:
    return fetch_data.load_candles(data_dir / fetch_data.MARKET_CSV)


def run_pipeline(data_dir: Path, fixtures: Path, *extra: str) -> tuple[int, str, str]:
    """Run fetch_data.main() against the fixture transport; return (code, out, err)."""
    argv = [
        "--data-dir",
        str(data_dir),
        "--transport",
        "fixture",
        "--fixtures",
        str(fixtures),
        *extra,
    ]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch_data.main(argv)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Freeze the pipeline's clock so "today" is stable for every test."""
    monkeypatch.setattr(fetch_data, "_utcnow", lambda: NOW)
    return NOW
