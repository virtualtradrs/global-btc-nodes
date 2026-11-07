"""Tests for the README figure guard (scripts/check_readme_numbers.py).

Regression context: every figure the README quotes about the dataset, the node
snapshots and the baseline is a snapshot of files the daily pipeline rewrites,
so the prose drifted out of step with the data within a day (and did: the
committed README still described 2026-11-05 while the dataset had moved on to
2026-11-06). These tests pin the guard down: it must pass on figures that match
the data, name both values and exit non-zero on one that does not, and be able
to put the measured value back.

The good/mismatch cases build a throwaway repository under `tmp_path`, so they
need no live repo and no network. One extra case runs the guard against this
checkout, which is what CI does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import check_readme_numbers as figures
import pytest

from conftest import REPO_ROOT

DATASET_CSV = """\
date,open,high,low,close,volume,source
2026-11-01,70000.00,71000.00,69000.00,70500.00,100.0,kraken:XBTUSD
2026-11-02,70500.00,72000.00,70000.00,71800.00,101.0,kraken:XBTUSD
2026-11-03,71800.00,72500.00,70000.00,70100.00,102.0,kraken:XBTUSD
2026-11-04,70100.00,71200.00,69000.00,69900.00,103.0,kraken:XBTUSD
2026-11-05,69900.00,76000.00,69500.00,75400.00,104.0,kraken:XBTUSD
"""

SNAPSHOTS = {
    "schema_version": 1,
    "snapshots": [
        {
            "date": "2026-11-04",
            "captured_at": "2026-11-04T06:00:00Z",
            "chain": {"tip_height": 900100, "mempool_tx_count": 1000, "mempool_vsize_bytes": 2000000, "mempool_size_mb": 2.0},
            "node_network": {"reachable_nodes": 20000},
            "observation_count": 1,
            "first_seen_at": "2026-11-04T06:00:00Z",
        },
        {
            "date": "2026-11-05",
            "captured_at": "2026-11-05T06:00:00Z",
            "chain": {"tip_height": 900244, "mempool_tx_count": 1200, "mempool_vsize_bytes": 3145728, "mempool_size_mb": 3.14},
            "node_network": {"reachable_nodes": 20123},
            "observation_count": 1,
            "first_seen_at": "2026-11-05T06:00:00Z",
        },
    ],
}

BASELINE = {
    "as_of_date": "2026-11-05",
    "dataset_rows": 5,
    "period": {"start_date": "2026-11-02", "end_date": "2026-11-05", "trading_days": 3, "warmup_rows_used": 2},
    "metrics": {
        "sma_crossover_20_100": {
            "total_return_pct": -12.84,
            "cagr_pct": -7.66,
            "max_drawdown_pct": 33.72,
            "num_trades": 9,
            "win_rate_pct": 40.0,
            "pct_time_in_market": 44.69,
            "final_equity": 8715.67,
        },
        "buy_and_hold": {
            "total_return_pct": -22.4,
            "cagr_pct": -13.68,
            "max_drawdown_pct": 53.09,
            "num_trades": 1,
            "win_rate_pct": 0.0,
            "pct_time_in_market": 100.0,
            "final_equity": 7759.58,
        },
    },
    "outperformance_vs_buy_and_hold_pct": 9.56,
}

FETCH_SCRIPT = 'parser.add_argument("--max-days", type=int, default=730, help="max snapshots/candles kept per file")\n'


def render_readme(facts: dict[str, str]) -> str:
    """Build a README that quotes exactly `facts`, one line per claim."""
    return "\n\n".join(claim.template.format(*[facts[key] for key in claim.keys]) for claim in figures.CLAIMS) + "\n"


def render_data_readme() -> str:
    return (
        "# The dataset\n\n"
        "Real header and real rows (first and last two of the file):\n\n"
        "```csv\n" + figures._csv_excerpt([], DATASET_CSV.splitlines()) + "\n```\n\n"
        "Real snapshot:\n\n"
        "```json\n" + figures._json_excerpt(SNAPSHOTS) + "\n```\n"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal but complete repository for the guard to read."""
    (tmp_path / "data").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data/btc_market_daily.csv").write_text(DATASET_CSV, encoding="utf-8")
    (tmp_path / "data/btc_node_metrics.json").write_text(json.dumps(SNAPSHOTS, indent=2), encoding="utf-8")
    (tmp_path / "results/baseline_results.json").write_text(json.dumps(BASELINE, indent=2), encoding="utf-8")
    (tmp_path / "scripts/fetch_data.py").write_text(FETCH_SCRIPT, encoding="utf-8")
    (tmp_path / "README.md").write_text(render_readme(figures.load_facts(tmp_path)), encoding="utf-8")
    (tmp_path / "data/README.md").write_text(render_data_readme(), encoding="utf-8")
    return tmp_path


