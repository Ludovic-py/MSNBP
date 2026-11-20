# -*- coding: utf-8 -*-
"""
Boolean Wiring Explorer (Unified Search, Verification & Visual Simulator)
Python 3.7+ compatible, standard-library only (Tkinter + multiprocessing).
"""

import ast
import csv
import json
import math
import multiprocessing
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from multiprocessing import cpu_count
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Constants & Safety Caps
# ---------------------------------------------------------------------------

VALID_LOGIC_FUNCS = ("OR", "AND", "XOR", "NAND")
MAX_VERIFY_CABLES = 14        # 2^14 = 16,384 truth-table rows
MAX_SEARCH_CABLES = 9         # Safeguard against combinatorial explosion
MAX_SOLUTIONS_CAP = 5000
MAX_WORKERS_CAP = max(2, (cpu_count() or 1) * 2)
SETTINGS_FILENAME = "logic_network_settings.json"

GATE_COLORS = {
    "OR": "#2563EB",    # Blue
    "AND": "#7C3AED",   # Violet
    "XOR": "#D97706",   # Amber
    "NAND": "#DC2626",  # Red
}


# ---------------------------------------------------------------------------
# Data Models & Validation
# ---------------------------------------------------------------------------

class WiringValidationError(ValueError):
    """Raised when user wiring or search configuration fails validation."""

    def __init__(self, errors):
        # type: (Sequence[str]) -> None
        self.errors = list(errors)
        super(WiringValidationError, self).__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Connection:
    """Single two-cable boolean gate connection."""
    cable1: int
    cable2: int
    upper_logic: str
    lower_logic: str

    def to_tuple(self):
        # type: () -> Tuple[int, int, str, str]
        return (self.cable1, self.cable2, self.upper_logic, self.lower_logic)

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "cable1": self.cable1,
            "cable2": self.cable2,
            "upper_logic": self.upper_logic,
            "lower_logic": self.lower_logic,
        }

    @classmethod
    def from_tuple(cls, raw):
        # type: (Any) -> "Connection"
        if not isinstance(raw, (tuple, list)) or len(raw) != 4:
            raise WiringValidationError(
                ["Expected a 4-tuple (cable1, cable2, upper_logic, lower_logic), got: {!r}".format(raw)]
            )
        c1, c2, upper, lower = raw
        if isinstance(c1, bool) or not isinstance(c1, int) or isinstance(c2, bool) or not isinstance(c2, int):
            raise WiringValidationError(
                ["Cable indices must be integers, got: ({!r}, {!r})".format(c1, c2)]
            )
        if not isinstance(upper, str) or not isinstance(lower, str):
            raise WiringValidationError(
                ["Logic functions must be strings, got: ({!r}, {!r})".format(upper, lower)]
            )
        return cls(c1, c2, upper.strip().upper(), lower.strip().upper())

    @classmethod
    def from_dict(cls, data):
        # type: (Dict[str, Any]) -> "Connection"
        return cls.from_tuple((
            data.get("cable1"),
            data.get("cable2"),
            data.get("upper_logic"),
            data.get("lower_logic"),
        ))


@dataclass(frozen=True)
class SearchConfig:
    """Validated parameters for a parallel search run."""
    num_cables: int
    start_size: int
    max_size: int
    max_solutions: int
    workers: int
    prune_noops: bool = True


@dataclass(frozen=True)
class SearchProgress:
    """Progress snapshot reported from the search coordinator to the UI."""
    current_size: int
    subsets_done: int
    subsets_total: int
    perms_checked: int
    solutions_found: int
    elapsed_sec: float
    eta_sec: Optional[float]


@dataclass(frozen=True)
class SearchResult:
    """Final outcome of a search run."""
    solutions: Tuple[Tuple[Tuple[int, int, str, str], ...], ...]
    cancelled: bool
    elapsed_sec: float
    perms_checked: int


@dataclass(frozen=True)
class VerificationSummary:
    """Summary of verifying a single wiring configuration."""
    num_cables: int
    connections: Tuple[Connection, ...]
    truth_table: Tuple[Tuple[Tuple[int, ...], Tuple[int, ...]], ...]
    is_reliable: bool
    failing_indices: Tuple[int, ...]


def comb_count(n, k):
    # type: (int, int) -> int
    """Python 3.7-compatible binomial coefficient C(n, k)."""
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    result = 1
    for i in range(1, k + 1):
        result = result * (n - k + i) // i
    return result


def normalize_connections(connections):
    # type: (Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> List[Connection]
    """Convert a sequence of Connection objects or 4-tuples into Connection objects."""
    normalized = []
    for item in connections:
        if isinstance(item, Connection):
            normalized.append(item)
        else:
            normalized.append(Connection.from_tuple(item))
    return normalized


def parse_connections_text(text):
    # type: (str) -> List[Connection]
    """
    Safely parse wiring tuples from plain text using ast.literal_eval().
    Accepts either:
      - One 4-tuple per line (optional trailing comma, # comments ignored)
      - A Python list/tuple of 4-tuples
      - A JSON array of objects or 4-element arrays
    """
    stripped = text.strip()
    if not stripped:
        return []

    # Try JSON array first if it starts with '[' and looks like JSON
    if stripped.startswith("["):
        try:
            parsed_json = json.loads(stripped)
            if isinstance(parsed_json, list):
                results = []
                for entry in parsed_json:
                    if isinstance(entry, dict):
                        results.append(Connection.from_dict(entry))
                    else:
                        results.append(Connection.from_tuple(entry))
                return results
        except ValueError:
            pass

    # Try full Python literal list/tuple of tuples
    if stripped.startswith("[") or stripped.startswith("(("):
        try:
            parsed_block = ast.literal_eval(stripped)
            if isinstance(parsed_block, (list, tuple)):
                return [Connection.from_tuple(item) for item in parsed_block]
        except (ValueError, SyntaxError):
            pass

    # Otherwise parse line-by-line safely
    connections = []
    errors = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(","):
            line = line[:-1].strip()
        try:
            parsed = ast.literal_eval(line)
            connections.append(Connection.from_tuple(parsed))
        except WiringValidationError as exc:
            errors.append("Line {}: {}".format(line_no, exc.errors[0]))
        except (ValueError, SyntaxError):
            errors.append(
                "Line {}: Invalid tuple syntax {!r}. Expected e.g. (0, 2, 'OR', 'AND')".format(
                    line_no, raw_line.strip()
                )
            )

    if errors:
        raise WiringValidationError(errors)
    return connections


def validate_connections(num_cables, connections):
    # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> List[Connection]
    """Validate cable count and every connection step. Raises WiringValidationError if invalid."""
    errors = []
    if isinstance(num_cables, bool) or not isinstance(num_cables, int):
        errors.append("Number of cables must be an integer.")
    elif num_cables < 2 or num_cables > MAX_VERIFY_CABLES:
        errors.append(
            "Number of cables must be between 2 and {} (got {}).".format(MAX_VERIFY_CABLES, num_cables)
        )

    normalized = []
    try:
        normalized = normalize_connections(connections)
    except WiringValidationError as exc:
        errors.extend(exc.errors)

    if not errors:
        for idx, conn in enumerate(normalized, start=1):
            if not (0 <= conn.cable1 < num_cables):
                errors.append(
                    "Step {}: cable1 ({}) out of range [0, {}].".format(idx, conn.cable1, num_cables - 1)
                )
            if not (0 <= conn.cable2 < num_cables):
                errors.append(
                    "Step {}: cable2 ({}) out of range [0, {}].".format(idx, conn.cable2, num_cables - 1)
                )
            if conn.cable1 == conn.cable2:
                errors.append(
                    "Step {}: cable1 and cable2 cannot be identical (both {}).".format(idx, conn.cable1)
                )
            if conn.upper_logic not in VALID_LOGIC_FUNCS:
                errors.append(
                    "Step {}: unsupported upper_logic {!r}. Allowed: {}.".format(
                        idx, conn.upper_logic, ", ".join(VALID_LOGIC_FUNCS)
                    )
                )
            if conn.lower_logic not in VALID_LOGIC_FUNCS:
                errors.append(
                    "Step {}: unsupported lower_logic {!r}. Allowed: {}.".format(
                        idx, conn.lower_logic, ", ".join(VALID_LOGIC_FUNCS)
                    )
                )

    if errors:
        raise WiringValidationError(errors)
    return normalized


def validate_search_config(num_cables, start_size, max_size, max_solutions, workers, prune_noops=True):
    # type: (int, int, Optional[int], int, int, bool) -> SearchConfig
    """Validate search settings and clamp/enforce safe limits."""
    errors = []
    if num_cables < 2 or num_cables > MAX_SEARCH_CABLES:
        errors.append(
            "Search cables must be between 2 and {} (got {}).".format(MAX_SEARCH_CABLES, num_cables)
        )

    total_pairs = comb_count(max(num_cables, 0), 2)
    resolved_max_size = total_pairs if (max_size is None or max_size <= 0) else max_size

    if start_size < 1:
        errors.append("Start size must be at least 1.")
    elif total_pairs > 0 and start_size > total_pairs:
        errors.append(
            "Start size ({}) exceeds maximum unique cable pairs ({}) for {} cables.".format(
                start_size, total_pairs, num_cables
            )
        )

    if resolved_max_size < start_size:
        errors.append(
            "Max size ({}) cannot be less than start size ({}).".format(resolved_max_size, start_size)
        )
    elif total_pairs > 0 and resolved_max_size > total_pairs:
        resolved_max_size = total_pairs

    if max_solutions < 1 or max_solutions > MAX_SOLUTIONS_CAP:
        errors.append(
            "Max solutions must be between 1 and {}.".format(MAX_SOLUTIONS_CAP)
        )

    if workers < 1 or workers > MAX_WORKERS_CAP:
        errors.append(
            "Workers must be between 1 and {}.".format(MAX_WORKERS_CAP)
        )

    if errors:
        raise WiringValidationError(errors)

    return SearchConfig(
        num_cables=num_cables,
        start_size=start_size,
        max_size=resolved_max_size,
        max_solutions=max_solutions,
        workers=workers,
        prune_noops=bool(prune_noops),
    )


