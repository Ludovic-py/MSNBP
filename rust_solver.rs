// rust_solver.rs
// Moteur natif de recherche de reseaux de tri binaires minimaux
// (Couches BFS + deduplication d'etats sharde + masques de bits u64 / u128 + multi-threading).
// Version optimisee RAM :
//   - worker_buckets compactes (u32,u8) au lieu de ([W;MAX_N],u32,u8)
//   - option --prefix pour fixer des portes initiales (L1/L2)
//   - coupe de commutation Foata
//   - filtre final ecart=1
//
// Zero dependance externe (bibliotheque standard uniquement, Rust 1.63+).
// Compilation Windows (PowerShell) :
//   rustc -O -C target-cpu=native rust_solver.rs -o rust_solver.exe
//
// Compilation Linux / macOS :
//   rustc -O -C target-cpu=native rust_solver.rs -o rust_solver

use std::collections::HashSet;
use std::env;
use std::hash::{BuildHasherDefault, Hash, Hasher};
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------------------
// Fast non-cryptographic FxHasher (zero external crate)
// ---------------------------------------------------------------------------

#[derive(Default)]
struct FxHasher {
    hash: u64,
}

impl FxHasher {
    const K: u64 = 0x517cc1b727220a95;

    #[inline]
    fn add_to_hash(&mut self, i: u64) {
        self.hash = (self.hash.rotate_left(5) ^ i).wrapping_mul(Self::K);
    }
}

impl Hasher for FxHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.hash
    }

    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut chunks = bytes.chunks_exact(8);
        for chunk in &mut chunks {
            let v = u64::from_ne_bytes([
                chunk[0], chunk[1], chunk[2], chunk[3], chunk[4], chunk[5], chunk[6], chunk[7],
            ]);
            self.add_to_hash(v);
        }
        for &b in chunks.remainder() {
            self.add_to_hash(b as u64);
        }
    }

    #[inline]
    fn write_u64(&mut self, i: u64) {
        self.add_to_hash(i);
    }

    #[inline]
    fn write_u128(&mut self, i: u128) {
        self.add_to_hash(i as u64);
        self.add_to_hash((i >> 64) as u64);
    }
}

type FastHashSet<T> = HashSet<T, BuildHasherDefault<FxHasher>>;

#[inline]
fn shard_for<T: Hash>(val: &T, num_shards: usize) -> usize {
    let mut h = FxHasher::default();
    val.hash(&mut h);
    ((h.finish() >> 32) as usize) % num_shards
}

// ---------------------------------------------------------------------------
// Generic Bitmask Word (u64 for <= 6 cables, u128 for 7 cables, [u64;2] for 8 cables)
// ---------------------------------------------------------------------------

trait MaskWord: Copy + Eq + Hash + Send + Sync + Default + 'static {
    fn zero() -> Self;
    fn bit(pos: usize) -> Self;
    fn bitor(self, rhs: Self) -> Self;
    fn bitand(self, rhs: Self) -> Self;
}

impl MaskWord for u64 {
    #[inline]
    fn zero() -> Self { 0 }
    #[inline]
    fn bit(pos: usize) -> Self { 1u64 << pos }
    #[inline]
    fn bitor(self, rhs: Self) -> Self { self | rhs }
    #[inline]
    fn bitand(self, rhs: Self) -> Self { self & rhs }
}

impl MaskWord for u128 {
    #[inline]
    fn zero() -> Self { 0 }
    #[inline]
    fn bit(pos: usize) -> Self { 1u128 << pos }
    #[inline]
    fn bitor(self, rhs: Self) -> Self { self | rhs }
    #[inline]
    fn bitand(self, rhs: Self) -> Self { self & rhs }
}

// u256 pour N=8 (2 × u128)
#[derive(Copy, Clone, Default, Hash, Eq, PartialEq)]
struct U256(u128, u128);

impl MaskWord for U256 {
    #[inline]
    fn zero() -> Self { U256(0, 0) }
    #[inline]
    fn bit(pos: usize) -> Self {
        if pos < 128 { U256(1u128 << pos, 0) } else { U256(0, 1u128 << (pos - 128)) }
    }
    #[inline]
    fn bitor(self, rhs: Self) -> Self { U256(self.0 | rhs.0, self.1 | rhs.1) }
    #[inline]
    fn bitand(self, rhs: Self) -> Self { U256(self.0 & rhs.0, self.1 & rhs.1) }
}

