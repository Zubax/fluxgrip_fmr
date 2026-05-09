#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


RUN_PATTERN = re.compile(
    r"^Run\s+(?P<run_id>\d+)\s+complete:\s+"
    r"peak=(?P<peak>[+-]?\d+(?:\.\d+)?)\s+N\s+"
    r"score=(?P<score>\d+(?:\.\d+)?)\s+N\s+"
    r"touchdown=(?P<touchdown>[+-]?\d+(?:\.\d+)?)\s+N\s+"
    r"pull=(?P<pull>\d+(?:\.\d+)?)\s+s$"
)


@dataclass(frozen=True)
class RunResult:
    run_id: int
    peak_n: float
    score_n: float
    touchdown_n: float
    pull_s: float
    line_number: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List force-rig optimization results from an llm_optimize terminal log."
    )
    parser.add_argument(
        "log_file",
        type=Path,
        nargs="?",
        default=Path("results.log"),
        help="Log file to parse. Defaults to results.log in the current directory.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=positive_int,
        help="Only print the best N runs.",
    )
    parser.add_argument(
        "--by-run",
        action="store_true",
        help="Sort by run number instead of score.",
    )
    args = parser.parse_args()

    results = parse_results(args.log_file)
    if not results:
        raise SystemExit(f"No run results found in {args.log_file}")

    total_count = len(results)
    results = sorted(
        results,
        key=(lambda result: result.run_id) if args.by_run else (lambda result: (result.score_n, result.run_id)),
    )
    if args.limit is not None:
        results = results[: args.limit]

    print_results(results, total_count=total_count, sort_label="run" if args.by_run else "score")
    return 0


def parse_results(path: Path) -> list[RunResult]:
    results: list[RunResult] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            match = RUN_PATTERN.match(line.strip())
            if match is None:
                continue
            results.append(
                RunResult(
                    run_id=int(match["run_id"]),
                    peak_n=float(match["peak"]),
                    score_n=float(match["score"]),
                    touchdown_n=float(match["touchdown"]),
                    pull_s=float(match["pull"]),
                    line_number=line_number,
                )
            )
    return results


def print_results(results: list[RunResult], *, total_count: int, sort_label: str) -> None:
    if len(results) == total_count:
        print(f"Found {total_count} result(s), sorted by {sort_label}. Lower score is better.")
    else:
        print(f"Showing {len(results)} of {total_count} result(s), sorted by {sort_label}. Lower score is better.")
    print()
    print(f"{'rank':>4} {'run':>5} {'score_N':>9} {'peak_N':>9} {'touch_N':>9} {'pull_s':>8} {'line':>6}")
    print("-" * 62)
    for rank, result in enumerate(results, start=1):
        print(
            f"{rank:>4} "
            f"{result.run_id:>5} "
            f"{result.score_n:>9.3f} "
            f"{result.peak_n:>+9.3f} "
            f"{result.touchdown_n:>+9.3f} "
            f"{result.pull_s:>8.3f} "
            f"{result.line_number:>6}"
        )


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