def format_connections_plain_text(connections):
    # type: (Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> str
    """Format connections as clean one-tuple-per-line plain text."""
    normalized = normalize_connections(connections)
    return "\n".join(repr(conn.to_tuple()) for conn in normalized)


# ---------------------------------------------------------------------------
# Core Bitwise Boolean Engine
# ---------------------------------------------------------------------------

_DESCENDING_BITMASKS_CACHE = {}  # type: Dict[int, FrozenSet[int]]


def _get_descending_bitmasks(num_cables):
    # type: (int) -> FrozenSet[int]
    """Precompute bitmasks of all binary vectors sorted in descending order."""
    cached = _DESCENDING_BITMASKS_CACHE.get(num_cables)
    if cached is not None:
        return cached

    masks = {((1 << k) - 1) << (num_cables - k) for k in range(num_cables + 1)}
    frozen = frozenset(masks)
    _DESCENDING_BITMASKS_CACHE[num_cables] = frozen
    return frozen


def _apply_logic_mask(mask1, mask2, logic_func, all_ones_mask):
    # type: (int, int, str, int) -> int
    if logic_func == "OR":
        return mask1 | mask2
    if logic_func == "AND":
        return mask1 & mask2
    if logic_func == "XOR":
        return mask1 ^ mask2
    if logic_func == "NAND":
        return (mask1 & mask2) ^ all_ones_mask
    raise ValueError("logique non suportee: {}".format(logic_func))


@lru_cache(maxsize=32)
def _initial_wire_masks(num_cables):
    # type: (int) -> Tuple[int, ...]
    """
    Bitmask per cable across all 2^num_cables input combinations in lexicographical
    order (matching itertools.product([0, 1], repeat=num_cables)).
    Bit (2^n - 1 - combo_idx) corresponds to combination combo_idx.
    """
    total_combos = 1 << num_cables
    masks = []
    for cable_idx in range(num_cables):
        shift = num_cables - 1 - cable_idx
        mask = 0
        for combo_idx in range(total_combos):
            if (combo_idx >> shift) & 1:
                mask |= 1 << (total_combos - 1 - combo_idx)
        masks.append(mask)
    return tuple(masks)


def _is_masks_descending(wire_masks):
    # type: (Sequence[int]) -> bool
    """
    Check in O(num_cables) bitwise ops whether ALL 2^n truth-table rows are sorted
    in descending order (cable[0] >= cable[1] >= ... >= cable[n-1]).

    For each bit position, b_i >= b_{i+1} iff (b_i | b_{i+1}) == b_i.
    """
    for i in range(len(wire_masks) - 1):
        if (wire_masks[i] | wire_masks[i + 1]) != wire_masks[i]:
            return False
    return True


def apply_logic(input1, input2, logic_func):
    # type: (int, int, str) -> int
    """Apply a single boolean gate on scalar 0/1 values. Supports OR, AND, XOR, NAND."""
    b1 = 1 if input1 else 0
    b2 = 1 if input2 else 0
    if logic_func == "OR":
        return b1 | b2
    if logic_func == "AND":
        return b1 & b2
    if logic_func == "XOR":
        return b1 ^ b2
    if logic_func == "NAND":
        return 0 if (b1 and b2) else 1
    raise ValueError("logique non suportee: {}".format(logic_func))


def boolean_calculations(num_cables, connections):
    # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> List[Tuple[Tuple[int, ...], List[int]]]
    """
    Generate the full truth table for (num_cables, connections).
    Returns a list of (input_tuple, output_list) pairs in product([0, 1], repeat=num_cables) order.
    """
    validated = validate_connections(num_cables, connections)
    total_combos = 1 << num_cables
    all_ones_mask = (1 << total_combos) - 1
    wire_masks = list(_initial_wire_masks(num_cables))

    for conn in validated:
        m1 = wire_masks[conn.cable1]
        m2 = wire_masks[conn.cable2]
        wire_masks[conn.cable1] = _apply_logic_mask(m1, m2, conn.upper_logic, all_ones_mask)
        wire_masks[conn.cable2] = _apply_logic_mask(m1, m2, conn.lower_logic, all_ones_mask)

    truth_table = []
    for combo_idx in range(total_combos):
        bit_pos = total_combos - 1 - combo_idx
        inp = tuple((combo_idx >> (num_cables - 1 - c)) & 1 for c in range(num_cables))
        out = [(wire_masks[c] >> bit_pos) & 1 for c in range(num_cables)]
        truth_table.append((inp, out))

    return truth_table


def evaluate_step_by_step(num_cables, connections, input_bits):
    # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]], Sequence[int]) -> List[List[int]]
    """
    Simulate one specific binary input vector step-by-step for interactive visualization.
    Returns a list of length len(connections) + 1 where index 0 is initial input state
    and index s is the cable state after step s.
    """
    validated = validate_connections(num_cables, connections)
    if len(input_bits) != num_cables:
        raise WiringValidationError(
            ["Expected {} input bits, got {}.".format(num_cables, len(input_bits))]
        )

    state = [1 if bit else 0 for bit in input_bits]
    history = [list(state)]
    for conn in validated:
        v1 = state[conn.cable1]
        v2 = state[conn.cable2]
        state[conn.cable1] = apply_logic(v1, v2, conn.upper_logic)
        state[conn.cable2] = apply_logic(v1, v2, conn.lower_logic)
        history.append(list(state))
    return history


def is_ordered_descending(output):
    # type: (Sequence[int]) -> bool
    """Return True iff binary sequence is ordered descending (all 1s before all 0s)."""
    seen_zero = False
    for val in output:
        if val:
            if seen_zero:
                return False
        else:
            seen_zero = True
    return True


def verify_reliability(truth_table):
    # type: (Sequence[Tuple[Sequence[int], Sequence[int]]]) -> bool
    """Return True iff every output row in truth_table is sorted in descending order."""
    if not truth_table:
        return True
    num_cables = len(truth_table[0][1])
    valid_masks = _get_descending_bitmasks(num_cables)
    for _, outputs in truth_table:
        out_mask = 0
        for val in outputs:
            out_mask = (out_mask << 1) | (1 if val else 0)
        if out_mask not in valid_masks:
            return False
    return True


def run_full_verification(num_cables, connections):
    # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> VerificationSummary
    """Validate, compute truth table, and identify any failing rows."""
    validated = validate_connections(num_cables, connections)
    tt = boolean_calculations(num_cables, validated)
    failing = []
    frozen_tt = []
    for idx, (inp, out) in enumerate(tt):
        frozen_tt.append((tuple(inp), tuple(out)))
        if not is_ordered_descending(out):
            failing.append(idx)
    return VerificationSummary(
        num_cables=num_cables,
        connections=tuple(validated),
        truth_table=tuple(frozen_tt),
        is_reliable=(len(failing) == 0),
        failing_indices=tuple(failing),
    )


# ---------------------------------------------------------------------------
# Parallel Search Engine (with Pruning, Stop Checks & Live Progress)
# ---------------------------------------------------------------------------

def generate_all_connections(num_cables):
    # type: (int) -> List[Tuple[int, int, str, str]]
    """Generate all unique (c1, c2, 'OR', 'AND') comparator connections for num_cables."""
    return [
        (input1, input2, "OR", "AND")
        for input1, input2 in combinations(range(num_cables), 2)
    ]


def test_subset(num_cables, subset, prune_noops=True, stop_event=None, max_needed=0):
    # type: (int, Sequence[Tuple[int, int, str, str]], bool, Any, int) -> Tuple[List[Tuple[Tuple[int, int, str, str], ...]], int]
    """
    Evaluate all permutations of `subset` using fast bitwise DFS.

    - When `prune_noops=True`, skips any branch where an (OR, AND) gate does not
      alter either cable's bitmask across all 2^n inputs (mathematically redundant step).
    - Periodically checks `stop_event` so large subsets cancel promptly.
    - Returns (found_solutions, nodes_or_perms_checked).
    """
    k = len(subset)
    init_masks = list(_initial_wire_masks(num_cables))
    if k == 0:
        if _is_masks_descending(init_masks):
            return ([()], 1)
        return ([], 1)

    solutions = []
    used = [False] * k
    path = [None] * k  # type: List[Optional[Tuple[int, int, str, str]]]
    stats = [0]        # Mutable counter for checked leaves/steps
    stop_hit = [False]

    def dfs(depth, wire_masks):
        if stop_hit[0]:
            return
        if depth == k:
            stats[0] += 1
            if stop_event is not None and (stats[0] & 2047) == 0:
                if stop_event.is_set():
                    stop_hit[0] = True
                    return
            if _is_masks_descending(wire_masks):
                solutions.append(tuple(path))  # type: ignore
                if max_needed > 0 and len(solutions) >= max_needed:
                    stop_hit[0] = True
            return

        for idx in range(k):
            if used[idx]:
                continue
            conn = subset[idx]
            c1, c2, upper_f, lower_f = conn
            m1 = wire_masks[c1]
            m2 = wire_masks[c2]

            # Fast path for standard OR / AND comparator
            if upper_f == "OR" and lower_f == "AND":
                new_m1 = m1 | m2
                new_m2 = m1 & m2
            else:
                all_ones = (1 << (1 << num_cables)) - 1
                new_m1 = _apply_logic_mask(m1, m2, upper_f, all_ones)
                new_m2 = _apply_logic_mask(m1, m2, lower_f, all_ones)

            if prune_noops and new_m1 == m1 and new_m2 == m2:
                stats[0] += 1
                if stop_event is not None and (stats[0] & 2047) == 0:
                    if stop_event.is_set():
                        stop_hit[0] = True
                        return
                continue

            used[idx] = True
            path[depth] = conn
            wire_masks[c1] = new_m1
            wire_masks[c2] = new_m2

            dfs(depth + 1, wire_masks)

            wire_masks[c1] = m1
            wire_masks[c2] = m2
            used[idx] = False

            if stop_hit[0]:
                return

    dfs(0, init_masks)
    return (solutions, stats[0])