// u384 pour N=9 (3 × u128)
#[derive(Copy, Clone, Default, Hash, Eq, PartialEq)]
struct U384(u128, u128, u128);

impl MaskWord for U384 {
    #[inline]
    fn zero() -> Self { U384(0, 0, 0) }
    #[inline]
    fn bit(pos: usize) -> Self {
        if pos < 128 { U384(1u128 << pos, 0, 0) }
        else if pos < 256 { U384(0, 1u128 << (pos - 128), 0) }
        else { U384(0, 0, 1u128 << (pos - 256)) }
    }
    #[inline]
    fn bitor(self, rhs: Self) -> Self { U384(self.0 | rhs.0, self.1 | rhs.1, self.2 | rhs.2) }
    #[inline]
    fn bitand(self, rhs: Self) -> Self { U384(self.0 & rhs.0, self.1 & rhs.1, self.2 & rhs.2) }
}

fn initial_wire_masks<W: MaskWord, const MAX_N: usize>(n: usize) -> [W; MAX_N] {
    let total_combos = 1usize << n;
    let mut masks = [W::zero(); MAX_N];
    for c in 0..n {
        let shift = n - 1 - c;
        let mut mask = W::zero();
        for combo in 0..total_combos {
            if ((combo >> shift) & 1) != 0 {
                mask = mask.bitor(W::bit(total_combos - 1 - combo));
            }
        }
        masks[c] = mask;
    }
    masks
}

fn apply_gate_to_state<W: MaskWord, const MAX_N: usize>(
    state: &[W; MAX_N], c1: usize, c2: usize
) -> ([W; MAX_N], [W; MAX_N]) {
    let m1 = state[c1];
    let m2 = state[c2];
    let new_m1 = m1.bitor(m2);
    let new_m2 = m1.bitand(m2);
    let mut s1 = *state;
    let mut s2 = *state;
    s1[c1] = new_m1;
    s1[c2] = new_m2;
    s2[c1] = new_m2;
    s2[c2] = new_m1;
    (s1, s2)
}

#[inline]
fn is_descending<W: MaskWord, const MAX_N: usize>(masks: &[W; MAX_N], n: usize) -> bool {
    let mut i = 0;
    while i + 1 < n {
        if masks[i].bitor(masks[i + 1]) != masks[i] {
            return false;
        }
        i += 1;
    }
    true
}

fn generate_gates(n: usize, final_span_1: bool) -> Vec<(u8, u8)> {
    let mut gates = Vec::new();
    if final_span_1 {
        for i in 0..(n - 1) {
            gates.push((i as u8, (i + 1) as u8));
        }
    } else {
        for i in 0..n {
            for j in (i + 1)..n {
                gates.push((i as u8, j as u8));
            }
        }
    }
    gates
}

/// Reconstruit le chemin d'une solution.
fn reconstruct_path(
    target_depth: usize,
    mut parent_idx: u32,
    last_gate: u8,
    parents: &[Vec<(u32, u8)>],
    gates: &[(u8, u8)],
) -> Vec<(u8, u8)> {
    let mut gate_indices = Vec::with_capacity(target_depth);
    gate_indices.push(last_gate);
    let max_idx = parents.len().saturating_sub(1);
    for d in (1..target_depth).rev() {
        if d <= max_idx && (parent_idx as usize) < parents[d].len() {
            let (prev_parent, prev_gate) = parents[d][parent_idx as usize];
            gate_indices.push(prev_gate);
            parent_idx = prev_parent;
        }
    }
    gate_indices.reverse();
    gate_indices
        .into_iter()
        .map(|g| gates[g as usize])
        .collect()
}

fn format_solution_line(seq: &[(u8, u8)]) -> String {
    let mut out = String::from("SOLUTION ");
    for (i, &(c1, c2)) in seq.iter().enumerate() {
        if i > 0 { out.push(';'); }
        out.push_str(&format!("{},{}", c1, c2));
    }
    out
}

fn emit_line(lock: &Mutex<()>, line: &str) {
    let _guard = lock.lock().unwrap();
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    let _ = writeln!(handle, "{}", line);
    let _ = handle.flush();
}

/// Applique les portes d'un préfixe (OR/AND) à l'état initial.
fn apply_prefix<W: MaskWord, const MAX_N: usize>(
    init: &[W; MAX_N], prefix: &[(u8, u8)], _n: usize
) -> [W; MAX_N] {
    let mut state = *init;
    for &(c1_u8, c2_u8) in prefix {
        let c1 = c1_u8 as usize;
        let c2 = c2_u8 as usize;
        let m1 = state[c1];
        let m2 = state[c2];
        state[c1] = m1.bitor(m2);
        state[c2] = m1.bitand(m2);
    }
    state
}

