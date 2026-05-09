#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fluxgrip_interface import DEMAG_VALUE_COUNT, FluxGripConfig, normalize_demag_values
from force_rig_interface import ForceRigConfig, ForceRigInterface, RemainingForceResult
from setup_serial_links import FORCE_SENSOR_PORT, STEP_DRIVE_PORT

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SLCAN_REMOVE_COMMAND = "sudo ./scripts/setup_slcan --remove-all"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_COMPLETION_TOKENS = 2000
DEFAULT_RESULTS_CSV = PROJECT_ROOT / "force_rig" / "llm_optimize_results.csv"
CSV_FIELDNAMES = [
    "run_id",
    "timestamp",
    "source",
    "model",
    "score_n",
    "peak_remaining_force_n",
    "touchdown_force_n",
    "pull_elapsed_s",
    "detached",
    "recovery_performed",
    "demag_values",
    "llm_rationale",
]

DEFAULT_DEMAGNETIZATION_SEQUENCE = [
    -100,
    +100,
    -100,
    +100,
    -99,
    -92,
    -88,
    +80,
    +74,
    -67,
    -61,
    +56,
    +51,
    -46,
    -42,
    +38,
    +35,
    -32,
    -29,
    +27,
    +25,
    -22,
    -20,
    +18,
    -16,
    +14,
    -11,
    +10,
    -8,
    +7,
    -6,
    +5,
    -4,
    +4,
    -3,
    +3,
    -2,
    +2,
    -1,
    +2,
    -1,
    +2,
    -1,
    +1,
    -1,
    +1,
    -1,
    +1,
    -1,
    +1,
    -1,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
]
STATE_VERSION = 1
RECENT_RUN_LIMIT = 10


@dataclasses.dataclass(frozen=True)
class LlmSuggestion:
    demag_values: list[int]
    rationale: str
    raw_response: str


