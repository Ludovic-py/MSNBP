# -*- coding: utf-8 -*-
"""
Explorateur de Câblages Booléens (Recherche, Vérification & Simulateur Visuel)
Compatible Python 3.7+, bibliothèque standard uniquement (Tkinter + multiprocessing).
Portable : stocke les réglages et le cache de calculs dans un dossier local
`logic_network_data/` à côté du script ou de l'exécutable, tout en pouvant
tourner seul sans rien de préinstallé (repli en mémoire si lecture seule).
"""

import ast
import csv
import json
import math
import multiprocessing
import os
import queue
import random
import shutil
import subprocess
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
# Constantes & Limites de sécurité
# ---------------------------------------------------------------------------

VALID_LOGIC_FUNCS = ("OR", "AND", "XOR", "NAND")
MAX_VERIFY_CABLES = 14        # 2^14 = 16 384 lignes de table de vérité
MAX_SEARCH_CABLES = 9         # Protection contre l'explosion combinatoire
MAX_SOLUTIONS_CAP = 5000
MAX_WORKERS_CAP = max(2, (cpu_count() or 1) * 2)

DATA_DIR_NAME = "logic_network_data"
SETTINGS_FILENAME = "settings.json"
CACHE_FILENAME = "search_cache.json"

GATE_COLORS = {
    "OR": "#2563EB",    # Bleu
    "AND": "#7C3AED",   # Violet
    "XOR": "#D97706",   # Ambre
    "NAND": "#DC2626",  # Rouge
}


# ---------------------------------------------------------------------------
# Modèles de données & Validation
# ---------------------------------------------------------------------------