def test_figures_that_match_the_data_pass(repo: Path):
    comparisons, problems = figures.check(repo)

    assert problems == []
    assert comparisons == sum(len(claim.keys) for claim in figures.CLAIMS) + 2  # + the two data/README blocks


def test_the_guard_compares_every_claim(repo: Path):
    """Each claim must be found in the README; none may be silently skipped."""
    text = (repo / "README.md").read_text(encoding="utf-8")
    for claim in figures.CLAIMS:
        assert claim.pattern().search(text), claim.label


def test_a_corrupted_figure_fails_with_both_values(repo: Path, capsys):
    readme = repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("75,400.00", "75,979.90"), encoding="utf-8")

    code = figures.main(["--root", str(repo)])
    output = capsys.readouterr().out

    assert code == 1
    assert "latest close" in output
    assert "says '75,979.90'" in output
    assert "data says '75,400.00'" in output


def test_a_stale_window_date_fails(repo: Path, capsys):
    readme = repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("2026-11-05", "2026-11-04"), encoding="utf-8")

    code = figures.main(["--root", str(repo)])
    output = capsys.readouterr().out

    assert code == 1
    assert "says '2026-11-04'" in output
    assert "data says '2026-11-05'" in output


def test_a_missing_claim_is_reported_not_skipped(repo: Path, capsys):
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("the latest recording chain tip", "the tip was"),
        encoding="utf-8",
    )

    code = figures.main(["--root", str(repo)])
    output = capsys.readouterr().out

    assert code == 1
    assert "could not find the 'chain tip height' figure" in output


def test_a_stale_csv_sample_block_in_data_readme_fails(repo: Path, capsys):
    data_readme = repo / "data/README.md"
    data_readme.write_text(
        data_readme.read_text(encoding="utf-8").replace("2026-11-01,70000.00", "2024-10-23,67384.20"),
        encoding="utf-8",
    )

    code = figures.main(["--root", str(repo)])
    output = capsys.readouterr().out

    assert code == 1
    assert "CSV sample rows" in output


def test_fix_rewrites_the_figures_from_the_data(repo: Path):
    readme = repo / "README.md"
    corrupted = readme.read_text(encoding="utf-8").replace("75,400.00", "75,979.90").replace("+9.56", "+9.03")
    readme.write_text(corrupted, encoding="utf-8")

    changed, _comparisons, problems = figures.fix(repo)

    assert changed is True
    assert problems == []
    assert figures.check(repo)[1] == []
    assert "75,400.00" in readme.read_text(encoding="utf-8")
    assert "+9.56" in readme.read_text(encoding="utf-8")


def test_the_script_runs_as_a_module_and_exits_non_zero(tmp_path: Path, repo: Path):
    """The exact command CI runs, through the CLI, on a tree with one wrong figure."""
    readme = repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("730", "731", 1), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check_readme_numbers.py"), "--root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "data says '730'" in result.stdout


def test_the_committed_repository_is_in_step():
    """This checkout must pass: the guard is only useful if the real docs are true."""
    _comparisons, problems = figures.check(REPO_ROOT)

    assert problems == []
