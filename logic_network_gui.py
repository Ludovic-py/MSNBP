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
# API HTTP locale (contrôle à distance par l'agent AI)
# ---------------------------------------------------------------------------

import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

API_PORT = 39227
API_TOKEN = "dsh-lnetwork-2024"  # Token simple pour éviter les accès sauvages

_httpd_instance = None  # type: Optional[socketserver.TCPServer]
_app_instance = None    # type: Optional["LogicNetworkApp"]

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
    prefix_connections: Optional[Tuple[Tuple[int, int, str, str], ...]] = None
    foata_cut: bool = True
    final_span_1: bool = True

    def cache_key(self):
        # type: () -> str
        prefix_part = ""
        if self.prefix_connections:
            prefix_part = "|prefix={}".format(len(self.prefix_connections))
        return "n={}|mode={}|start={}|max={}|prune={}{}|foata={}|span1={}".format(
            self.num_cables,
            self.search_mode,
            self.start_size,
            self.max_size,
            int(self.prune_noops),
            prefix_part,
            int(self.foata_cut),
            int(self.final_span_1),
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
    prefix_connections=None,
    foata_cut=True,
    final_span_1=True,
):
    # type: (int, int, Optional[int], int, int, bool, str, bool, Optional[Sequence[Union[Connection, Tuple[int, int, str, str]]]], bool, bool) -> SearchConfig
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

    prefix_normalized = None
    if prefix_connections is not None:
        try:
            parsed_prefix = normalize_connections(prefix_connections)
            # Valider les indices par rapport au nombre de câbles
            for conn in parsed_prefix:
                if not (0 <= conn.cable1 < num_cables):
                    errors.append("Prefix: cable1 {} hors bornes [0, {}].".format(conn.cable1, num_cables - 1))
                if not (0 <= conn.cable2 < num_cables):
                    errors.append("Prefix: cable2 {} hors bornes [0, {}].".format(conn.cable2, num_cables - 1))
                if conn.upper_logic not in ("OR",) or conn.lower_logic not in ("AND",):
                    errors.append("Prefix: seules les portes OR/AND sont supportées dans le préfixe.")
            if not errors:
                prefix_normalized = tuple(conn.to_tuple() for conn in parsed_prefix)
        except WiringValidationError as exc:
            errors.extend(exc.errors)

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
        prefix_connections=prefix_normalized,
        foata_cut=bool(foata_cut),
        final_span_1=bool(final_span_1),
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
    init_masks = list(_initial_wire_masks(config.num_cables))
    all_ones_mask = (1 << (1 << config.num_cables)) - 1

    # Appliquer le préfixe optionnel
    prefix_conns = config.prefix_connections
    prefix_seq = ()  # type: Tuple[Tuple[int, int, str, str], ...]
    state_list = list(init_masks)
    if prefix_conns:
        msg_queue.put(("log", "[ Prefix ] Applying {} prefix gate(s)...".format(len(prefix_conns))))
        for step in prefix_conns:
            c1, c2, uf, lf = step
            m1 = state_list[c1]
            m2 = state_list[c2]
            state_list[c1] = _apply_logic_mask(m1, m2, uf, all_ones_mask)
            state_list[c2] = _apply_logic_mask(m1, m2, lf, all_ones_mask)
            prefix_seq = prefix_seq + (step,)
        msg_queue.put(("log", "[ Prefix ] Initial state after prefix: {}".format(state_list)))

    init_state = tuple(state_list)

    frontier = {init_state: prefix_seq}  # type: Dict[Tuple[int, ...], Tuple[Tuple[int, int, str, str], ...]]
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

    # Le préfixe est appliqué à l'état racine par le Rust : il ne compte donc pas
    # dans la profondeur BFS. On ajuste les bornes pour que la taille TOTALE du
    # réseau (préfixe inclus) reste start_size..max_size.
    prefix_conns = list(config.prefix_connections or ())
    prefix_len = len(prefix_conns)
    prefix_steps = tuple((int(c[0]), int(c[1]), "OR", "AND") for c in prefix_conns)
    adjusted_start = max(1, config.start_size - prefix_len)
    adjusted_max = config.max_size - prefix_len
    if adjusted_max < 1:
        adjusted_max = 1

    cmd = [
        binary_path,
        "--cables", str(config.num_cables),
        "--start-size", str(adjusted_start),
        "--max-size", str(adjusted_max),
        "--max-solutions", str(config.max_solutions),
        "--workers", str(config.workers),
    ]
    if not config.prune_noops:
        cmd.append("--no-prune")
    if prefix_steps:
        flat = []
        for step in prefix_steps:
            flat.append(str(step[0]))
            flat.append(str(step[1]))
        cmd.append("--prefix")
        cmd.append(",".join(flat))
    if not config.foata_cut:
        cmd.append("--no-foata")
    if not config.final_span_1:
        cmd.append("--no-final-span-1")

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
                    # Le Rust n'émet que les portes ajoutées par le BFS :
                    # on préfixe pour reconstituer le réseau complet.
                    sol_tuple = tuple(prefix_steps) + tuple(steps)
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
            # Appliquer le préfixe optionnel avant le BFS
            # On crée une config modifiée avec le start_size ajusté
            prefix_conns = config.prefix_connections
            if prefix_conns:
                adjusted = SearchConfig(
                    num_cables=config.num_cables,
                    start_size=max(1, config.start_size - len(prefix_conns)),
                    max_size=max(1, config.max_size - len(prefix_conns)),
                    max_solutions=config.max_solutions,
                    workers=config.workers,
                    prune_noops=config.prune_noops,
                    search_mode=config.search_mode,
                    use_cache=config.use_cache,
                    prefix_connections=prefix_conns,
                    foata_cut=config.foata_cut,
                    final_span_1=config.final_span_1,
                )
                res = _run_layer_dedup_search(adjusted, msg_queue, stop_event)
            else:
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


# ---------------------------------------------------------------------------
# Export PNG presse-papiers & Analyse de similarité
# ---------------------------------------------------------------------------

def copy_canvas_png_to_clipboard(root, canvas_widget, status_callback=None):
    # type: (tk.Tk, tk.Canvas, Optional[Any]) -> bool
    """
    Capture un widget tk.Canvas en image PNG et le copie dans le presse-papiers
    Windows (via Pillow ImageGrab + PowerShell .NET Clipboard.SetImage).
    """
    try:
        root.update_idletasks()
        x1 = canvas_widget.winfo_rootx()
        y1 = canvas_widget.winfo_rooty()
        x2 = x1 + canvas_widget.winfo_width()
        y2 = y1 + canvas_widget.winfo_height()
        if x2 <= x1 or y2 <= y1:
            raise RuntimeError("Zone du schéma vide ou non affichée.")

        tmp_dir = get_data_dir(create=True) or os.environ.get("TEMP", ".")
        png_path = os.path.join(tmp_dir, "_schema_clipboard.png")

        pil_ok = False
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
            img.save(png_path, format="PNG")
            pil_ok = True
        except Exception:
            pil_ok = False

        if not pil_ok:
            raise RuntimeError(
                "La capture PNG nécessite Pillow (PIL.ImageGrab) pour enregistrer le PNG."
            )

        if os.name == "nt":
            escaped = png_path.replace("'", "''")
            ps_cmd = (
                "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
                "$img = [System.Drawing.Image]::FromFile('{0}'); "
                "[System.Windows.Forms.Clipboard]::SetImage($img); "
                "$img.Dispose()".format(escaped)
            )
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=creationflags,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "Échec de copie dans le presse-papiers Windows.")
        else:
            raise RuntimeError("Copie d'image PNG dans le presse-papiers implémentée pour Windows.")

        if callable(status_callback):
            status_callback("Schéma PNG copié dans le presse-papiers.")
        return True
    except Exception as exc:
        messagebox.showerror("Export PNG impossible", str(exc))
        return False


def parse_solutions_collection_file(filepath):
    # type: (str) -> Tuple[Optional[int], List[Tuple[Tuple[int, int, str, str], ...]]]
    """
    Charge un fichier JSON (export de recherche ou liste de solutions) ou CSV
    (export_solutions_csv) et retourne (num_cables_ou_None, liste_de_solutions).
    """
    with open(filepath, "r", encoding="utf-8") as fh:
        raw = fh.read()

    stripped = raw.strip()
    if not stripped:
        return (None, [])

    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(stripped)
        num_cables = None
        raw_sols = []
        if isinstance(data, dict):
            cfg = data.get("search_config")
            if isinstance(cfg, dict) and isinstance(cfg.get("num_cables"), int):
                num_cables = cfg["num_cables"]
            raw_sols = data.get("solutions", [])
        elif isinstance(data, list):
            raw_sols = data

        parsed_sols = []
        for sol in raw_sols:
            if not isinstance(sol, list):
                continue
            steps = []
            for item in sol:
                if isinstance(item, dict):
                    conn = Connection.from_dict(item)
                else:
                    conn = Connection.from_tuple(item)
                steps.append(conn.to_tuple())
            if steps:
                if num_cables is None:
                    max_c = max(max(s[0], s[1]) for s in steps)
                    num_cables = max_c + 1
                parsed_sols.append(tuple(steps))
        return (num_cables, parsed_sols)

    grouped = {}  # type: Dict[int, List[Tuple[int, int, str, str]]]
    order = []  # type: List[int]
    max_c = 1
    with open(filepath, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                s_idx = int(row["solution_index"])
                c1 = int(row["cable1"])
                c2 = int(row["cable2"])
                up = row.get("upper_logic", "OR").strip().upper()
                low = row.get("lower_logic", "AND").strip().upper()
                max_c = max(max_c, c1, c2)
                if s_idx not in grouped:
                    grouped[s_idx] = []
                    order.append(s_idx)
                grouped[s_idx].append((c1, c2, up, low))
            except Exception:
                continue

    sols = [tuple(grouped[k]) for k in order if grouped[k]]
    return ((max_c + 1) if sols else None, sols)


def compute_solutions_similarity_report(solutions, ref_idx=0):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]], int) -> Dict[str, Any]
    """
    Calcule des statistiques de similitude sur un ensemble de solutions :
      - fréquence globale des paires (c1, c2)
      - matrice positionnelle [étape][paire]
      - similarité de chaque solution par rapport à la solution de référence
        (Jaccard sur l'ensemble des paires + correspondance exacte au même rang)
      - bigrammes consécutifs de paires les plus fréquents
    """
    total_sols = len(solutions)
    if total_sols == 0:
        return {
            "total": 0,
            "pair_counts": {},
            "step_pair_counts": [],
            "bigram_counts": {},
            "similarities": [],
        }

    pair_counts = {}  # type: Dict[Tuple[int, int], int]
    step_pair_counts = []  # type: List[Dict[Tuple[int, int], int]]
    bigram_counts = {}  # type: Dict[Tuple[Tuple[int, int], Tuple[int, int]], int]

    for sol in solutions:
        for step_i, step in enumerate(sol):
            pair = (step[0], step[1])
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
            while len(step_pair_counts) <= step_i:
                step_pair_counts.append({})
            step_pair_counts[step_i][pair] = step_pair_counts[step_i].get(pair, 0) + 1
            if step_i + 1 < len(sol):
                next_pair = (sol[step_i + 1][0], sol[step_i + 1][1])
                bg = (pair, next_pair)
                bigram_counts[bg] = bigram_counts.get(bg, 0) + 1

    safe_ref = max(0, min(ref_idx, total_sols - 1))
    ref_sol = solutions[safe_ref]
    ref_pairs_set = set((s[0], s[1]) for s in ref_sol)

    sims = []
    for idx, sol in enumerate(solutions):
        cur_pairs_list = [(s[0], s[1]) for s in sol]
        cur_pairs_set = set(cur_pairs_list)
        union_sz = len(ref_pairs_set | cur_pairs_set)
        jaccard = (len(ref_pairs_set & cur_pairs_set) / float(union_sz)) if union_sz > 0 else 1.0

        min_len = min(len(ref_sol), len(sol))
        max_len = max(len(ref_sol), len(sol))
        same_pos = 0
        for k in range(min_len):
            if (ref_sol[k][0], ref_sol[k][1]) == (sol[k][0], sol[k][1]):
                same_pos += 1
        pos_ratio = (same_pos / float(max_len)) if max_len > 0 else 1.0
        sims.append((idx, jaccard, same_pos, max_len, pos_ratio))

    sims.sort(key=lambda item: (-item[1], -item[4], item[0]))

    return {
        "total": total_sols,
        "pair_counts": pair_counts,
        "step_pair_counts": step_pair_counts,
        "bigram_counts": bigram_counts,
        "similarities": sims,
    }




def compute_last_gate_distribution(solutions):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]]) -> Dict[str, Any]
    """Distribution de la dernière porte E_K : paires et écarts."""
    if not solutions:
        return {"last_pair_counts": {}, "last_span_counts": {}, "total": 0}
    pair_counts = {}  # type: Dict[Tuple[int, int], int]
    span_counts = {}  # type: Dict[int, int]
    for sol in solutions:
        if not sol:
            continue
        c1, c2 = sol[-1][0], sol[-1][1]
        pair = (c1, c2)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        span = abs(c2 - c1)
        span_counts[span] = span_counts.get(span, 0) + 1
    return {"last_pair_counts": pair_counts, "last_span_counts": span_counts, "total": len(solutions)}