def _worker_loop(num_cables, prune_noops, max_solutions, task_queue, result_queue, stop_event):
    """Dedicated worker process loop (spawn-safe on Windows/PyInstaller)."""
    try:
        while not stop_event.is_set():
            try:
                item = task_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is None:
                break

            size_k, subset_batch = item
            batch_solutions = []
            batch_checked = 0
            subsets_completed = 0

            for subset in subset_batch:
                if stop_event.is_set():
                    break
                sols, checked = test_subset(
                    num_cables,
                    subset,
                    prune_noops=prune_noops,
                    stop_event=stop_event,
                    max_needed=max_solutions,
                )
                subsets_completed += 1
                batch_checked += checked
                if sols:
                    batch_solutions.extend(sols)
                    if len(batch_solutions) >= max_solutions:
                        break

            result_queue.put((size_k, subsets_completed, batch_checked, batch_solutions))
    except Exception as exc:
        try:
            result_queue.put(("ERROR", str(exc)))
        except Exception:
            pass
    finally:
        try:
            task_queue.cancel_join_thread()
            result_queue.cancel_join_thread()
        except Exception:
            pass


def _search_coordinator_entry(config, msg_queue, stop_event):
    # type: (SearchConfig, Any, Any) -> None
    """
    Search coordinator (runs in a lightweight background thread so workers are
    spawned only once with zero nested multiprocessing overhead).
    Uses a direct in-thread fast path when workers == 1.
    """
    start_time = time.monotonic()
    total_checked = 0
    solutions = []  # type: List[Tuple[Tuple[int, int, str, str], ...]]

    mode_str = "pruned (no-ops skipped)" if config.prune_noops else "exhaustive"
    msg_queue.put((
        "log",
        "[ Launch ] Cables={} | Size={}..{} | MaxSol={} | Workers={} | Mode={}".format(
            config.num_cables,
            config.start_size,
            config.max_size,
            config.max_solutions,
            config.workers,
            mode_str,
        ),
    ))

    all_conns = generate_all_connections(config.num_cables)
    total_pairs = len(all_conns)

    # Fast in-thread path for single worker (instant startup, zero spawn cost)
    if config.workers == 1:
        try:
            for size_k in range(config.start_size, min(config.max_size, total_pairs) + 1):
                if stop_event.is_set():
                    break
                subsets_total = comb_count(total_pairs, size_k)
                subsets_done = 0
                size_start_time = time.monotonic()
                last_progress_emit = 0.0
                msg_queue.put(("log", "[ Update ] Testing size {} ({:,} subsets)...".format(size_k, subsets_total)))

                for subset in combinations(all_conns, size_k):
                    if stop_event.is_set():
                        break
                    remaining = max(1, config.max_solutions - len(solutions))
                    sols, checked = test_subset(
                        config.num_cables,
                        subset,
                        prune_noops=config.prune_noops,
                        stop_event=stop_event,
                        max_needed=remaining,
                    )
                    subsets_done += 1
                    total_checked += checked
                    if sols:
                        for sol in sols:
                            solutions.append(sol)
                            msg_queue.put(("solution", sol))
                            if len(solutions) >= config.max_solutions:
                                stop_event.set()
                                break

                    now = time.monotonic()
                    if (now - last_progress_emit) >= 0.12 or subsets_done >= subsets_total or stop_event.is_set():
                        elapsed = now - start_time
                        size_elapsed = now - size_start_time
                        eta = None  # type: Optional[float]
                        if 0 < subsets_done < subsets_total:
                            eta = (subsets_total - subsets_done) / max(subsets_done / max(size_elapsed, 1e-6), 1e-9)
                        elif subsets_done >= subsets_total:
                            eta = 0.0
                        msg_queue.put((
                            "progress",
                            SearchProgress(
                                current_size=size_k,
                                subsets_done=subsets_done,
                                subsets_total=subsets_total,
                                perms_checked=total_checked,
                                solutions_found=len(solutions),
                                elapsed_sec=elapsed,
                                eta_sec=eta,
                            ),
                        ))
                        last_progress_emit = now

                if len(solutions) >= config.max_solutions:
                    break

            cancelled = stop_event.is_set() and len(solutions) < config.max_solutions
            msg_queue.put((
                "done",
                SearchResult(
                    solutions=tuple(solutions[: config.max_solutions]),
                    cancelled=cancelled,
                    elapsed_sec=time.monotonic() - start_time,
                    perms_checked=total_checked,
                ),
            ))
        except Exception as exc:
            msg_queue.put(("error", str(exc)))
        return

    # Multi-worker parallel path (spawn-safe on Windows/PyInstaller)
    ctx = multiprocessing.get_context("spawn")
    task_queue = ctx.Queue(maxsize=max(config.workers * 4, 16))
    result_queue = ctx.Queue()
    workers = []  # type: List[Any]

    try:
        for _ in range(config.workers):
            proc = ctx.Process(
                target=_worker_loop,
                args=(
                    config.num_cables,
                    config.prune_noops,
                    config.max_solutions,
                    task_queue,
                    result_queue,
                    stop_event,
                ),
            )
            proc.daemon = True
            proc.start()
            workers.append(proc)

        for size_k in range(config.start_size, min(config.max_size, total_pairs) + 1):
            if stop_event.is_set():
                break

            subsets_total = comb_count(total_pairs, size_k)
            subsets_done = 0
            size_start_time = time.monotonic()
            msg_queue.put((
                "log",
                "[ Update ] Testing size {} ({:,} subsets)...".format(size_k, subsets_total),
            ))

            # Choose batch granularity so progress updates smoothly even on small/large k
            if size_k <= 5:
                batch_size = max(1, min(32, subsets_total // max(config.workers * 2, 1) or 1))
            elif size_k <= 7:
                batch_size = 4
            else:
                batch_size = 1

            subset_iter = combinations(all_conns, size_k)
            pending_batches = 0
            exhausted_iter = False
            max_inflight = config.workers * 3
            last_progress_emit = 0.0

            while not stop_event.is_set():
                # Fill worker queue without blocking indefinitely
                while not exhausted_iter and pending_batches < max_inflight and not stop_event.is_set():
                    batch = []
                    for _ in range(batch_size):
                        try:
                            batch.append(next(subset_iter))
                        except StopIteration:
                            exhausted_iter = True
                            break
                    if batch:
                        try:
                            task_queue.put((size_k, batch), timeout=0.1)
                            pending_batches += 1
                        except queue.Full:
                            # Put back by breaking? Wait: if queue is full, wait briefly
                            # Better: since max_inflight < Queue maxsize, this won't stall unless stopping
                            while not stop_event.is_set():
                                try:
                                    task_queue.put((size_k, batch), timeout=0.1)
                                    pending_batches += 1
                                    break
                                except queue.Full:
                                    continue

                if exhausted_iter and pending_batches == 0:
                    break

                # Collect results
                try:
                    res = result_queue.get(timeout=0.1)
                except queue.Empty:
                    # Guard against crashed workers
                    if not any(p.is_alive() for p in workers):
                        msg_queue.put(("error", "All worker processes terminated unexpectedly."))
                        return
                    continue

                if isinstance(res, tuple) and len(res) == 2 and res[0] == "ERROR":
                    msg_queue.put(("error", "Worker error: {}".format(res[1])))
                    stop_event.set()
                    return

                _, done_count, checked_count, batch_sols = res
                pending_batches -= 1
                subsets_done += done_count
                total_checked += checked_count

                if batch_sols:
                    for sol in batch_sols:
                        solutions.append(sol)
                        msg_queue.put(("solution", sol))
                        if len(solutions) >= config.max_solutions:
                            stop_event.set()
                            break

                now = time.monotonic()
                if (now - last_progress_emit) >= 0.12 or subsets_done >= subsets_total or stop_event.is_set():
                    elapsed = now - start_time
                    size_elapsed = now - size_start_time
                    eta = None  # type: Optional[float]
                    if subsets_done > 0 and subsets_done < subsets_total:
                        rate = subsets_done / max(size_elapsed, 1e-6)
                        eta = (subsets_total - subsets_done) / max(rate, 1e-9)
                    elif subsets_done >= subsets_total:
                        eta = 0.0

                    msg_queue.put((
                        "progress",
                        SearchProgress(
                            current_size=size_k,
                            subsets_done=subsets_done,
                            subsets_total=subsets_total,
                            perms_checked=total_checked,
                            solutions_found=len(solutions),
                            elapsed_sec=elapsed,
                            eta_sec=eta,
                        ),
                    ))
                    last_progress_emit = now

            if len(solutions) >= config.max_solutions:
                break

        cancelled = stop_event.is_set() and len(solutions) < config.max_solutions
        final_result = SearchResult(
            solutions=tuple(solutions[: config.max_solutions]),
            cancelled=cancelled,
            elapsed_sec=time.monotonic() - start_time,
            perms_checked=total_checked,
        )
        msg_queue.put(("done", final_result))
    except Exception as exc:
        msg_queue.put(("error", str(exc)))
    finally:
        stop_event.set()
        for _ in workers:
            try:
                task_queue.put_nowait(None)
            except Exception:
                pass
        for proc in workers:
            proc.join(timeout=0.5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=0.5)
        try:
            task_queue.cancel_join_thread()
            result_queue.cancel_join_thread()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Settings Persistence & Export Utilities
# ---------------------------------------------------------------------------

def get_settings_path():
    # type: () -> str
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, SETTINGS_FILENAME)


def load_settings():
    # type: () -> Dict[str, Any]
    path = get_settings_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def save_settings(data):
    # type: (Dict[str, Any]) -> None
    path = get_settings_path()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def export_solutions_json(filepath, config, result):
    # type: (str, SearchConfig, SearchResult) -> None
    payload = {
        "search_config": {
            "num_cables": config.num_cables,
            "start_size": config.start_size,
            "max_size": config.max_size,
            "max_solutions": config.max_solutions,
            "workers": config.workers,
            "prune_noops": config.prune_noops,
        },
        "summary": {
            "solutions_found": len(result.solutions),
            "cancelled": result.cancelled,
            "elapsed_sec": round(result.elapsed_sec, 4),
            "evaluations_checked": result.perms_checked,
        },
        "solutions": [
            [Connection.from_tuple(step).to_dict() for step in sol]
            for sol in result.solutions
        ],
    }
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def export_solutions_csv(filepath, solutions):
    # type: (str, Sequence[Sequence[Tuple[int, int, str, str]]]) -> None
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["solution_index", "step_index", "cable1", "cable2", "upper_logic", "lower_logic"])
        for sol_idx, sol in enumerate(solutions, start=1):
            for step_idx, step in enumerate(sol, start=1):
                writer.writerow([sol_idx, step_idx, step[0], step[1], step[2], step[3]])


