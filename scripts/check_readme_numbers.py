#!/usr/bin/env python3
"""Guard the figures quoted in the docs against the committed data.

The daily pipeline appends candles and node snapshots, which changes the candle
count, the window dates, the latest close and the baseline metrics — so every
hard number in `README.md` goes stale on its own within a day. A prose number
that disagrees with the committed data is a defect in a repository whose whole
claim is "every number here is measured", so this module parses those figures
out of the prose and compares them with what `data/` and `results/` actually
contain.

The README stays the single source of prose truth: this module never rewrites a
sentence, it only reads the numbers inside them (and, with `--fix`, writes the
measured value back into the same spot).

What is checked:

* `README.md` — candle count, window start/end, retention window, highest /
  lowest / latest close and their dates, snapshot count and date range, latest
  chain-tip height, mempool size, reachable-node count (latest and first), the
  baseline's period / trading days / warmup rows, total returns, CAGR, max
  drawdown, trade counts, win rate, time in market, final equity and the
  outperformance in percentage points.
* `data/README.md` — the "Real header and real rows" CSV excerpt (header plus
  the first and last two rows of the file) and the `btc_node_metrics.json`
  excerpt (which must be the real first snapshot, not a remembered one).

Runs on the standard library only, like `scripts/check_repo.py`, so it works in
a bare checkout and in CI before any dependency install.

Usage:
    python scripts/check_readme_numbers.py [--root PATH] [--fix]

Exit code is 0 when every quoted figure matches the data, 1 otherwise. With
`--fix` a mismatch is corrected in place instead of reported as a failure.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

README = "README.md"
DATA_README = "data/README.md"
MARKET_CSV = "data/btc_market_daily.csv"
NODE_JSON = "data/btc_node_metrics.json"
BASELINE_JSON = "results/baseline_results.json"
FETCH_SCRIPT = "scripts/fetch_data.py"


# --- formatting -----------------------------------------------------------
#
# The comparison is on the rendered string, not on a float tolerance: a figure
# that reads "730" against 730 candles passes, "729" fails, and so does a value
# that is right but written with the wrong precision ("124766.5" instead of
# "124,766.50"). Whatever these helpers return is exactly what belongs in the
# README.


def money(value: float) -> str:
    return f"{value:,.2f}"


def pct(value: float) -> str:
    return f"{value:.2f}"


def signed_pct(value: float) -> str:
    """Percentage points, always with the sign: the README writes "+9.03"."""
    return f"{value:+.2f}"


def whole(value: float) -> str:
    return f"{int(value):,}"


def megabytes(value: float) -> str:
    return f"{value:.1f}"


@dataclass(frozen=True)
class Claim:
    """One quoted figure: a template of the prose with holes for the numbers.

    `template` is the sentence as it appears in the README, with `{0}`, `{1}`…
    where the numbers go. The regex is derived from it, so the template is the
    only place the wording lives: edit the prose and the claim stops matching
    (a reported failure, never a silent skip), and there is no second copy of
    the sentence to keep in step.
    """

    label: str
    template: str
    keys: tuple[str, ...]

    def pattern(self) -> re.Pattern[str]:
        pieces: list[str] = []
        for chunk in re.split(r"(\{\d+\})", self.template):
            if not chunk:
                continue
            if re.fullmatch(r"\{\d+\}", chunk):
                # A hole holds exactly one figure: no whitespace, no markdown
                # emphasis and no table pipe. Without that the hole would happily
                # span from an earlier "**" to this one and compare nonsense.
                pieces.append("([^\\s*|]+?)")
            else:
                pieces.append(re.escape(chunk))
        return re.compile("".join(pieces))


# Every figure the README quotes about the dataset, the node snapshots and the
# committed baseline. Keep these templates byte-identical to the README prose.
CLAIMS: tuple[Claim, ...] = (
    Claim(
        "candle count and window (intro)",
        "**{0} closed daily candles ({1} → {2})**",
        ("candles.count", "candles.start", "candles.end"),
    ),
    Claim(
        "snapshot count and window (intro)",
        "**{0} node/mempool snapshots ({1} → {2})**",
        ("snapshots.count", "snapshots.start", "snapshots.end"),
    ),
    Claim(
        "retention window",
        "both files keep the most recent {0} entries",
        ("retention.max_days",),
    ),
    Claim(
        "candle count and window (showcase)",
        "**{0} candles from {1} to {2}**",
        ("candles.count", "candles.start", "candles.end"),
    ),
    Claim(
        "highest close",
        "a highest close of **${0} ({1})**",
        ("candles.highest", "candles.highest_date"),
    ),
    Claim(
        "lowest close",
        "a lowest close of **${0} ({1})**",
        ("candles.lowest", "candles.lowest_date"),
    ),
    Claim(
        "latest close",
        "a latest close of **${0} ({1})**",
        ("candles.latest", "candles.latest_date"),
    ),
    Claim(
        "snapshot count and window (showcase)",
        "**{0} snapshots from {1} to {2}**",
        ("snapshots.count", "snapshots.start", "snapshots.end"),
    ),
    Claim(
        "chain tip height",
        "the latest recording chain tip **{0}**",
        ("snapshots.tip",),
    ),
    Claim(
        "mempool size",
        "a mempool of **{0} MB**",
        ("snapshots.mempool_mb",),
    ),
    Claim(
        "reachable nodes",
        "**{0}** reachable nodes, against {1} in the first snapshot",
        ("snapshots.reachable_latest", "snapshots.reachable_first"),
    ),
    Claim(
        "snapshot count (baseline section)",
        "holds {0} daily snapshots",
        ("snapshots.count",),
    ),
    Claim(
        "baseline period",
        "Over the {0} trading days from **{1} to {2}** ({3} committed candles, the first {4} rows",
        ("baseline.trading_days", "baseline.start", "baseline.end", "candles.count", "baseline.warmup_rows"),
    ),
    Claim(
        "baseline outperformance",
        "finished **{0} percentage points** ahead of buy-and-hold",
        ("baseline.outperformance",),
    ),
    Claim(
        "buy-and-hold loss (prose)",
        "BTC-USD itself lost {0}%",
        ("baseline.buy_and_hold_loss",),
    ),
    Claim(
        "baseline table row: SMA 20/100 crossover",
        "| SMA 20/100 crossover | {0}% | {1}% | {2}% | {3} | {4}% | {5}% | ${6} |",
        (
            "baseline.sma.total_return",
            "baseline.sma.cagr",
            "baseline.sma.max_drawdown",
            "baseline.sma.num_trades",
            "baseline.sma.win_rate",
            "baseline.sma.time_in_market",
            "baseline.sma.final_equity",
        ),
    ),
    Claim(
        "baseline table row: buy & hold",
        "| Buy & hold | {0}% | {1}% | {2}% | {3} | {4}% | {5}% | ${6} |",
        (
            "baseline.buy_and_hold.total_return",
            "baseline.buy_and_hold.cagr",
            "baseline.buy_and_hold.max_drawdown",
            "baseline.buy_and_hold.num_trades",
            "baseline.buy_and_hold.win_rate",
            "baseline.buy_and_hold.time_in_market",
            "baseline.buy_and_hold.final_equity",
        ),
    ),
)


# --- the data side --------------------------------------------------------


class FiguresUnavailable(RuntimeError):
    """A file the figures are derived from is missing or unusable."""


def _read_candles(root: Path) -> list[dict[str, str]]:
    path = root / MARKET_CSV
    if not path.is_file():
        raise FiguresUnavailable(f"missing {MARKET_CSV}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        raise FiguresUnavailable(f"{MARKET_CSV} has no data rows")
    return rows


def _read_snapshots(root: Path) -> list[dict]:
    path = root / NODE_JSON
    if not path.is_file():
        raise FiguresUnavailable(f"missing {NODE_JSON}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FiguresUnavailable(f"{NODE_JSON} is not valid JSON ({exc})") from exc
    snapshots = document.get("snapshots") if isinstance(document, dict) else None
    if not isinstance(snapshots, list) or not snapshots:
        raise FiguresUnavailable(f"{NODE_JSON} has no 'snapshots' entries")
    return snapshots


def _read_baseline(root: Path) -> dict:
    path = root / BASELINE_JSON
    if not path.is_file():
        raise FiguresUnavailable(f"missing {BASELINE_JSON}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FiguresUnavailable(f"{BASELINE_JSON} is not valid JSON ({exc})") from exc
    if not isinstance(document, dict) or not isinstance(document.get("metrics"), dict):
        raise FiguresUnavailable(f"{BASELINE_JSON} has no 'metrics' object")
    return document


def _retention_days(root: Path) -> str:
    """The `--max-days` default the pipeline actually uses, read from its source."""
    path = root / FETCH_SCRIPT
    if not path.is_file():
        raise FiguresUnavailable(f"missing {FETCH_SCRIPT}")
    match = re.search(r'"--max-days"\s*,\s*type=int\s*,\s*default=(\d+)', path.read_text(encoding="utf-8"))
    if not match:
        raise FiguresUnavailable(f"could not read the --max-days default from {FETCH_SCRIPT}")
    return whole(int(match.group(1)))


def load_facts(root: Path) -> dict[str, str]:
    """Every figure the docs are allowed to quote, rendered the way it is quoted."""
    candles = _read_candles(root)
    snapshots = _read_snapshots(root)
    baseline = _read_baseline(root)

    closes = [(float(row["close"]), row["date"]) for row in candles]
    highest_value, highest_date = max(closes, key=lambda pair: pair[0])
    lowest_value, lowest_date = min(closes, key=lambda pair: pair[0])
    latest_value, latest_date = closes[-1]

    censuses = [entry for entry in snapshots if isinstance(entry.get("node_network"), dict)]
    if not censuses:
        raise FiguresUnavailable(f"{NODE_JSON} has no snapshot with a node_network census")
    latest_chain = snapshots[-1].get("chain") or {}
    if latest_chain.get("tip_height") is None or latest_chain.get("mempool_size_mb") is None:
        raise FiguresUnavailable(f"{NODE_JSON}: the latest snapshot is missing chain.tip_height or mempool_size_mb")

    period = baseline.get("period") or {}
    metrics = baseline["metrics"]
    sma_key = "sma_crossover_20_100" if "sma_crossover_20_100" in metrics else next(
        (name for name in metrics if name != "buy_and_hold"), None
    )
    if sma_key is None or "buy_and_hold" not in metrics:
        raise FiguresUnavailable(f"{BASELINE_JSON} needs both a crossover and a buy_and_hold entry in 'metrics'")
    sma = metrics[sma_key]
    hold = metrics["buy_and_hold"]
    outperformance = baseline.get("outperformance_vs_buy_and_hold_pct")
    if outperformance is None:
        raise FiguresUnavailable(f"{BASELINE_JSON} is missing 'outperformance_vs_buy_and_hold_pct'")

    facts = {
        "candles.count": whole(len(candles)),
        "candles.start": candles[0]["date"],
        "candles.end": candles[-1]["date"],
        "candles.highest": money(highest_value),
        "candles.highest_date": highest_date,
        "candles.lowest": money(lowest_value),
        "candles.lowest_date": lowest_date,
        "candles.latest": money(latest_value),
        "candles.latest_date": latest_date,
        "retention.max_days": _retention_days(root),
        "snapshots.count": whole(len(snapshots)),
        "snapshots.start": snapshots[0]["date"],
        "snapshots.end": snapshots[-1]["date"],
        "snapshots.tip": whole(latest_chain["tip_height"]),
        "snapshots.mempool_mb": megabytes(float(latest_chain["mempool_size_mb"])),
        "snapshots.reachable_latest": whole(censuses[-1]["node_network"]["reachable_nodes"]),
        "snapshots.reachable_first": whole(censuses[0]["node_network"]["reachable_nodes"]),
        "baseline.trading_days": whole(period["trading_days"]),
        "baseline.start": period["start_date"],
        "baseline.end": period["end_date"],
        "baseline.warmup_rows": whole(period["warmup_rows_used"]),
        "baseline.outperformance": signed_pct(float(outperformance)),
        "baseline.buy_and_hold_loss": pct(abs(float(hold["total_return_pct"]))),
    }
    for name, entry in ((f"baseline.{sma_key}", sma), ("baseline.buy_and_hold", hold)):
        facts[f"{name}.total_return"] = pct(float(entry["total_return_pct"]))
        facts[f"{name}.cagr"] = pct(float(entry["cagr_pct"]))
        facts[f"{name}.max_drawdown"] = pct(float(entry["max_drawdown_pct"]))
        facts[f"{name}.num_trades"] = whole(int(entry["num_trades"]))
        facts[f"{name}.win_rate"] = pct(float(entry["win_rate_pct"]))
        facts[f"{name}.time_in_market"] = pct(float(entry["pct_time_in_market"]))
        facts[f"{name}.final_equity"] = money(float(entry["final_equity"]))
    # The crossover row is labelled "SMA 20/100 crossover" in the README whatever
    # the JSON key is, so alias the facts under the name the claim asks for.
    for suffix in (
        "total_return",
        "cagr",
        "max_drawdown",
        "num_trades",
        "win_rate",
        "time_in_market",
        "final_equity",
    ):
        facts[f"baseline.sma.{suffix}"] = facts[f"baseline.{sma_key}.{suffix}"]
    return facts


# --- the README side ------------------------------------------------------


@dataclass
class Mismatch:
    path: str
    label: str
    said: str
    actual: str
    start: int
    end: int

    def message(self) -> str:
        return f"{self.path}: {self.label} says '{self.said}', data says '{self.actual}'"

    def problem(self) -> str:
        return f"{self.message()} (regenerate with: python scripts/check_readme_numbers.py --fix)"


def _scan_claims(text: str, path: str, facts: dict[str, str]) -> tuple[int, list[Mismatch], list[str]]:
    comparisons = 0
    mismatches: list[Mismatch] = []
    problems: list[str] = []
    for claim in CLAIMS:
        matches = list(claim.pattern().finditer(text))
        if not matches:
            problems.append(f"{path}: could not find the '{claim.label}' figure (the prose changed; update CLAIMS in scripts/check_readme_numbers.py)")
            continue
        for match in matches:
            for index, key in enumerate(claim.keys):
                comparisons += 1
                said = match.group(index + 1)
                actual = facts[key]
                if said != actual:
                    mismatches.append(Mismatch(path, claim.label, said, actual, match.start(index + 1), match.end(index + 1)))
    return comparisons, mismatches, problems


def _csv_excerpt(rows: list[dict[str, str]], raw_lines: list[str]) -> str:
    """Header plus the first and last two rows, taken verbatim from the file."""
    del rows  # the raw lines are the file's own bytes; no re-formatting
    body = [raw_lines[0].rstrip("\r\n")] + [line.rstrip("\r\n") for line in raw_lines[1:3]]
    body += [line.rstrip("\r\n") for line in raw_lines[-2:]]
    return "\n".join(body)


def _json_excerpt(document: dict) -> str:
    excerpt = {"schema_version": document.get("schema_version"), "snapshots": document["snapshots"][:1]}
    return json.dumps(excerpt, indent=2)


def _find_block(text: str, language: str) -> re.Match[str] | None:
    return re.search(r"```" + language + r"\n(.*?)```", text, flags=re.DOTALL)


def _scan_data_readme(root: Path) -> tuple[int, list[Mismatch], list[str]]:
    """The dataset README quotes real rows and a real snapshot; both must be current."""
    path = root / DATA_README
    if not path.is_file():
        return 0, [], [f"missing {DATA_README}"]
    text = path.read_text(encoding="utf-8")
    comparisons = 0
    mismatches: list[Mismatch] = []
    problems: list[str] = []

    csv_path = root / MARKET_CSV
    if not csv_path.is_file():
        problems.append(f"missing {MARKET_CSV}")
    else:
        raw_lines = csv_path.read_text(encoding="utf-8").splitlines()
        expected = _csv_excerpt([], raw_lines)
        match = _find_block(text, "csv")
        comparisons += 1
        if match is None:
            problems.append(f"{DATA_README}: no ```csv sample block found")
        elif match.group(1).strip() != expected.strip():
            mismatches.append(Mismatch(DATA_README, "CSV sample rows", _shorten(match.group(1)), _shorten(expected), match.start(1), match.end(1)))

    json_path = root / NODE_JSON
    if not json_path.is_file():
        problems.append(f"missing {NODE_JSON}")
    else:
        document = json.loads(json_path.read_text(encoding="utf-8"))
        expected = _json_excerpt(document)
        match = _find_block(text, "json")
        comparisons += 1
        if match is None:
            problems.append(f"{DATA_README}: no ```json sample block found")
        else:
            try:
                said = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                problems.append(f"{DATA_README}: the ```json sample block is not valid JSON ({exc})")
                said = None
            if said is not None and said != json.loads(expected):
                mismatches.append(Mismatch(DATA_README, "JSON sample snapshot", _shorten(match.group(1)), _shorten(expected), match.start(1), match.end(1)))
    return comparisons, mismatches, problems


def _shorten(block: str, limit: int = 120) -> str:
    text = " ".join(block.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def check(root: Path) -> tuple[int, list[str]]:
    """Compare every quoted figure with the data. Returns (comparisons, problems)."""
    root = Path(root)
    comparisons = 0
    problems: list[str] = []
    try:
        facts = load_facts(root)
    except FiguresUnavailable as exc:
        return 0, [f"cannot check the README figures: {exc}"]

    readme_path = root / README
    if not readme_path.is_file():
        problems.append(f"missing {README}")
    else:
        counted, mismatches, missing = _scan_claims(readme_path.read_text(encoding="utf-8"), README, facts)
        comparisons += counted
        problems.extend(mismatch.problem() for mismatch in mismatches)
        problems.extend(missing)

    counted, mismatches, missing = _scan_data_readme(root)
    comparisons += counted
    problems.extend(mismatch.problem() for mismatch in mismatches)
    problems.extend(missing)
    return comparisons, problems


def fix(root: Path) -> tuple[bool, int, list[str]]:
    """Write the measured value into every quoted figure. Returns (changed, comparisons, problems)."""
    root = Path(root)
    try:
        facts = load_facts(root)
    except FiguresUnavailable as exc:
        return False, 0, [f"cannot refresh the README figures: {exc}"]

    comparisons = 0
    problems: list[str] = []
    changed_any = False

    readme_path = root / README
    if readme_path.is_file():
        text = readme_path.read_text(encoding="utf-8")
        counted, mismatches, missing = _scan_claims(text, README, facts)
        comparisons += counted
        problems.extend(missing)
        if mismatches:
            for mismatch in sorted(mismatches, key=lambda item: item.start, reverse=True):
                text = text[: mismatch.start] + mismatch.actual + text[mismatch.end :]
                print(f"check_readme_numbers: fixed {README}: {mismatch.label}: '{mismatch.said}' -> '{mismatch.actual}'")
            readme_path.write_text(text, encoding="utf-8")
            changed_any = True
    else:
        problems.append(f"missing {README}")

    counted, mismatches, missing = _scan_data_readme(root)
    comparisons += counted
    problems.extend(missing)
    if mismatches:
        data_readme_path = root / DATA_README
        text = data_readme_path.read_text(encoding="utf-8")
        raw_lines = (root / MARKET_CSV).read_text(encoding="utf-8").splitlines()
        document = json.loads((root / NODE_JSON).read_text(encoding="utf-8"))
        for mismatch in sorted(mismatches, key=lambda item: item.start, reverse=True):
            if mismatch.label == "CSV sample rows":
                replacement = _csv_excerpt([], raw_lines) + "\n"
            else:
                replacement = _json_excerpt(document) + "\n"
            text = text[: mismatch.start] + replacement + text[mismatch.end :]
            print(f"check_readme_numbers: fixed {DATA_README}: {mismatch.label} -> current data")
        data_readme_path.write_text(text, encoding="utf-8")
        changed_any = True
    return changed_any, comparisons, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the figures quoted in the READMEs against the committed data.")
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument("--fix", action="store_true", help="rewrite stale figures from the data instead of failing")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.fix:
        changed, comparisons, problems = fix(root)
        for problem in problems:
            print(f"check_readme_numbers: {problem}")
        if problems:
            print(f"\ncheck_readme_numbers: {len(problems)} problem(s) could not be fixed in {root}")
            return 1
        state = "updated" if changed else "already current"
        print(f"check_readme_numbers: OK - {comparisons} figures {state} in {root}")
        return 0

    comparisons, problems = check(root)
    if problems:
        print(f"check_readme_numbers: {len(problems)} problem(s) found in {root}\n")
        for problem in problems:
            print(f"  - {problem}")
        print("\nThe docs must quote the committed data. Fix the numbers, or run:")
        print("  python scripts/check_readme_numbers.py --fix")
        return 1
    print(f"check_readme_numbers: OK - {comparisons} quoted figures match the data in {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