def compute_first_layer_patterns(solutions, num_cables):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]], int) -> Dict[str, Any]
    """Analyse de la première couche L1 : patterns d'ouverture."""
    if not solutions:
        return {"l1_patterns": {}, "l1_gate_counts": {}, "max_disjoint_l1": 0}
    l1_patterns = {}  # type: Dict[str, int]
    l1_gate_counts = {}  # type: Dict[Tuple[int, int], int]
    max_disjoint = 0
    for sol in solutions:
        if not sol:
            continue
        g1 = sol[0]
        l1_gate_counts[(g1[0], g1[1])] = l1_gate_counts.get((g1[0], g1[1]), 0) + 1
        # Collecter les portes disjointes de la première couche
        used = {g1[0], g1[1]}
        l1_set = [(g1[0], g1[1])]
        for step in sol[1:]:
            c1, c2 = step[0], step[1]
            if c1 not in used and c2 not in used:
                used.add(c1)
                used.add(c2)
                l1_set.append((c1, c2))
            else:
                break
        pattern = ";".join("({},{})".format(k, v) for k, v in l1_set)
        l1_patterns[pattern] = l1_patterns.get(pattern, 0) + 1
        max_disjoint = max(max_disjoint, len(l1_set))
    return {"l1_patterns": l1_patterns, "l1_gate_counts": l1_gate_counts, "max_disjoint_l1": max_disjoint}


def compute_parallel_depth(solutions):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]]) -> Dict[str, Any]
    """Calcule la profondeur parallèle ASAP (As Soon As Possible)."""
    if not solutions:
        return {"max_parallel_depth": 0, "depth_distribution": {}}
    depth_dist = {}  # type: Dict[int, int]
    max_depth = 0
    for sol in solutions:
        if not sol:
            continue
        # Regroupement glouton par couche parallèle
        used = set()
        depth = 0
        for step in sol:
            c1, c2 = step[0], step[1]
            if c1 in used or c2 in used:
                depth += 1
                used = {c1, c2}
            else:
                used.add(c1)
                used.add(c2)
        if used:
            depth += 1
        depth_dist[depth] = depth_dist.get(depth, 0) + 1
        max_depth = max(max_depth, depth)
    return {"max_parallel_depth": max_depth, "depth_distribution": depth_dist}


def compute_symmetry_report(solutions):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]]) -> Dict[str, Any]
    """Détecte les symétries par réflexion haut/bas."""
    if not solutions:
        return {"symmetric_count": 0, "total": 0}
    sym_count = 0
    for sol in solutions:
        max_c = max(max(s[0], s[1]) for s in sol)
        # Réflexion : remplacer chaque (c1, c2) par (max_c-c1, max_c-c2) et trier
        reflected = tuple(sorted(
            ((max_c - s[0], max_c - s[1], s[2], s[3]) for s in sol),
            key=lambda x: (x[0], x[1])
        ))
        original = tuple(sol)
        if reflected == original:
            sym_count += 1
    return {"symmetric_count": sym_count, "total": len(solutions)}


def compute_rich_network_analytics(solutions, num_cables, ref_idx=0):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]], int, int) -> Dict[str, Any]
    """
    Calcule des métriques exploitables sur un ensemble de solutions :
      - heatmap[step_idx][(c1, c2)] -> count
      - activité par câble C0..C(n-1) (nombre de fois où chaque câble est touché)
      - répartition OR/AND (upper_logic / lower_logic)
      - solutions "noyau / centrales" (plus forte similarité moyenne Jaccard)
      - solutions "atypiques / rares" (plus faible similarité Jaccard)
      - NOW: dernière porte E_K, première couche L1, profondeur parallèle, symétries
    """
    total = len(solutions)
    if total == 0:
        return {
            "total": 0,
            "max_steps": 0,
            "heatmap": [],
            "cable_activity": [0] * max(2, num_cables),
            "logic_counts": {},
            "central_sols": [],
            "outlier_sols": [],
            "pair_totals": {},
            "last_gate": {},
            "first_layer": {},
            "parallel_depth": {},
            "symmetry": {},
        }

    max_steps = max(len(s) for s in solutions)
    heatmap = [{} for _ in range(max_steps)]  # type: List[Dict[Tuple[int, int], int]]
    pair_totals = {}  # type: Dict[Tuple[int, int], int]
    cable_activity = [0] * max(2, num_cables)
    logic_counts = {}  # type: Dict[str, int]

    pair_sets = []
    for sol in solutions:
        pset = set()
        for step_i, (c1, c2, up_f, low_f) in enumerate(sol):
            pair = (c1, c2)
            pset.add(pair)
            pair_totals[pair] = pair_totals.get(pair, 0) + 1
            heatmap[step_i][pair] = heatmap[step_i].get(pair, 0) + 1
            if 0 <= c1 < len(cable_activity):
                cable_activity[c1] += 1
            if 0 <= c2 < len(cable_activity):
                cable_activity[c2] += 1
            logic_counts[up_f] = logic_counts.get(up_f, 0) + 1
            logic_counts[low_f] = logic_counts.get(low_f, 0) + 1
        pair_sets.append(pset)

    sample_indices = list(range(total))
    if total > 250:
        step_stride = total / 250.0
        sample_indices = [int(i * step_stride) for i in range(250)]

    scored = []
    for i in sample_indices:
        si = pair_sets[i]
        acc = 0.0
        denom = 0
        for j in sample_indices:
            if i == j:
                continue
            sj = pair_sets[j]
            u = len(si | sj)
            acc += (len(si & sj) / float(u)) if u > 0 else 1.0
            denom += 1
        avg_sim = (acc / float(denom)) if denom > 0 else 1.0
        scored.append((avg_sim, i))

    scored_by_central = sorted(scored, key=lambda item: (-item[0], item[1]))
    scored_by_outlier = sorted(scored, key=lambda item: (item[0], item[1]))

    # Nouvelles métriques
    last_gate = compute_last_gate_distribution(solutions)
    first_layer = compute_first_layer_patterns(solutions, num_cables)
    parallel_depth = compute_parallel_depth(solutions)
    symmetry = compute_symmetry_report(solutions)

    return {
        "total": total,
        "max_steps": max_steps,
        "heatmap": heatmap,
        "cable_activity": cable_activity,
        "logic_counts": logic_counts,
        "central_sols": scored_by_central[:8],
        "outlier_sols": scored_by_outlier[:8],
        "pair_totals": pair_totals,
        "last_gate": last_gate,
        "first_layer": first_layer,
        "parallel_depth": parallel_depth,
        "symmetry": symmetry,
    }


class CollapsibleSection(ttk.Frame):
    """
    Conteneur repliable/minimisable avec barre de titre compacte (▾ / ▸).
    Permet d'économiser de la hauteur et de réorganiser l'espace dans chaque onglet.
    """

    def __init__(self, parent, title="", initially_open=True, show_header=True, padding=4):
        super(CollapsibleSection, self).__init__(parent)
        self.title_text = title
        self.is_open = bool(initially_open)
        self.show_header = bool(show_header)

        if self.show_header:
            header = ttk.Frame(self)
            header.pack(fill=tk.X, pady=(0, 2))
            self._toggle_btn = ttk.Button(
                header,
                text=("▾ " + title) if self.is_open else ("▸ " + title),
                command=self.toggle,
            )
            self._toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        else:
            self._toggle_btn = None

        self.body = ttk.Frame(self, padding=padding)
        if self.is_open:
            self.body.pack(fill=tk.BOTH, expand=True)

    def toggle(self):
        self.is_open = not self.is_open
        if self._toggle_btn is not None:
            self._toggle_btn.config(
                text=("▾ " + self.title_text) if self.is_open else ("▸ " + self.title_text)
            )
        if self.is_open:
            self.body.pack(fill=tk.BOTH, expand=True)
        else:
            self.body.pack_forget()


def _is_subsequence_pairs(sub_seq, full_seq):
    # type: (Sequence[Tuple[int, int]], Sequence[Tuple[int, int]]) -> bool
    """Vérifie si `sub_seq` apparaît comme sous-séquence ordonnée dans `full_seq`."""
    if not sub_seq:
        return True
    it = iter(full_seq)
    return all(item in it for item in sub_seq)


def compute_cross_n_similarity_report(sols_main, n_main, sols_other, n_other):
    # type: (Sequence[Sequence[Tuple[int, int, str, str]]], int, Sequence[Sequence[Tuple[int, int, str, str]]], int) -> Dict[str, Any]
    """
    Croise deux collections d'optimaux (par ex. n=6 vs n=5) :
      - fréquence relative des écarts |c2 - c1| (span des comparateurs)
      - fréquence relative des paires normalisées (c1/(N-1), c2/(N-1))
      - recherche de plongements / sous-réseaux : si n_other < n_main, compte combien
        de solutions de n_main contiennent une solution optimale de n_other comme
        sous-séquence ordonnée (directe ou décalée sur les câbles +offset, ou via
        projection sur un sous-ensemble de n_other câbles).
    """
    main_pairs_seq = [[(s[0], s[1]) for s in sol] for sol in sols_main]
    other_pairs_seq = [[(s[0], s[1]) for s in sol] for sol in sols_other]

    def span_dist(seqs, n_val):
        counts = {}  # type: Dict[int, int]
        tot = 0
        for sq in seqs:
            for c1, c2 in sq:
                d = abs(c2 - c1)
                counts[d] = counts.get(d, 0) + 1
                tot += 1
        return counts, tot

    span_main, tot_main = span_dist(main_pairs_seq, n_main)
    span_other, tot_other = span_dist(other_pairs_seq, n_other)

    # Paires absolues communes
    set_main = set(p for sq in main_pairs_seq for p in sq)
    set_other = set(p for sq in other_pairs_seq for p in sq)
    common_abs_pairs = sorted(set_main & set_other)

    # Recherche de plongement si n_main != n_other
    big_seqs, big_n = (main_pairs_seq, n_main) if n_main >= n_other else (other_pairs_seq, n_other)
    small_seqs, small_n = (other_pairs_seq, n_other) if n_main >= n_other else (main_pairs_seq, n_main)

    embedded_matches = 0
    matched_small_indices = set()
    sample_embeddings = []  # type: List[Tuple[int, int, int]]

    if big_n > small_n and small_seqs and big_seqs:
        max_shift = big_n - small_n
        shifted_small = []
        for s_idx, sq in enumerate(small_seqs):
            variants = []
            for shift in range(max_shift + 1):
                variants.append((shift, tuple((c1 + shift, c2 + shift) for (c1, c2) in sq)))
            shifted_small.append((s_idx, variants))

        for b_idx, b_sq in enumerate(big_seqs):
            found_for_b = False
            for s_idx, variants in shifted_small:
                for shift, v_sq in variants:
                    if _is_subsequence_pairs(v_sq, b_sq):
                        found_for_b = True
                        matched_small_indices.add(s_idx)
                        if len(sample_embeddings) < 12:
                            sample_embeddings.append((b_idx, s_idx, shift))
                        break
                if found_for_b:
                    break
            if found_for_b:
                embedded_matches += 1

    # Top paires les plus fréquentes dans each collection
    freq_main = {}  # type: Dict[Tuple[int, int], int]
    for sq in main_pairs_seq:
        for p in sq:
            freq_main[p] = freq_main.get(p, 0) + 1

    freq_other = {}  # type: Dict[Tuple[int, int], int]
    for sq in other_pairs_seq:
        for p in sq:
            freq_other[p] = freq_other.get(p, 0) + 1

    return {
        "n_main": n_main,
        "count_main": len(sols_main),
        "tot_gates_main": tot_main,
        "span_main": span_main,
        "freq_main": freq_main,
        "n_other": n_other,
        "count_other": len(sols_other),
        "tot_gates_other": tot_other,
        "span_other": span_other,
        "freq_other": freq_other,
        "common_abs_pairs": common_abs_pairs,
        "big_n": big_n,
        "small_n": small_n,
        "big_count": len(big_seqs),
        "small_count": len(small_seqs),
        "embedded_matches": embedded_matches,
        "matched_small_count": len(matched_small_indices),
        "sample_embeddings": sample_embeddings,
    }