// ---------------------------------------------------------------------------
// Sharded Parallel Layer BFS with RAM-conscious Cleanup
// ---------------------------------------------------------------------------

struct Config {
    num_cables: usize,
    start_size: usize,
    max_size: usize,
    max_solutions: usize,
    workers: usize,
    prune_noops: bool,
    foata_cut: bool,
    final_span_1: bool,
    prefix: Vec<(u8, u8)>,
}

fn run_layer_bfs<W: MaskWord, const MAX_N: usize>(cfg: &Config) {
    let start_instant = Instant::now();
    let gates = generate_gates(cfg.num_cables, false);
    let span1_gates = generate_gates(cfg.num_cables, true);
    // Correspondance porte-adjacente -> index dans la liste COMPLETE.
    // Indispensable : les index de portes stockes dans `parents` referencent
    // toujours la liste complete, meme quand la couche finale n'utilise que
    // les portes adjacentes.
    let span1_full_idx: Vec<u8> = span1_gates
        .iter()
        .map(|&eg| gates.iter().position(|&g| g == eg).unwrap_or(0) as u8)
        .collect();
    let init_state_base = initial_wire_masks::<W, MAX_N>(cfg.num_cables);

    // Appliquer le préfixe optionnel
    let init_state = if cfg.prefix.is_empty() {
        init_state_base
    } else {
        apply_prefix::<W, MAX_N>(&init_state_base, &cfg.prefix, cfg.num_cables)
    };

    let stdout_lock = Mutex::new(());
    let prefix_desc = if cfg.prefix.is_empty() { String::new() } else { format!(" | Prefix={}", cfg.prefix.len()) };
    emit_line(
        &stdout_lock,
        &format!(
            "LOG [ Rust ] Cables={} | Size={}..{} | MaxSol={} | Workers={} | Prune={} | Foata={} | Span1={}{}",
            cfg.num_cables,
            cfg.start_size,
            cfg.max_size,
            cfg.max_solutions,
            cfg.workers,
            cfg.prune_noops,
            cfg.foata_cut,
            cfg.final_span_1,
            prefix_desc
        ),
    );

    let num_shards = (cfg.workers * 4).max(8);
    let mut visited_shards: Vec<FastHashSet<[W; MAX_N]>> = (0..num_shards)
        .map(|_| FastHashSet::default())
        .collect();
    visited_shards[shard_for(&init_state, num_shards)].insert(init_state);

    let mut frontier: Vec<[W; MAX_N]> = vec![init_state];
    let mut parents: Vec<Vec<(u32, u8)>> = vec![Vec::new()];

    let sols_found = AtomicUsize::new(0);
    let stop_flag = AtomicBool::new(false);
    let total_checks = AtomicU64::new(0);

    for depth in 1..=cfg.max_size {
        if stop_flag.load(Ordering::Relaxed) || frontier.is_empty() {
            if frontier.is_empty() && !stop_flag.load(Ordering::Relaxed) {
                emit_line(&stdout_lock, &format!("LOG [ Info ] Frontier exhausted at depth {}.", depth - 1));
            }
            break;
        }

        let total_states = frontier.len();
        let use_final_span1 = cfg.final_span_1 && depth == cfg.max_size;
        let effective_gates: &Vec<(u8, u8)> = if use_final_span1 {
            &span1_gates
        } else {
            &gates
        };
        let effective_num_gates = effective_gates.len();

        emit_line(
            &stdout_lock,
            &format!(
                "LOG [ Layer {} ] Expanding {} unique states ({} gate choices/state)...",
                depth, total_states, effective_num_gates
            ),
        );

        let layer_start = Instant::now();
        let states_done = AtomicUsize::new(0);
        let phase1_done = AtomicBool::new(false);
        let need_next_frontier = depth < cfg.max_size;
        let active_workers = cfg.workers.min(total_states).max(1);
        let chunk_size = (total_states + active_workers - 1) / active_workers;

        // Phase 1: Expansion parallele — buckets compacts (u32, u8) = 8 octets au lieu de 128
        let worker_buckets: Vec<Vec<Vec<([W; MAX_N], u32, u8)>>> = thread::scope(|s| {
            let sd_ref = &states_done;
            let tc_ref = &total_checks;
            let sf_ref = &sols_found;
            let p1_ref = &phase1_done;
            let lock_ref = &stdout_lock;
            let reporter = s.spawn(move || {
                while !p1_ref.load(Ordering::Relaxed) {
                    thread::sleep(Duration::from_millis(150));
                    if p1_ref.load(Ordering::Relaxed) { break; }
                    let done = sd_ref.load(Ordering::Relaxed);
                    let checks = tc_ref.load(Ordering::Relaxed);
                    let sols = sf_ref.load(Ordering::Relaxed);
                    let elapsed_ms = start_instant.elapsed().as_millis() as u64;
                    let layer_ms = layer_start.elapsed().as_millis() as u64;
                    let eta_ms: i64 = if done > 0 && done < total_states {
                        let rate = (done as f64) / ((layer_ms.max(1) as f64) / 1000.0);
                        (((total_states - done) as f64) / rate * 1000.0) as i64
                    } else if done >= total_states { 0 } else { -1 };
                    emit_line(lock_ref, &format!(
                        "PROGRESS {} {} {} {} {} {} {}",
                        depth, done, total_states, checks, sols, elapsed_ms, eta_ms
                    ));
                }
            });

            let mut handles = Vec::with_capacity(active_workers);
            let frontier_ref = &frontier;
            let visited_ref = &visited_shards;
            let parents_ref = &parents;
            let gates_ref = &effective_gates;
            // Liste COMPLETE des portes + correspondance span1 -> index complet :
            // les index stockes dans `parents` et les reconstructions de chemin
            // doivent toujours passer par la liste complete.
            let full_gates_ref = &gates;
            let span1_map_ref = &span1_full_idx;
            let use_span1 = use_final_span1;
            let stop_ref = &stop_flag;
            let foata_ref = &cfg.foata_cut;

            for w_idx in 0..active_workers {
                let start_i = w_idx * chunk_size;
                if start_i >= total_states { break; }
                let end_i = (start_i + chunk_size).min(total_states);

                handles.push(s.spawn(move || {
                    // Buckets avec état complet (nécessaire pour Phase 2)
                    let mut buckets: Vec<Vec<([W; MAX_N], u32, u8)>> = if need_next_frontier {
                        (0..num_shards).map(|_| Vec::new()).collect()
                    } else {
                        Vec::new()
                    };

                    let mut local_done: usize = 0;
                    let mut local_checks: u64 = 0;

                    for idx in start_i..end_i {
                        if stop_ref.load(Ordering::Relaxed) { break; }
                        let state = frontier_ref[idx];

                        // Récupérer la dernière porte pour la coupe Foata
                        // (toujours via la liste COMPLETE).
                        let mut last_gate: Option<(u8, u8)> = None;
                        if *foata_ref && depth > 1 && (idx as usize) < parents_ref.len() && !parents_ref[parents_ref.len()-1].is_empty() {
                            let parent_info = parents_ref[parents_ref.len()-1][idx as usize];
                            if (parent_info.1 as usize) < full_gates_ref.len() {
                                last_gate = Some(full_gates_ref[parent_info.1 as usize]);
                            }
                        }

                        for (g_idx, &(c1_u8, c2_u8)) in gates_ref.iter().enumerate() {
                            let c1 = c1_u8 as usize;
                            let c2 = c2_u8 as usize;

                            // Coupe de commutation Foata
                            if let Some((la, lb)) = last_gate {
                                let a = la as usize;
                                let b = lb as usize;
                                // Si (c1,c2) est disjointe de (a,b) et que (c1,c2) < (a,b) canoniquement, on skip
                                let disjoint = c1 != a && c1 != b && c2 != a && c2 != b;
                                if disjoint && (c1 < a || (c1 == a && c2 < b)) {
                                    local_checks += 1;
                                    continue;
                                }
                            }

                            let m1 = state[c1];
                            let m2 = state[c2];
                            let new_m1 = m1.bitor(m2);
                            let new_m2 = m1.bitand(m2);
                            local_checks += 1;

                            if cfg.prune_noops && new_m1 == m1 && new_m2 == m2 {
                                continue;
                            }

                            // Construire le nouvel état
                            let mut new_state = state;
                            new_state[c1] = new_m1;
                            new_state[c2] = new_m2;

                            // Index dans la liste COMPLETE (pour parents / reconstruction)
                            let full_g_idx: u8 = if use_span1 {
                                *span1_map_ref.get(g_idx).unwrap_or(&(g_idx as u8))
                            } else {
                                g_idx as u8
                            };

                            if depth >= cfg.start_size && is_descending(&new_state, cfg.num_cables) {
                                let prev = sf_ref.fetch_add(1, Ordering::Relaxed);
                                if prev < cfg.max_solutions {
                                    let seq = reconstruct_path(
                                        depth, idx as u32, full_g_idx, parents_ref, full_gates_ref
                                    );
                                    emit_line(lock_ref, &format_solution_line(&seq));
                                    if prev + 1 >= cfg.max_solutions {
                                        stop_ref.store(true, Ordering::Relaxed);
                                        break;
                                    }
                                } else {
                                    stop_ref.store(true, Ordering::Relaxed);
                                    break;
                                }
                                continue;
                            }

                            if need_next_frontier {
                                let sh = shard_for(&new_state, num_shards);
                                if !visited_ref[sh].contains(&new_state) {
                                    // Toujours stocker l'index COMPLET dans parents
                                    buckets[sh].push((new_state, idx as u32, full_g_idx));
                                }
                            }
                        }

                        local_done += 1;
                        if (local_done & 255) == 0 {
                            sd_ref.fetch_add(local_done, Ordering::Relaxed);
                            tc_ref.fetch_add(local_checks, Ordering::Relaxed);
                            local_done = 0;
                            local_checks = 0;
                        }
                    }

                    if local_done > 0 { sd_ref.fetch_add(local_done, Ordering::Relaxed); }
                    if local_checks > 0 { tc_ref.fetch_add(local_checks, Ordering::Relaxed); }
                    buckets
                }));
            }

            let collected: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
            p1_ref.store(true, Ordering::Relaxed);
            let _ = reporter.join();
            collected
        });

        // Decharge la frontiere courante
        frontier = Vec::new();

        let done_now = states_done.load(Ordering::Relaxed);
        let checks_now = total_checks.load(Ordering::Relaxed);
        let sols_now = sols_found.load(Ordering::Relaxed).min(cfg.max_solutions);
        let elapsed_ms = start_instant.elapsed().as_millis() as u64;
        emit_line(&stdout_lock, &format!(
            "PROGRESS {} {} {} {} {} {} 0",
            depth, done_now, total_states, checks_now, sols_now, elapsed_ms
        ));

        if stop_flag.load(Ordering::Relaxed) || !need_next_frontier {
            break;
        }

        // Phase 2: Deduplication — on recalcule new_state à partir de (parent_idx, gate_idx)
        // au lieu de stocker le state complet dans les buckets.
        let shards_per_worker = (num_shards + cfg.workers - 1) / cfg.workers;
        let merged_parts: Vec<(Vec<[W; MAX_N]>, Vec<(u32, u8)>)> = thread::scope(|s| {
            let mut handles = Vec::with_capacity(cfg.workers);
            let wb_ref = &worker_buckets;
            for (chunk_idx, shard_slice) in visited_shards.chunks_mut(shards_per_worker).enumerate() {
                let base_shard = chunk_idx * shards_per_worker;
                handles.push(s.spawn(move || {
                    let mut local_states = Vec::new();
                    let mut local_parents = Vec::new();
                    for (offset, shard_set) in shard_slice.iter_mut().enumerate() {
                        let sh = base_shard + offset;
                        for w_buckets in wb_ref {
                            for &(state, parent_idx, gate_idx) in &w_buckets[sh] {
                                if shard_set.insert(state) {
                                    local_states.push(state);
                                    local_parents.push((parent_idx, gate_idx));
                                }
                            }
                        }
                    }
                    (local_states, local_parents)
                }));
            }
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });

        drop(worker_buckets);

        let total_next: usize = merged_parts.iter().map(|p| p.0.len()).sum();
        let mut next_frontier = Vec::with_capacity(total_next);
        let mut next_parents = Vec::with_capacity(total_next);
        for (mut st, mut pa) in merged_parts {
            next_frontier.append(&mut st);
            next_parents.append(&mut pa);
        }

        frontier = next_frontier;
        parents.push(next_parents);
    }

    drop(frontier);
    drop(parents);
    drop(visited_shards);

    let final_checks = total_checks.load(Ordering::Relaxed);
    let final_sols = sols_found.load(Ordering::Relaxed).min(cfg.max_solutions);
    let final_ms = start_instant.elapsed().as_millis() as u64;
    emit_line(&stdout_lock, &format!("DONE {} {} {}", final_checks, final_ms, final_sols));
}

