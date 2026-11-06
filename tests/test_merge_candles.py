"""Unit tests for merge_candles / trim_candles.

Regression context: merge_candles used to return *indices* into the existing
list. main() trimmed that list to `--max-days` immediately afterwards, which
deletes rows from the front and shifts every index, so the indices pointed at
the wrong rows -- or past the end of the list -- producing
`IndexError: list index out of range` on every run once the dataset was full.
"""

from __future__ import annotations

from conftest import LAST_CLOSED, TODAY, day, make_candles

import fetch_data


def test_merge_returns_the_appended_rows_not_indices():
    existing = make_candles([day(-4), day(-3), day(-2)])
    fetched = make_candles([day(-1), TODAY])

    added = fetch_data.merge_candles(existing, fetched, TODAY)

    assert all(isinstance(row, dict) for row in added), added
    assert [row["date"] for row in added] == [LAST_CLOSED]
    # The open candle (dated today) is dropped, and the existing rows are untouched.
    assert [row["date"] for row in existing] == [day(-4), day(-3), day(-2), LAST_CLOSED]


def test_merge_is_idempotent_and_never_rewrites_existing_rows():
    existing = make_candles([day(-2), day(-1)])
    before = [dict(row) for row in existing]

    added = fetch_data.merge_candles(existing, make_candles([day(-2), day(-1)]), TODAY)

    assert added == []
    assert existing == before


def test_reported_dates_survive_trimming_a_full_window():
    """The exact failure mode: a full window plus one new candle, then a trim."""
    max_days = 5
    existing = make_candles([day(-6), day(-5), day(-4), day(-3), day(-2)])
    fetched = make_candles([day(-1), TODAY])

    added = fetch_data.merge_candles(existing, fetched, TODAY)
    fetch_data.validate_candles(existing)
    fetch_data.trim_candles(existing, max_days)

    # The window is full again, so the front row was dropped and every index shifted.
    assert len(existing) == max_days
    assert existing[0]["date"] == day(-5)
    # An index captured before the trim (5) is now out of range for a 5-row list;
    # the row object itself still names the right date.
    assert [row["date"] for row in added] == [LAST_CLOSED]


def test_multi_day_backfill_returns_every_missing_row_in_order():
    existing = make_candles([day(-3)])
    fetched = make_candles([day(-2), day(-1), TODAY])

    added = fetch_data.merge_candles(existing, fetched, TODAY)

    assert [row["date"] for row in added] == [day(-2), LAST_CLOSED]
    assert [row["date"] for row in existing] == [day(-3), day(-2), LAST_CLOSED]


def test_trim_candles_keeps_the_newest_rows():
    rows = make_candles([day(-3), day(-2), day(-1)])

    fetch_data.trim_candles(rows, 2)

    assert [row["date"] for row in rows] == [day(-2), LAST_CLOSED]