class WiringCanvas(ttk.Frame):
    """
    Schéma visuel d'un réseau de câbles booléens avec pastilles 0/1 cliquables,
    propagation en direct, mise en évidence des portes sans effet (no-op) sur l'entrée
    courante, et édition à la souris avec aperçu d'insertion à l'endroit cliqué.
    """

    def __init__(
        self,
        parent,
        interactive=True,
        height=230,
        editable=False,
        highlight_noops=True,
        on_add_connection=None,
        on_remove_connection=None,
        get_active_gates=None,
    ):
        super(WiringCanvas, self).__init__(parent)
        self.interactive = interactive
        self.editable = editable
        self.highlight_noops = highlight_noops
        self.on_add_connection = on_add_connection
        self.on_remove_connection = on_remove_connection
        self.get_active_gates = get_active_gates

        self.num_cables = 4
        self.connections = []  # type: List[Connection]
        self.input_bits = [0, 1, 0, 1]
        self.selected_cable = None  # type: Optional[int]
        self.hover_insert_idx = None  # type: Optional[int]
        self.diff_steps = set()  # type: set
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
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)
        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

    def set_editable(self, enabled):
        # type: (bool) -> None
        self.editable = bool(enabled)
        self.selected_cable = None
        if not self.editable:
            self.hover_insert_idx = None
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
        if self.hover_insert_idx is not None:
            self.hover_insert_idx = min(self.hover_insert_idx, len(self.connections))

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

    def set_diff_steps(self, diff_indices):
        # type: (Any) -> None
        self.diff_steps = set(diff_indices or ())
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

    def _compute_insert_index(self, cx):
        # type: (float) -> int
        """
        Détermine l'index d'insertion dans `self.connections` à partir de la
        coordonnée horizontale `cx` sur le canevas.
        Exemple : un clic entre E2 et E3 renvoie l'index 2 (avant l'ancien E3).
        """
        steps = len(self.connections)
        if steps == 0 or not self._step_xs:
            return 0
        if cx < self._step_xs[0]:
            return 0
        for idx in range(1, steps):
            mid = 0.5 * (self._step_xs[idx - 1] + self._step_xs[idx])
            if cx < mid:
                return idx
        return steps

    def _insert_guide_x(self, insert_idx):
        # type: (int) -> float
        steps = len(self.connections)
        if steps == 0 or not self._step_xs:
            return self._x_start + 39.0
        if insert_idx <= 0:
            return max(self._x_start - 6.0, (self._x_start - 12.0 + self._step_xs[0]) * 0.5)
        if insert_idx >= steps:
            return self._step_xs[-1] + 39.0
        return 0.5 * (self._step_xs[insert_idx - 1] + self._step_xs[insert_idx])

    def _on_mouse_move(self, event):
        if not self.editable:
            if self.hover_insert_idx is not None:
                self.hover_insert_idx = None
                self.redraw()
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        if cx < self._x_start - 16 or cy < 24:
            if self.hover_insert_idx is not None:
                self.hover_insert_idx = None
                self.redraw()
            return
        new_idx = self._compute_insert_index(cx)
        if new_idx != self.hover_insert_idx:
            self.hover_insert_idx = new_idx
            self.redraw()

    def _on_mouse_leave(self, _event):
        if self.hover_insert_idx is not None:
            self.hover_insert_idx = None
            self.redraw()

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

        insert_idx = self._compute_insert_index(cx)

        if self.selected_cable is None:
            self.selected_cable = cable_idx
            self.hover_insert_idx = insert_idx
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
                self.on_add_connection(Connection(c1, c2, up_f, low_f), insert_idx)

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
                pos_txt = (
                    "avant E{}".format(self.hover_insert_idx + 1)
                    if self.hover_insert_idx is not None and self.hover_insert_idx < steps
                    else "en fin de chaîne"
                )
                hint = "C{} sélectionné -> Cliquez un autre câble pour insérer {} (Clic droit : annuler)".format(
                    self.selected_cable, pos_txt
                )
            else:
                hint = "Édition souris ACTIVE : Cliquez 2 câbles pour insérer un lien à la position indiquée | Clic droit : supprimer"
            self.canvas.create_text(
                12,
                12,
                text=hint,
                anchor="w",
                fill="#FBBF24",
                font=("Segoe UI", 8, "bold"),
            )

        # Aperçu vertical en pointillés de la position d'insertion au survol
        if self.editable and self.hover_insert_idx is not None:
            gx = self._insert_guide_x(self.hover_insert_idx)
            top_y = self._cable_ys[0] - 18
            bot_y = self._cable_ys[-1] + 18
            self.canvas.create_line(
                gx, top_y, gx, bot_y, fill="#FBBF24", width=2, dash=(4, 3)
            )
            slot_lbl = (
                "→E{}".format(self.hover_insert_idx + 1)
                if self.hover_insert_idx < steps
                else "→Fin"
            )
            self.canvas.create_text(
                gx, top_y - 6, text=slot_lbl, fill="#FDE68A", font=("Consolas", 7, "bold")
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

        diff_steps = getattr(self, "diff_steps", set())
        for s_idx, conn in enumerate(self.connections):
            sx = self._step_xs[s_idx]
            y1 = self._cable_ys[conn.cable1]
            y2 = self._cable_ys[conn.cable2]

            is_diff = s_idx in diff_steps
            if is_diff:
                self.canvas.create_rectangle(
                    sx - 26,
                    min(y1, y2) - 16,
                    sx + 26,
                    max(y1, y2) + 16,
                    outline="#F59E0B",
                    width=2,
                    dash=(4, 2),
                )

            # Vérifie si la porte est sans effet (no-op) sur l'entrée courante
            is_noop = False
            if self.highlight_noops and s_idx + 1 < len(history):
                prev_state = history[s_idx]
                next_state = history[s_idx + 1]
                is_noop = (
                    next_state[conn.cable1] == prev_state[conn.cable1]
                    and next_state[conn.cable2] == prev_state[conn.cable2]
                )

            link_color = "#FBBF24" if is_diff else ("#475569" if is_noop else "#94A3B8")
            link_dash = (3, 3) if is_noop else ()
            self.canvas.create_line(
                sx, y1, sx, y2, fill=link_color, width=(3 if is_diff else 2), dash=link_dash
            )

            u_color = "#334155" if is_noop else GATE_COLORS.get(conn.upper_logic, "#2563EB")
            u_outline = "#64748B" if is_noop else "#F8FAFC"
            u_text_fill = "#94A3B8" if is_noop else "#FFFFFF"
            self.canvas.create_rectangle(
                sx - 20, y1 - 10, sx + 20, y1 + 10,
                fill=u_color, outline=u_outline, width=1, dash=link_dash
            )
            self.canvas.create_text(
                sx, y1, text=conn.upper_logic, fill=u_text_fill, font=("Consolas", 7, "bold")
            )

            l_color = "#334155" if is_noop else GATE_COLORS.get(conn.lower_logic, "#7C3AED")
            l_outline = "#64748B" if is_noop else "#F8FAFC"
            l_text_fill = "#94A3B8" if is_noop else "#FFFFFF"
            self.canvas.create_rectangle(
                sx - 20, y2 - 10, sx + 20, y2 + 10,
                fill=l_color, outline=l_outline, width=1, dash=link_dash
            )
            self.canvas.create_text(
                sx, y2, text=conn.lower_logic, fill=l_text_fill, font=("Consolas", 7, "bold")
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
        self._build_analysis_tab()

    # ---------------------- PONT THREAD UI (pour l'API HTTP) ----------------------

    def _ui_call(self, fn, timeout=8.0):
        """
        Exécute `fn` dans le thread Tkinter principal et renvoie son résultat.
        Indispensable : Tkinter n'est pas thread-safe, et l'API HTTP tourne
        dans un thread secondaire.
        """
        if threading.current_thread() is threading.main_thread():
            return fn()
        box = {}
        done = threading.Event()

        def runner():
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - on renvoie l'erreur au thread appelant
                box["error"] = exc
            finally:
                done.set()

        try:
            self.root.after(0, runner)
        except Exception as exc:
            raise RuntimeError("UI thread indisponible: {}".format(exc))
        if not done.wait(timeout):
            raise TimeoutError("Appel UI expiré (timeout {}s)".format(timeout))
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ---------------------- ONGLET 1 : MSO ----------------------

    def _build_search_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="MSO")

        params_sec = CollapsibleSection(tab, title="Paramètres", initially_open=True, show_header=True, padding=4)
        params_sec.pack(fill=tk.X, pady=(0, 4))
        params = params_sec.body

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

        ttk.Label(params, text="Workers :").grid(
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

        # Options avancées supplémentaires
        adv_row = ttk.Frame(params)
        adv_row.grid(row=2, column=0, columnspan=10, sticky=tk.W, pady=(6, 0))
        self.prefix_var = tk.StringVar(value=self.settings.get("search_prefix", ""))
        ttk.Label(adv_row, text="Préfixe (L1/L2) :").pack(side=tk.LEFT, padx=(0, 4))
        self.prefix_text = scrolledtext.ScrolledText(adv_row, wrap=tk.WORD, font=("Consolas", 8), height=3, width=60)
        self.prefix_text.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(adv_row, text="ex: (0,1,'OR','AND'),\n(2,3),(4,5)", foreground="#475569").pack(side=tk.LEFT, padx=(0, 4))

        opt_row = ttk.Frame(params)
        opt_row.grid(row=3, column=0, columnspan=10, sticky=tk.W, pady=(2, 0))
        self.foata_var = tk.BooleanVar(value=bool(self.settings.get("foata_cut", True)))
        ttk.Checkbutton(opt_row, text="Coupe de commutation Foata", variable=self.foata_var).pack(side=tk.LEFT, padx=(0, 8))
        self.final_span1_var = tk.BooleanVar(value=bool(self.settings.get("final_span_1", True)))
        ttk.Checkbutton(opt_row, text="Dernière couche écart=1 uniquement", variable=self.final_span1_var).pack(side=tk.LEFT, padx=(0, 8))

        btns = ttk.Frame(tab)
        btns.pack(fill=tk.X, pady=(4, 4))

        self.start_btn = ttk.Button(btns, text="Lancer la recherche", command=self.start_search)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = ttk.Button(btns, text="Arrêter", command=self.stop_search, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(btns, text="Copier le log texte", command=self.copy_search_log).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Exporter JSON", command=self.export_search_json_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Exporter CSV", command=self.export_search_csv_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Vider le cache", command=self.clear_cache_ui).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Effacer", command=self.clear_search_results).pack(side=tk.RIGHT)

        prog_sec = CollapsibleSection(tab, title="Progression", initially_open=True, show_header=True, padding=4)
        prog_sec.pack(fill=tk.X, pady=(0, 6))
        prog_frame = prog_sec.body

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))

        self.progress_label_var = tk.StringVar(
            value="Prêt."
        )
        ttk.Label(prog_frame, textvariable=self.progress_label_var, font=("Consolas", 9)).pack(anchor=tk.W)

        paned = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Panneau horizontal redimensionnable : Solutions à gauche | Schéma sans titre à droite
        h_paned = ttk.PanedWindow(paned, orient=tk.HORIZONTAL)
        paned.add(h_paned, weight=3)

        sol_sec = CollapsibleSection(h_paned, title="Solutions", initially_open=True, show_header=True, padding=4)
        h_paned.add(sol_sec, weight=1)

        self.sol_listbox = tk.Listbox(sol_sec.body, width=28, font=("Consolas", 9))
        self.sol_listbox.pack(fill=tk.BOTH, expand=True)
        self.sol_listbox.bind("<<ListboxSelect>>", self._on_select_solution)

        ttk.Button(
            sol_sec.body, text="Ouvrir dans T3", command=self.send_selected_to_verifier
        ).pack(fill=tk.X, pady=(6, 0))

        # Visuel 1er onglet : épuré, sans titre de cadre
        vis_container = ttk.Frame(h_paned, padding=2)
        h_paned.add(vis_container, weight=4)

        self.search_canvas = WiringCanvas(vis_container, interactive=True, height=200, editable=False)
        self.search_canvas.pack(fill=tk.BOTH, expand=True)

        # Panneau bas repliable et redimensionnable : titre court "Log"
        log_sec = CollapsibleSection(paned, title="Log", initially_open=True, show_header=True, padding=4)
        paned.add(log_sec, weight=2)

        self.search_log = scrolledtext.ScrolledText(log_sec.body, wrap=tk.WORD, font=("Consolas", 9), height=10)
        self.search_log.pack(fill=tk.BOTH, expand=True)

    # ---------------------- ONGLET 2 : T3 ----------------------

    def _build_verify_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="T3")

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

        main_v_paned = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        main_v_paned.pack(fill=tk.BOTH, expand=True)

        vis_sec = CollapsibleSection(main_v_paned, title="Schéma & Simulateur", initially_open=True, show_header=True, padding=4)
        main_v_paned.add(vis_sec, weight=2)
        vis_box = vis_sec.body

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

        ttk.Button(
            preset_bar,
            text="Copier PNG",
            command=lambda: copy_canvas_png_to_clipboard(
                self.root,
                self.verify_canvas.canvas,
                lambda msg: self.verify_banner_var.set(msg),
            ),
        ).pack(side=tk.RIGHT, padx=(0, 8))

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

        v_paned = ttk.PanedWindow(main_v_paned, orient=tk.HORIZONTAL)
        main_v_paned.add(v_paned, weight=3)

        editor_sec = CollapsibleSection(v_paned, title="Éditeur d'étapes", initially_open=True, show_header=True, padding=4)
        v_paned.add(editor_sec, weight=2)
        editor_box = editor_sec.body

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

        res_sec = CollapsibleSection(v_paned, title="Résultats T3", initially_open=True, show_header=True, padding=4)
        v_paned.add(res_sec, weight=3)
        res_box = res_sec.body

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
        # Restaure les collections de solutions présentes à côté de l'exécutable
        self._autoload_solution_files()

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

    def _autoload_solution_files(self):
        """Charge automatiquement les fichiers solutionsn*.json du dossier de l'app."""
        try:
            base_dir = get_app_base_dir()
            candidates = []
            for name in os.listdir(base_dir):
                low = name.lower()
                if low.startswith("solutionsn") and low.endswith(".json"):
                    candidates.append(os.path.join(base_dir, name))
            if not candidates:
                return
            loaded = []
            for path in sorted(candidates):
                try:
                    detected_n, sols = parse_solutions_collection_file(path)
                    if not sols:
                        continue
                    n_val = int(detected_n) if detected_n and detected_n >= 2 else max(
                        2, max(max(s[0], s[1]) for sol in sols for s in sol) + 1
                    )
                    self.collections_by_n[n_val] = list(sols)
                    loaded.append("n={} ({:,})".format(n_val, len(sols)))
                except Exception:
                    continue
            if loaded and hasattr(self, "_refresh_collection_selectors"):
                # Choisir le plus petit N comme jeu principal (analyse de démarrage rapide)
                preferred = min(self.collections_by_n.keys()) if self.collections_by_n else None
                self._refresh_collection_selectors(preferred_primary_n=preferred)
                self._append_search_log(
                    "[ Auto-load ] Collections restaurées : {}".format(", ".join(loaded))
                )
        except Exception:
            pass

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
                "foata_cut": bool(self.foata_var.get()) if hasattr(self, "foata_var") else True,
                "final_span_1": bool(self.final_span1_var.get()) if hasattr(self, "final_span1_var") else True,
                "search_prefix": self.prefix_var.get() if hasattr(self, "prefix_var") else "",
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
        self.progress_label_var.set("Prêt.")

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
            # Lire le préfixe optionnel (depuis ScrolledText ou Entry)
            prefix_raw = ""
            if hasattr(self, "prefix_text"):
                try:
                    prefix_raw = self.prefix_text.get("1.0", tk.END).strip()
                except Exception:
                    pass
            if not prefix_raw and hasattr(self, "prefix_var"):
                prefix_raw = self.prefix_var.get().strip()
            prefix_connections = None
            if prefix_raw:
                try:
                    prefix_connections = parse_connections_text(prefix_raw)
                except WiringValidationError as exc:
                    messagebox.showerror("Préfixe invalide", str(exc))
                    return
            config = validate_search_config(
                num_cables=num_cables,
                start_size=start_size,
                max_size=(None if max_size_raw == 0 else max_size_raw),
                max_solutions=max_solutions,
                workers=workers,
                prune_noops=self.prune_var.get(),
                search_mode=self._selected_search_mode(),
                use_cache=self.use_cache_var.get(),
                prefix_connections=prefix_connections,
                foata_cut=self.foata_var.get() if hasattr(self, "foata_var") else True,
                final_span_1=self.final_span1_var.get() if hasattr(self, "final_span1_var") else True,
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
                    if hasattr(self, "collections_by_n") and len(self.found_solutions) <= 5000:
                        n_val = self.last_search_config.num_cables if self.last_search_config else 6
                        self._store_collection_for_n(n_val, self.found_solutions, set_as_primary=True, refresh_ui=False)
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
                    if hasattr(self, "collections_by_n") and self.found_solutions:
                        n_val = self.last_search_config.num_cables if self.last_search_config else 6
                        self._store_collection_for_n(n_val, self.found_solutions, set_as_primary=True, refresh_ui=True)
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

    def _on_canvas_add_connection(self, conn, insert_idx=None):
        # type: (Connection, Optional[int]) -> None
        try:
            num_c = int(self.verify_cables_var.get())
            validate_connections(num_c, [conn])
        except ValueError as exc:
            messagebox.showerror("Connexion invalide", str(exc))
            return
        if insert_idx is None or insert_idx < 0 or insert_idx > len(self.verify_connections):
            self.verify_connections.append(conn)
        else:
            self.verify_connections.insert(insert_idx, conn)
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
            (2, 4, "OR", "AND"),
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


    # ---------------------- ONGLET 3 : ANALYSE ----------------------

    def _build_analysis_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="Analyse")

        self.collections_by_n = {}          # type: Dict[int, List[Tuple[Tuple[int, int, str, str], ...]]]
        self.analysis_solutions = []        # type: List[Tuple[Tuple[int, int, str, str], ...]]
        self.analysis_cables = 6
        self.cross_other_solutions = []     # type: List[Tuple[Tuple[int, int, str, str], ...]]
        self.cross_other_cables = 5

        top = ttk.Frame(tab)
        top.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(top, text="Importer (JSON / CSV)", command=self._import_analysis_file).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(top, text="Charger depuis MSO", command=self._refresh_analysis_from_search).pack(
            side=tk.LEFT, padx=(0, 8)
        )

        ttk.Label(top, text="Jeu principal :").pack(side=tk.LEFT, padx=(0, 4))
        self.primary_n_var = tk.StringVar(value="")
        self.primary_n_combo = ttk.Combobox(
            top, textvariable=self.primary_n_var, values=[], width=16, state="readonly"
        )
        self.primary_n_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.primary_n_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_select_collection_combos())

        ttk.Label(top, text="Jeu croisé :").pack(side=tk.LEFT, padx=(0, 4))
        self.cross_n_var = tk.StringVar(value="Aucun")
        self.cross_n_combo = ttk.Combobox(
            top, textvariable=self.cross_n_var, values=["Aucun"], width=16, state="readonly"
        )
        self.cross_n_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.cross_n_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_select_collection_combos())

        ttk.Button(top, text="Analyser", command=self._run_similarity_analysis).pack(
            side=tk.LEFT, padx=(0, 8)
        )

        ttk.Label(top, text="Réf # :").pack(side=tk.LEFT, padx=(0, 4))
        self.ref_sol_var = tk.StringVar(value="1")
        self.ref_sol_spin = tk.Spinbox(
            top, from_=1, to=5000, textvariable=self.ref_sol_var, width=5, command=self._run_similarity_analysis
        )
        self.ref_sol_spin.pack(side=tk.LEFT, padx=(0, 8))

        self.analysis_summary_var = tk.StringVar(value="Aucune collection chargée.")
        ttk.Label(top, textvariable=self.analysis_summary_var, font=("Consolas", 9)).pack(side=tk.LEFT)

        outer_v_paned = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        outer_v_paned.pack(fill=tk.BOTH, expand=True)

        cmp_sec = CollapsibleSection(outer_v_paned, title="Comparaison côte-à-côte (A vs B)", initially_open=True, show_header=True, padding=4)
        outer_v_paned.add(cmp_sec, weight=2)
        cmp_box = cmp_sec.body

        cmp_ctrl = ttk.Frame(cmp_box)
        cmp_ctrl.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(cmp_ctrl, text="Solution A :").pack(side=tk.LEFT, padx=(0, 4))
        self.cmp_a_var = tk.StringVar(value="1")
        self.cmp_a_spin = tk.Spinbox(
            cmp_ctrl, from_=1, to=5000, textvariable=self.cmp_a_var, width=6, command=self._update_side_by_side_comparison
        )
        self.cmp_a_spin.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(cmp_ctrl, text="Solution B :").pack(side=tk.LEFT, padx=(0, 4))
        self.cmp_b_var = tk.StringVar(value="2")
        self.cmp_b_spin = tk.Spinbox(
            cmp_ctrl, from_=1, to=5000, textvariable=self.cmp_b_var, width=6, command=self._update_side_by_side_comparison
        )
        self.cmp_b_spin.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(cmp_ctrl, text="Comparer A vs B", command=self._update_side_by_side_comparison).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(cmp_ctrl, text="Ouvrir A dans T3", command=lambda: self._send_analysis_sol_to_verifier("A")).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(cmp_ctrl, text="Ouvrir B dans T3", command=lambda: self._send_analysis_sol_to_verifier("B")).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(cmp_ctrl, text="Copier PNG A", command=lambda: copy_canvas_png_to_clipboard(
            self.root, self.cmp_canvas_a.canvas
        )).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(cmp_ctrl, text="Copier PNG B", command=lambda: copy_canvas_png_to_clipboard(
            self.root, self.cmp_canvas_b.canvas
        )).pack(side=tk.LEFT, padx=(0, 8))

        self.cmp_diff_var = tk.StringVar(value="")
        ttk.Label(cmp_ctrl, textvariable=self.cmp_diff_var, font=("Consolas", 8)).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(cmp_ctrl, text="Extraire L1", command=self._extract_l1_to_mso).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(cmp_ctrl, text="Envoyer L1/L2 → MSO", command=self._send_prefix_to_mso).pack(side=tk.LEFT, padx=(0, 4))

        cmp_h_paned = ttk.PanedWindow(cmp_box, orient=tk.HORIZONTAL)
        cmp_h_paned.pack(fill=tk.BOTH, expand=True)

        # Panneau gauche avec liste A et canvas A
        left_cmp_frame = ttk.Frame(cmp_h_paned, padding=2)
        cmp_h_paned.add(left_cmp_frame, weight=3)
        left_cmp_split = ttk.PanedWindow(left_cmp_frame, orient=tk.VERTICAL)
        left_cmp_split.pack(fill=tk.BOTH, expand=True)

        left_list_sec = CollapsibleSection(left_cmp_split, title="Solutions A (jeu principal)", initially_open=True, show_header=True, padding=2)
        left_cmp_split.add(left_list_sec, weight=1)
        self.analysis_listbox_a = tk.Listbox(left_list_sec.body, width=22, font=("Consolas", 8))
        self.analysis_listbox_a.pack(fill=tk.BOTH, expand=True)
        self.analysis_listbox_a.bind("<<ListboxSelect>>", lambda _e: self._on_select_analysis_sol("A"))
        self.primary_info_var = tk.StringVar(value="N=6 (0 sol.)")
        ttk.Label(left_list_sec.body, textvariable=self.primary_info_var, font=("Consolas", 8)).pack(anchor=tk.W, pady=(1, 0))

        left_canvas_frame = ttk.Frame(left_cmp_split, padding=2)
        left_cmp_split.add(left_canvas_frame, weight=1)
        ttk.Label(left_canvas_frame, text="Schéma A", font=("Segoe UI", 8, "bold")).pack(anchor=tk.W)
        self.cmp_canvas_a = WiringCanvas(left_canvas_frame, interactive=True, height=130, editable=False)
        self.cmp_canvas_a.pack(fill=tk.BOTH, expand=True)

        # Panneau central: détail comparaison A vs B
        center_cmp = ttk.Frame(cmp_h_paned, padding=2)
        cmp_h_paned.add(center_cmp, weight=2)

        cmp_center_split = ttk.PanedWindow(center_cmp, orient=tk.VERTICAL)
        cmp_center_split.pack(fill=tk.BOTH, expand=True)

        cmp_canvas_frame = ttk.Frame(cmp_center_split, padding=2)
        cmp_center_split.add(cmp_canvas_frame, weight=1)

        center_cmp_top = ttk.Frame(cmp_canvas_frame)
        center_cmp_top.pack(fill=tk.X)
        ttk.Label(center_cmp_top, text="A", font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._cmp_display_a = WiringCanvas(center_cmp_top, interactive=True, height=100, editable=False)
        self._cmp_display_a.pack(fill=tk.BOTH, expand=True)

        center_cmp_bot = ttk.Frame(cmp_canvas_frame)
        center_cmp_bot.pack(fill=tk.X)
        ttk.Label(center_cmp_bot, text="B", font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._cmp_display_b = WiringCanvas(center_cmp_bot, interactive=True, height=100, editable=False)
        self._cmp_display_b.pack(fill=tk.BOTH, expand=True)

        self.cmp_detail_text = scrolledtext.ScrolledText(cmp_canvas_frame, wrap=tk.WORD, font=("Consolas", 8), height=5)
        self.cmp_detail_text.pack(fill=tk.BOTH, expand=True)

        # Panneau droit avec liste B et canvas B
        right_cmp_frame = ttk.Frame(cmp_h_paned, padding=2)
        cmp_h_paned.add(right_cmp_frame, weight=3)
        right_cmp_split = ttk.PanedWindow(right_cmp_frame, orient=tk.VERTICAL)
        right_cmp_split.pack(fill=tk.BOTH, expand=True)

        right_list_sec = CollapsibleSection(right_cmp_split, title="Solutions B (jeu croisé)", initially_open=True, show_header=True, padding=2)
        right_cmp_split.add(right_list_sec, weight=1)
        self.analysis_listbox_b = tk.Listbox(right_list_sec.body, width=22, font=("Consolas", 8))
        self.analysis_listbox_b.pack(fill=tk.BOTH, expand=True)
        self.analysis_listbox_b.bind("<<ListboxSelect>>", lambda _e: self._on_select_analysis_sol("B"))
        self.cross_info_var = tk.StringVar(value="Aucun jeu croisé")
        ttk.Label(right_list_sec.body, textvariable=self.cross_info_var, font=("Consolas", 8)).pack(anchor=tk.W, pady=(1, 0))

        right_canvas_frame = ttk.Frame(right_cmp_split, padding=2)
        right_cmp_split.add(right_canvas_frame, weight=1)
        ttk.Label(right_canvas_frame, text="Schéma B", font=("Segoe UI", 8, "bold")).pack(anchor=tk.W)
        self.cmp_canvas_b = WiringCanvas(right_canvas_frame, interactive=True, height=130, editable=False)
        self.cmp_canvas_b.pack(fill=tk.BOTH, expand=True)

        bottom_paned = ttk.PanedWindow(outer_v_paned, orient=tk.HORIZONTAL)
        outer_v_paned.add(bottom_paned, weight=3)

        sim_sec = CollapsibleSection(bottom_paned, title="Similitudes (Jaccard & Rang)", initially_open=True, show_header=True, padding=4)
        bottom_paned.add(sim_sec, weight=2)
        sim_left = sim_sec.body

        sim_cols = ("rank", "sol_num", "jaccard", "same_pos", "pos_pct")
        self.sim_tree = ttk.Treeview(sim_left, columns=sim_cols, show="headings", height=10)
        self.sim_tree.heading("rank", text="#")
        self.sim_tree.heading("sol_num", text="Solution")
        self.sim_tree.heading("jaccard", text="Jaccard")
        self.sim_tree.heading("same_pos", text="Même rang")
        self.sim_tree.heading("pos_pct", text="% Rang")
        self.sim_tree.column("rank", width=36, anchor="center")
        self.sim_tree.column("sol_num", width=80, anchor="center")
        self.sim_tree.column("jaccard", width=74, anchor="center")
        self.sim_tree.column("same_pos", width=84, anchor="center")
        self.sim_tree.column("pos_pct", width=68, anchor="center")
        self.sim_tree.pack(fill=tk.BOTH, expand=True)
        self.sim_tree.bind("<Double-1>", self._on_double_click_sim_row)

        stats_sec = CollapsibleSection(bottom_paned, title="Carte Visuelle & Statistiques", initially_open=True, show_header=True, padding=4)
        bottom_paned.add(stats_sec, weight=3)

        self.stats_nb = ttk.Notebook(stats_sec.body)
        self.stats_nb.pack(fill=tk.BOTH, expand=True)

        heat_tab = ttk.Frame(self.stats_nb, padding=4)
        self.stats_nb.add(heat_tab, text="Carte Étapes × Paires")

        self.heatmap_canvas = tk.Canvas(heat_tab, bg="#0F172A", highlightthickness=1, highlightbackground="#334155")
        h_hsb = ttk.Scrollbar(heat_tab, orient=tk.HORIZONTAL, command=self.heatmap_canvas.xview)
        h_vsb = ttk.Scrollbar(heat_tab, orient=tk.VERTICAL, command=self.heatmap_canvas.yview)
        self.heatmap_canvas.configure(xscrollcommand=h_hsb.set, yscrollcommand=h_vsb.set)
        self.heatmap_canvas.grid(row=0, column=0, sticky="nsew")
        h_vsb.grid(row=0, column=1, sticky="ns")
        h_hsb.grid(row=1, column=0, sticky="ew")
        heat_tab.rowconfigure(0, weight=1)
        heat_tab.columnconfigure(0, weight=1)

        txt_tab = ttk.Frame(self.stats_nb, padding=4)
        self.stats_nb.add(txt_tab, text="Rapport & Croisement")

        self.stats_text = scrolledtext.ScrolledText(txt_tab, wrap=tk.WORD, font=("Consolas", 9))
        self.stats_text.pack(fill=tk.BOTH, expand=True)

    def _format_n_label(self, n_val):
        sols = self.collections_by_n.get(n_val, [])
        return "n={} ({:,} sols)".format(n_val, len(sols))

    def _parse_n_from_label(self, label):
        if not label or not label.startswith("n="):
            return None
        try:
            return int(label.split("=", 1)[1].split()[0])
        except Exception:
            return None

    def _clamp_analysis_spinboxes(self, total):
        max_bound = max(1, total)
        for sp in (self.ref_sol_spin, self.cmp_a_spin, self.cmp_b_spin):
            sp.configure(from_=1, to=max_bound)
        if total > 0:
            cur_a = self._parse_1based_index(self.cmp_a_var.get(), total)
            if cur_a is None:
                self.cmp_a_var.set("1")
            cur_b = self._parse_1based_index(self.cmp_b_var.get(), total)
            if cur_b is None:
                self.cmp_b_var.set(str(min(2, total)))
            cur_ref = self._parse_1based_index(self.ref_sol_var.get(), total)
            if cur_ref is None:
                self.ref_sol_var.set("1")

    def _refresh_collection_selectors(self, preferred_primary_n=None):
        sorted_ns = sorted(self.collections_by_n.keys())
        labels = [self._format_n_label(n) for n in sorted_ns]
        self.primary_n_combo["values"] = labels
        self.cross_n_combo["values"] = ["Aucun"] + labels

        if not sorted_ns:
            self.primary_n_var.set("")
            self.cross_n_var.set("Aucun")
            self.analysis_solutions = []
            self.cross_other_solutions = []
            self._clamp_analysis_spinboxes(0)
            self._update_analysis_banner()
            return

        cur_p = preferred_primary_n if preferred_primary_n in self.collections_by_n else self._parse_n_from_label(self.primary_n_var.get())
        if cur_p not in self.collections_by_n:
            cur_p = sorted_ns[-1]
        self.primary_n_var.set(self._format_n_label(cur_p))

        cur_c = self._parse_n_from_label(self.cross_n_var.get())
        if cur_c not in self.collections_by_n or cur_c == cur_p:
            other_ns = [n for n in sorted_ns if n != cur_p]
            if other_ns:
                cur_c = other_ns[0]
                self.cross_n_var.set(self._format_n_label(cur_c))
            else:
                self.cross_n_var.set("Aucun")
        else:
            self.cross_n_var.set(self._format_n_label(cur_c))

        self._on_select_collection_combos()

    def _on_select_collection_combos(self):
        p_n = self._parse_n_from_label(self.primary_n_var.get())
        if p_n is not None and p_n in self.collections_by_n:
            self.analysis_cables = p_n
            self.analysis_solutions = list(self.collections_by_n[p_n])
        else:
            self.analysis_solutions = []

        c_n = self._parse_n_from_label(self.cross_n_var.get())
        if c_n is not None and c_n in self.collections_by_n and c_n != p_n:
            self.cross_other_cables = c_n
            self.cross_other_solutions = list(self.collections_by_n[c_n])
        else:
            self.cross_other_solutions = []

        self._clamp_analysis_spinboxes(len(self.analysis_solutions))
        self._update_analysis_banner()
        self._populate_analysis_listboxes()
        self._update_side_by_side_comparison()
        if self.analysis_solutions:
            self._run_similarity_analysis()

    def _store_collection_for_n(self, num_cables, sols, set_as_primary=True, refresh_ui=True):
        if not sols:
            return
        n_val = int(num_cables) if num_cables and num_cables >= 2 else max(
            2, max(max(s[0], s[1]) for sol in sols for s in sol) + 1
        )
        self.collections_by_n[n_val] = list(sols)
        if refresh_ui:
            self._refresh_collection_selectors(preferred_primary_n=(n_val if set_as_primary else None))

    def _populate_analysis_listboxes(self):
        self.analysis_listbox_a.delete(0, tk.END)
        for idx, sol in enumerate(self.analysis_solutions[:1000], start=1):
            self.analysis_listbox_a.insert(tk.END, "#{} ({} étapes)".format(idx, len(sol)))
        if hasattr(self, "primary_info_var"):
            n_val = self.analysis_cables
            total = len(self.analysis_solutions)
            self.primary_info_var.set("N={} ({} solutions)".format(n_val, total))
        self.analysis_listbox_b.delete(0, tk.END)
        for idx, sol in enumerate(self.cross_other_solutions[:1000], start=1):
            self.analysis_listbox_b.insert(tk.END, "#{} ({} étapes)".format(idx, len(sol)))
        if hasattr(self, "cross_info_var"):
            if self.cross_other_solutions:
                n_val = self.cross_other_cables
                total = len(self.cross_other_solutions)
                self.cross_info_var.set("N={} ({} solutions)".format(n_val, total))
            else:
                self.cross_info_var.set("Aucun jeu croisé")

    def _on_select_analysis_sol(self, which="A"):
        lb = self.analysis_listbox_a if which == "A" else self.analysis_listbox_b
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        if which == "A":
            self.cmp_a_var.set(str(idx + 1))
        else:
            self.cmp_b_var.set(str(idx + 1))
        self._update_side_by_side_comparison()

    def _extract_l1_to_mso(self):
        """Analyse les 20 premières solutions pour trouver la première couche L1 la plus fréquente."""
        if not self.analysis_solutions:
            messagebox.showinfo("Pas de données", "Aucune solution dans le jeu principal.")
            return
        # Prendre les 20 premières solutions
        sample = self.analysis_solutions[:20]
        l1_patterns = {}
        l1_gates = {}
        for sol in sample:
            if not sol:
                continue
            g1 = sol[0]
            l1_gates[(g1[0], g1[1])] = l1_gates.get((g1[0], g1[1]), 0) + 1
            used = {g1[0], g1[1]}
            l1_set = [(g1[0], g1[1])]
            for step in sol[1:]:
                c1, c2 = step[0], step[1]
                if c1 not in used and c2 not in used:
                    used.add(c1)
                    used.add(c2)
                    l1_set.append((c1, c2))
                else:
                    break
            pattern = ";".join("({},{})".format(k, v) for k, v in l1_set)
            l1_patterns[pattern] = l1_patterns.get(pattern, 0) + 1

        # Trouver le pattern L1 le plus fréquent
        if not l1_patterns:
            messagebox.showinfo("Pas de L1", "Impossible d'extraire L1 des solutions.")
            return
        best_pattern, best_count = max(l1_patterns.items(), key=lambda kv: (kv[1], kv[0]))
        # Reconstruire le pattern sous forme de tuples
        gates = []
        for part in best_pattern.split(";"):
            part = part.strip()
            if part.startswith("(") and part.endswith(")"):
                inner = part[1:-1]
                parts = inner.split(",")
                if len(parts) == 2:
                    gates.append((int(parts[0].strip()), int(parts[1].strip()), "OR", "AND"))
        if not gates:
            # Fallback: utiliser la première paire des 20 solutions
            gates = [(sample[0][0][0], sample[0][0][1], "OR", "AND")]
        prefix_text = format_connections_plain_text(gates)
        if hasattr(self, "prefix_text"):
            self.prefix_text.delete("1.0", tk.END)
            self.prefix_text.insert("1.0", prefix_text)
            if hasattr(self, "l1_result_var"):
                self.l1_result_var.set("L1: {} portes (fréq: {}/{})".format(len(gates), best_count, len(sample)))
            self._append_search_log("[ Analyse ] Pattern L1 extrait des {} premières solutions: {} ({}x)".format(
                len(sample), best_pattern, best_count
            ))
            self.notebook.select(0)
        elif hasattr(self, "prefix_var"):
            self.prefix_var.set(prefix_text.replace("\n", ", "))
            self._append_search_log("[ Analyse ] Pattern L1: {}".format(best_pattern))
        else:
            messagebox.showinfo("Pas de champ préfixe", "L'onglet MSO n'a pas de champ préfixe.")

    def _extract_l1l2_to_mso(self):
        """Extrait les 6 premières portes (L1+L2) de la solution A."""
        total = len(self.analysis_solutions)
        idx = self._parse_1based_index(self.cmp_a_var.get(), total)
        if idx is None:
            messagebox.showinfo("Pas de solution", "Sélectionnez d'abord une solution A.")
            return
        sol = self.analysis_solutions[idx]
        if not sol:
            return
        prefix_count = min(6, len(sol))
        l1l2 = sol[:prefix_count]
        prefix_text = format_connections_plain_text(l1l2)
        if hasattr(self, "prefix_text"):
            self.prefix_text.delete("1.0", tk.END)
            self.prefix_text.insert("1.0", prefix_text)
            if hasattr(self, "l1_result_var"):
                self.l1_result_var.set("L1+L2: {} portes".format(len(l1l2)))
            self._append_search_log("[ Analyse ] Préfixe L1+L2 importé depuis l'Analyse: {} portes".format(len(l1l2)))
            self.notebook.select(0)
        elif hasattr(self, "prefix_var"):
            self.prefix_var.set(prefix_text.replace("\n", ", "))
            self._append_search_log("[ Analyse ] Préfixe L1+L2: {} portes".format(len(l1l2)))
        else:
            messagebox.showinfo("Pas de champ préfixe", "L'onglet MSO n'a pas de champ préfixe.")

    def _update_analysis_banner(self):
        if not self.collections_by_n:
            self.analysis_summary_var.set("Aucune collection chargée.")
            return
        loaded_summary = ", ".join(
            "n={} ({:,})".format(n, len(self.collections_by_n[n])) for n in sorted(self.collections_by_n.keys())
        )
        self.analysis_summary_var.set("En mémoire : {}".format(loaded_summary))

    def _refresh_analysis_from_search(self):
        if not self.found_solutions:
            messagebox.showinfo("Aucune solution", "Aucune solution calculée dans MSO.")
            return
        n_cables = self.last_search_config.num_cables if self.last_search_config else 6
        self._store_collection_for_n(n_cables, self.found_solutions, set_as_primary=True, refresh_ui=True)

    def _import_analysis_file(self):
        path = filedialog.askopenfilename(
            filetypes=[
                ("Fichiers JSON et CSV", "*.json;*.csv"),
                ("Fichiers JSON", "*.json"),
                ("Fichiers CSV", "*.csv"),
                ("Tous les fichiers", "*.*"),
            ],
            title="Importer une collection (JSON ou CSV)",
        )
        if not path:
            return
        try:
            detected_cables, sols = parse_solutions_collection_file(path)
            if not sols:
                messagebox.showinfo("Aucune solution", "Aucune solution exploitable trouvée dans ce fichier.")
                return
            self._store_collection_for_n(detected_cables, sols, set_as_primary=True, refresh_ui=True)
        except Exception as exc:
            messagebox.showerror("Erreur d'import", str(exc))

    def _parse_1based_index(self, raw_val, total):
        try:
            idx = int(raw_val) - 1
        except ValueError:
            return None
        if 0 <= idx < total:
            return idx
        return None

    def _send_analysis_sol_to_verifier(self, which="A"):
        total = len(self.analysis_solutions)
        if total == 0:
            return
        raw_val = self.cmp_a_var.get() if which == "A" else self.cmp_b_var.get()
        idx = self._parse_1based_index(raw_val, total)
        if idx is None:
            idx = 0
        self.verify_cables_var.set(str(self.analysis_cables))
        self.verify_connections = normalize_connections(self.analysis_solutions[idx])
        self._sync_verify_views()
        self.run_verify()
        self.notebook.select(1)

    def _update_side_by_side_comparison(self):
        total_a = len(self.analysis_solutions)
        total_b = len(self.cross_other_solutions) if self.cross_other_solutions else total_a
        if total_a == 0:
            self.cmp_canvas_a.set_network(self.analysis_cables, [])
            self.cmp_canvas_b.set_network(self.analysis_cables, [])
            self._cmp_display_a.set_network(self.analysis_cables, [])
            self._cmp_display_b.set_network(self.analysis_cables, [])
            self.cmp_diff_var.set("")
            if hasattr(self, "cmp_detail_text"):
                self.cmp_detail_text.delete("1.0", tk.END)
            return

        self._clamp_analysis_spinboxes(total_a)
        idx_a = self._parse_1based_index(self.cmp_a_var.get(), total_a)
        if idx_a is None:
            idx_a = 0
            self.cmp_a_var.set("1")
        idx_b = self._parse_1based_index(self.cmp_b_var.get(), total_b)
        if idx_b is None:
            idx_b = min(1, total_b - 1)
            self.cmp_b_var.set(str(idx_b + 1))

        sol_a = self.analysis_solutions[idx_a]
        sol_b = (self.cross_other_solutions[idx_b] if self.cross_other_solutions
                 else self.analysis_solutions[idx_b])

        n_a = self.analysis_cables
        n_b = self.cross_other_cables if self.cross_other_solutions else n_a

        # Determine if same N or different N
        same_n = (n_a == n_b)

        self._cmp_show_diff_only = self.cmp_diff_only_var.get() if hasattr(self, "cmp_diff_only_var") else False

        diff_steps_a = set()
        diff_steps_b = set()
        same_n_highlight = set()
        common_steps = set()
        extra_steps = set()
        subsequence_info = ""

        if same_n:
            # Same N: compare step-by-step, highlight differences in ORANGE
            max_len = max(len(sol_a), len(sol_b))
            for k in range(max_len):
                sa = sol_a[k] if k < len(sol_a) else None
                sb = sol_b[k] if k < len(sol_b) else None
                if sa != sb:
                    if k < len(sol_a):
                        diff_steps_a.add(k)
                    if k < len(sol_b):
                        diff_steps_b.add(k)
                else:
                    if not self._cmp_show_diff_only:
                        same_n_highlight.add(k)

            diff_list_a = sorted(i + 1 for i in diff_steps_a)
            diff_list_b = sorted(i + 1 for i in diff_steps_b)
            same_count = max_len - max(len(diff_steps_a), len(diff_steps_b))
            self.cmp_diff_var.set(
                "A#{} vs B#{} | Identiques: ~{}/{} | Diff A: {} | Diff B: {}".format(
                    idx_a + 1, idx_b + 1,
                    max(0, same_count), max_len,
                    diff_list_a, diff_list_b
                )
            )
        else:
            # Different N: detect if B is a subsequence of A (by projection)
            pairs_a = [(s[0], s[1]) for s in sol_a]
            pairs_b = [(s[0], s[1]) for s in sol_b]
            # Try to find B as subsequence in A
            is_subseq = False
            shift = 0
            # Normalize B to A's index space: try shifts
            for test_shift in range(0, n_a - n_b + 1 if n_a > n_b else 1):
                shifted_b = tuple((c1 + test_shift, c2 + test_shift) for c1, c2 in pairs_b)
                if _is_subsequence_pairs(shifted_b, pairs_a):
                    is_subseq = True
                    shift = test_shift
                    break
            if is_subseq:
                subsequence_info = "B est contenu dans A (décalage câbles +{})".format(shift)
                # Mark common steps (GREEN) and extra steps in A (YELLOW)
                iter_a = 0
                for step_b in pairs_b:
                    shifted = (step_b[0] + shift, step_b[1] + shift)
                    while iter_a < len(pairs_a) and pairs_a[iter_a] != shifted:
                        extra_steps.add(iter_a)
                        iter_a += 1
                    if iter_a < len(pairs_a):
                        common_steps.add(iter_a)
                        iter_a += 1
                # Remaining steps in A are extra
                for ka in range(iter_a, len(pairs_a)):
                    extra_steps.add(ka)
                diff_steps_a = extra_steps | common_steps
                diff_steps_b = set(range(len(pairs_b)))
            else:
                subsequence_info = "B n'est pas une sous-séquence de A"
                # Fall back to simple positional comparison
                max_len = max(len(sol_a), len(sol_b))
                for k in range(max_len):
                    sa = sol_a[k] if k < len(sol_a) else None
                    sb = sol_b[k] if k < len(sol_b) else None
                    if sa != sb:
                        if k < len(sol_a):
                            diff_steps_a.add(k)
                        if k < len(sol_b):
                            diff_steps_b.add(k)

            self.cmp_diff_var.set(
                "A#{} (n={}) vs B#{} (n={}) | {}".format(
                    idx_a + 1, n_a, idx_b + 1, n_b, subsequence_info
                )
            )

        # Apply diff highlighting to left/right canvases
        self.cmp_canvas_a.diff_steps = diff_steps_a
        self.cmp_canvas_b.diff_steps = diff_steps_b
        self.cmp_canvas_a.set_network(n_a, sol_a)
        self.cmp_canvas_b.set_network(n_b, sol_b)

        # Also apply to center comparison canvases if they exist
        if hasattr(self, "_cmp_display_a"):
            self._cmp_display_a.diff_steps = diff_steps_a
            self._cmp_display_b.diff_steps = diff_steps_b
            if same_n:
                self._cmp_display_a.set_network(n_a, sol_a)
                self._cmp_display_b.set_network(n_a, sol_b)
            else:
                self._cmp_display_a.set_network(n_a, sol_a)
                self._cmp_display_b.set_network(n_b, sol_b)

        # Update the detail text widget in the center panel
        if hasattr(self, "cmp_detail_text"):
            self.cmp_detail_text.delete("1.0", tk.END)
            detail_lines = [
                "--- Comparaison A vs B ---",
                "A = #{} (n={}, {} étapes)".format(idx_a + 1, n_a, len(sol_a)),
                "B = #{} (n={}, {} étapes)".format(idx_b + 1, n_b, len(sol_b)),
                "",
            ]
            if same_n:
                diff_list = sorted(i + 1 for i in diff_steps_a)
                if diff_list:
                    detail_lines.append("Différences aux étapes : E{}".format(
                        ", E".join(str(d) for d in diff_list)
                    ))
                else:
                    detail_lines.append("Les deux solutions sont identiques.")
            else:
                detail_lines.append(subsequence_info)
                if extra_steps:
                    detail_lines.append("Étapes supplémentaires dans A : E{}".format(
                        ", E".join(str(e + 1) for e in sorted(extra_steps))
                    ))
                if common_steps:
                    detail_lines.append("Étapes communes : E{}".format(
                        ", E".join(str(c + 1) for c in sorted(common_steps))
                    ))

            # Énumération step-by-step
            detail_lines.append("")
            detail_lines.append("{:<5} {:<20} {:<20}".format("Étape", "Sol A", "Sol B"))
            max_s = max(len(sol_a), len(sol_b))
            for k in range(max_s):
                sa_str = str(sol_a[k]) if k < len(sol_a) else "(fin)"
                sb_str = str(sol_b[k]) if k < len(sol_b) else "(fin)"
                marker = " <--" if (sa_str != sb_str or (k < len(sol_a) and k >= len(sol_b)) or (k >= len(sol_a) and k < len(sol_b))) else ""
                detail_lines.append("E{:<3d} {:<20} {:<20}{}".format(k + 1, sa_str, sb_str, marker))

            self.cmp_detail_text.insert("1.0", "\n".join(detail_lines))

        # Détection de sous-séquence si N diffèrent
        detail_lines = [
            "=== Comparaison A(#{} {} étapes) ↔ B(#{} {} étapes) ===".format(
                idx_a + 1, len(sol_a), idx_b + 1, len(sol_b)
            ),
        ]
        if self.cross_other_solutions and self.cross_other_cables != self.analysis_cables:
            # Vérifier si A est sous-séquence de B ou vice versa
            pairs_a = [(s[0], s[1]) for s in sol_a]
            pairs_b = [(s[0], s[1]) for s in sol_b]
            # Essaye A dans B
            if _is_subsequence_pairs(pairs_a, pairs_b):
                detail_lines.append("  ✅ A est sous-séquence de B (projection directe)")
            # Essaye B dans A
            if _is_subsequence_pairs(pairs_b, pairs_a):
                detail_lines.append("  ✅ B est sous-séquence de A (projection directe)")
        detail_lines.append("")
        detail_lines.append("Steps différents A: {}".format(sorted(i + 1 for i in diff_steps_a)))
        detail_lines.append("Steps différents B: {}".format(sorted(i + 1 for i in diff_steps_b)))
        if hasattr(self, "cmp_detail_text"):
            self.cmp_detail_text.delete("1.0", tk.END)
            self.cmp_detail_text.insert("1.0", "\n".join(detail_lines))

    def _draw_heatmap(self, rich_report):
        c = self.heatmap_canvas
        c.delete("all")
        total = rich_report["total"]
        max_steps = rich_report["max_steps"]
        if total == 0 or max_steps == 0:
            return

        sorted_pairs = [
            p for p, _cnt in sorted(rich_report["pair_totals"].items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        if not sorted_pairs:
            return

        max_cell_cnt = max(
            (rich_report["heatmap"][s].get(p, 0) for s in range(max_steps) for p in sorted_pairs),
            default=1,
        )
        max_cell_cnt = max(1, max_cell_cnt)

        left_w = 82
        top_h = 28
        cell_w = 54
        cell_h = 24
        width = left_w + max_steps * cell_w + 20
        height = top_h + len(sorted_pairs) * cell_h + 20
        c.configure(scrollregion=(0, 0, width, height))

        for s in range(max_steps):
            x = left_w + s * cell_w + cell_w * 0.5
            c.create_text(x, 14, text="E{}".format(s + 1), fill="#E2E8F0", font=("Consolas", 8, "bold"))

        for r_i, pair in enumerate(sorted_pairs):
            y = top_h + r_i * cell_h
            yc = y + cell_h * 0.5
            c.create_text(12, yc, text="({}, {})".format(pair[0], pair[1]), anchor="w", fill="#CBD5E1", font=("Consolas", 8, "bold"))

            for s in range(max_steps):
                x = left_w + s * cell_w
                cnt = rich_report["heatmap"][s].get(pair, 0)
                ratio = cnt / float(max_cell_cnt)
                if ratio <= 0.0:
                    bg = "#1E293B"
                    fg = "#64748B"
                elif ratio < 0.35:
                    bg = "#1D4ED8"
                    fg = "#EFF6FF"
                elif ratio < 0.70:
                    bg = "#047857"
                    fg = "#ECFDF5"
                else:
                    bg = "#D97706"
                    fg = "#FFFBEB"
                c.create_rectangle(x + 1, y + 1, x + cell_w - 1, y + cell_h - 1, fill=bg, outline="#0F172A")
                if cnt > 0:
                    c.create_text(x + cell_w * 0.5, yc, text=str(cnt), fill=fg, font=("Consolas", 7, "bold"))

    def _run_similarity_analysis(self):
        total = len(self.analysis_solutions)
        if total == 0:
            messagebox.showinfo("Aucune donnée", "Chargez ou calculez d'abord des solutions.")
            return

        self._clamp_analysis_spinboxes(total)
        ref_idx = self._parse_1based_index(self.ref_sol_var.get(), total)
        if ref_idx is None:
            ref_idx = 0

        report = compute_solutions_similarity_report(self.analysis_solutions, ref_idx=ref_idx)
        rich = compute_rich_network_analytics(self.analysis_solutions, self.analysis_cables, ref_idx=ref_idx)
        self._draw_heatmap(rich)

        for item in self.sim_tree.get_children():
            self.sim_tree.delete(item)

        for rank_i, (sol_i, jaccard, same_pos, max_len, pos_ratio) in enumerate(
            report["similarities"][:200], start=1
        ):
            self.sim_tree.insert(
                "",
                tk.END,
                iid=str(sol_i),
                values=(
                    rank_i,
                    "#{}".format(sol_i + 1),
                    "{:.3f}".format(jaccard),
                    "{}/{}".format(same_pos, max_len),
                    "{:.1f}%".format(pos_ratio * 100.0),
                ),
            )

        lines = [
            "=== ANALYSE DE SIMILITUDE ({:,} solutions, n={}, réf = #{}) ===".format(
                total, self.analysis_cables, ref_idx + 1
            ),
            "",
            "-- Solutions centrales (plus forte similarité moyenne) --",
            "  " + ", ".join("#{} ({:.3f})".format(idx + 1, sc) for sc, idx in rich["central_sols"]),
            "-- Solutions atypiques (plus faible similarité moyenne) --",
            "  " + ", ".join("#{} ({:.3f})".format(idx + 1, sc) for sc, idx in rich["outlier_sols"]),
            "",
            "-- Activité par câble C0..C{} --".format(self.analysis_cables - 1),
            "  " + " | ".join(
                "C{}: {:,}".format(c_i, cnt) for c_i, cnt in enumerate(rich["cable_activity"])
            ),
            "",
            "-- Top 15 paires (c1, c2) les plus fréquentes --",
        ]
        sorted_pairs = sorted(report["pair_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
        total_steps_all = sum(report["pair_counts"].values())
        for pair, cnt in sorted_pairs[:15]:
            pct = (100.0 * cnt / max(total_steps_all, 1))
            lines.append("  Paire ({}, {}) : {:,} ({:.1f}%)".format(pair[0], pair[1], cnt, pct))

        # Nouvelles métriques : dernière porte E_K
        lines.append("")
        lines.append("-- Dernière porte E_K (distribution) --")
        last_g = rich.get("last_gate", {})
        if last_g and last_g.get("total", 0) > 0:
            for (c1, c2), cnt in sorted(last_g.get("last_pair_counts", {}).items(), key=lambda kv: -kv[1])[:10]:
                pct = 100.0 * cnt / max(last_g.get("total", 1), 1)
                lines.append("  E_K = ({}, {}) : {:,} ({:.1f}%)".format(c1, c2, cnt, pct))
            for sp, cnt in sorted(last_g.get("last_span_counts", {}).items(), key=lambda kv: -kv[1]):
                pct = 100.0 * cnt / max(last_g.get("total", 1), 1)
                lines.append("  Écart final {} : {:.1f}%".format(sp, pct))

        # Première couche L1
        lines.append("")
        lines.append("-- Première couche L1 (pattern d'ouverture) --")
        fl = rich.get("first_layer", {})
        if fl:
            lines.append("  Max paires disjointes L1 : {}".format(fl.get("max_disjoint_l1", 0)))
            for (c1, c2), cnt in sorted(fl.get("l1_gate_counts", {}).items(), key=lambda kv: -kv[1])[:8]:
                pct = 100.0 * cnt / max(total, 1)
                lines.append("  Première porte ({}, {}) : {:,} ({:.1f}%)".format(c1, c2, cnt, pct))
            top_patterns = sorted(fl.get("l1_patterns", {}).items(), key=lambda kv: -kv[1])[:5]
            for pat, cnt in top_patterns:
                pct = 100.0 * cnt / max(total, 1)
                lines.append("  Pattern L1: [{}] : {:,} ({:.1f}%)".format(pat, cnt, pct))

        # Profondeur parallèle ASAP
        lines.append("")
        lines.append("-- Profondeur parallèle ASAP --")
        pd = rich.get("parallel_depth", {})
        if pd and pd.get("max_parallel_depth", 0) > 0:
            lines.append("  Profondeur max : {}".format(pd.get("max_parallel_depth", 0)))
            for d, cnt in sorted(pd.get("depth_distribution", {}).items()):
                pct = 100.0 * cnt / max(total, 1)
                lines.append("  Profondeur {} : {:,} ({:.1f}%)".format(d, cnt, pct))

        # Symétries
        sym = rich.get("symmetry", {})
        if sym and sym.get("total", 0) > 0:
            pct_sym = 100.0 * sym["symmetric_count"] / max(sym["total"], 1)
            lines.append("  Symétriques par réflexion : {}/{} ({:.1f}%)".format(
                sym["symmetric_count"], sym["total"], pct_sym
            ))

        lines.append("")
        lines.append("-- Paire dominante par étape E1..Ek --")
        for step_i, pmap in enumerate(report["step_pair_counts"], start=1):
            if not pmap:
                continue
            top_pair, top_cnt = max(pmap.items(), key=lambda kv: (kv[1], -kv[0][0], -kv[0][1]))
            pct_sol = (100.0 * top_cnt / max(total, 1))
            lines.append(
                "  E{:<2d} : ({}, {}) dans {:,}/{:,} ({:.1f}%)".format(
                    step_i, top_pair[0], top_pair[1], top_cnt, total, pct_sol
                )
            )

        lines.append("")
        lines.append("-- Top 10 bigrammes consécutifs --")
        sorted_bg = sorted(report["bigram_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
        for (p1, p2), cnt in sorted_bg[:10]:
            lines.append("  ({}, {}) -> ({}, {}) : {:,} fois".format(p1[0], p1[1], p2[0], p2[1], cnt))

        if self.cross_other_solutions and self.cross_other_cables != self.analysis_cables:
            cross = compute_cross_n_similarity_report(
                self.analysis_solutions,
                self.analysis_cables,
                self.cross_other_solutions,
                self.cross_other_cables,
            )
            lines.append("")
            lines.append(
                "=== CROISEMENT MULTI-N : n={} ({:,} sols) vs n={} ({:,} sols) ===".format(
                    cross["n_main"], cross["count_main"], cross["n_other"], cross["count_other"]
                )
            )
            lines.append(
                "  Paires absolues communes ({}) : {}".format(
                    len(cross["common_abs_pairs"]), cross["common_abs_pairs"]
                )
            )
            lines.append("  Distribution des écarts |c2 - c1| (span des portes) :")
            all_spans = sorted(set(cross["span_main"].keys()) | set(cross["span_other"].keys()))
            for sp in all_spans:
                p_m = 100.0 * cross["span_main"].get(sp, 0) / max(cross["tot_gates_main"], 1)
                p_o = 100.0 * cross["span_other"].get(sp, 0) / max(cross["tot_gates_other"], 1)
                lines.append(
                    "    Écart {} : n={} -> {:.1f}% | n={} -> {:.1f}%".format(
                        sp, cross["n_main"], p_m, cross["n_other"], p_o
                    )
                )

            if cross["big_n"] > cross["small_n"]:
                pct_emb = 100.0 * cross["embedded_matches"] / max(cross["big_count"], 1)
                lines.append(
                    "  Plongement de sous-réseaux (n={} inclus comme sous-séquence dans n={}) :".format(
                        cross["small_n"], cross["big_n"]
                    )
                )
                lines.append(
                    "    {:,}/{:,} solutions n={} ({:.1f}%) contiennent un optimal n={} (couvrant {}/{} optimaux n={})".format(
                        cross["embedded_matches"],
                        cross["big_count"],
                        cross["big_n"],
                        pct_emb,
                        cross["small_n"],
                        cross["matched_small_count"],
                        cross["small_count"],
                        cross["small_n"],
                    )
                )
                for b_i, s_i, shift in cross["sample_embeddings"][:6]:
                    lines.append(
                        "      Ex : Sol n={} #{} contient Sol n={} #{} (décalage câbles +{})".format(
                            cross["big_n"], b_i + 1, cross["small_n"], s_i + 1, shift
                        )
                    )

            # Analyse de projection N → N-1 : suppression d'un câble extrême
            if cross["big_n"] > cross["small_n"]:
                lines.append("")
                lines.append("  Analyse de projection N -> N-1 (suppression du câble extrême) :")
                # Vérifier les solutions du grand jeu
                projection_hits = 0
                for b_idx, b_sol in enumerate(self.analysis_solutions if self.analysis_cables >= cross["big_n"] else self.cross_other_solutions):
                    if not b_sol:
                        continue
                    n_big = cross["big_n"]
                    # Supprimer C0
                    remaining_c0 = [s for s in b_sol if s[0] != 0 and s[1] != 0]
                    shifted_c0 = [(s[0]-1, s[1]-1, s[2], s[3]) for s in remaining_c0]
                    # Supprimer CN-1
                    remaining_cn = [s for s in b_sol if s[0] != n_big-1 and s[1] != n_big-1]
                    shifted_cn = [(s[0], s[1], s[2], s[3]) for s in remaining_cn]
                    # Vérifier si une de ces projections est valide sur le petit jeu
                    for projection in [shifted_c0, shifted_cn]:
                        try:
                            v = run_full_verification(cross["small_n"], projection)
                            if v.is_reliable:
                                projection_hits += 1
                                break
                        except Exception:
                            continue
                total_big = max(len(self.analysis_solutions if self.analysis_cables >= cross["big_n"] else self.cross_other_solutions), 1)
                lines.append("    Projections valides (C0 ou CN-1 supprimé) : {}/{} ({:.1f}%)".format(
                    projection_hits, total_big, 100.0 * projection_hits / total_big
                ))

        self.stats_text.delete("1.0", tk.END)
        self.stats_text.insert("1.0", "\n".join(lines))

    def _on_double_click_sim_row(self, _event=None):
        sel = self.sim_tree.selection()
        if not sel:
            return
        try:
            sol_idx = int(sel[0])
        except ValueError:
            return
        self.cmp_b_var.set(str(sol_idx + 1))
        self._update_side_by_side_comparison()

    def _export_primary_solution_csv(self):
        """Exporte la solution sélectionnée dans le panneau gauche (jeu principal) en CSV."""
        total = len(self.analysis_solutions)
        idx = self._parse_1based_index(self.cmp_a_var.get(), total)
        if idx is None:
            messagebox.showinfo("Pas de sélection", "Sélectionnez d'abord une solution dans le panneau gauche.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")],
            title="Exporter la solution A en CSV",
        )
        if not path:
            return
        export_solutions_csv(path, [self.analysis_solutions[idx]])
        messagebox.showinfo("Export terminé", "Solution A exportée dans :\n{}".format(path))

    def _export_cross_solution_csv(self):
        """Exporte la solution sélectionnée dans le panneau droit (jeu croisé) en CSV."""
        total = len(self.cross_other_solutions) if self.cross_other_solutions else 0
        if total == 0:
            messagebox.showinfo("Pas de jeu croisé", "Aucune solution dans le jeu croisé.")
            return
        idx = self._parse_1based_index(self.cmp_b_var.get(), total)
        if idx is None:
            idx = 0
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")],
            title="Exporter la solution B en CSV",
        )
        if not path:
            return
        sols = self.cross_other_solutions if self.cross_other_solutions else self.analysis_solutions
        export_solutions_csv(path, [sols[idx]])
        messagebox.showinfo("Export terminé", "Solution B exportée dans :\n{}".format(path))

    def _send_prefix_to_mso(self):
        """Prend les 6 premières portes de la solution A et les envoie comme préfixe dans MSO."""
        total = len(self.analysis_solutions)
        idx = self._parse_1based_index(self.cmp_a_var.get(), total)
        if idx is None:
            messagebox.showinfo("Pas de solution", "Sélectionnez d'abord une solution A.")
            return
        sol = self.analysis_solutions[idx]
        if not sol:
            return
        # Prendre jusqu'à 6 premières portes
        prefix_count = min(6, len(sol))
        prefix = sol[:prefix_count]
        prefix_text = format_connections_plain_text(prefix)
        # Mettre à jour le champ préfixe dans MSO
        dest_ok = False
        if hasattr(self, "prefix_text"):
            self.prefix_text.delete("1.0", tk.END)
            self.prefix_text.insert("1.0", format_connections_plain_text(prefix))
            dest_ok = True
        elif hasattr(self, "prefix_var"):
            self.prefix_var.set(format_connections_plain_text(prefix).replace("\n", ", "))
            dest_ok = True
        if dest_ok:
            self._append_search_log("[ Analyse ] Préfixe importé vers MSO: {} portes".format(len(prefix)))
            if hasattr(self, "l1_result_var"):
                self.l1_result_var.set("→ MSO: {} portes".format(len(prefix)))
            self.notebook.select(0)
        else:
            messagebox.showinfo("Pas de champ préfixe", "L'onglet MSO n'a pas de champ préfixe disponible.")

    def on_close(self):
        self._save_current_settings()
        self._cleanup_search_process(force=True)
        stop_api_server()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Serveur API HTTP local (contrôle à distance)
# ---------------------------------------------------------------------------

class APIHandler(http.server.BaseHTTPRequestHandler):
    """Handler REST pour piloter l'application Tkinter à distance."""

    def log_message(self, format, *args):
        pass  # Silencieux

    def _check_auth(self):
        params = parse_qs(urlparse(self.path).query)
        tokens = params.get("token", [])
        if tokens and tokens[0] == API_TOKEN:
            return True
        if self.headers.get("X-API-Token") == API_TOKEN:
            return True
        self.send_response(403)
        self.end_headers()
        self.wfile.write(json.dumps({"error": "invalid token"}).encode())
        return False

    def _json_ok(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def _json_error(self, msg, code=400):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}, ensure_ascii=False).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except Exception:
                return raw.decode("utf-8", errors="replace")
        return {}

    def _get_app(self):
        global _app_instance
        return _app_instance

    def do_GET(self):
        if not self._check_auth():
            return
        app = self._get_app()
        if app is None:
            self._json_error("Application not ready", 503)
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        try:
            if path == "/status" or path == "":
                self._json_ok(app._ui_call(lambda: self._build_status(app)))
            elif path == "/collections":
                self._json_ok(app._ui_call(lambda: self._build_collections(app)))
            elif path == "/solutions":
                n_str = params.get("n", [None])[0]
                if not n_str or not n_str.isdigit():
                    self._json_error("Missing or invalid ?n=N parameter")
                    return
                n = int(n_str)
                self._json_ok(app._ui_call(lambda: self._build_solutions(app, n)))
            elif path == "/analysis":
                self._json_ok(app._ui_call(lambda: self._build_analysis_state(app)))
            elif path == "/search-log":
                self._json_ok(app._ui_call(lambda: {"log": app.search_log.get("1.0", tk.END)}))
            elif path == "/verify-status":
                self._json_ok(app._ui_call(lambda: self._build_verify_status(app)))
            else:
                self._json_error("Not found", 404)
        except TimeoutError as exc:
            self._json_error(str(exc), 504)
        except Exception as exc:
            self._json_error("GET error: {}".format(exc), 500)

    def do_POST(self):
        if not self._check_auth():
            return
        app = self._get_app()
        if app is None:
            self._json_error("Application not ready", 503)
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)
        body = self._read_body()
        if not isinstance(body, dict):
            body = {}

        try:
            if path == "/set-prefix":
                prefix_str = self._format_prefix(body.get("prefix", ""))
                res = app._ui_call(lambda: self._apply_prefix(app, prefix_str))
                self._json_ok(res)
            elif path == "/switch-tab":
                idx_str = params.get("index", [str(body.get("index", "0"))])[0]
                res = app._ui_call(lambda: self._switch_tab(app, int(idx_str)))
                self._json_ok(res)
            elif path == "/search":
                res = app._ui_call(lambda: self._start_search(app, body, params))
                self._json_ok(res)
            elif path == "/stop-search":
                app._ui_call(lambda: app.stop_search())
                self._json_ok({"status": "stop_requested"})
            elif path == "/run-analysis":
                app._ui_call(lambda: app._run_similarity_analysis())
                self._json_ok({"status": "analysis_run"})
            elif path == "/import-solutions":
                res = app._ui_call(lambda: self._import_solutions(app, body))
                self._json_ok(res)
            elif path == "/select-solution":
                res = app._ui_call(lambda: self._select_solution(app, body))
                self._json_ok(res)
            elif path == "/select-primary":
                n_val = body.get("n", params.get("n", [None])[0])
                res = app._ui_call(lambda: self._select_primary(app, n_val))
                self._json_ok(res)
            elif path == "/send-to-mso":
                app._ui_call(lambda: app.send_selected_to_verifier())
                self._json_ok({"status": "sent_to_mso"})
            else:
                self._json_error("Not found", 404)
        except TimeoutError as exc:
            self._json_error(str(exc), 504)
        except Exception as exc:
            self._json_error("POST error: {}".format(exc), 500)

    # ---------------------- Helpers de payload ----------------------

    @staticmethod
    def _format_prefix(raw):
        """Accepte une liste de paires ou une chaîne -> texte un tuple par ligne."""
        if isinstance(raw, (list, tuple)):
            lines = []
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    up = item[2] if len(item) > 2 else "OR"
                    low = item[3] if len(item) > 3 else "AND"
                    lines.append("({}, {}, {!r}, {!r})".format(int(item[0]), int(item[1]), str(up), str(low)))
            return "\n".join(lines)
        text = str(raw or "")
        # Corrige les "\n" littéraux envoyés par certains clients JSON
        text = text.replace("\\n", "\n").replace("\\t", "\t")
        return text.strip()

    def _apply_prefix(self, app, prefix_str):
        if not hasattr(app, "prefix_text"):
            raise RuntimeError("Champ préfixe indisponible")
        app.prefix_text.delete("1.0", tk.END)
        app.prefix_text.insert("1.0", prefix_str)
        # Valide immédiatement pour renvoyer une erreur claire
        try:
            parsed = parse_connections_text(prefix_str) if prefix_str else []
        except WiringValidationError as exc:
            raise ValueError("Préfixe invalide : {}".format(exc))
        return {"status": "prefix_set", "gates": len(parsed), "prefix": prefix_str}

    def _switch_tab(self, app, idx):
        end = app.notebook.index("end")
        if not (0 <= idx < end):
            raise ValueError("Tab index out of range [0, {})".format(end))
        app.notebook.select(idx)
        return {"tab": idx, "tab_title": app.notebook.tab(idx, "text")}

    def _start_search(self, app, body, params):
        def pick(name, default):
            if name in body and body[name] is not None:
                return body[name]
            if name in params:
                return params[name][0]
            return default

        n = int(pick("cables", app.last_search_config.num_cables if app.last_search_config else 4))
        start = int(pick("start_size", 1))
        max_size = int(pick("max_size", 0))
        max_sol = int(pick("max_solutions", 5))
        workers = int(pick("workers", int(app.workers_var.get())))

        app.cables_var.set(str(n))
        app.start_size_var.set(str(start))
        app.max_size_var.set(str(max_size))
        app.max_sol_var.set(str(max_sol))
        app.workers_var.set(str(workers))

        prefix_raw = body.get("prefix", "")
        if prefix_raw:
            prefix_str = self._format_prefix(prefix_raw)
            app.prefix_text.delete("1.0", tk.END)
            app.prefix_text.insert("1.0", prefix_str)
            parse_connections_text(prefix_str)  # lève si invalide

        app.start_search()
        running = bool(app.coord_proc is not None and app.coord_proc.is_alive())
        return {
            "status": "search_started" if running else "search_not_started",
            "cables": n,
            "start_size": start,
            "max_size": max_size,
            "max_solutions": max_sol,
            "workers": workers,
            "running": running,
        }

    def _import_solutions(self, app, body):
        n = int(body.get("n", body.get("cables", 0)))
        sols_raw = body.get("solutions", [])
        if n < 2 or not sols_raw:
            raise ValueError("Need n>=2 and a non-empty solutions array")
        parsed_sols = []
        for sol in sols_raw:
            steps = []
            # PowerShell ConvertFrom-Json enrobe parfois les tableaux
            # dans des objets {value:[...], Count:N}
            raw_steps = getattr(sol, "value", sol) if hasattr(sol, "value") else sol
            if isinstance(raw_steps, (list, tuple)):
                for step in raw_steps:
                    raw_step = getattr(step, "value", step) if hasattr(step, "value") else step
                    if isinstance(raw_step, (list, tuple)) and len(raw_step) >= 2:
                        up = str(raw_step[2]) if len(raw_step) > 2 else "OR"
                        low = str(raw_step[3]) if len(raw_step) > 3 else "AND"
                        steps.append((int(raw_step[0]), int(raw_step[1]), up, low))
            if steps:
                parsed_sols.append(tuple(steps))
        if not parsed_sols:
            raise ValueError("No valid solutions in payload (got {} top-level items)".format(len(sols_raw)))
        app._store_collection_for_n(n, parsed_sols, set_as_primary=True, refresh_ui=True)
        return {"status": "imported", "n": n, "count": len(parsed_sols)}

    def _select_solution(self, app, body):
        n = int(body.get("n", 0))
        idx = int(body.get("index", 1)) - 1
        target = str(body.get("target", "A")).upper()
        sols = app.collections_by_n.get(n)
        if sols is None:
            raise ValueError("Collection n={} introuvable".format(n))
        if not (0 <= idx < len(sols)):
            raise ValueError("Index hors bornes (1..{})".format(len(sols)))
        if target == "A":
            app.cmp_a_var.set(str(idx + 1))
        else:
            app.cmp_b_var.set(str(idx + 1))
        app._update_side_by_side_comparison()
        return {"status": "selected", "n": n, "index": idx + 1, "target": target,
                "steps": len(sols[idx])}

    def _select_primary(self, app, n_val):
        if n_val is None or n_val == "":
            raise ValueError("Paramètre n manquant")
        try:
            n = int(n_val)
        except (ValueError, TypeError):
            raise ValueError("n invalide: {!r}".format(n_val))
        if n not in app.collections_by_n:
            raise ValueError("n={} absent des collections (disponibles: {})".format(n, sorted(app.collections_by_n.keys())))
        app._refresh_collection_selectors(preferred_primary_n=n)
        return {"status": "primary_set", "n": n}

    # ---------------------- Constructeurs d'état ----------------------

    def _build_collections(self, app):
        cols = {}
        for n, sols in app.collections_by_n.items():
            cols[str(n)] = {"count": len(sols), "max_steps": max((len(s) for s in sols), default=0)}
        return cols

    def _build_solutions(self, app, n):
        sols = app.collections_by_n.get(n, [])
        sols_out = []
        for idx, sol in enumerate(sols[:200]):
            sols_out.append({"index": idx + 1, "steps": len(sol),
                             "gates": [list(s) for s in sol]})
        return {"n": n, "count": len(sols), "solutions": sols_out}

    def _build_verify_status(self, app):
        summary = getattr(app, "last_verify_summary", None)
        if summary is None:
            return {"verified": False}
        return {
            "num_cables": summary.num_cables,
            "connections_count": len(summary.connections),
            "is_reliable": summary.is_reliable,
            "failing_count": len(summary.failing_indices),
            "total_rows": len(summary.truth_table),
        }

    def _build_status(self, app):
        """Construit un résumé complet de l'état de l'application."""
        collections_info = {}
        for n, sols in app.collections_by_n.items():
            collections_info[str(n)] = {"count": len(sols), "max_steps": max((len(s) for s in sols), default=0)}
        current_tab = app.notebook.index("current") if hasattr(app, "notebook") else -1
        tab_titles = []
        if hasattr(app, "notebook"):
            for i in range(app.notebook.index("end")):
                tab_titles.append(app.notebook.tab(i, "text"))
        prefix_val = ""
        if hasattr(app, "prefix_text"):
            try:
                prefix_val = app.prefix_text.get("1.0", tk.END).strip()
            except Exception:
                pass
        search_running = app.coord_proc is not None and app.coord_proc.is_alive() if hasattr(app, "coord_proc") else False
        return {
            "app": "LogicNetworkExplorer",
            "collections": collections_info,
            "current_tab": current_tab,
            "tab_titles": tab_titles,
            "search_running": search_running,
            "prefix": prefix_val,
            "analysis_cables": app.analysis_cables if hasattr(app, "analysis_cables") else 0,
            "analysis_solutions_count": len(app.analysis_solutions) if hasattr(app, "analysis_solutions") else 0,
            "cross_other_cables": app.cross_other_cables if hasattr(app, "cross_other_cables") else 0,
            "cross_other_count": len(app.cross_other_solutions) if hasattr(app, "cross_other_solutions") else 0,
            "found_solutions_count": len(app.found_solutions) if hasattr(app, "found_solutions") else 0,
        }

    def _build_analysis_state(self, app):
        """Renvoie l'état détaillé de l'onglet Analyse."""
        return {
            "primary": {
                "n": app.analysis_cables if hasattr(app, "analysis_cables") else 0,
                "count": len(app.analysis_solutions) if hasattr(app, "analysis_solutions") else 0,
            },
            "cross": {
                "n": app.cross_other_cables if hasattr(app, "cross_other_cables") else 0,
                "count": len(app.cross_other_solutions) if hasattr(app, "cross_other_solutions") else 0,
            },
            "cmp_a": app.cmp_a_var.get() if hasattr(app, "cmp_a_var") else "1",
            "cmp_b": app.cmp_b_var.get() if hasattr(app, "cmp_b_var") else "2",
        }


def start_api_server(app):
    """Démarre le serveur HTTP local dans un thread daemon."""
    global _app_instance, _httpd_instance
    _app_instance = app

    class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        server = ThreadedServer(("127.0.0.1", API_PORT), APIHandler)
        _httpd_instance = server
        t = threading.Thread(target=server.serve_forever, daemon=True, name="api-server")
        t.start()
        # Écrire un message dans le log de l'application
        if hasattr(app, "_append_search_log"):
            app._append_search_log("[ API ] Serveur local demarre sur http://127.0.0.1:{}".format(API_PORT))
    except Exception as exc:
        if hasattr(app, "_append_search_log"):
            app._append_search_log("[ API ] Impossible de demarrer le serveur: {}".format(exc))


def stop_api_server():
    """Arrête le serveur HTTP local."""
    global _httpd_instance
    if _httpd_instance is not None:
        try:
            _httpd_instance.shutdown()
        except Exception:
            pass
        _httpd_instance = None


def main():
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = LogicNetworkApp(root)
    # Démarrer le serveur API après la construction de l'UI
    start_api_server(app)
    root.mainloop()
    stop_api_server()


if __name__ == "__main__":
    main()