def export_truth_table_csv(filepath, summary):
    # type: (str, VerificationSummary) -> None
    n = summary.num_cables
    failing_set = set(summary.failing_indices)
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        header = (
            ["row"]
            + ["in_c{}".format(i) for i in range(n)]
            + ["out_c{}".format(i) for i in range(n)]
            + ["reliable"]
        )
        writer.writerow(header)
        for idx, (inp, out) in enumerate(summary.truth_table):
            writer.writerow([idx] + list(inp) + list(out) + ["FAIL" if idx in failing_set else "PASS"])


def export_wiring_json(filepath, num_cables, connections):
    # type: (str, int, Sequence[Connection]) -> None
    payload = {
        "num_cables": num_cables,
        "connections": [c.to_dict() for c in connections],
    }
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def format_duration(seconds):
    # type: (Optional[float]) -> str
    if seconds is None or math.isnan(seconds) or math.isinf(seconds):
        return "--:--"
    secs = max(0.0, float(seconds))
    if secs < 60:
        return "{:.1f}s".format(secs)
    mins, rem = divmod(int(secs), 60)
    if mins < 60:
        return "{:02d}m {:02d}s".format(mins, rem)
    hrs, mins = divmod(mins, 60)
    return "{:d}h {:02d}m {:02d}s".format(hrs, mins, rem)


# ---------------------------------------------------------------------------
# Visual Wiring Diagram Canvas (Interactive Signal Flow)
# ---------------------------------------------------------------------------

class WiringCanvas(ttk.Frame):
    """
    Visual diagram of a boolean cable network with clickable 0/1 input toggles
    and live step-by-step signal propagation.
    """

    def __init__(self, parent, interactive=True, height=230):
        super(WiringCanvas, self).__init__(parent)
        self.interactive = interactive
        self.num_cables = 4
        self.connections = []  # type: List[Connection]
        self.input_bits = [0, 1, 0, 1]

        self.canvas = tk.Canvas(
            self,
            height=height,
            bg="#0F172A",
            highlightthickness=1,
            highlightbackground="#334155",
        )
        self.h_scroll = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.v_scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=self.h_scroll.set, yscrollcommand=self.v_scroll.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas.bind("<Configure>", lambda _e: self.redraw())

    def set_network(self, num_cables, connections, input_bits=None):
        # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]], Optional[Sequence[int]]) -> None
        self.num_cables = max(2, int(num_cables))
        try:
            self.connections = validate_connections(self.num_cables, connections)
        except WiringValidationError:
            self.connections = []

        if input_bits is not None and len(input_bits) == self.num_cables:
            self.input_bits = [1 if b else 0 for b in input_bits]
        elif len(self.input_bits) != self.num_cables:
            self.input_bits = [i % 2 for i in range(self.num_cables)]
        self.redraw()

    def set_input_bits(self, bits):
        # type: (Sequence[int]) -> None
        if len(bits) == self.num_cables:
            self.input_bits = [1 if b else 0 for b in bits]
            self.redraw()

    def _toggle_bit(self, cable_idx):
        if not self.interactive:
            return
        if 0 <= cable_idx < len(self.input_bits):
            self.input_bits[cable_idx] = 0 if self.input_bits[cable_idx] else 1
            self.redraw()

    def redraw(self):
        self.canvas.delete("all")
        n = self.num_cables
        steps = len(self.connections)

        left_pad = 105
        right_pad = 120
        top_pad = 36
        bottom_pad = 26
        row_gap = 36 if n <= 6 else 28
        col_gap = 78

        content_w = max(self.canvas.winfo_width() or 600, left_pad + right_pad + max(steps, 1) * col_gap)
        content_h = max(self.canvas.winfo_height() or 210, top_pad + bottom_pad + max(n - 1, 1) * row_gap)
        self.canvas.configure(scrollregion=(0, 0, content_w, content_h))

        # Compute step-by-step states for current input_bits
        try:
            history = evaluate_step_by_step(n, self.connections, self.input_bits)
        except WiringValidationError:
            history = [list(self.input_bits)]

        cable_ys = [top_pad + i * row_gap for i in range(n)]
        x_start = left_pad
        x_end = left_pad + max(steps, 1) * col_gap + 24

        # Header hint
        hint = "Click 0/1 pills to simulate signal flow" if self.interactive else "Wiring Diagram Preview"
        self.canvas.create_text(
            12, 14, text=hint, anchor="w", fill="#94A3B8", font=("Segoe UI", 8, "italic")
        )

        # Draw column x positions
        step_xs = [x_start + (s + 0.5) * col_gap for s in range(steps)]

        # Draw step labels along top
        for s_idx, sx in enumerate(step_xs, start=1):
            self.canvas.create_text(
                sx, 14, text="S{}".format(s_idx), fill="#CBD5E1", font=("Consolas", 8, "bold")
            )

        # Draw wire segments colored by active bit (1 = emerald, 0 = slate)
        for c_idx in range(n):
            y = cable_ys[c_idx]

            # Cable label
            self.canvas.create_text(
                14, y, text="C{}".format(c_idx), anchor="w", fill="#E2E8F0", font=("Consolas", 9, "bold")
            )

            # Interactive input pill
            bit_val = history[0][c_idx]
            pill_bg = "#10B981" if bit_val else "#334155"
            pill_fg = "#022C22" if bit_val else "#E2E8F0"
            tag = "input_pill_{}".format(c_idx)
            self.canvas.create_rectangle(
                44, y - 11, 86, y + 11, fill=pill_bg, outline="#64748B", width=1, tags=(tag,)
            )
            self.canvas.create_text(
                65, y, text=str(bit_val), fill=pill_fg, font=("Consolas", 9, "bold"), tags=(tag,)
            )
            if self.interactive:
                self.canvas.tag_bind(tag, "<Button-1>", lambda _e, idx=c_idx: self._toggle_bit(idx))

            # Segments between steps
            seg_points = [x_start - 12] + step_xs + [x_end]
            for seg_i in range(len(seg_points) - 1):
                state_idx = min(seg_i, len(history) - 1)
                val = history[state_idx][c_idx]
                color = "#34D399" if val else "#475569"
                width = 3 if val else 2
                self.canvas.create_line(
                    seg_points[seg_i], y, seg_points[seg_i + 1], y, fill=color, width=width
                )

            # Output pill on right
            out_val = history[-1][c_idx]
            out_bg = "#10B981" if out_val else "#1E293B"
            out_fg = "#022C22" if out_val else "#CBD5E1"
            self.canvas.create_rectangle(
                x_end + 8, y - 10, x_end + 42, y + 10, fill=out_bg, outline="#64748B", width=1
            )
            self.canvas.create_text(
                x_end + 25, y, text=str(out_val), fill=out_fg, font=("Consolas", 9, "bold")
            )

        # Draw vertical gates/connections
        for s_idx, conn in enumerate(self.connections):
            sx = step_xs[s_idx]
            y1 = cable_ys[conn.cable1]
            y2 = cable_ys[conn.cable2]

            self.canvas.create_line(sx, y1, sx, y2, fill="#94A3B8", width=2)

            # Upper node (cable1)
            u_color = GATE_COLORS.get(conn.upper_logic, "#2563EB")
            self.canvas.create_rectangle(
                sx - 20, y1 - 10, sx + 20, y1 + 10, fill=u_color, outline="#F8FAFC", width=1
            )
            self.canvas.create_text(
                sx, y1, text=conn.upper_logic, fill="#FFFFFF", font=("Consolas", 7, "bold")
            )

            # Lower node (cable2)
            l_color = GATE_COLORS.get(conn.lower_logic, "#7C3AED")
            self.canvas.create_rectangle(
                sx - 20, y2 - 10, sx + 20, y2 + 10, fill=l_color, outline="#F8FAFC", width=1
            )
            self.canvas.create_text(
                sx, y2, text=conn.lower_logic, fill="#FFFFFF", font=("Consolas", 7, "bold")
            )

        # Overall status badge for the simulated input vector
        final_out = history[-1]
        ok = is_ordered_descending(final_out)
        badge_bg = "#065F46" if ok else "#991B1B"
        badge_txt = "SORTED OK" if ok else "UNSORTED"
        badge_x = x_end + 52
        mid_y = (cable_ys[0] + cable_ys[-1]) / 2.0
        self.canvas.create_rectangle(
            badge_x, mid_y - 14, badge_x + 64, mid_y + 14, fill=badge_bg, outline="#E2E8F0", width=1
        )
        self.canvas.create_text(
            badge_x + 32, mid_y, text=badge_txt, fill="#FFFFFF", font=("Segoe UI", 7, "bold")
        )