class InvalidLlmResponseError(ValueError):
    def __init__(self, message: str, raw_response: str) -> None:
        self.raw_response = raw_response
        super().__init__(message)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.CRITICAL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    slcan_interfaces = setup_slcan_interfaces()
    if slcan_interfaces:
        print(
            f"ERROR: Existing SLCAN interface(s) detected: {', '.join(slcan_interfaces)}",
            file=sys.stderr,
        )
        print("Please remove them before running this optimizer:", file=sys.stderr)
        print(f"  {SLCAN_REMOVE_COMMAND}", file=sys.stderr)
        return 1

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print()
        LOGGER.info("Interrupted")
        return 130
    except Exception as ex:
        print(f"ERROR: {type(ex).__name__}: {ex}", file=sys.stderr)
        LOGGER.debug("LLM optimization failed", exc_info=True)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Optimize FluxGrip demag values by repeatedly measuring remaining force "
            f"and asking the OpenAI API for the next {DEMAG_VALUE_COUNT}-value sequence to test."
        )
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=PROJECT_ROOT / "force_rig" / "llm_optimize_state.json",
        help="JSON file used to persist best run and the last 10 measured runs.",
    )
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=DEFAULT_RESULTS_CSV,
        help=f"CSV file appended with every completed run. Defaults to {DEFAULT_RESULTS_CSV}.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore any existing state file and start a fresh optimization history.",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Do not factory-reset and measure the factory-default demag sequence at startup.",
    )
    parser.add_argument(
        "--iterations",
        "-n",
        type=non_negative_int,
        default=10,
        help="Number of LLM-proposed demag sequences to test after the baseline run.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        help=f"OpenAI model used for proposing demag values. Defaults to OPENAI_MODEL or {DEFAULT_OPENAI_MODEL}.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh"),
        default=os.environ.get("OPENAI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT),
        help=(
            "Reasoning effort for OpenAI reasoning models. "
            f"Defaults to OPENAI_REASONING_EFFORT or {DEFAULT_REASONING_EFFORT}."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=non_negative_float,
        default=DEFAULT_TEMPERATURE,
        help=f"Sampling temperature for the OpenAI request. Defaults to {DEFAULT_TEMPERATURE}.",
    )
    parser.add_argument(
        "--llm-retries",
        type=non_negative_int,
        default=2,
        help="Retry count when the model returns invalid or repeated demag values.",
    )
    parser.add_argument(
        "--demag-min",
        type=int,
        default=-100,
        help="Minimum allowed demag value sent to the rig.",
    )
    parser.add_argument(
        "--demag-max",
        type=int,
        default=100,
        help="Maximum allowed demag value sent to the rig.",
    )
    parser.add_argument(
        "--force-port",
        default=FORCE_SENSOR_PORT,
        help="Force-sensor serial port or URI.",
    )
    parser.add_argument(
        "--drive-port", default=STEP_DRIVE_PORT, help="Step-drive serial port or URI."
    )
    parser.add_argument(
        "--touch-force", type=float, default=0.3, help="Touchdown threshold in newtons."
    )
    parser.add_argument(
        "--touch-timeout",
        type=positive_float,
        default=30.0,
        help="Max seconds to search downward.",
    )
    parser.add_argument(
        "--pull-timeout",
        type=positive_float,
        default=15.0,
        help="Seconds to pull upward per run.",
    )
    parser.add_argument(
        "--tare-samples",
        type=positive_int,
        default=50,
        help="Samples used for each force tare.",
    )
    parser.add_argument(
        "--sample-period",
        type=positive_float,
        default=0.02,
        help="Force polling period in seconds.",
    )
    parser.add_argument(
        "--max-pull-force",
        type=positive_float,
        default=10.0,
        help="Safety limit: recover and retry if upward pull force magnitude reaches this value.",
    )
    parser.add_argument("--settle-after-touch", type=non_negative_float, default=0.5)
    parser.add_argument("--settle-after-demag", type=non_negative_float, default=1.0)
    parser.add_argument("--magnetized-hold", type=non_negative_float, default=1.0)
    parser.add_argument(
        "--can-iface",
        help="Cyphal CAN interface. Use /dev/... for SLCAN or pass a full URI.",
    )
    parser.add_argument("--can-iface-index", type=non_negative_int, default=0)
    parser.add_argument("--controller-node-id", type=node_id, default=1)
    parser.add_argument("--target-node-id", type=node_id, default=None)
    parser.add_argument(
        "--debug",
        "--verbose",
        "-v",
        dest="debug",
        action="store_true",
        help="Enable debug logging and detailed terminal output.",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    if args.demag_min > args.demag_max:
        raise ValueError("--demag-min must be <= --demag-max")
    if not os.environ.get("OPENAI_API_KEY") and args.iterations > 0:
        raise RuntimeError("OPENAI_API_KEY is not set")

    state = new_state() if args.reset_state else load_state(args.state_file)
    config = ForceRigConfig(
        force_port=args.force_port,
        drive_port=args.drive_port,
        touch_force_n=args.touch_force,
        touch_timeout_s=args.touch_timeout,
        pull_timeout_s=args.pull_timeout,
        tare_samples=args.tare_samples,
        sample_period_s=args.sample_period,
        max_pull_force_n=args.max_pull_force,
        settle_after_touch_s=args.settle_after_touch,
        settle_after_demag_s=args.settle_after_demag,
        magnetized_hold_s=args.magnetized_hold,
        fluxgrip=FluxGripConfig(
            controller_node_id=args.controller_node_id,
            target_node_id=args.target_node_id,
            can_iface=args.can_iface,
            can_iface_index=args.can_iface_index,
        ),
    )

    reporter = RunReporter(config, debug=args.debug)

    print_header(args, state)
    print("Configuration: opening force rig interfaces")
    async with ForceRigInterface(
        config,
        progress=reporter.print_progress if args.debug else None,
        phase=reporter.print_phase,
    ) as rig:
        if not args.skip_baseline:
            reporter.start_run(
                run_id=int(state.get("run_count", 0)) + 1,
                label="baseline",
                state=state,
            )
            reporter.status("configuration: factory reset and default demag baseline")
            await rig.factory_reset_fluxgrip()
            result = await rig.measure_remaining_force()
            record = make_run_record(
                state=state,
                source="factory-default",
                demag_values=DEFAULT_DEMAGNETIZATION_SEQUENCE,
                result=result,
                model=None,
                llm_rationale="Factory-reset FluxGrip default demag sequence.",
            )
            store_run(state, record)
            save_state(args.state_file, state)
            append_run_csv(args.results_csv, record)
            print_run_summary(record, state, debug=args.debug)

        for iteration in range(1, args.iterations + 1):
            reporter.start_run(
                run_id=int(state.get("run_count", 0)) + 1,
                label=f"iteration {iteration}/{args.iterations}",
                state=state,
            )
            reporter.status("requesting next demag sequence from OpenAI")
            suggestion = await request_valid_suggestion(args, state)
            if args.debug:
                print(f"LLM rationale: {suggestion.rationale}")
                print(
                    f"Testing demag values: {format_demag_values(suggestion.demag_values)}"
                )
            else:
                reporter.status("testing proposed demag sequence")

            result = await rig.measure_remaining_force(suggestion.demag_values)
            record = make_run_record(
                state=state,
                source="llm",
                demag_values=suggestion.demag_values,
                result=result,
                model=args.model,
                llm_rationale=suggestion.rationale,
                raw_llm_response=suggestion.raw_response,
            )
            store_run(state, record)
            save_state(args.state_file, state)
            append_run_csv(args.results_csv, record)
            print_run_summary(record, state, debug=args.debug)

    print_final_summary(args.state_file, args.results_csv, state, debug=args.debug)


async def request_valid_suggestion(
    args: argparse.Namespace, state: dict[str, Any]
) -> LlmSuggestion:
    validation_error: str | None = None
    for attempt in range(args.llm_retries + 1):
        try:
            suggestion = await asyncio.to_thread(
                request_llm_suggestion,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                temperature=args.temperature,
                state=state,
                demag_min=args.demag_min,
                demag_max=args.demag_max,
                validation_error=validation_error,
            )
            validate_candidate(
                suggestion.demag_values,
                state=state,
                demag_min=args.demag_min,
                demag_max=args.demag_max,
            )
            return suggestion
        except InvalidLlmResponseError as ex:
            validation_error = str(ex)
            print_invalid_llm_response(
                attempt + 1, ex.raw_response, debug=args.debug
            )
            if args.debug:
                LOGGER.warning(
                    "Invalid OpenAI response on attempt %d: %s",
                    attempt + 1,
                    validation_error,
                )
        except ValueError as ex:
            validation_error = str(ex)
            print_invalid_llm_response(
                attempt + 1, suggestion.raw_response, debug=args.debug
            )
            if args.debug:
                LOGGER.warning(
                    "Invalid LLM suggestion on attempt %d: %s",
                    attempt + 1,
                    validation_error,
                )
    raise RuntimeError(
        f"OpenAI did not return a valid demag sequence: {validation_error}"
    )


def request_llm_suggestion(
    *,
    model: str,
    reasoning_effort: str,
    temperature: float,
    state: dict[str, Any],
    demag_min: int,
    demag_max: int,
    validation_error: str | None,
) -> LlmSuggestion:
    try:
        from openai import OpenAI
    except ModuleNotFoundError as ex:
        raise RuntimeError(
            "The openai package is not installed in this Python environment"
        ) from ex

    client = OpenAI()
    prompt_payload = build_prompt_payload(
        state=state,
        demag_min=demag_min,
        demag_max=demag_max,
        validation_error=validation_error,
    )
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(prompt_payload, indent=2)},
        ],
        "reasoning_effort": reasoning_effort,
        "response_format": llm_response_format(
            demag_min=demag_min,
            demag_max=demag_max,
        ),
        "max_completion_tokens": DEFAULT_MAX_COMPLETION_TOKENS,
    }
    if model_supports_custom_temperature(model):
        request["temperature"] = temperature
    elif temperature != DEFAULT_TEMPERATURE:
        LOGGER.warning(
            "%s only supports the default temperature; using %.1f instead of %.3f",
            model,
            DEFAULT_TEMPERATURE,
            temperature,
        )

    completion = client.chat.completions.create(**request)
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned an empty response")
    try:
        return parse_llm_suggestion(content)
    except ValueError as ex:
        raise InvalidLlmResponseError(str(ex), content) from ex


