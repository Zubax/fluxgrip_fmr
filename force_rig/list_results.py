#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RESULTS_CSV = Path(__file__).resolve().with_name("llm_optimize_results.csv")
DEFAULT_LIMIT = 10
SAFETY_LIMIT_SCORE_N = 100.0


@dataclass(frozen=True)
class RunResult:
    run_id: int
    source: str
    peak_n: float
    score_n: float
    effective_score_n: float
    touchdown_n: float
    pull_s: float
    recovery_performed: bool
    row_number: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rank force-rig optimization results from llm_optimize_results.csv."
    )
    parser.add_argument(
        "results_csv",
        type=Path,
        nargs="?",
        default=DEFAULT_RESULTS_CSV,
        help=f"CSV file to parse. Defaults to {DEFAULT_RESULTS_CSV}.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=positive_int,
        default=DEFAULT_LIMIT,
        help=f"Only print the best N runs. Defaults to {DEFAULT_LIMIT}.",
    )
    parser.add_argument(
        "--by-run",
        action="store_true",
        help="Sort by run number instead of score.",
    )
    args = parser.parse_args()

    results = parse_results(args.results_csv)
    if not results:
        raise SystemExit(f"No run results found in {args.results_csv}")

    total_count = len(results)
    results = sorted(
        results,
        key=(lambda result: result.run_id)
        if args.by_run
        else (lambda result: (result.effective_score_n, result.run_id)),
    )
    results = results[: args.limit]

    print_results(
        results,
        total_count=total_count,
        sort_label="run" if args.by_run else "effective score",
    )
    return 0


def parse_results(path: Path) -> list[RunResult]:
    results: list[RunResult] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_index, row in enumerate(reader, start=2):
            recovery_performed = parse_bool(row["recovery_performed"])
            score_n = float(row["score_n"])
            results.append(
                RunResult(
                    run_id=int(row["run_id"]),
                    source=row["source"],
                    peak_n=float(row["peak_remaining_force_n"]),
                    score_n=score_n,
                    effective_score_n=(
                        SAFETY_LIMIT_SCORE_N if recovery_performed else score_n
                    ),
                    touchdown_n=float(row["touchdown_force_n"]),
                    pull_s=float(row["pull_elapsed_s"]),
                    recovery_performed=recovery_performed,
                    row_number=row_index,
                )
            )
    return results


def print_results(results: list[RunResult], *, total_count: int, sort_label: str) -> None:
    if len(results) == total_count:
        print(f"Found {total_count} result(s), sorted by {sort_label}. Lower score is better.")
    else:
        print(f"Showing {len(results)} of {total_count} result(s), sorted by {sort_label}. Lower score is better.")
    print()
    print(
        f"{'rank':>4} {'run':>5} {'score_N':>9} {'peak_N':>9} "
        f"{'touch_N':>9} {'pull_s':>8} {'recov':>5} {'source':<15} {'row':>5}"
    )
    print("-" * 83)
    for rank, result in enumerate(results, start=1):
        print(
            f"{rank:>4} "
            f"{result.run_id:>5} "
            f"{result.effective_score_n:>9.3f} "
            f"{result.peak_n:>+9.3f} "
            f"{result.touchdown_n:>+9.3f} "
            f"{result.pull_s:>8.3f} "
            f"{int(result.recovery_performed):>5} "
            f"{result.source:<15.15} "
            f"{result.row_number:>5}"
        )


def parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