# ---------------------------------------------------------------------------
# Main Tkinter Application
# ---------------------------------------------------------------------------

class LogicNetworkApp:
    def __init__(self, root):
        # type: (tk.Tk) -> None
        self.root = root
        self.root.title("Boolean Wiring Explorer — Visual & Parallel Edition")
        self.settings = load_settings()
        self.root.geometry(self.settings.get("geometry", "1040x760"))
        self.root.minsize(880, 620)

        self._apply_style()

        self.ctx = multiprocessing.get_context("spawn")
        self.msg_queue = None         # type: Optional[Any]
        self.stop_event = None        # type: Optional[Any]
        self.coord_proc = None        # type: Optional[Any]
        self.poll_id = None           # type: Optional[str]

        self.last_search_config = None   # type: Optional[SearchConfig]
        self.last_search_result = None   # type: Optional[SearchResult]
        self.found_solutions = []        # type: List[Tuple[Tuple[int, int, str, str], ...]]
        self.last_verify_summary = None  # type: Optional[VerificationSummary]
        self.verify_connections = []     # type: List[Connection]

        self._build_ui()
        self._load_initial_state()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_style(self):
        style = ttk.Style()
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        style.configure("BannerOK.TLabel", background="#DCFCE7", foreground="#166534", font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("BannerFail.TLabel", background="#FEE2E2", foreground="#991B1B", font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("BannerInfo.TLabel", background="#E2E8F0", foreground="#1E293B", font=("Segoe UI", 10, "bold"), padding=6)

    def _build_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._build_search_tab()
        self._build_verify_tab()
        self._build_docs_tab()

    # ---------------------- TAB 1: PARALLEL SEARCH ----------------------

    def _build_search_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="1. Parallel Search (MT34)")

        # Parameters
        params = ttk.LabelFrame(tab, text="Search Configuration", padding=8)
        params.pack(fill=tk.X)

        default_workers = max(1, (cpu_count() or 2) - 1)

        ttk.Label(params, text="Cables:").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
        self.cables_var = tk.StringVar(value=str(self.settings.get("search_cables", 4)))
        tk.Spinbox(params, from_=2, to=MAX_SEARCH_CABLES, textvariable=self.cables_var, width=5).grid(
            row=0, column=1, padx=(0, 12)
        )

        ttk.Label(params, text="Start Size:").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
        self.start_size_var = tk.StringVar(value=str(self.settings.get("start_size", 1)))
        tk.Spinbox(params, from_=1, to=36, textvariable=self.start_size_var, width=5).grid(
            row=0, column=3, padx=(0, 12)
        )

        ttk.Label(params, text="Max Size (0=auto):").grid(row=0, column=4, sticky=tk.W, padx=(0, 4))
        self.max_size_var = tk.StringVar(value=str(self.settings.get("max_size", 0)))
        tk.Spinbox(params, from_=0, to=36, textvariable=self.max_size_var, width=5).grid(
            row=0, column=5, padx=(0, 12)
        )

        ttk.Label(params, text="Max Solutions:").grid(row=0, column=6, sticky=tk.W, padx=(0, 4))
        self.max_sol_var = tk.StringVar(value=str(self.settings.get("max_solutions", 5)))
        tk.Spinbox(params, from_=1, to=MAX_SOLUTIONS_CAP, textvariable=self.max_sol_var, width=6).grid(
            row=0, column=7, padx=(0, 12)
        )

        ttk.Label(params, text="Workers (cores={}):".format(cpu_count() or 1)).grid(
            row=0, column=8, sticky=tk.W, padx=(0, 4)
        )
        self.workers_var = tk.StringVar(value=str(self.settings.get("workers", default_workers)))
        tk.Spinbox(params, from_=1, to=MAX_WORKERS_CAP, textvariable=self.workers_var, width=5).grid(
            row=0, column=9, padx=(0, 12)
        )

        self.prune_var = tk.BooleanVar(value=bool(self.settings.get("prune_noops", True)))
        ttk.Checkbutton(
            params,
            text="Prune no-op gates (faster, skips redundant steps)",
            variable=self.prune_var,
        ).grid(row=1, column=0, columnspan=10, sticky=tk.W, pady=(6, 0))

        # Action bar
        btns = ttk.Frame(tab)
        btns.pack(fill=tk.X, pady=(8, 6))

        self.start_btn = ttk.Button(btns, text="Start Search", command=self.start_search)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_search, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(btns, text="Copy Plain Text", command=self.copy_search_log).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Export JSON", command=self.export_search_json_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Export CSV", command=self.export_search_csv_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Clear", command=self.clear_search_results).pack(side=tk.RIGHT)

        # Progress Dashboard
        prog_frame = ttk.LabelFrame(tab, text="Live Progress", padding=8)
        prog_frame.pack(fill=tk.X, pady=(0, 8))

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))

        self.progress_label_var = tk.StringVar(
            value="Idle — Ready. Tip: 7 workers recommended on an 8-core CPU."
        )
        ttk.Label(prog_frame, textvariable=self.progress_label_var, font=("Consolas", 9)).pack(anchor=tk.W)

        # Split pane: Visual Solutions Preview + Plain Text Log
        paned = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Top pane: Found Solutions list + Visual Wiring Canvas
        visual_frame = ttk.Frame(paned)
        paned.add(visual_frame, weight=3)

        sol_left = ttk.LabelFrame(visual_frame, text="Found Solutions (Click to Preview)", padding=6)
        sol_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(0, 6))

        self.sol_listbox = tk.Listbox(sol_left, width=28, font=("Consolas", 9))
        self.sol_listbox.pack(fill=tk.BOTH, expand=True)
        self.sol_listbox.bind("<<ListboxSelect>>", self._on_select_solution)

        ttk.Button(
            sol_left, text="Open in Verifier & Simulator", command=self.send_selected_to_verifier
        ).pack(fill=tk.X, pady=(6, 0))

        sol_right = ttk.LabelFrame(visual_frame, text="Visual Diagram & Interactive Simulator", padding=6)
        sol_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.search_canvas = WiringCanvas(sol_right, interactive=True, height=200)
        self.search_canvas.pack(fill=tk.BOTH, expand=True)

        # Bottom pane: Plain-Text Output
        plain_frame = ttk.LabelFrame(paned, text="Plain-Text Log & Solutions", padding=6)
        paned.add(plain_frame, weight=2)

        self.search_log = scrolledtext.ScrolledText(plain_frame, wrap=tk.WORD, font=("Consolas", 9), height=10)
        self.search_log.pack(fill=tk.BOTH, expand=True)

    # ---------------------- TAB 2: VERIFY & VISUALIZE ----------------------

    def _build_verify_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="2. Verify & Visual Simulator (T3)")

        # Top toolbar
        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(top, text="Cables:").pack(side=tk.LEFT, padx=(0, 4))
        self.verify_cables_var = tk.StringVar(value=str(self.settings.get("verify_cables", 5)))
        v_spin = tk.Spinbox(
            top,
            from_=2,
            to=MAX_VERIFY_CABLES,
            textvariable=self.verify_cables_var,
            width=5,
            command=self._refresh_verify_canvas_only,
        )
        v_spin.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(top, text="Run Verification", command=self.run_verify).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Example: 4-Cable Valid", command=self.load_example_4_valid).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Example: 5-Cable Invalid", command=self.load_example_5_invalid).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(top, text="Import JSON", command=self.import_wiring_json_ui).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Export JSON", command=self.export_wiring_json_ui).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Export CSV", command=self.export_tt_csv_ui).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Copy Plain Text", command=self.copy_verify_plain_text).pack(side=tk.RIGHT)

        # Visual Wiring Canvas + Quick Input Presets
        vis_box = ttk.LabelFrame(tab, text="Visual Wiring Diagram (Click 0/1 Inputs to Trace Live)", padding=6)
        vis_box.pack(fill=tk.X, pady=(0, 6))

        preset_bar = ttk.Frame(vis_box)
        preset_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(preset_bar, text="Quick Inputs:").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(preset_bar, text="All 0", command=lambda: self._apply_sim_preset("zeros")).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_bar, text="All 1", command=lambda: self._apply_sim_preset("ones")).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_bar, text="Alternating", command=lambda: self._apply_sim_preset("alt")).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_bar, text="Random", command=lambda: self._apply_sim_preset("rand")).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            preset_bar, text="Load First Failing Input", command=self._load_first_failing_input
        ).pack(side=tk.LEFT, padx=(10, 2))

        self.verify_canvas = WiringCanvas(vis_box, interactive=True, height=210)
        self.verify_canvas.pack(fill=tk.BOTH, expand=True)

        # Middle + Bottom PanedWindow
        v_paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        v_paned.pack(fill=tk.BOTH, expand=True)

        # LEFT: Wiring Editor (Interactive Table + Plain Text)
        editor_box = ttk.LabelFrame(v_paned, text="Wiring Steps Editor", padding=6)
        v_paned.add(editor_box, weight=2)

        self.editor_nb = ttk.Notebook(editor_box)
        self.editor_nb.pack(fill=tk.BOTH, expand=True)

        # Sub-tab A: Interactive Table
        tbl_tab = ttk.Frame(self.editor_nb, padding=4)
        self.editor_nb.add(tbl_tab, text="Interactive Table")

        cols = ("step", "c1", "c2", "upper", "lower")
        self.steps_tree = ttk.Treeview(tbl_tab, columns=cols, show="headings", height=8)
        self.steps_tree.heading("step", text="#")
        self.steps_tree.heading("c1", text="Cable 1")
        self.steps_tree.heading("c2", text="Cable 2")
        self.steps_tree.heading("upper", text="Upper Gate")
        self.steps_tree.heading("lower", text="Lower Gate")
        self.steps_tree.column("step", width=36, anchor="center")
        self.steps_tree.column("c1", width=64, anchor="center")
        self.steps_tree.column("c2", width=64, anchor="center")
        self.steps_tree.column("upper", width=80, anchor="center")
        self.steps_tree.column("lower", width=80, anchor="center")
        self.steps_tree.pack(fill=tk.BOTH, expand=True)
        self.steps_tree.bind("<<TreeviewSelect>>", self._on_select_step_row)

        add_bar = ttk.Frame(tbl_tab)
        add_bar.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(add_bar, text="C1:").pack(side=tk.LEFT)
        self.step_c1_var = tk.StringVar(value="0")
        tk.Spinbox(add_bar, from_=0, to=MAX_VERIFY_CABLES - 1, textvariable=self.step_c1_var, width=4).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(add_bar, text="C2:").pack(side=tk.LEFT)
        self.step_c2_var = tk.StringVar(value="1")
        tk.Spinbox(add_bar, from_=0, to=MAX_VERIFY_CABLES - 1, textvariable=self.step_c2_var, width=4).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(add_bar, text="Up:").pack(side=tk.LEFT)
        self.step_up_var = tk.StringVar(value="OR")
        ttk.Combobox(add_bar, textvariable=self.step_up_var, values=VALID_LOGIC_FUNCS, width=6, state="readonly").pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(add_bar, text="Low:").pack(side=tk.LEFT)
        self.step_low_var = tk.StringVar(value="AND")
        ttk.Combobox(add_bar, textvariable=self.step_low_var, values=VALID_LOGIC_FUNCS, width=6, state="readonly").pack(
            side=tk.LEFT, padx=(2, 0)
        )

        edit_btns = ttk.Frame(tbl_tab)
        edit_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(edit_btns, text="Add Step", command=self.add_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Update", command=self.update_selected_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Delete", command=self.delete_selected_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Up", command=lambda: self.move_selected_step(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Down", command=lambda: self.move_selected_step(1)).pack(side=tk.LEFT, padx=2)

        # Sub-tab B: Plain-Text Tuples Editor (Safe ast.literal_eval)
        txt_tab = ttk.Frame(self.editor_nb, padding=4)
        self.editor_nb.add(txt_tab, text="Plain Text (Tuples / JSON)")

        ttk.Label(
            txt_tab,
            text="One tuple per line e.g. (0, 2, 'OR', 'AND') — parsed safely with ast.literal_eval:",
        ).pack(anchor=tk.W, pady=(0, 4))

        self.conn_input = scrolledtext.ScrolledText(txt_tab, height=8, font=("Consolas", 9))
        self.conn_input.pack(fill=tk.BOTH, expand=True)

        ttk.Button(
            txt_tab, text="Apply Plain Text -> Table & Diagram", command=self.apply_plain_text_to_table
        ).pack(anchor=tk.E, pady=(4, 0))

        # RIGHT: Truth Table Results (Visual Color Table + Plain Text)
        res_box = ttk.LabelFrame(v_paned, text="Verification Results", padding=6)
        v_paned.add(res_box, weight=3)

        self.verify_banner_var = tk.StringVar(value="Click 'Run Verification' to evaluate all input rows.")
        self.verify_banner = ttk.Label(res_box, textvariable=self.verify_banner_var, style="BannerInfo.TLabel")
        self.verify_banner.pack(fill=tk.X, pady=(0, 6))

        res_nb = ttk.Notebook(res_box)
        res_nb.pack(fill=tk.BOTH, expand=True)

        # Sub-tab A: Visual Truth Table
        vtt_tab = ttk.Frame(res_nb, padding=4)
        res_nb.add(vtt_tab, text="Visual Truth Table")

        vtt_ctrl = ttk.Frame(vtt_tab)
        vtt_ctrl.pack(fill=tk.X, pady=(0, 4))
        self.only_fail_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            vtt_ctrl, text="Show failing rows only", variable=self.only_fail_var, command=self._populate_tt_tree
        ).pack(side=tk.LEFT)
        ttk.Label(vtt_ctrl, text="(Double-click any row to simulate it above)", foreground="#475569").pack(
            side=tk.RIGHT
        )

        tt_cols = ("row", "inputs", "outputs", "status")
        self.tt_tree = ttk.Treeview(vtt_tab, columns=tt_cols, show="headings", height=10)
        self.tt_tree.heading("row", text="#")
        self.tt_tree.heading("inputs", text="Inputs (C0..Cn)")
        self.tt_tree.heading("outputs", text="Outputs (C0..Cn)")
        self.tt_tree.heading("status", text="Status")
        self.tt_tree.column("row", width=44, anchor="center")
        self.tt_tree.column("inputs", width=155, anchor="center")
        self.tt_tree.column("outputs", width=155, anchor="center")
        self.tt_tree.column("status", width=70, anchor="center")
        self.tt_tree.tag_configure("pass_row", background="#ECFDF5", foreground="#065F46")
        self.tt_tree.tag_configure("fail_row", background="#FEF2F2", foreground="#991B1B")

        tt_scroll = ttk.Scrollbar(vtt_tab, orient=tk.VERTICAL, command=self.tt_tree.yview)
        self.tt_tree.configure(yscrollcommand=tt_scroll.set)
        self.tt_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tt_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tt_tree.bind("<Double-1>", self._on_double_click_tt_row)

        # Sub-tab B: Plain-Text Truth Table
        ptt_tab = ttk.Frame(res_nb, padding=4)
        res_nb.add(ptt_tab, text="Plain-Text Truth Table")

        self.verify_log = scrolledtext.ScrolledText(ptt_tab, wrap=tk.WORD, font=("Consolas", 9))
        self.verify_log.pack(fill=tk.BOTH, expand=True)

    # ---------------------- TAB 3: GUIDE & DOCS ----------------------

    def _build_docs_tab(self):
        tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(tab, text="3. Guide & Reliability Spec")

        doc_text = scrolledtext.ScrolledText(tab, wrap=tk.WORD, font=("Segoe UI", 10), padx=10, pady=10)
        doc_text.pack(fill=tk.BOTH, expand=True)

        guide = (
            "BOOLEAN WIRING EXPLORER — SPECIFICATION & GUIDE\n"
            "===============================================\n\n"
            "1. What does 'Reliable' mean?\n"
            "   A wiring network with N cables (indexed 0 at the top down to N-1 at the bottom) is\n"
            "   considered RELIABLE if and only if for EVERY possible binary input combination\n"
            "   (all 2^N rows in the truth table), the final cable outputs are ordered in descending\n"
            "   order from Cable 0 to Cable N-1:\n\n"
            "       Cable[0] >= Cable[1] >= ... >= Cable[N-1]\n\n"
            "   In other words, all 1s are pushed to the top cables and all 0s to the bottom cables\n"
            "   (for example: [1, 1, 0, 0] is PASS; [1, 0, 1, 0] is FAIL). With (OR, AND) gates,\n"
            "   a reliable network is a classic binary sorting network.\n\n"
            "2. Supported Gate Operations\n"
            "   Each connection tuple has the format: (cable1, cable2, upper_logic, lower_logic)\n"
            "   - OR   : 1 if either input is 1\n"
            "   - AND  : 1 only if both inputs are 1\n"
            "   - XOR  : 1 if inputs differ\n"
            "   - NAND : opposite of AND\n\n"
            "3. Search Pruning vs. Exhaustive Mode\n"
            "   - Prune no-op gates (ON by default): Skips any branch where a gate leaves all 2^N\n"
            "     input states unchanged. Because a no-op step can always be removed to yield a\n"
            "     strictly shorter equivalent network, pruning dramatically accelerates search\n"
            "     without missing any minimal or irredundant reliable network.\n"
            "   - Exhaustive (OFF): Evaluates every single permutation of every subset of size k.\n\n"
            "4. Tips for Best Performance\n"
            "   - Set Workers to (logical cores - 1) so Windows and the GUI stay responsive.\n"
            "   - For 4 cables, minimal reliable networks require 5 connections.\n"
            "   - For 5 cables, minimal reliable networks require 9 connections (note: using only\n"
            "     unique pairs limits each cable pair to appear at most once per subset).\n"
            "   - Double-click any row in the Visual Truth Table to inspect its live signal path\n"
            "     in the wiring diagram above.\n"
        )
        doc_text.insert("1.0", guide)
        doc_text.config(state=tk.DISABLED)

    # ---------------------- STATE INIT & PERSISTENCE ----------------------

    def _load_initial_state(self):
        saved_conns = self.settings.get("verify_connections")
        if saved_conns:
            try:
                num_c = int(self.verify_cables_var.get())
                conns = [Connection.from_dict(c) for c in saved_conns]
                self.verify_connections = validate_connections(num_c, conns)
                self._sync_verify_views()
                self.run_verify()
                return
            except Exception:
                pass
        self.load_example_4_valid()

    def _save_current_settings(self):
        try:
            data = {
                "geometry": self.root.geometry(),
                "search_cables": int(self.cables_var.get()),
                "start_size": int(self.start_size_var.get()),
                "max_size": int(self.max_size_var.get()),
                "max_solutions": int(self.max_sol_var.get()),
                "workers": int(self.workers_var.get()),
                "prune_noops": bool(self.prune_var.get()),
                "verify_cables": int(self.verify_cables_var.get()),
                "verify_connections": [c.to_dict() for c in self.verify_connections],
            }
            save_settings(data)
        except Exception:
            pass

    # ---------------------- SEARCH TAB ACTIONS ----------------------

    def _append_search_log(self, text):
        self.search_log.insert(tk.END, text + "\n")
        self.search_log.see(tk.END)

    def clear_search_results(self):
        self.search_log.delete("1.0", tk.END)
        self.sol_listbox.delete(0, tk.END)
        self.found_solutions = []
        self.last_search_result = None
        self.progress_bar["value"] = 0
        self.progress_label_var.set("Cleared — Ready.")

    def start_search(self):
        try:
            num_cables = int(self.cables_var.get())
            start_size = int(self.start_size_var.get())
            max_size_raw = int(self.max_size_var.get())
            max_solutions = int(self.max_sol_var.get())
            workers = int(self.workers_var.get())
            config = validate_search_config(
                num_cables=num_cables,
                start_size=start_size,
                max_size=(None if max_size_raw == 0 else max_size_raw),
                max_solutions=max_solutions,
                workers=workers,
                prune_noops=self.prune_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror("Invalid Search Configuration", str(exc))
            return

        self.clear_search_results()
        self.last_search_config = config
        self._save_current_settings()

        self.msg_queue = queue.Queue()
        self.stop_event = self.ctx.Event()
        self.coord_proc = threading.Thread(
            target=_search_coordinator_entry,
            args=(config, self.msg_queue, self.stop_event),
            daemon=True,
        )
        self.coord_proc.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress_label_var.set("Starting worker processes...")
        self.poll_id = self.root.after(100, self._poll_search_queue)

    def stop_search(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.stop_btn.config(state=tk.DISABLED)
        self.progress_label_var.set("Stopping search gracefully...")

    def _cleanup_search_process(self, force=False):
        if self.poll_id is not None:
            try:
                self.root.after_cancel(self.poll_id)
            except Exception:
                pass
            self.poll_id = None

        if self.stop_event is not None:
            self.stop_event.set()

        if self.coord_proc is not None:
            self.coord_proc.join(timeout=(0.5 if force else 2.0))
            self.coord_proc = None

        self.msg_queue = None

        self.stop_event = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def _poll_search_queue(self):
        if self.msg_queue is None:
            return

        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_search_log(payload)
                elif kind == "progress":
                    self._update_progress_ui(payload)
                elif kind == "solution":
                    self.found_solutions.append(payload)
                    idx = len(self.found_solutions)
                    self.sol_listbox.insert(tk.END, "Solution #{} ({} steps)".format(idx, len(payload)))
                    self._append_search_log("Solution #{} ({} steps):".format(idx, len(payload)))
                    for step in payload:
                        self._append_search_log("  {}".format(step))
                    if idx == 1:
                        self.sol_listbox.selection_set(0)
                        self._preview_solution(0)
                elif kind == "done":
                    self.last_search_result = payload
                    status_tag = "Cancelled" if payload.cancelled else "Completed"
                    self._append_search_log(
                        "[  End!  ] {} in {} | Solutions: {} | Evaluated: {:,}".format(
                            status_tag,
                            format_duration(payload.elapsed_sec),
                            len(payload.solutions),
                            payload.perms_checked,
                        )
                    )
                    self.progress_bar["value"] = 100 if not payload.cancelled else self.progress_bar["value"]
                    self.progress_label_var.set(
                        "{} — {} solution(s) in {} ({:,} checks)".format(
                            status_tag,
                            len(payload.solutions),
                            format_duration(payload.elapsed_sec),
                            payload.perms_checked,
                        )
                    )
                    self._cleanup_search_process()
                    return
                elif kind == "error":
                    self._append_search_log("[ Error ] {}".format(payload))
                    self.progress_label_var.set("Error: {}".format(payload))
                    self._cleanup_search_process()
                    return
        except queue.Empty:
            pass

        # Guard against unexpected coordinator crash
        if self.coord_proc is not None and not self.coord_proc.is_alive():
            self._append_search_log("[ Error ] Search coordinator exited unexpectedly.")
            self.progress_label_var.set("Search stopped unexpectedly.")
            self._cleanup_search_process()
            return

        self.poll_id = self.root.after(100, self._poll_search_queue)

    def _update_progress_ui(self, prog):
        # type: (SearchProgress) -> None
        pct = (100.0 * prog.subsets_done / max(prog.subsets_total, 1))
        self.progress_bar["value"] = min(100.0, pct)
        speed = prog.perms_checked / max(prog.elapsed_sec, 1e-6)
        self.progress_label_var.set(
            "Size {} | Subsets {:,}/{:,} ({:.1f}%) | Sols: {} | Checks: {:,} ({:,.0f}/s) | Elapsed: {} | ETA: {}".format(
                prog.current_size,
                prog.subsets_done,
                prog.subsets_total,
                pct,
                prog.solutions_found,
                prog.perms_checked,
                speed,
                format_duration(prog.elapsed_sec),
                format_duration(prog.eta_sec),
            )
        )

    def _on_select_solution(self, _event=None):
        sel = self.sol_listbox.curselection()
        if sel:
            self._preview_solution(sel[0])

    def _preview_solution(self, index):
        if 0 <= index < len(self.found_solutions) and self.last_search_config is not None:
            sol = self.found_solutions[index]
            self.search_canvas.set_network(self.last_search_config.num_cables, sol)

    def send_selected_to_verifier(self):
        sel = self.sol_listbox.curselection()
        if not sel or self.last_search_config is None:
            messagebox.showinfo("No Selection", "Select a found solution from the list first.")
            return
        sol = self.found_solutions[sel[0]]
        self.verify_cables_var.set(str(self.last_search_config.num_cables))
        self.verify_connections = normalize_connections(sol)
        self._sync_verify_views()
        self.run_verify()
        self.notebook.select(1)

    def copy_search_log(self):
        content = self.search_log.get("1.0", tk.END).strip()
        if not content:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.progress_label_var.set("Copied plain-text search log to clipboard.")

    def export_search_json_ui(self):
        if not self.found_solutions or self.last_search_config is None:
            messagebox.showinfo("Nothing to Export", "Run a search with at least one solution first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Export Search Results as JSON",
        )
        if not path:
            return
        res = self.last_search_result or SearchResult(
            solutions=tuple(self.found_solutions), cancelled=False, elapsed_sec=0.0, perms_checked=0
        )
        export_solutions_json(path, self.last_search_config, res)
        messagebox.showinfo("Export Complete", "Saved JSON to:\n{}".format(path))

    def export_search_csv_ui(self):
        if not self.found_solutions:
            messagebox.showinfo("Nothing to Export", "Run a search with at least one solution first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Export Solutions as CSV",
        )
        if not path:
            return
        export_solutions_csv(path, self.found_solutions)
        messagebox.showinfo("Export Complete", "Saved CSV to:\n{}".format(path))

    # ---------------------- VERIFY TAB ACTIONS ----------------------

    def load_example_4_valid(self):
        self.verify_cables_var.set("4")
        self.verify_connections = normalize_connections([
            (0, 2, "OR", "AND"),
            (1, 3, "OR", "AND"),
            (0, 1, "OR", "AND"),
            (2, 3, "OR", "AND"),
            (1, 2, "OR", "AND"),
        ])
        self._sync_verify_views()
        self.run_verify()

    def load_example_5_invalid(self):
        self.verify_cables_var.set("5")
        self.verify_connections = normalize_connections([
            (0, 2, "OR", "AND"),
            (1, 3, "OR", "AND"),
            (0, 1, "OR", "AND"),
            (2, 3, "OR", "AND"),
            (1, 2, "OR", "AND"),
        ])
        self._sync_verify_views()
        self.run_verify()

    def _sync_verify_views(self):
        # Populate interactive table
        for item in self.steps_tree.get_children():
            self.steps_tree.delete(item)
        for idx, conn in enumerate(self.verify_connections, start=1):
            self.steps_tree.insert(
                "", tk.END, values=(idx, conn.cable1, conn.cable2, conn.upper_logic, conn.lower_logic)
            )
        # Populate plain-text editor
        self.conn_input.delete("1.0", tk.END)
        self.conn_input.insert("1.0", format_connections_plain_text(self.verify_connections))
        # Redraw visual diagram
        self._refresh_verify_canvas_only()

    def _refresh_verify_canvas_only(self):
        try:
            num_c = int(self.verify_cables_var.get())
        except ValueError:
            return
        self.verify_canvas.set_network(num_c, self.verify_connections)

    def _on_select_step_row(self, _event=None):
        sel = self.steps_tree.selection()
        if not sel:
            return
        idx = self.steps_tree.index(sel[0])
        if 0 <= idx < len(self.verify_connections):
            conn = self.verify_connections[idx]
            self.step_c1_var.set(str(conn.cable1))
            self.step_c2_var.set(str(conn.cable2))
            self.step_up_var.set(conn.upper_logic)
            self.step_low_var.set(conn.lower_logic)

    def _read_step_inputs(self):
        # type: () -> Connection
        num_c = int(self.verify_cables_var.get())
        conn = Connection(
            cable1=int(self.step_c1_var.get()),
            cable2=int(self.step_c2_var.get()),
            upper_logic=self.step_up_var.get().strip().upper(),
            lower_logic=self.step_low_var.get().strip().upper(),
        )
        validate_connections(num_c, [conn])
        return conn

    def add_step(self):
        try:
            conn = self._read_step_inputs()
        except ValueError as exc:
            messagebox.showerror("Invalid Step", str(exc))
            return
        self.verify_connections.append(conn)
        self._sync_verify_views()

    def update_selected_step(self):
        sel = self.steps_tree.selection()
        if not sel:
            messagebox.showinfo("Select a Step", "Select a row in the table to update.")
            return
        idx = self.steps_tree.index(sel[0])
        try:
            conn = self._read_step_inputs()
        except ValueError as exc:
            messagebox.showerror("Invalid Step", str(exc))
            return
        self.verify_connections[idx] = conn
        self._sync_verify_views()

    def delete_selected_step(self):
        sel = self.steps_tree.selection()
        if not sel:
            return
        idx = self.steps_tree.index(sel[0])
        if 0 <= idx < len(self.verify_connections):
            del self.verify_connections[idx]
            self._sync_verify_views()

    def move_selected_step(self, delta):
        sel = self.steps_tree.selection()
        if not sel:
            return
        idx = self.steps_tree.index(sel[0])
        new_idx = idx + delta
        if 0 <= new_idx < len(self.verify_connections):
            self.verify_connections[idx], self.verify_connections[new_idx] = (
                self.verify_connections[new_idx],
                self.verify_connections[idx],
            )
            self._sync_verify_views()
            children = self.steps_tree.get_children()
            if 0 <= new_idx < len(children):
                self.steps_tree.selection_set(children[new_idx])

    def apply_plain_text_to_table(self):
        try:
            num_c = int(self.verify_cables_var.get())
            raw_text = self.conn_input.get("1.0", tk.END)
            parsed = parse_connections_text(raw_text)
            self.verify_connections = validate_connections(num_c, parsed)
            self._sync_verify_views()
            self.run_verify()
        except ValueError as exc:
            messagebox.showerror("Wiring Parse / Validation Error", str(exc))

    def run_verify(self):
        try:
            num_c = int(self.verify_cables_var.get())
            # If user is currently editing plain text tab, parse it cleanly first
            if self.editor_nb.index(self.editor_nb.select()) == 1:
                parsed = parse_connections_text(self.conn_input.get("1.0", tk.END))
                self.verify_connections = validate_connections(num_c, parsed)
                self._sync_verify_views()
            else:
                self.verify_connections = validate_connections(num_c, self.verify_connections)
                self._refresh_verify_canvas_only()

            summary = run_full_verification(num_c, self.verify_connections)
            self.last_verify_summary = summary
            self._save_current_settings()
        except ValueError as exc:
            messagebox.showerror("Verification Error", str(exc))
            return

        total = len(summary.truth_table)
        fail_count = len(summary.failing_indices)
        if summary.is_reliable:
            self.verify_banner.configure(style="BannerOK.TLabel")
            self.verify_banner_var.set(
                "RELIABLE (cablage fiable) — All {:,}/{:,} input rows sorted descending.".format(total, total)
            )
        else:
            self.verify_banner.configure(style="BannerFail.TLabel")
            self.verify_banner_var.set(
                "UNRELIABLE (cablage pas fiable) — {:,}/{:,} rows failed descending order.".format(
                    fail_count, total
                )
            )

        self._populate_tt_tree()
        self._populate_tt_plain_text()

    def _populate_tt_tree(self):
        for item in self.tt_tree.get_children():
            self.tt_tree.delete(item)
        if self.last_verify_summary is None:
            return
        failing_set = set(self.last_verify_summary.failing_indices)
        only_fail = self.only_fail_var.get()

        # Cap displayed Treeview rows for huge tables (e.g. 2^14) so Tk stays snappy
        max_rows = 2048
        shown = 0
        for idx, (inp, out) in enumerate(self.last_verify_summary.truth_table):
            is_fail = idx in failing_set
            if only_fail and not is_fail:
                continue
            tag = "fail_row" if is_fail else "pass_row"
            status = "FAIL" if is_fail else "PASS"
            self.tt_tree.insert("", tk.END, iid=str(idx), values=(idx, str(inp), str(list(out)), status), tags=(tag,))
            shown += 1
            if shown >= max_rows:
                break

    def _populate_tt_plain_text(self):
        self.verify_log.delete("1.0", tk.END)
        if self.last_verify_summary is None:
            return
        s = self.last_verify_summary
        lines = ["Truth Table (Inputs -> Outputs) [{} cables, {} steps]:".format(s.num_cables, len(s.connections))]
        failing_set = set(s.failing_indices)
        for idx, (inp, out) in enumerate(s.truth_table):
            marker = "  [FAIL]" if idx in failing_set else ""
            lines.append("Inputs: {} -> Outputs: {}{}".format(inp, list(out), marker))
        lines.append("")
        lines.append("cablage fiable." if s.is_reliable else "cablage pas fiable.")
        self.verify_log.insert("1.0", "\n".join(lines))

    def _on_double_click_tt_row(self, _event=None):
        sel = self.tt_tree.selection()
        if not sel or self.last_verify_summary is None:
            return
        row_idx = int(sel[0])
        inp, _ = self.last_verify_summary.truth_table[row_idx]
        self.verify_canvas.set_input_bits(inp)

    def _apply_sim_preset(self, kind):
        try:
            n = int(self.verify_cables_var.get())
        except ValueError:
            return
        if kind == "zeros":
            bits = [0] * n
        elif kind == "ones":
            bits = [1] * n
        elif kind == "alt":
            bits = [i % 2 for i in range(n)]
        else:
            bits = [random.randint(0, 1) for _ in range(n)]
        self.verify_canvas.set_input_bits(bits)

    def _load_first_failing_input(self):
        if self.last_verify_summary is None or not self.last_verify_summary.failing_indices:
            messagebox.showinfo("No Failing Rows", "Current wiring has no failing rows (or has not been run yet).")
            return
        first_idx = self.last_verify_summary.failing_indices[0]
        inp, _ = self.last_verify_summary.truth_table[first_idx]
        self.verify_canvas.set_input_bits(inp)

    def copy_verify_plain_text(self):
        content = self.verify_log.get("1.0", tk.END).strip()
        if not content:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("Copied", "Plain-text truth table copied to clipboard.")

    def export_tt_csv_ui(self):
        if self.last_verify_summary is None:
            messagebox.showinfo("Nothing to Export", "Run verification first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Export Truth Table as CSV",
        )
        if not path:
            return
        export_truth_table_csv(path, self.last_verify_summary)
        messagebox.showinfo("Export Complete", "Saved truth table CSV to:\n{}".format(path))

    def export_wiring_json_ui(self):
        try:
            num_c = int(self.verify_cables_var.get())
            validated = validate_connections(num_c, self.verify_connections)
        except ValueError as exc:
            messagebox.showerror("Cannot Export", str(exc))
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Export Wiring Configuration as JSON",
        )
        if not path:
            return
        export_wiring_json(path, num_c, validated)
        messagebox.showinfo("Export Complete", "Saved wiring config JSON to:\n{}".format(path))

    def import_wiring_json_ui(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Import Wiring Configuration JSON",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            num_c = int(data["num_cables"])
            conns = [Connection.from_dict(item) for item in data["connections"]]
            self.verify_connections = validate_connections(num_c, conns)
            self.verify_cables_var.set(str(num_c))
            self._sync_verify_views()
            self.run_verify()
        except Exception as exc:
            messagebox.showerror("Import Error", str(exc))

    def on_close(self):
        self._save_current_settings()
        self._cleanup_search_process(force=True)
        self.root.destroy()


def main():
    multiprocessing.freeze_support()
    root = tk.Tk()
    LogicNetworkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