SYSTEM_PROMPT = f"""
You optimize the demagnetization sequence of a FluxGrip magnet on a physical force rig.

Goal:
- Minimize abs(peak_remaining_force_n).
- peak_remaining_force_n is normally negative; values closer to 0 N are better.
- Large negative values are bad because they mean stronger residual magnetic holding force.

Constraints:
- Return exactly one JSON object and no markdown.
- The JSON object must contain "demag_values" and "rationale".
- "demag_values" must be exactly {DEMAG_VALUE_COUNT} signed integers.
- Stay within the provided inclusive demag_min/demag_max limits.
- Do not repeat a sequence already shown in best_run or recent_runs.
- The magnet ignores every value after the first zero in the sequence; make meaningful changes before the first zero.
- Small changes at the end of the sequence mostly have no effect, focus on the first half and only then optimize the second half of the sequence.
""".strip()


def build_prompt_payload(
    *,
    state: dict[str, Any],
    demag_min: int,
    demag_max: int,
    validation_error: str | None,
) -> dict[str, Any]:
    return {
        "objective": "minimize abs(peak_remaining_force_n)",
        "demag_value_count": DEMAG_VALUE_COUNT,
        "demag_min": demag_min,
        "demag_max": demag_max,
        "factory_default_demag_values": DEFAULT_DEMAGNETIZATION_SEQUENCE,
        "zero_rule": "The magnet ignores all values after the first zero.",
        "best_run": compact_run_for_prompt(state.get("best_run")),
        "recent_runs": [
            compact_run_for_prompt(run)
            for run in state.get("recent_runs", [])[-RECENT_RUN_LIMIT:]
        ],
        "previous_validation_error": validation_error,
        "required_response_shape": {
            "demag_values": [f"exactly {DEMAG_VALUE_COUNT} integers"],
            "rationale": "short explanation of why this sequence is the next best experiment",
        },
    }