class WiringValidationError(ValueError):
    """Levée lorsque la configuration de câblage ou de recherche est invalide."""

    def __init__(self, errors):
        # type: (Sequence[str]) -> None
        self.errors = list(errors)
        super(WiringValidationError, self).__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Connection:
    """Une connexion entre deux câbles avec porte supérieure et inférieure."""
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
                ["Attendu un tuple de 4 éléments (cable1, cable2, upper_logic, lower_logic), reçu : {!r}".format(raw)]
            )
        c1, c2, upper, lower = raw
        if isinstance(c1, bool) or not isinstance(c1, int) or isinstance(c2, bool) or not isinstance(c2, int):
            raise WiringValidationError(
                ["Les indices de câbles doivent être des entiers, reçu : ({!r}, {!r})".format(c1, c2)]
            )
        if not isinstance(upper, str) or not isinstance(lower, str):
            raise WiringValidationError(
                ["Les portes logiques doivent être des chaînes, reçu : ({!r}, {!r})".format(upper, lower)]
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
    """Paramètres validés pour une recherche."""
    num_cables: int
    start_size: int
    max_size: int
    max_solutions: int
    workers: int
    prune_noops: bool = True
    search_mode: str = "subset_perms"  # "subset_perms" ou "layer_dedup"
    use_cache: bool = True

    def cache_key(self):
        # type: () -> str
        return "n={}|mode={}|start={}|max={}|prune={}".format(
            self.num_cables,
            self.search_mode,
            self.start_size,
            self.max_size,
            int(self.prune_noops),
        )


@dataclass(frozen=True)
class SearchProgress:
    """Instantané de progression envoyé à l'interface."""
    current_size: int
    subsets_done: int
    subsets_total: int
    perms_checked: int
    solutions_found: int
    elapsed_sec: float
    eta_sec: Optional[float]


@dataclass(frozen=True)
class SearchResult:
    """Résultat final d'une recherche."""
    solutions: Tuple[Tuple[Tuple[int, int, str, str], ...], ...]
    cancelled: bool
    elapsed_sec: float
    perms_checked: int
    from_cache: bool = False


@dataclass(frozen=True)
class VerificationSummary:
    """Résumé de la vérification d'un câblage unique."""
    num_cables: int
    connections: Tuple[Connection, ...]
    truth_table: Tuple[Tuple[Tuple[int, ...], Tuple[int, ...]], ...]
    is_reliable: bool
    failing_indices: Tuple[int, ...]


def comb_count(n, k):
    # type: (int, int) -> int
    """Coefficient binomial C(n, k) compatible Python 3.7."""
    if k < 0 or k > n:
        return 0
    k = min(k, n - k)
    result = 1
    for i in range(1, k + 1):
        result = result * (n - k + i) // i
    return result


def normalize_connections(connections):
    # type: (Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> List[Connection]
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
    Parse de façon sécurisée des tuples de câblage avec ast.literal_eval().
    Accepte :
      - Un tuple de 4 éléments par ligne (virgule finale et commentaires # ignorés)
      - Une liste/tuple Python de tuples
      - Un tableau JSON d'objets ou de tableaux de 4 éléments
    """
    stripped = text.strip()
    if not stripped:
        return []

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

    if stripped.startswith("[") or stripped.startswith("(("):
        try:
            parsed_block = ast.literal_eval(stripped)
            if isinstance(parsed_block, (list, tuple)):
                return [Connection.from_tuple(item) for item in parsed_block]
        except (ValueError, SyntaxError):
            pass

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
            errors.append("Ligne {} : {}".format(line_no, exc.errors[0]))
        except (ValueError, SyntaxError):
            errors.append(
                "Ligne {} : Syntaxe invalide {!r}. Exemple attendu : (0, 2, 'OR', 'AND')".format(
                    line_no, raw_line.strip()
                )
            )

    if errors:
        raise WiringValidationError(errors)
    return connections


def validate_connections(num_cables, connections):
    # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> List[Connection]
    errors = []
    if isinstance(num_cables, bool) or not isinstance(num_cables, int):
        errors.append("Le nombre de câbles doit être un entier.")
    elif num_cables < 2 or num_cables > MAX_VERIFY_CABLES:
        errors.append(
            "Le nombre de câbles doit être compris entre 2 et {} (reçu : {}).".format(MAX_VERIFY_CABLES, num_cables)
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
                    "Étape {} : cable1 ({}) hors bornes [0, {}].".format(idx, conn.cable1, num_cables - 1)
                )
            if not (0 <= conn.cable2 < num_cables):
                errors.append(
                    "Étape {} : cable2 ({}) hors bornes [0, {}].".format(idx, conn.cable2, num_cables - 1)
                )
            if conn.cable1 == conn.cable2:
                errors.append(
                    "Étape {} : cable1 et cable2 ne peuvent pas être identiques ({}).".format(idx, conn.cable1)
                )
            if conn.upper_logic not in VALID_LOGIC_FUNCS:
                errors.append(
                    "Étape {} : porte supérieure non supportée {!r}. Autorisées : {}.".format(
                        idx, conn.upper_logic, ", ".join(VALID_LOGIC_FUNCS)
                    )
                )
            if conn.lower_logic not in VALID_LOGIC_FUNCS:
                errors.append(
                    "Étape {} : porte inférieure non supportée {!r}. Autorisées : {}.".format(
                        idx, conn.lower_logic, ", ".join(VALID_LOGIC_FUNCS)
                    )
                )

    if errors:
        raise WiringValidationError(errors)
    return normalized


def validate_search_config(
    num_cables,
    start_size,
    max_size,
    max_solutions,
    workers,
    prune_noops=True,
    search_mode="subset_perms",
    use_cache=True,
):
    # type: (int, int, Optional[int], int, int, bool, str, bool) -> SearchConfig
    errors = []
    if num_cables < 2 or num_cables > MAX_SEARCH_CABLES:
        errors.append(
            "Le nombre de câbles en recherche doit être entre 2 et {} (reçu : {}).".format(
                MAX_SEARCH_CABLES, num_cables
            )
        )

    mode = (
        search_mode
        if search_mode in ("subset_perms", "layer_dedup", "rust_layer_dedup")
        else "subset_perms"
    )
    if mode == "rust_layer_dedup" and num_cables > 7:
        errors.append("Le moteur Rust bitmask supporte jusqu'à 7 câbles (reçu : {}).".format(num_cables))
    total_pairs = comb_count(max(num_cables, 0), 2)
    max_allowed_depth = total_pairs if mode == "subset_perms" else max(total_pairs * 2, 12)
    resolved_max_size = max_allowed_depth if (max_size is None or max_size <= 0) else max_size

    if start_size < 1:
        errors.append("La taille de départ doit être au moins 1.")
    elif mode == "subset_perms" and total_pairs > 0 and start_size > total_pairs:
        errors.append(
            "La taille de départ ({}) dépasse le nombre max de paires uniques ({}) pour {} câbles.".format(
                start_size, total_pairs, num_cables
            )
        )

    if resolved_max_size < start_size:
        errors.append(
            "La taille max ({}) ne peut pas être inférieure à la taille de départ ({}).".format(
                resolved_max_size, start_size
            )
        )
    elif mode == "subset_perms" and total_pairs > 0 and resolved_max_size > total_pairs:
        resolved_max_size = total_pairs

    if max_solutions < 1 or max_solutions > MAX_SOLUTIONS_CAP:
        errors.append("Le nombre max de solutions doit être entre 1 et {}.".format(MAX_SOLUTIONS_CAP))

    if workers < 1 or workers > MAX_WORKERS_CAP:
        errors.append("Le nombre de workers doit être entre 1 et {}.".format(MAX_WORKERS_CAP))

    if errors:
        raise WiringValidationError(errors)

    return SearchConfig(
        num_cables=num_cables,
        start_size=start_size,
        max_size=resolved_max_size,
        max_solutions=max_solutions,
        workers=workers,
        prune_noops=bool(prune_noops),
        search_mode=mode,
        use_cache=bool(use_cache),
    )


def format_connections_plain_text(connections):
    # type: (Sequence[Union[Connection, Tuple[int, int, str, str]]]) -> str
    normalized = normalize_connections(connections)
    return "\n".join(repr(conn.to_tuple()) for conn in normalized)


# ---------------------------------------------------------------------------
# Dossier de données externe portable (réglages + cache de calculs)
# ---------------------------------------------------------------------------

def get_app_base_dir():
    # type: () -> str
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_data_dir(create=True):
    # type: (bool) -> Optional[str]
    """
    Retourne le chemin du dossier externe `logic_network_data/` dans le même
    répertoire que le script/.exe. En mode portable lecture seule, retourne None
    sans lever d'erreur pour que l'application puisse tourner seule.
    """
    path = os.path.join(get_app_base_dir(), DATA_DIR_NAME)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            return None
    return path if os.path.isdir(path) else None


def load_settings():
    # type: () -> Dict[str, Any]
    data_dir = get_data_dir(create=False)
    if not data_dir:
        return {}
    path = os.path.join(data_dir, SETTINGS_FILENAME)
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
    # type: (Dict[str, Any]) -> bool
    data_dir = get_data_dir(create=True)
    if not data_dir:
        return False
    path = os.path.join(data_dir, SETTINGS_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def load_search_cache():
    # type: () -> Dict[str, Any]
    data_dir = get_data_dir(create=False)
    if not data_dir:
        return {}
    path = os.path.join(data_dir, CACHE_FILENAME)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def save_search_cache_entry(config, result):
    # type: (SearchConfig, SearchResult) -> bool
    """Enregistre les solutions déjà calculées dans le dossier externe portable."""
    if result.cancelled and not result.solutions:
        return False
    data_dir = get_data_dir(create=True)
    if not data_dir:
        return False
    path = os.path.join(data_dir, CACHE_FILENAME)
    cache = load_search_cache()
    key = config.cache_key()
    existing = cache.get(key)
    # Ne pas écraser un cache plus complet par un résultat partiel plus petit
    if isinstance(existing, dict):
        prev_sols = existing.get("solutions", [])
        if len(prev_sols) > len(result.solutions) and not existing.get("cancelled", False):
            return True

    cache[key] = {
        "num_cables": config.num_cables,
        "search_mode": config.search_mode,
        "start_size": config.start_size,
        "max_size": config.max_size,
        "prune_noops": config.prune_noops,
        "cancelled": result.cancelled,
        "perms_checked": result.perms_checked,
        "elapsed_sec": round(result.elapsed_sec, 4),
        "solutions": [[list(step) for step in sol] for sol in result.solutions],
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
        return True
    except Exception:
        return False


def lookup_cached_search(config):
    # type: (SearchConfig) -> Optional[SearchResult]
    """Recherche si un calcul identique existe déjà dans le cache externe."""
    cache = load_search_cache()
    entry = cache.get(config.cache_key())
    if not isinstance(entry, dict):
        return None
    raw_sols = entry.get("solutions", [])
    sols = tuple(tuple(tuple(step) for step in sol) for sol in raw_sols)
    was_cancelled = bool(entry.get("cancelled", False))
    # On réutilise le cache s'il contient déjà assez de solutions ou s'il était exhaustif (non annulé)
    if len(sols) >= config.max_solutions or not was_cancelled:
        return SearchResult(
            solutions=sols[: config.max_solutions],
            cancelled=False,
            elapsed_sec=0.0,
            perms_checked=int(entry.get("perms_checked", 0)),
            from_cache=True,
        )
    return None


def clear_search_cache():
    # type: () -> bool
    data_dir = get_data_dir(create=False)
    if not data_dir:
        return True
    path = os.path.join(data_dir, CACHE_FILENAME)
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Moteur Booléen par Masques de Bits (Bitmasks)
# ---------------------------------------------------------------------------

_DESCENDING_BITMASKS_CACHE = {}  # type: Dict[int, FrozenSet[int]]


def _get_descending_bitmasks(num_cables):
    # type: (int) -> FrozenSet[int]
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
    for i in range(len(wire_masks) - 1):
        if (wire_masks[i] | wire_masks[i + 1]) != wire_masks[i]:
            return False
    return True


def apply_logic(input1, input2, logic_func):
    # type: (int, int, str) -> int
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
    validated = validate_connections(num_cables, connections)
    if len(input_bits) != num_cables:
        raise WiringValidationError(
            ["Attendu {} bits d'entrée, reçu {}.".format(num_cables, len(input_bits))]
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
# Moteurs de Recherche (Sous-ensembles Parallèles + Couches BFS Dédupliquées)
# ---------------------------------------------------------------------------

def generate_all_connections(num_cables):
    # type: (int) -> List[Tuple[int, int, str, str]]
    return [
        (input1, input2, "OR", "AND")
        for input1, input2 in combinations(range(num_cables), 2)
    ]


def test_subset(num_cables, subset, prune_noops=True, stop_event=None, max_needed=0):
    # type: (int, Sequence[Tuple[int, int, str, str]], bool, Any, int) -> Tuple[List[Tuple[Tuple[int, int, str, str], ...]], int]
    k = len(subset)
    init_masks = list(_initial_wire_masks(num_cables))
    if k == 0:
        if _is_masks_descending(init_masks):
            return ([()], 1)
        return ([], 1)

    solutions = []
    used = [False] * k
    path = [None] * k  # type: List[Optional[Tuple[int, int, str, str]]]
    stats = [0]
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


def _run_layer_dedup_search(config, msg_queue, stop_event):
    # type: (SearchConfig, Any, Any) -> SearchResult
    start_time = time.monotonic()
    all_conns = generate_all_connections(config.num_cables)
    init_state = tuple(_initial_wire_masks(config.num_cables))

    frontier = {init_state: ()}  # type: Dict[Tuple[int, ...], Tuple[Tuple[int, int, str, str], ...]]
    visited_states = {init_state}
    solutions = []  # type: List[Tuple[Tuple[int, int, str, str], ...]]
    checked = 0

    for depth in range(1, config.max_size + 1):
        if stop_event.is_set() or len(solutions) >= config.max_solutions:
            break
        if not frontier:
            msg_queue.put(("log", "[ Info ] Frontier exhausted at depth {}.".format(depth - 1)))
            break

        total_states = len(frontier)
        msg_queue.put((
            "log",
            "[ Layer {} ] Expanding {:,} unique states ({} gate choices/state)...".format(
                depth, total_states, len(all_conns)
            ),
        ))

        next_frontier = {}  # type: Dict[Tuple[int, ...], Tuple[Tuple[int, int, str, str], ...]]
        states_done = 0
        layer_start = time.monotonic()
        last_emit = 0.0

        for state, seq in frontier.items():
            if stop_event.is_set() or len(solutions) >= config.max_solutions:
                break
            states_done += 1

            for conn in all_conns:
                c1, c2, _, _ = conn
                m1 = state[c1]
                m2 = state[c2]
                new_m1 = m1 | m2
                new_m2 = m1 & m2
                checked += 1

                if config.prune_noops and new_m1 == m1 and new_m2 == m2:
                    continue

                new_state_list = list(state)
                new_state_list[c1] = new_m1
                new_state_list[c2] = new_m2
                new_state = tuple(new_state_list)

                new_seq = seq + (conn,)
                if depth >= config.start_size and _is_masks_descending(new_state):
                    solutions.append(new_seq)
                    msg_queue.put(("solution", new_seq))
                    if len(solutions) >= config.max_solutions:
                        break
                    continue

                if new_state in visited_states:
                    continue
                visited_states.add(new_state)
                next_frontier[new_state] = new_seq

            now = time.monotonic()
            if (now - last_emit) >= 0.12 or states_done >= total_states:
                elapsed = now - start_time
                layer_elapsed = now - layer_start
                eta = None  # type: Optional[float]
                if 0 < states_done < total_states:
                    eta = (total_states - states_done) / max(states_done / max(layer_elapsed, 1e-6), 1e-9)
                elif states_done >= total_states:
                    eta = 0.0
                msg_queue.put((
                    "progress",
                    SearchProgress(
                        current_size=depth,
                        subsets_done=states_done,
                        subsets_total=total_states,
                        perms_checked=checked,
                        solutions_found=len(solutions),
                        elapsed_sec=elapsed,
                        eta_sec=eta,
                    ),
                ))
                last_emit = now

        frontier = next_frontier

    cancelled = stop_event.is_set() and len(solutions) < config.max_solutions
    return SearchResult(
        solutions=tuple(solutions[: config.max_solutions]),
        cancelled=cancelled,
        elapsed_sec=time.monotonic() - start_time,
        perms_checked=checked,
    )



def find_rust_solver_binary():
    # type: () -> Optional[str]
    """
    Cherche l'exécutable compilé du moteur Rust (`rust_solver.exe` ou `rust_solver`)
    à côté du programme, dans `logic_network_data/`, ou dans le PATH.
    """
    base_dir = get_app_base_dir()
    names = ["rust_solver.exe", "rust_solver"] if os.name == "nt" else ["rust_solver", "rust_solver.exe"]
    candidates = []
    for name in names:
        candidates.append(os.path.join(base_dir, name))
        candidates.append(os.path.join(base_dir, DATA_DIR_NAME, name))
        candidates.append(os.path.join(base_dir, "target", "release", name))
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _run_rust_layer_search(config, msg_queue, stop_event, binary_path):
    # type: (SearchConfig, Any, Any, str) -> SearchResult
    """Lance le moteur natif Rust en sous-processus et lit ses résultats en direct."""
    start_time = time.monotonic()
    cmd = [
        binary_path,
        "--cables", str(config.num_cables),
        "--start-size", str(config.start_size),
        "--max-size", str(config.max_size),
        "--max-solutions", str(config.max_solutions),
        "--workers", str(config.workers),
    ]
    if not config.prune_noops:
        cmd.append("--no-prune")

    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        bufsize=1,
        creationflags=creationflags,
    )

    solutions = []  # type: List[Tuple[Tuple[int, int, str, str], ...]]
    total_checked = 0
    done_seen = False

    def _watch_stop():
        while proc.poll() is None:
            if stop_event.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            time.sleep(0.1)

    watcher = threading.Thread(target=_watch_stop, daemon=True)
    watcher.start()

    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            if stop_event.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("LOG "):
                msg_queue.put(("log", line[4:]))
            elif line.startswith("PROGRESS "):
                parts = line.split()
                if len(parts) >= 8:
                    depth = int(parts[1])
                    done = int(parts[2])
                    total = int(parts[3])
                    checked = int(parts[4])
                    sols_count = int(parts[5])
                    elapsed_ms = int(parts[6])
                    eta_ms = int(parts[7])
                    total_checked = checked
                    msg_queue.put((
                        "progress",
                        SearchProgress(
                            current_size=depth,
                            subsets_done=done,
                            subsets_total=total,
                            perms_checked=checked,
                            solutions_found=sols_count,
                            elapsed_sec=elapsed_ms / 1000.0,
                            eta_sec=(None if eta_ms < 0 else eta_ms / 1000.0),
                        ),
                    ))
            elif line.startswith("SOLUTION "):
                body = line[9:].strip()
                if body:
                    steps = []
                    for pair_str in body.split(";"):
                        c1_s, c2_s = pair_str.split(",")
                        steps.append((int(c1_s), int(c2_s), "OR", "AND"))
                    sol_tuple = tuple(steps)
                    solutions.append(sol_tuple)
                    msg_queue.put(("solution", sol_tuple))
                    if len(solutions) >= config.max_solutions:
                        stop_event.set()
                        break
            elif line.startswith("DONE "):
                parts = line.split()
                if len(parts) >= 3:
                    total_checked = int(parts[1])
                done_seen = True

        proc.wait(timeout=3.0)
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    stderr_text = ""
    if proc.stderr is not None:
        try:
            stderr_text = proc.stderr.read().strip()
        except Exception:
            stderr_text = ""

    if proc.returncode not in (0, None) and not stop_event.is_set() and not done_seen:
        raise RuntimeError(stderr_text or "Le moteur Rust s'est arrêté avec le code {}.".format(proc.returncode))

    cancelled = stop_event.is_set() and len(solutions) < config.max_solutions
    return SearchResult(
        solutions=tuple(solutions[: config.max_solutions]),
        cancelled=cancelled,
        elapsed_sec=time.monotonic() - start_time,
        perms_checked=total_checked,
    )

def _worker_loop(num_cables, prune_noops, max_solutions, task_queue, result_queue, stop_event):
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
    start_time = time.monotonic()
    total_checked = 0
    solutions = []  # type: List[Tuple[Tuple[int, int, str, str], ...]]

    if config.search_mode == "rust_layer_dedup":
        mode_label = "Rust layer BFS + state dedup (multi-core)"
    elif config.search_mode == "layer_dedup":
        mode_label = "layer BFS + state dedup (pair reuse)"
    else:
        mode_label = "subset perms + no-op prune" if config.prune_noops else "subset perms exhaustive"
    msg_queue.put((
        "log",
        "[ Launch ] Cables={} | Size={}..{} | MaxSol={} | Workers={} | Mode={}".format(
            config.num_cables,
            config.start_size,
            config.max_size,
            config.max_solutions,
            config.workers,
            mode_label,
        ),
    ))

    # Vérification du cache local portable si activé
    if config.use_cache:
        cached_res = lookup_cached_search(config)
        if cached_res is not None:
            msg_queue.put((
                "log",
                "[ Cache ] Loaded {} precomputed solution(s) from {}/{}".format(
                    len(cached_res.solutions), DATA_DIR_NAME, CACHE_FILENAME
                ),
            ))
            for sol in cached_res.solutions:
                msg_queue.put(("solution", sol))
            msg_queue.put(("done", cached_res))
            return

    if config.search_mode == "rust_layer_dedup":
        try:
            rust_bin = find_rust_solver_binary()
            if not rust_bin:
                raise RuntimeError(
                    "Binaire Rust introuvable (rust_solver.exe). Compilez-le avec : "
                    "rustc -O -C target-cpu=native rust_solver.rs -o rust_solver.exe"
                )
            res = _run_rust_layer_search(config, msg_queue, stop_event, rust_bin)
            if config.use_cache:
                save_search_cache_entry(config, res)
            msg_queue.put(("done", res))
        except Exception as exc:
            msg_queue.put(("error", str(exc)))
        return

    if config.search_mode == "layer_dedup":
        try:
            res = _run_layer_dedup_search(config, msg_queue, stop_event)
            if config.use_cache:
                save_search_cache_entry(config, res)
            msg_queue.put(("done", res))
        except Exception as exc:
            msg_queue.put(("error", str(exc)))
        return

    all_conns = generate_all_connections(config.num_cables)
    total_pairs = len(all_conns)

    if config.workers == 1:
        try:
            for size_k in range(config.start_size, min(config.max_size, total_pairs) + 1):
                if stop_event.is_set():
                    break
                subsets_total = comb_count(total_pairs, size_k)
                subsets_done = 0
                size_start_time = time.monotonic()
                last_progress_emit = 0.0
                msg_queue.put(
                    ("log", "[ Update ] Testing size {} ({:,} subsets)...".format(size_k, subsets_total))
                )

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
                            eta = (subsets_total - subsets_done) / max(
                                subsets_done / max(size_elapsed, 1e-6), 1e-9
                            )
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
            final_res = SearchResult(
                solutions=tuple(solutions[: config.max_solutions]),
                cancelled=cancelled,
                elapsed_sec=time.monotonic() - start_time,
                perms_checked=total_checked,
            )
            if config.use_cache:
                save_search_cache_entry(config, final_res)
            msg_queue.put(("done", final_res))
        except Exception as exc:
            msg_queue.put(("error", str(exc)))
        return

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
            msg_queue.put(
                ("log", "[ Update ] Testing size {} ({:,} subsets)...".format(size_k, subsets_total))
            )

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
                while not exhausted_iter and pending_batches < max_inflight and not stop_event.is_set():
                    batch = []
                    for _ in range(batch_size):
                        try:
                            batch.append(next(subset_iter))
                        except StopIteration:
                            exhausted_iter = True
                            break
                    if batch:
                        while not stop_event.is_set():
                            try:
                                task_queue.put((size_k, batch), timeout=0.1)
                                pending_batches += 1
                                break
                            except queue.Full:
                                continue

                if exhausted_iter and pending_batches == 0:
                    break

                try:
                    res = result_queue.get(timeout=0.1)
                except queue.Empty:
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
                    if 0 < subsets_done < subsets_total:
                        eta = (subsets_total - subsets_done) / max(
                            subsets_done / max(size_elapsed, 1e-6), 1e-9
                        )
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
        if config.use_cache:
            save_search_cache_entry(config, final_result)
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
# Utilitaires d'export
# ---------------------------------------------------------------------------

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
            "search_mode": config.search_mode,
        },
        "summary": {
            "solutions_found": len(result.solutions),
            "cancelled": result.cancelled,
            "from_cache": result.from_cache,
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
# Schéma Visuel Interactif (Flux de signaux + Édition à la souris)
# ---------------------------------------------------------------------------

class WiringCanvas(ttk.Frame):
    """
    Schéma visuel d'un réseau de câbles booléens avec pastilles 0/1 cliquables,
    propagation en direct et édition optionnelle à la souris.
    """

    def __init__(
        self,
        parent,
        interactive=True,
        height=230,
        editable=False,
        on_add_connection=None,
        on_remove_connection=None,
        get_active_gates=None,
    ):
        super(WiringCanvas, self).__init__(parent)
        self.interactive = interactive
        self.editable = editable
        self.on_add_connection = on_add_connection
        self.on_remove_connection = on_remove_connection
        self.get_active_gates = get_active_gates

        self.num_cables = 4
        self.connections = []  # type: List[Connection]
        self.input_bits = [0, 1, 0, 1]
        self.selected_cable = None  # type: Optional[int]
        self._cable_ys = []         # type: List[float]
        self._step_xs = []          # type: List[float]
        self._x_start = 105.0
        self._x_end = 200.0

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
        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

    def set_editable(self, enabled):
        # type: (bool) -> None
        self.editable = bool(enabled)
        self.selected_cable = None
        self.redraw()

    def set_network(self, num_cables, connections, input_bits=None):
        # type: (int, Sequence[Union[Connection, Tuple[int, int, str, str]]], Optional[Sequence[int]]) -> None
        self.num_cables = max(2, int(num_cables))
        try:
            self.connections = validate_connections(self.num_cables, connections)
        except WiringValidationError:
            self.connections = []

        if self.selected_cable is not None and self.selected_cable >= self.num_cables:
            self.selected_cable = None

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

    def _nearest_cable(self, cy):
        # type: (float) -> Optional[int]
        best_idx = None
        best_dist = 16.0
        for idx, y in enumerate(self._cable_ys):
            d = abs(cy - y)
            if d <= best_dist:
                best_dist = d
                best_idx = idx
        return best_idx

    def _nearest_step(self, cx):
        # type: (float) -> Optional[int]
        best_idx = None
        best_dist = 24.0
        for idx, sx in enumerate(self._step_xs):
            d = abs(cx - sx)
            if d <= best_dist:
                best_dist = d
                best_idx = idx
        return best_idx

    def _on_left_click(self, event):
        if not self.editable:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)

        if 44 <= cx <= 86:
            return

        cable_idx = self._nearest_cable(cy)
        if cable_idx is None:
            return

        if self.selected_cable is None:
            self.selected_cable = cable_idx
            self.redraw()
        elif self.selected_cable == cable_idx:
            self.selected_cable = None
            self.redraw()
        else:
            c1 = self.selected_cable
            c2 = cable_idx
            self.selected_cable = None
            up_f, low_f = ("OR", "AND")
            if callable(self.get_active_gates):
                gates = self.get_active_gates()
                if gates and len(gates) == 2:
                    up_f, low_f = gates
            if callable(self.on_add_connection):
                self.on_add_connection(Connection(c1, c2, up_f, low_f))

    def _on_right_click(self, event):
        if not self.editable:
            return
        if self.selected_cable is not None:
            self.selected_cable = None
            self.redraw()
            return

        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        step_idx = self._nearest_step(cx)
        if step_idx is None or not (0 <= step_idx < len(self.connections)):
            return
        conn = self.connections[step_idx]
        y_min = min(self._cable_ys[conn.cable1], self._cable_ys[conn.cable2]) - 14
        y_max = max(self._cable_ys[conn.cable1], self._cable_ys[conn.cable2]) + 14
        if y_min <= cy <= y_max and callable(self.on_remove_connection):
            self.on_remove_connection(step_idx)

    def redraw(self):
        self.canvas.delete("all")
        n = self.num_cables
        steps = len(self.connections)

        left_pad = 105
        right_pad = 126
        top_pad = 50 if self.editable else 36
        step_label_y = 32 if self.editable else 14
        bottom_pad = 26
        row_gap = 36 if n <= 6 else 28
        col_gap = 78

        content_w = max(self.canvas.winfo_width() or 600, left_pad + right_pad + max(steps, 1) * col_gap)
        content_h = max(self.canvas.winfo_height() or 210, top_pad + bottom_pad + max(n - 1, 1) * row_gap)
        self.canvas.configure(scrollregion=(0, 0, content_w, content_h))

        try:
            history = evaluate_step_by_step(n, self.connections, self.input_bits)
        except WiringValidationError:
            history = [list(self.input_bits)]

        self._cable_ys = [top_pad + i * row_gap for i in range(n)]
        self._x_start = float(left_pad)
        self._x_end = float(left_pad + max(steps, 1) * col_gap + 24)
        self._step_xs = [self._x_start + (s + 0.5) * col_gap for s in range(steps)]

        if self.editable:
            if self.selected_cable is not None:
                hint = "C{} sélectionné -> Cliquez sur un autre câble pour relier (Clic droit : annuler)".format(
                    self.selected_cable
                )
            else:
                hint = "Édition souris ACTIVE : Cliquez 2 câbles pour ajouter un lien | Clic droit sur une porte pour supprimer"
            self.canvas.create_text(
                12,
                12,
                text=hint,
                anchor="w",
                fill="#FBBF24",
                font=("Segoe UI", 8, "bold"),
            )

        for s_idx, sx in enumerate(self._step_xs, start=1):
            self.canvas.create_text(
                sx, step_label_y, text="E{}".format(s_idx), fill="#CBD5E1", font=("Consolas", 8, "bold")
            )

        for c_idx in range(n):
            y = self._cable_ys[c_idx]
            is_sel = (self.editable and self.selected_cable == c_idx)

            self.canvas.create_text(
                14,
                y,
                text="C{}".format(c_idx),
                anchor="w",
                fill="#FBBF24" if is_sel else "#E2E8F0",
                font=("Consolas", 9, "bold"),
            )

            bit_val = history[0][c_idx]
            pill_bg = "#10B981" if bit_val else "#334155"
            pill_fg = "#022C22" if bit_val else "#E2E8F0"
            tag = "input_pill_{}".format(c_idx)
            self.canvas.create_rectangle(
                44,
                y - 11,
                86,
                y + 11,
                fill=pill_bg,
                outline="#FBBF24" if is_sel else "#64748B",
                width=2 if is_sel else 1,
                tags=(tag,),
            )
            self.canvas.create_text(
                65, y, text=str(bit_val), fill=pill_fg, font=("Consolas", 9, "bold"), tags=(tag,)
            )
            if self.interactive:
                self.canvas.tag_bind(tag, "<Button-1>", lambda _e, idx=c_idx: self._toggle_bit(idx))

            seg_points = [self._x_start - 12] + self._step_xs + [self._x_end]
            for seg_i in range(len(seg_points) - 1):
                state_idx = min(seg_i, len(history) - 1)
                val = history[state_idx][c_idx]
                color = "#34D399" if val else "#475569"
                width = 3 if val else 2
                self.canvas.create_line(
                    seg_points[seg_i], y, seg_points[seg_i + 1], y, fill=color, width=width
                )

            out_val = history[-1][c_idx]
            out_bg = "#10B981" if out_val else "#1E293B"
            out_fg = "#022C22" if out_val else "#CBD5E1"
            self.canvas.create_rectangle(
                self._x_end + 8, y - 10, self._x_end + 42, y + 10, fill=out_bg, outline="#64748B", width=1
            )
            self.canvas.create_text(
                self._x_end + 25, y, text=str(out_val), fill=out_fg, font=("Consolas", 9, "bold")
            )

        for s_idx, conn in enumerate(self.connections):
            sx = self._step_xs[s_idx]
            y1 = self._cable_ys[conn.cable1]
            y2 = self._cable_ys[conn.cable2]

            self.canvas.create_line(sx, y1, sx, y2, fill="#94A3B8", width=2)

            u_color = GATE_COLORS.get(conn.upper_logic, "#2563EB")
            self.canvas.create_rectangle(
                sx - 20, y1 - 10, sx + 20, y1 + 10, fill=u_color, outline="#F8FAFC", width=1
            )
            self.canvas.create_text(
                sx, y1, text=conn.upper_logic, fill="#FFFFFF", font=("Consolas", 7, "bold")
            )

            l_color = GATE_COLORS.get(conn.lower_logic, "#7C3AED")
            self.canvas.create_rectangle(
                sx - 20, y2 - 10, sx + 20, y2 + 10, fill=l_color, outline="#F8FAFC", width=1
            )
            self.canvas.create_text(
                sx, y2, text=conn.lower_logic, fill="#FFFFFF", font=("Consolas", 7, "bold")
            )

        final_out = history[-1]
        ok = is_ordered_descending(final_out)
        badge_bg = "#065F46" if ok else "#991B1B"
        badge_txt = "TRIÉ OK" if ok else "NON TRIÉ"
        badge_x = self._x_end + 52
        mid_y = (self._cable_ys[0] + self._cable_ys[-1]) / 2.0
        self.canvas.create_rectangle(
            badge_x, mid_y - 14, badge_x + 66, mid_y + 14, fill=badge_bg, outline="#E2E8F0", width=1
        )
        self.canvas.create_text(
            badge_x + 33, mid_y, text=badge_txt, fill="#FFFFFF", font=("Segoe UI", 7, "bold")
        )


# ---------------------------------------------------------------------------
# Application Principale Tkinter (Interface en Français)
# ---------------------------------------------------------------------------

class LogicNetworkApp:
    def __init__(self, root):
        # type: (tk.Tk) -> None
        self.root = root
        self.root.title("Explorateur de Câblages Booléens — Visuel, Parallèle & Portable")
        self.settings = load_settings()
        self.root.geometry(self.settings.get("geometry", "1080x780"))
        self.root.minsize(900, 640)

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

    # ---------------------- ONGLET 1 : RECHERCHE ----------------------

    def _build_search_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="1. Recherche de Câblages (MT34)")

        params = ttk.LabelFrame(tab, text="Paramètres de recherche", padding=8)
        params.pack(fill=tk.X)

        default_workers = max(1, (cpu_count() or 2) - 1)

        ttk.Label(params, text="Câbles :").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
        self.cables_var = tk.StringVar(value=str(self.settings.get("search_cables", 4)))
        tk.Spinbox(params, from_=2, to=MAX_SEARCH_CABLES, textvariable=self.cables_var, width=5).grid(
            row=0, column=1, padx=(0, 10)
        )

        ttk.Label(params, text="Taille départ :").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
        self.start_size_var = tk.StringVar(value=str(self.settings.get("start_size", 1)))
        tk.Spinbox(params, from_=1, to=36, textvariable=self.start_size_var, width=5).grid(
            row=0, column=3, padx=(0, 10)
        )

        ttk.Label(params, text="Taille max (0=auto) :").grid(row=0, column=4, sticky=tk.W, padx=(0, 4))
        self.max_size_var = tk.StringVar(value=str(self.settings.get("max_size", 0)))
        tk.Spinbox(params, from_=0, to=36, textvariable=self.max_size_var, width=5).grid(
            row=0, column=5, padx=(0, 10)
        )

        ttk.Label(params, text="Solutions max :").grid(row=0, column=6, sticky=tk.W, padx=(0, 4))
        self.max_sol_var = tk.StringVar(value=str(self.settings.get("max_solutions", 5)))
        tk.Spinbox(params, from_=1, to=MAX_SOLUTIONS_CAP, textvariable=self.max_sol_var, width=6).grid(
            row=0, column=7, padx=(0, 10)
        )

        ttk.Label(params, text="Workers (cœurs={}) :".format(cpu_count() or 1)).grid(
            row=0, column=8, sticky=tk.W, padx=(0, 4)
        )
        self.workers_var = tk.StringVar(value=str(self.settings.get("workers", default_workers)))
        tk.Spinbox(params, from_=1, to=MAX_WORKERS_CAP, textvariable=self.workers_var, width=5).grid(
            row=0, column=9
        )

        mode_row = ttk.Frame(params)
        mode_row.grid(row=1, column=0, columnspan=10, sticky=tk.W, pady=(6, 0))

        saved_mode = self.settings.get("search_mode")
        if saved_mode == "rust_layer_dedup":
            init_mode_lbl = "Couches BFS — Moteur Rust (Multi-cœurs)"
        elif saved_mode == "layer_dedup":
            init_mode_lbl = "Couches BFS + Dédup. d'états (Python)"
        elif saved_mode == "subset_perms":
            init_mode_lbl = "Permutations de sous-ensembles (Parallèle)"
        else:
            init_mode_lbl = (
                "Couches BFS — Moteur Rust (Multi-cœurs)"
                if find_rust_solver_binary()
                else "Couches BFS + Dédup. d'états (Python)"
            )

        ttk.Label(mode_row, text="Stratégie :").pack(side=tk.LEFT, padx=(0, 4))
        self.search_mode_label_var = tk.StringVar(value=init_mode_lbl)
        ttk.Combobox(
            mode_row,
            textvariable=self.search_mode_label_var,
            values=[
                "Couches BFS — Moteur Rust (Multi-cœurs)",
                "Couches BFS + Dédup. d'états (Python)",
                "Permutations de sous-ensembles (Parallèle)",
            ],
            width=44,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 12))

        self.prune_var = tk.BooleanVar(value=bool(self.settings.get("prune_noops", True)))
        ttk.Checkbutton(
            mode_row,
            text="Ignorer les portes sans effet (plus rapide)",
            variable=self.prune_var,
        ).pack(side=tk.LEFT, padx=(0, 12))

        self.use_cache_var = tk.BooleanVar(value=bool(self.settings.get("use_cache", True)))
        ttk.Checkbutton(
            mode_row,
            text="Utiliser le cache local ({}/)".format(DATA_DIR_NAME),
            variable=self.use_cache_var,
        ).pack(side=tk.LEFT)

        btns = ttk.Frame(tab)
        btns.pack(fill=tk.X, pady=(8, 6))

        self.start_btn = ttk.Button(btns, text="Lancer la recherche", command=self.start_search)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = ttk.Button(btns, text="Arrêter", command=self.stop_search, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(btns, text="Copier le log texte", command=self.copy_search_log).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Exporter JSON", command=self.export_search_json_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Exporter CSV", command=self.export_search_csv_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Vider le cache", command=self.clear_cache_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Effacer", command=self.clear_search_results).pack(side=tk.RIGHT)

        prog_frame = ttk.LabelFrame(tab, text="Progression en direct", padding=8)
        prog_frame.pack(fill=tk.X, pady=(0, 8))

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))

        self.progress_label_var = tk.StringVar(
            value="En attente — Prêt. Astuce : 7 workers conseillés sur un processeur 8 cœurs."
        )
        ttk.Label(prog_frame, textvariable=self.progress_label_var, font=("Consolas", 9)).pack(anchor=tk.W)

        paned = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        visual_frame = ttk.Frame(paned)
        paned.add(visual_frame, weight=3)

        sol_left = ttk.LabelFrame(visual_frame, text="Solutions trouvées (cliquer pour voir)", padding=6)
        sol_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(0, 6))

        self.sol_listbox = tk.Listbox(sol_left, width=30, font=("Consolas", 9))
        self.sol_listbox.pack(fill=tk.BOTH, expand=True)
        self.sol_listbox.bind("<<ListboxSelect>>", self._on_select_solution)

        ttk.Button(
            sol_left, text="Ouvrir dans le Vérificateur", command=self.send_selected_to_verifier
        ).pack(fill=tk.X, pady=(6, 0))

        sol_right = ttk.LabelFrame(visual_frame, text="Schéma visuel & Simulateur interactif", padding=6)
        sol_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.search_canvas = WiringCanvas(sol_right, interactive=True, height=200, editable=False)
        self.search_canvas.pack(fill=tk.BOTH, expand=True)

        plain_frame = ttk.LabelFrame(paned, text="Logs & Solutions (texte brut)", padding=6)
        paned.add(plain_frame, weight=2)

        self.search_log = scrolledtext.ScrolledText(plain_frame, wrap=tk.WORD, font=("Consolas", 9), height=10)
        self.search_log.pack(fill=tk.BOTH, expand=True)

    # ---------------------- ONGLET 2 : VÉRIFICATION & SIMULATEUR ----------------------

    def _build_verify_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="2. Vérificateur & Simulateur (T3)")

        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(top, text="Câbles :").pack(side=tk.LEFT, padx=(0, 4))
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

        ttk.Button(top, text="Vérifier", command=self.run_verify).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Ex : 4 câbles fiable", command=self.load_example_4_valid).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Ex : 5 câbles non fiable", command=self.load_example_5_invalid).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(top, text="Importer JSON", command=self.import_wiring_json_ui).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Exporter JSON", command=self.export_wiring_json_ui).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Exporter CSV", command=self.export_tt_csv_ui).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(top, text="Copier texte brut", command=self.copy_verify_plain_text).pack(side=tk.RIGHT)

        vis_box = ttk.LabelFrame(tab, text="Schéma du câblage & Édition à la souris", padding=6)
        vis_box.pack(fill=tk.X, pady=(0, 6))

        preset_bar = ttk.Frame(vis_box)
        preset_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(preset_bar, text="Entrées rapides :").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_bar, text="Tout à 0", command=lambda: self._apply_sim_preset("zeros")).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_bar, text="Tout à 1", command=lambda: self._apply_sim_preset("ones")).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_bar, text="Alterné", command=lambda: self._apply_sim_preset("alt")).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_bar, text="Aléatoire", command=lambda: self._apply_sim_preset("rand")).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            preset_bar, text="Charger 1er cas en échec", command=self._load_first_failing_input
        ).pack(side=tk.LEFT, padx=(8, 6))

        self.mouse_edit_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            preset_bar,
            text="Mode édition à la souris (cliquer 2 câbles)",
            variable=self.mouse_edit_var,
            command=self._toggle_mouse_edit_mode,
        ).pack(side=tk.RIGHT)

        self.verify_canvas = WiringCanvas(
            vis_box,
            interactive=True,
            height=210,
            editable=False,
            on_add_connection=self._on_canvas_add_connection,
            on_remove_connection=self._on_canvas_remove_connection,
            get_active_gates=lambda: (self.step_up_var.get().strip().upper(), self.step_low_var.get().strip().upper()),
        )
        self.verify_canvas.pack(fill=tk.BOTH, expand=True)

        v_paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        v_paned.pack(fill=tk.BOTH, expand=True)

        editor_box = ttk.LabelFrame(v_paned, text="Éditeur d'étapes de câblage", padding=6)
        v_paned.add(editor_box, weight=2)

        self.editor_nb = ttk.Notebook(editor_box)
        self.editor_nb.pack(fill=tk.BOTH, expand=True)

        tbl_tab = ttk.Frame(self.editor_nb, padding=4)
        self.editor_nb.add(tbl_tab, text="Tableau interactif")

        cols = ("step", "c1", "c2", "upper", "lower")
        self.steps_tree = ttk.Treeview(tbl_tab, columns=cols, show="headings", height=8)
        self.steps_tree.heading("step", text="#")
        self.steps_tree.heading("c1", text="Câble 1")
        self.steps_tree.heading("c2", text="Câble 2")
        self.steps_tree.heading("upper", text="Porte Haut")
        self.steps_tree.heading("lower", text="Porte Bas")
        self.steps_tree.column("step", width=36, anchor="center")
        self.steps_tree.column("c1", width=64, anchor="center")
        self.steps_tree.column("c2", width=64, anchor="center")
        self.steps_tree.column("upper", width=80, anchor="center")
        self.steps_tree.column("lower", width=80, anchor="center")
        self.steps_tree.pack(fill=tk.BOTH, expand=True)
        self.steps_tree.bind("<<TreeviewSelect>>", self._on_select_step_row)

        add_bar = ttk.Frame(tbl_tab)
        add_bar.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(add_bar, text="C1 :").pack(side=tk.LEFT)
        self.step_c1_var = tk.StringVar(value="0")
        tk.Spinbox(add_bar, from_=0, to=MAX_VERIFY_CABLES - 1, textvariable=self.step_c1_var, width=4).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(add_bar, text="C2 :").pack(side=tk.LEFT)
        self.step_c2_var = tk.StringVar(value="1")
        tk.Spinbox(add_bar, from_=0, to=MAX_VERIFY_CABLES - 1, textvariable=self.step_c2_var, width=4).pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(add_bar, text="Haut :").pack(side=tk.LEFT)
        self.step_up_var = tk.StringVar(value="OR")
        ttk.Combobox(add_bar, textvariable=self.step_up_var, values=VALID_LOGIC_FUNCS, width=6, state="readonly").pack(
            side=tk.LEFT, padx=(2, 6)
        )
        ttk.Label(add_bar, text="Bas :").pack(side=tk.LEFT)
        self.step_low_var = tk.StringVar(value="AND")
        ttk.Combobox(add_bar, textvariable=self.step_low_var, values=VALID_LOGIC_FUNCS, width=6, state="readonly").pack(
            side=tk.LEFT, padx=(2, 0)
        )

        edit_btns = ttk.Frame(tbl_tab)
        edit_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(edit_btns, text="Ajouter", command=self.add_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Modifier", command=self.update_selected_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Supprimer", command=self.delete_selected_step).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Monter", command=lambda: self.move_selected_step(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(edit_btns, text="Descendre", command=lambda: self.move_selected_step(1)).pack(side=tk.LEFT, padx=2)

        txt_tab = ttk.Frame(self.editor_nb, padding=4)
        self.editor_nb.add(txt_tab, text="Texte brut (Tuples / JSON)")

        ttk.Label(
            txt_tab,
            text="Un tuple par ligne ex: (0, 2, 'OR', 'AND') — lecture sécurisée (ast.literal_eval) :",
        ).pack(anchor=tk.W, pady=(0, 4))

        self.conn_input = scrolledtext.ScrolledText(txt_tab, height=8, font=("Consolas", 9))
        self.conn_input.pack(fill=tk.BOTH, expand=True)

        ttk.Button(
            txt_tab, text="Appliquer le texte -> Tableau & Schéma", command=self.apply_plain_text_to_table
        ).pack(anchor=tk.E, pady=(4, 0))

        res_box = ttk.LabelFrame(v_paned, text="Résultats de vérification", padding=6)
        v_paned.add(res_box, weight=3)

        self.verify_banner_var = tk.StringVar(value="Cliquez sur « Vérifier » pour évaluer toutes les entrées.")
        self.verify_banner = ttk.Label(res_box, textvariable=self.verify_banner_var, style="BannerInfo.TLabel")
        self.verify_banner.pack(fill=tk.X, pady=(0, 6))

        res_nb = ttk.Notebook(res_box)
        res_nb.pack(fill=tk.BOTH, expand=True)

        vtt_tab = ttk.Frame(res_nb, padding=4)
        res_nb.add(vtt_tab, text="Table de vérité visuelle")

        vtt_ctrl = ttk.Frame(vtt_tab)
        vtt_ctrl.pack(fill=tk.X, pady=(0, 4))
        self.only_fail_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            vtt_ctrl, text="Afficher uniquement les échecs", variable=self.only_fail_var, command=self._populate_tt_tree
        ).pack(side=tk.LEFT)
        ttk.Label(vtt_ctrl, text="(Double-clic sur une ligne pour la simuler)", foreground="#475569").pack(
            side=tk.RIGHT
        )

        tt_cols = ("row", "inputs", "outputs", "status")
        self.tt_tree = ttk.Treeview(vtt_tab, columns=tt_cols, show="headings", height=10)
        self.tt_tree.heading("row", text="#")
        self.tt_tree.heading("inputs", text="Entrées (C0..Cn)")
        self.tt_tree.heading("outputs", text="Sorties (C0..Cn)")
        self.tt_tree.heading("status", text="Statut")
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

        ptt_tab = ttk.Frame(res_nb, padding=4)
        res_nb.add(ptt_tab, text="Table de vérité (texte brut)")

        self.verify_log = scrolledtext.ScrolledText(ptt_tab, wrap=tk.WORD, font=("Consolas", 9))
        self.verify_log.pack(fill=tk.BOTH, expand=True)

    # ---------------------- INITIALISATION & PERSISTANCE ----------------------

    def _selected_search_mode(self):
        # type: () -> str
        lbl = self.search_mode_label_var.get()
        if "Rust" in lbl:
            return "rust_layer_dedup"
        if "Couches BFS" in lbl:
            return "layer_dedup"
        return "subset_perms"

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
                "use_cache": bool(self.use_cache_var.get()),
                "search_mode": self._selected_search_mode(),
                "verify_cables": int(self.verify_cables_var.get()),
                "verify_connections": [c.to_dict() for c in self.verify_connections],
            }
            save_settings(data)
        except Exception:
            pass

    # ---------------------- ACTIONS ONGLET RECHERCHE ----------------------

    def _append_search_log(self, text):
        self.search_log.insert(tk.END, text + "\n")
        self.search_log.see(tk.END)

    def clear_search_results(self):
        self.search_log.delete("1.0", tk.END)
        self.sol_listbox.delete(0, tk.END)
        self.found_solutions = []
        self.last_search_result = None
        self.progress_bar["value"] = 0
        self.progress_label_var.set("Effacé — Prêt.")

    def clear_cache_ui(self):
        if messagebox.askyesno("Vider le cache", "Supprimer les calculs enregistrés dans {}/ ?".format(DATA_DIR_NAME)):
            if clear_search_cache():
                self.progress_label_var.set("Cache local vidé.")
            else:
                messagebox.showerror("Erreur", "Impossible de supprimer le fichier de cache.")

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
                search_mode=self._selected_search_mode(),
                use_cache=self.use_cache_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return

        if config.search_mode == "rust_layer_dedup" and not find_rust_solver_binary():
            fallback = messagebox.askyesno(
                "Moteur Rust non trouvé",
                "Le fichier rust_solver.exe n'a pas été trouvé à côté du programme.\n\n"
                "Pour le compiler (une seule fois) :\n"
                "rustc -O -C target-cpu=native rust_solver.rs -o rust_solver.exe\n\n"
                "Voulez-vous lancer cette recherche avec le moteur Python à la place ?",
            )
            if not fallback:
                return
            config = validate_search_config(
                num_cables=config.num_cables,
                start_size=config.start_size,
                max_size=config.max_size,
                max_solutions=config.max_solutions,
                workers=config.workers,
                prune_noops=config.prune_noops,
                search_mode="layer_dedup",
                use_cache=config.use_cache,
            )

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
        self.progress_label_var.set("Démarrage de la recherche...")
        self.poll_id = self.root.after(100, self._poll_search_queue)

    def stop_search(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.stop_btn.config(state=tk.DISABLED)
        self.progress_label_var.set("Arrêt en cours...")

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
                    self.sol_listbox.insert(tk.END, "Solution #{} ({} étapes)".format(idx, len(payload)))
                    self._append_search_log("Solution #{} ({} steps):".format(idx, len(payload)))
                    for step in payload:
                        self._append_search_log("  {}".format(step))
                    if idx == 1:
                        self.sol_listbox.selection_set(0)
                        self._preview_solution(0)
                elif kind == "done":
                    self.last_search_result = payload
                    status_en = "Loaded from cache" if payload.from_cache else ("Cancelled" if payload.cancelled else "Completed")
                    status_fr = "Chargé depuis le cache" if payload.from_cache else ("Annulé" if payload.cancelled else "Terminé")
                    self._append_search_log(
                        "[  End!  ] {} in {} | Solutions: {} | Evaluated: {:,}".format(
                            status_en,
                            format_duration(payload.elapsed_sec),
                            len(payload.solutions),
                            payload.perms_checked,
                        )
                    )
                    self.progress_bar["value"] = 100 if not payload.cancelled else self.progress_bar["value"]
                    self.progress_label_var.set(
                        "{} — {} solution(s) en {} ({:,} tests)".format(
                            status_fr,
                            len(payload.solutions),
                            format_duration(payload.elapsed_sec),
                            payload.perms_checked,
                        )
                    )
                    self._cleanup_search_process()
                    return
                elif kind == "error":
                    self._append_search_log("[ Error ] {}".format(payload))
                    self.progress_label_var.set("Erreur : {}".format(payload))
                    self._cleanup_search_process()
                    return
        except queue.Empty:
            pass

        if self.coord_proc is not None and not self.coord_proc.is_alive():
            self._append_search_log("[ Error ] Search coordinator exited unexpectedly.")
            self.progress_label_var.set("Recherche interrompue de manière inattendue.")
            self._cleanup_search_process()
            return

        self.poll_id = self.root.after(100, self._poll_search_queue)

    def _update_progress_ui(self, prog):
        # type: (SearchProgress) -> None
        pct = (100.0 * prog.subsets_done / max(prog.subsets_total, 1))
        self.progress_bar["value"] = min(100.0, pct)
        speed = prog.perms_checked / max(prog.elapsed_sec, 1e-6)
        self.progress_label_var.set(
            "Taille {} | Éléments {:,}/{:,} ({:.1f}%) | Sols : {} | Tests : {:,} ({:,.0f}/s) | Écoulé : {} | Restant : {}".format(
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
            messagebox.showinfo("Aucune sélection", "Sélectionnez d'abord une solution dans la liste.")
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
        self.progress_label_var.set("Log texte brut copié dans le presse-papiers.")

    def export_search_json_ui(self):
        if not self.found_solutions or self.last_search_config is None:
            messagebox.showinfo("Rien à exporter", "Lancez d'abord une recherche avec au moins une solution.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Fichiers JSON", "*.json"), ("Tous les fichiers", "*.*")],
            title="Exporter les résultats en JSON",
        )
        if not path:
            return
        res = self.last_search_result or SearchResult(
            solutions=tuple(self.found_solutions), cancelled=False, elapsed_sec=0.0, perms_checked=0
        )
        export_solutions_json(path, self.last_search_config, res)
        messagebox.showinfo("Export terminé", "JSON enregistré dans :\n{}".format(path))

    def export_search_csv_ui(self):
        if not self.found_solutions:
            messagebox.showinfo("Rien à exporter", "Lancez d'abord une recherche avec au moins une solution.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")],
            title="Exporter les solutions en CSV",
        )
        if not path:
            return
        export_solutions_csv(path, self.found_solutions)
        messagebox.showinfo("Export terminé", "CSV enregistré dans :\n{}".format(path))

    # ---------------------- ACTIONS ONGLET VÉRIFICATION ----------------------

    def _toggle_mouse_edit_mode(self):
        self.verify_canvas.set_editable(self.mouse_edit_var.get())

    def _on_canvas_add_connection(self, conn):
        # type: (Connection) -> None
        try:
            num_c = int(self.verify_cables_var.get())
            validate_connections(num_c, [conn])
        except ValueError as exc:
            messagebox.showerror("Connexion invalide", str(exc))
            return
        self.verify_connections.append(conn)
        self._sync_verify_views()
        self.run_verify()

    def _on_canvas_remove_connection(self, step_idx):
        # type: (int) -> None
        if 0 <= step_idx < len(self.verify_connections):
            del self.verify_connections[step_idx]
            self._sync_verify_views()
            self.run_verify()

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
        for item in self.steps_tree.get_children():
            self.steps_tree.delete(item)
        for idx, conn in enumerate(self.verify_connections, start=1):
            self.steps_tree.insert(
                "", tk.END, values=(idx, conn.cable1, conn.cable2, conn.upper_logic, conn.lower_logic)
            )
        self.conn_input.delete("1.0", tk.END)
        self.conn_input.insert("1.0", format_connections_plain_text(self.verify_connections))
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
            messagebox.showerror("Étape invalide", str(exc))
            return
        self.verify_connections.append(conn)
        self._sync_verify_views()

    def update_selected_step(self):
        sel = self.steps_tree.selection()
        if not sel:
            messagebox.showinfo("Sélection requise", "Sélectionnez une ligne dans le tableau pour la modifier.")
            return
        idx = self.steps_tree.index(sel[0])
        try:
            conn = self._read_step_inputs()
        except ValueError as exc:
            messagebox.showerror("Étape invalide", str(exc))
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
            messagebox.showerror("Erreur de lecture / validation", str(exc))

    def run_verify(self):
        try:
            num_c = int(self.verify_cables_var.get())
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
            messagebox.showerror("Erreur de vérification", str(exc))
            return

        total = len(summary.truth_table)
        fail_count = len(summary.failing_indices)
        if summary.is_reliable:
            self.verify_banner.configure(style="BannerOK.TLabel")
            self.verify_banner_var.set(
                "FIABLE (cablage fiable) — Les {:,}/{:,} lignes sont triées par ordre décroissant.".format(total, total)
            )
        else:
            self.verify_banner.configure(style="BannerFail.TLabel")
            self.verify_banner_var.set(
                "NON FIABLE (cablage pas fiable) — {:,}/{:,} lignes en échec.".format(fail_count, total)
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

        max_rows = 2048
        shown = 0
        for idx, (inp, out) in enumerate(self.last_verify_summary.truth_table):
            is_fail = idx in failing_set
            if only_fail and not is_fail:
                continue
            tag = "fail_row" if is_fail else "pass_row"
            status = "ÉCHEC" if is_fail else "OK"
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
            messagebox.showinfo("Aucun échec", "Le câblage actuel n'a aucune ligne en échec (ou n'a pas encore été vérifié).")
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
        messagebox.showinfo("Copié", "Table de vérité texte brut copiée dans le presse-papiers.")

    def export_tt_csv_ui(self):
        if self.last_verify_summary is None:
            messagebox.showinfo("Rien à exporter", "Lancez d'abord une vérification.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")],
            title="Exporter la table de vérité en CSV",
        )
        if not path:
            return
        export_truth_table_csv(path, self.last_verify_summary)
        messagebox.showinfo("Export terminé", "Table de vérité CSV enregistrée dans :\n{}".format(path))

    def export_wiring_json_ui(self):
        try:
            num_c = int(self.verify_cables_var.get())
            validated = validate_connections(num_c, self.verify_connections)
        except ValueError as exc:
            messagebox.showerror("Export impossible", str(exc))
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Fichiers JSON", "*.json"), ("Tous les fichiers", "*.*")],
            title="Exporter le câblage en JSON",
        )
        if not path:
            return
        export_wiring_json(path, num_c, validated)
        messagebox.showinfo("Export terminé", "Configuration JSON enregistrée dans :\n{}".format(path))

    def import_wiring_json_ui(self):
        path = filedialog.askopenfilename(
            filetypes=[("Fichiers JSON", "*.json"), ("Tous les fichiers", "*.*")],
            title="Importer un câblage JSON",
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
            messagebox.showerror("Erreur d'import", str(exc))

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
