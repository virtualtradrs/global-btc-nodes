"""End-to-end pipeline tests: `main()` against the offline fixture transport.

These are the tests that would have caught the daily red runs. Once the
committed dataset reaches `--max-days` (730 rows), every scheduled run appends
one closed candle and then trims one row off the front. The old code reported
the new dates by indexing into the *post-trim* list with *pre-trim* indices,
which raised `IndexError: list index out of range` and skipped the write, so the
dataset silently stopped at 2026-10-21 for two weeks.

No network: `run_pipeline()` always passes `--transport fixture`.
"""

from __future__ import annotations

from conftest import (
    LAST_CLOSED,
    TODAY,
    day,
    make_candle,
    make_candles,
    read_dataset,
    run_pipeline,
    seed_dataset,
    write_fixtures,
)

import fetch_data

MAX_DAYS = 730


def _full_window_ending(last_offset: int) -> list[str]:
    """Exactly MAX_DAYS dates, the newest one being `day(last_offset)`."""
    return [day(last_offset - (MAX_DAYS - 1) + offset) for offset in range(MAX_DAYS)]


def test_full_window_plus_one_new_candle_does_not_raise_and_reports_the_new_date(tmp_path):
    """Regression: at the cap, appending one candle used to raise IndexError."""
    data_dir = tmp_path / "data"
    existing = seed_dataset(data_dir, _full_window_ending(-2))  # newest row: 2026-11-04
    assert len(existing) == MAX_DAYS
    assert existing[-1]["date"] == day(-2)

    new_candle = make_candle(LAST_CLOSED, 5000)
    fixtures = write_fixtures(
        tmp_path / "fixtures",
        [make_candle(day(-2), 4999), new_candle, make_candle(TODAY, 5001)],  # today is still open
    )

    code, out, err = run_pipeline(data_dir, fixtures)

    assert code == 0, err
    assert f"new closed candles      : 1 ({LAST_CLOSED} .. {LAST_CLOSED})" in out
    assert "+1 candle)" in out  # the commit subject names the count

    rows = read_dataset(data_dir)
    assert len(rows) == MAX_DAYS, "the window must stay capped at --max-days"
    assert rows[-1]["date"] == LAST_CLOSED
    assert rows[-1]["open"] == new_candle["open"]
    assert rows[-1]["close"] == new_candle["close"]
    assert rows[-1]["volume"] == new_candle["volume"]
    # One row was trimmed off the front, so the window advanced by exactly one day.
    assert rows[0]["date"] == day(-730)
    # The pre-existing rows are untouched: the newest 729 of them are still there verbatim.
    assert rows[:-1] == existing[1:]


def test_multi_day_gap_backfills_every_missing_day_and_reports_the_range(tmp_path):
    """A stale dataset must catch up in one run, not one row per run."""
    data_dir = tmp_path / "data"
    existing = seed_dataset(data_dir, [day(-11 + offset) for offset in range(6)])  # .. 2026-10-31
    assert existing[-1]["date"] == "2026-10-31"

    backfill = make_candles([day(-5), day(-4), day(-3), day(-2), day(-1)])  # 11-01 .. 11-05
    fixtures = write_fixtures(tmp_path / "fixtures", [*backfill, make_candle(TODAY, 99)])

    code, out, err = run_pipeline(data_dir, fixtures)

    assert code == 0, err
    assert f"new closed candles      : 5 (2026-11-01 .. {LAST_CLOSED})" in out
    assert "+5 candles)" in out

    rows = read_dataset(data_dir)
    assert len(rows) == 11
    assert [row["date"] for row in rows] == [row["date"] for row in existing] + [c["date"] for c in backfill]
    assert rows[-1]["date"] == LAST_CLOSED
    # Existing rows are byte-for-byte what they were: append-only, never rewritten.
    assert rows[: len(existing)] == existing


def test_multi_day_catch_up_on_a_full_window_trims_and_still_reports_right(tmp_path):
    """Full window + a five-day gap: rows are trimmed from the front, report stays correct."""
    data_dir = tmp_path / "data"
    seed_dataset(data_dir, _full_window_ending(-6))  # newest row: 2026-10-31

    backfill = make_candles([day(-5), day(-4), day(-3), day(-2), day(-1)])
    fixtures = write_fixtures(tmp_path / "fixtures", [*backfill, make_candle(TODAY, 99)])

    code, out, err = run_pipeline(data_dir, fixtures)

    assert code == 0, err
    assert f"new closed candles      : 5 (2026-11-01 .. {LAST_CLOSED})" in out
    rows = read_dataset(data_dir)
    assert len(rows) == MAX_DAYS
    assert rows[-1]["date"] == LAST_CLOSED
    assert [row["date"] for row in rows[-5:]] == [c["date"] for c in backfill]


def test_dry_run_on_a_full_window_prints_the_new_row_and_writes_nothing(tmp_path):
    """--dry-run had the same stale-index hazard and must not crash either."""
    data_dir = tmp_path / "data"
    seed_dataset(data_dir, _full_window_ending(-2))
    new_candle = make_candle(LAST_CLOSED, 5000)
    fixtures = write_fixtures(tmp_path / "fixtures", [new_candle, make_candle(TODAY, 5001)])

    csv_path = data_dir / fetch_data.MARKET_CSV
    json_path = data_dir / fetch_data.NODE_JSON
    csv_before = csv_path.read_text(encoding="utf-8")
    json_existed = json_path.exists()

    summary_path = tmp_path / "commit-message.txt"
    code, out, err = run_pipeline(data_dir, fixtures, "--dry-run", "--summary-file", str(summary_path))

    assert code == 0, err
    assert f"rows {MAX_DAYS} -> {MAX_DAYS} | 1 new row(s) appended, existing rows untouched" in out
    assert f"{LAST_CLOSED},{new_candle['open']:.2f},{new_candle['high']:.2f}" in out
    assert "--- dry run: nothing written; tracked files untouched ---" in out
    # Nothing on disk moved.
    assert csv_path.read_text(encoding="utf-8") == csv_before
    assert json_path.exists() == json_existed
    assert not summary_path.exists()


def test_dry_run_multi_day_gap_lists_every_row_that_would_be_appended(tmp_path):
    data_dir = tmp_path / "data"
    seed_dataset(data_dir, [day(-11 + offset) for offset in range(6)])
    backfill = make_candles([day(-5), day(-4), day(-3), day(-2), day(-1)])
    fixtures = write_fixtures(tmp_path / "fixtures", [*backfill, make_candle(TODAY, 99)])

    code, out, err = run_pipeline(data_dir, fixtures, "--dry-run")

    assert code == 0, err
    assert f"new closed candles      : 5 (2026-11-01 .. {LAST_CLOSED})" in out
    for candle in backfill:
        assert f"{candle['date']},{candle['open']:.2f}" in out
    assert len(read_dataset(data_dir)) == 6  # unchanged on disk