def llm_response_format(*, demag_min: int, demag_max: int) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "demag_optimization_suggestion",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["demag_values", "rationale"],
                "properties": {
                    "demag_values": {
                        "type": "array",
                        "minItems": DEMAG_VALUE_COUNT,
                        "maxItems": DEMAG_VALUE_COUNT,
                        "items": {
                            "type": "integer",
                            "minimum": demag_min,
                            "maximum": demag_max,
                        },
                    },
                    "rationale": {
                        "type": "string",
                    },
                },
            },
        },
    }


def setup_slcan_interfaces() -> list[str]:
    return sorted(path.name for path in Path("/sys/class/net").glob("slcan*"))


def model_supports_custom_temperature(model: str) -> bool:
    return not model.lower().startswith("gpt-5")


def print_invalid_llm_response(
    attempt: int, raw_response: str, *, debug: bool
) -> None:
    if not debug:
        print(f"OpenAI returned an invalid response on attempt {attempt}; retrying.")
        return
    print()
    print(f"OpenAI returned an invalid response on attempt {attempt}:")
    print(raw_response)
    print()


def parse_llm_suggestion(raw_response: str) -> LlmSuggestion:
    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError:
        data = json.loads(extract_json_object(raw_response))

    values_raw = data.get("demag_values", data.get("values"))
    if values_raw is None:
        raise ValueError("OpenAI response did not contain demag_values")
    if not isinstance(values_raw, list):
        raise ValueError("demag_values must be a list")

    values = normalize_demag_values([int(value) for value in values_raw])
    rationale = str(data.get("rationale", "")).strip() or "No rationale provided."
    return LlmSuggestion(values, rationale, raw_response)


def extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("OpenAI response did not contain a JSON object")
    return text[start : end + 1]


def validate_candidate(
    values: list[int],
    *,
    state: dict[str, Any],
    demag_min: int,
    demag_max: int,
) -> None:
    normalize_demag_values(values)
    for index, value in enumerate(values):
        if value < demag_min or value > demag_max:
            raise ValueError(
                f"demag_values[{index}]={value} is outside allowed range {demag_min}..{demag_max}"
            )

    candidate_key = demag_key(values)
    tested_keys = {
        demag_key(run["demag_values"])
        for run in state.get("recent_runs", [])
        if isinstance(run.get("demag_values"), list)
    }
    best_run = state.get("best_run")
    if isinstance(best_run, dict) and isinstance(best_run.get("demag_values"), list):
        tested_keys.add(demag_key(best_run["demag_values"]))
    if candidate_key in tested_keys:
        raise ValueError("demag sequence repeats a previously stored run")


def make_run_record(
    *,
    state: dict[str, Any],
    source: str,
    demag_values: list[int],
    result: RemainingForceResult,
    model: str | None,
    llm_rationale: str,
    raw_llm_response: str | None = None,
) -> dict[str, Any]:
    run_id = int(state.get("run_count", 0)) + 1
    peak_force = float(result.peak_remaining_force_n)
    record: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "model": model,
        "demag_values": normalize_demag_values(demag_values),
        "peak_remaining_force_n": peak_force,
        "score_n": abs(peak_force),
        "touchdown_force_n": float(result.touchdown_force_n),
        "pull_elapsed_s": float(result.pull_elapsed_s),
        "detached": bool(result.detached),
        "recovery_performed": bool(result.recovery_performed),
        "llm_rationale": llm_rationale,
    }
    if raw_llm_response is not None:
        record["raw_llm_response"] = raw_llm_response
    return record