// ---------------------------------------------------------------------------
// CLI Entry Point
// ---------------------------------------------------------------------------

fn parse_args() -> Result<Config, String> {
    let args: Vec<String> = env::args().collect();
    let mut num_cables: usize = 4;
    let mut start_size: usize = 1;
    let mut max_size: usize = 16;
    let mut max_solutions: usize = 3;
    let mut workers: usize = thread::available_parallelism()
        .map(|n| n.get().saturating_sub(1).max(1))
        .unwrap_or(1);
    let mut prune_noops: bool = true;
    let mut foata_cut: bool = true;
    let mut final_span_1: bool = true;
    let mut prefix: Vec<(u8, u8)> = Vec::new();

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--cables" => {
                i += 1;
                num_cables = args.get(i).ok_or("Missing value for --cables")?.parse()
                    .map_err(|_| "Invalid --cables")?;
            }
            "--start-size" => {
                i += 1;
                start_size = args.get(i).ok_or("Missing value for --start-size")?.parse()
                    .map_err(|_| "Invalid --start-size")?;
            }
            "--max-size" => {
                i += 1;
                max_size = args.get(i).ok_or("Missing value for --max-size")?.parse()
                    .map_err(|_| "Invalid --max-size")?;
            }
            "--max-solutions" => {
                i += 1;
                max_solutions = args.get(i).ok_or("Missing value for --max-solutions")?.parse()
                    .map_err(|_| "Invalid --max-solutions")?;
            }
            "--workers" => {
                i += 1;
                workers = args.get(i).ok_or("Missing value for --workers")?.parse()
                    .map_err(|_| "Invalid --workers")?;
            }
            "--no-prune" => { prune_noops = false; }
            "--no-foata" => { foata_cut = false; }
            "--no-final-span-1" => { final_span_1 = false; }
            "--prefix" => {
                i += 1;
                let raw = args.get(i).ok_or("Missing value for --prefix")?;
                // Format: "0,1,2,3,4,5" -> [(0,1), (2,3), (4,5)]
                let parts: Vec<&str> = raw.split(",").collect();
                if parts.len() % 2 != 0 {
                    return Err(String::from("--prefix requires an even number of integers (c1,c2,c1,c2,...)"));
                }
                for p_idx in (0..parts.len()).step_by(2) {
                    let c1 = parts[p_idx].parse::<usize>()
                        .map_err(|_| format!("Invalid cable index in --prefix: {}", parts[p_idx]))?;
                    let c2 = parts[p_idx + 1].parse::<usize>()
                        .map_err(|_| format!("Invalid cable index in --prefix: {}", parts[p_idx + 1]))?;
                    if c1 >= num_cables || c2 >= num_cables {
                        return Err(format!("--prefix cable index out of range [0,{}): {} or {}", num_cables - 1, c1, c2));
                    }
                    prefix.push((c1 as u8, c2 as u8));
                }
            }
            "--help" | "-h" => {
                return Err(String::from(
                    "Usage: rust_solver --cables N --start-size S --max-size M --max-solutions K --workers W [--no-prune] [--no-foata] [--no-final-span-1] [--prefix c1,c2,c3,c4,...]",
                ));
            }
            other => { return Err(format!("Unknown argument: {}", other)); }
        }
        i += 1;
    }

    if num_cables < 2 || num_cables > 9 {
        return Err(format!("Unsupported --cables {} (Rust bitmask solver supports 2..=9 cables).", num_cables));
    }
    if num_cables == 8 && !prefix.is_empty() {
        // N=8 n'a de sens qu'avec un prefixe fort (sinon explosion combinatoire)
        // On ne bloque pas mais on emet un avertissement
    }
    if max_solutions < 1 { return Err(String::from("--max-solutions must be >= 1")); }
    if workers < 1 { workers = 1; }
    if max_size < start_size { return Err(String::from("--max-size must be >= --start-size")); }

    Ok(Config {
        num_cables,
        start_size,
        max_size,
        max_solutions,
        workers,
        prune_noops,
        foata_cut,
        final_span_1,
        prefix,
    })
}

fn main() {
    let cfg = match parse_args() {
        Ok(c) => c,
        Err(msg) => {
            eprintln!("ERROR {}", msg);
            std::process::exit(1);
        }
    };

    if cfg.num_cables <= 6 {
        run_layer_bfs::<u64, 6>(&cfg);
    } else if cfg.num_cables == 7 {
        run_layer_bfs::<u128, 7>(&cfg);
    } else if cfg.num_cables == 8 {
        run_layer_bfs::<U256, 8>(&cfg);
    } else {
        run_layer_bfs::<U384, 9>(&cfg);
    }
}