def compact_run_for_prompt(run: Any) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    return {
        "run_id": run.get("run_id"),
        "source": run.get("source"),
        "demag_values": run.get("demag_values"),
        "peak_remaining_force_n": run.get("peak_remaining_force_n"),
        "score_n": run.get("score_n"),
        "recovery_performed": run.get("recovery_performed"),
        "llm_rationale": run.get("llm_rationale"),
    }


def store_run(state: dict[str, Any], record: dict[str, Any]) -> None:
    state["run_count"] = int(record["run_id"])
    recent_runs = list(state.get("recent_runs", []))
    recent_runs.append(record)
    state["recent_runs"] = recent_runs[-RECENT_RUN_LIMIT:]

    best_run = state.get("best_run")
    if best_run is None or float(record["score_n"]) < float(best_run["score_n"]):
        state["best_run"] = record


def append_run_csv(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(csv_row(record))


def csv_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": record["run_id"],
        "timestamp": record["timestamp"],
        "source": record["source"],
        "model": record.get("model") or "",
        "score_n": f"{float(record['score_n']):.9f}",
        "peak_remaining_force_n": f"{float(record['peak_remaining_force_n']):.9f}",
        "touchdown_force_n": f"{float(record['touchdown_force_n']):.9f}",
        "pull_elapsed_s": f"{float(record['pull_elapsed_s']):.9f}",
        "detached": int(bool(record["detached"])),
        "recovery_performed": int(bool(record["recovery_performed"])),
        "demag_values": format_demag_values(record["demag_values"]),
        "llm_rationale": record.get("llm_rationale", ""),
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_state()
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") != STATE_VERSION:
        raise RuntimeError(
            f"Unsupported optimizer state version in {path}: {state.get('version')!r}"
        )
    state.setdefault("run_count", 0)
    state.setdefault("best_run", None)
    state.setdefault("recent_runs", [])
    return state


def new_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "objective": "minimize abs(peak_remaining_force_n)",
        "run_count": 0,
        "best_run": None,
        "recent_runs": [],
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    temporary_path.replace(path)


def print_header(args: argparse.Namespace, state: dict[str, Any]) -> None:
    print("----------------------------------------------")
    print("  FORCE RIG - LLM Demag Optimization")
    print("----------------------------------------------")
    print(f"State file: {args.state_file}")
    print(f"Results CSV: {args.results_csv}")
    print(f"Model: {args.model}")
    print(f"Reasoning effort: {args.reasoning_effort}")
    print(
        f"Existing stored runs: {len(state.get('recent_runs', []))} recent, {state.get('run_count', 0)} total"
    )
    best_run = state.get("best_run")
    if isinstance(best_run, dict):
        print(f"Current best: {format_best_run(best_run)}")


@dataclasses.dataclass
class RunReporter:
    config: ForceRigConfig
    debug: bool
    run_id: int | None = None
    label: str = ""

    def start_run(self, *, run_id: int, label: str, state: dict[str, Any]) -> None:
        self.run_id = run_id
        self.label = label
        print()
        print(f"Run {run_id} ({label})")
        print(f"Best so far: {format_best_run(state.get('best_run'))}")

    def status(self, message: str) -> None:
        print(f"{self._prefix()}{message}")

    def print_progress(
        self, phase: str, elapsed_s: float, force_n: float, peak_force_n: float
    ) -> None:
        if phase == "down":
            print(
                f"\r  down {elapsed_s:6.2f}s  F={force_n:+08.3f} N",
                end="",
                flush=True,
            )
            return
        print(
            f"\r  up   {elapsed_s:6.2f}s  F={force_n:+08.3f} N  peak={peak_force_n:+08.3f} N",
            end="",
            flush=True,
        )

    def print_phase(self, phase: str) -> None:
        if self.debug and phase in {
            "settle-after-touchdown",
            "fluxgrip-connect",
            "tare-before-pull",
            "factory-reset",
            "tare-before-pull-retry",
            "pull-up-retry",
        }:
            print()
        print(f"{self._prefix()}{self._phase_message(phase)}")

    def _prefix(self) -> str:
        if self.run_id is None:
            return ""
        return f"Run {self.run_id}: "

    def _phase_message(self, phase: str) -> str:
        messages = {
            "tare-before-touchdown": "configuration: taring force sensors before touchdown",
            "touchdown": f"down: moving arm until total force reaches {self.config.touch_force_n:+.3f} N",
            "settle-after-touchdown": f"configuration: settling after touchdown for {self.config.settle_after_touch_s:.3f} s",
            "fluxgrip-connect": "configuration: connecting to FluxGrip",
            "set-demag-values": "configuration: writing demag values",
            "magnetize": "magnetizing",
            "magnetized-hold": f"magnetized hold for {self.config.magnetized_hold_s:.3f} s",
            "demagnetize": "demagnetizing",
            "settle-after-demag": f"configuration: settling after demag for {self.config.settle_after_demag_s:.3f} s",
            "tare-before-pull": "configuration: re-taring force sensors before pull",
            "pull-up": "up: moving arm and recording peak remaining force",
            "pull-force-safety": "safety: pull force limit reached; stopping arm and recovering",
            "factory-reset": "configuration: factory resetting FluxGrip",
            "recovery-magnetize": "recovery: magnetizing",
            "recovery-magnetized-hold": f"recovery: magnetized hold for {self.config.magnetized_hold_s:.3f} s",
            "recovery-demagnetize": "recovery: demagnetizing",
            "recovery-settle-after-demag": f"recovery: settling after demag for {self.config.settle_after_demag_s:.3f} s",
            "tare-before-pull-retry": "recovery: re-taring force sensors before pull retry",
            "pull-up-retry": "recovery: retrying arm-up movement",
        }
        return messages.get(phase, phase)


def print_run_summary(
    record: dict[str, Any], state: dict[str, Any], *, debug: bool
) -> None:
    best_run = state.get("best_run")
    is_best = isinstance(best_run, dict) and best_run.get("run_id") == record["run_id"]
    print()
    if debug:
        print(
            f"Run {record['run_id']} complete: "
            f"peak={record['peak_remaining_force_n']:+.3f} N "
            f"score={record['score_n']:.3f} N "
            f"touchdown={record['touchdown_force_n']:+.3f} N "
            f"pull={record['pull_elapsed_s']:.3f} s"
        )
    else:
        print(
            f"Run {record['run_id']} complete: "
            f"score={record['score_n']:.3f} N "
            f"peak={record['peak_remaining_force_n']:+.3f} N"
        )
    if record["recovery_performed"]:
        print("Recovery was performed during this run.")
    if is_best:
        print(f"New best: {format_best_run(best_run)}")
        if debug:
            print(f"New best demag values: {format_demag_values(record['demag_values'])}")
    elif isinstance(best_run, dict):
        print(f"Best so far: {format_best_run(best_run)}")


def print_final_summary(
    state_path: Path, results_csv: Path, state: dict[str, Any], *, debug: bool
) -> None:
    print()
    print("----------------------------------------------")
    print("  Optimization state saved")
    print(f"  {state_path}")
    print("  Results CSV")
    print(f"  {results_csv}")
    best_run = state.get("best_run")
    if isinstance(best_run, dict):
        print(
            f"  Best run: {best_run['run_id']} "
            f"score={best_run['score_n']:.3f} N peak={best_run['peak_remaining_force_n']:+.3f} N"
        )
        if debug:
            print(f"  Best demag values: {format_demag_values(best_run['demag_values'])}")
    print("----------------------------------------------")


def format_best_run(best_run: Any) -> str:
    if not isinstance(best_run, dict):
        return "none"
    return (
        f"run {best_run['run_id']} "
        f"score={best_run['score_n']:.3f} N "
        f"peak={best_run['peak_remaining_force_n']:+.3f} N"
    )


def format_demag_values(values: list[int]) -> str:
    return ",".join(f"{value:+d}" for value in values)


def demag_key(values: list[int]) -> str:
    return ",".join(str(value) for value in values)


def positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def non_negative_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def node_id(raw: str) -> int:
    value = int(raw)
    if value < 0 or value > 127:
        raise argparse.ArgumentTypeError("node-ID must be in range 0..127")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
