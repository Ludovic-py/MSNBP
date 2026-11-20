

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
// Generic Bitmask Word (u64 for <= 6 cables, u128 for 7 cables)
// ---------------------------------------------------------------------------

trait MaskWord: Copy + Eq + Hash + Send + Sync + Default + 'static {
    fn zero() -> Self;
    fn bit(pos: usize) -> Self;
    fn bitor(self, rhs: Self) -> Self;
    fn bitand(self, rhs: Self) -> Self;
}

impl MaskWord for u64 {
    #[inline]
    fn zero() -> Self {
        0
    }
    #[inline]
    fn bit(pos: usize) -> Self {
        1u64 << pos
    }
    #[inline]
    fn bitor(self, rhs: Self) -> Self {
        self | rhs
    }
    #[inline]
    fn bitand(self, rhs: Self) -> Self {
        self & rhs
    }
}

impl MaskWord for u128 {
    #[inline]
    fn zero() -> Self {
        0
    }
    #[inline]
    fn bit(pos: usize) -> Self {
        1u128 << pos
    }
    #[inline]
    fn bitor(self, rhs: Self) -> Self {
        self | rhs
    }
    #[inline]
    fn bitand(self, rhs: Self) -> Self {
        self & rhs
    }
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

fn generate_gates(n: usize) -> Vec<(u8, u8)> {
    let mut gates = Vec::new();
    for i in 0..n {
        for j in (i + 1)..n {
            gates.push((i as u8, j as u8));
        }
    }
    gates
}

/// Reconstruit le chemin d'une solution en ne gardant en memoire
/// que les couches `parents` strictement necessaires (`1..=target_depth`).
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
        if i > 0 {
            out.push(';');
        }
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

// ---------------------------------------------------------------------------
// Sharded Parallel Layer BFS avec nettoyage RAM
// ---------------------------------------------------------------------------

struct Config {
    num_cables: usize,
    start_size: usize,
    max_size: usize,
    max_solutions: usize,
    workers: usize,
    prune_noops: bool,
}

fn run_layer_bfs<W: MaskWord, const MAX_N: usize>(cfg: &Config) {
    let start_instant = Instant::now();
    let gates = generate_gates(cfg.num_cables);
    let num_gates = gates.len();
    let init_state = initial_wire_masks::<W, MAX_N>(cfg.num_cables);

    let stdout_lock = Mutex::new(());
    emit_line(
        &stdout_lock,
        &format!(
            "LOG [ Rust ] Cables={} | Size={}..{} | MaxSol={} | Workers={} | Prune={}",
            cfg.num_cables,
            cfg.start_size,
            cfg.max_size,
            cfg.max_solutions,
            cfg.workers,
            cfg.prune_noops
        ),
    );

    // Moins de shards = moins de buckets temporaires alloues par worker a chaque couche
    let num_shards = (cfg.workers * 4).max(8);
    let mut visited_shards: Vec<FastHashSet<[W; MAX_N]>> = (0..num_shards)
        .map(|_| FastHashSet::default())
        .collect();
    visited_shards[shard_for(&init_state, num_shards)].insert(init_state);

    let mut frontier: Vec<[W; MAX_N]> = vec![init_state];
    // `parents` est alloue a la demande couche par couche et libere des que non requis
    let mut parents: Vec<Vec<(u32, u8)>> = vec![Vec::new()];

    let sols_found = AtomicUsize::new(0);
    let stop_flag = AtomicBool::new(false);
    let total_checks = AtomicU64::new(0);

    for depth in 1..=cfg.max_size {
        if stop_flag.load(Ordering::Relaxed) || frontier.is_empty() {
            if frontier.is_empty() && !stop_flag.load(Ordering::Relaxed) {
                emit_line(
                    &stdout_lock,
                    &format!("LOG [ Info ] Frontier exhausted at depth {}.", depth - 1),
                );
            }
            break;
        }

        let total_states = frontier.len();
        emit_line(
            &stdout_lock,
            &format!(
                "LOG [ Layer {} ] Expanding {} unique states ({} gate choices/state)...",
                depth, total_states, num_gates
            ),
        );

        let layer_start = Instant::now();
        let states_done = AtomicUsize::new(0);
        let phase1_done = AtomicBool::new(false);
        let need_next_frontier = depth < cfg.max_size;
        let active_workers = cfg.workers.min(total_states).max(1);
        let chunk_size = (total_states + active_workers - 1) / active_workers;

        // Phase 1: Expansion parallele sans verrou sur `visited_shards`
        let worker_buckets: Vec<Vec<Vec<([W; MAX_N], u32, u8)>>> = thread::scope(|s| {
            let sd_ref = &states_done;
            let tc_ref = &total_checks;
            let sf_ref = &sols_found;
            let p1_ref = &phase1_done;
            let lock_ref = &stdout_lock;
            let reporter = s.spawn(move || {
                while !p1_ref.load(Ordering::Relaxed) {
                    thread::sleep(Duration::from_millis(150));
                    if p1_ref.load(Ordering::Relaxed) {
                        break;
                    }
                    let done = sd_ref.load(Ordering::Relaxed);
                    let checks = tc_ref.load(Ordering::Relaxed);
                    let sols = sf_ref.load(Ordering::Relaxed);
                    let elapsed_ms = start_instant.elapsed().as_millis() as u64;
                    let layer_ms = layer_start.elapsed().as_millis() as u64;
                    let eta_ms: i64 = if done > 0 && done < total_states {
                        let rate = (done as f64) / ((layer_ms.max(1) as f64) / 1000.0);
                        (((total_states - done) as f64) / rate * 1000.0) as i64
                    } else if done >= total_states {
                        0
                    } else {
                        -1
                    };
                    emit_line(
                        lock_ref,
                        &format!(
                            "PROGRESS {} {} {} {} {} {} {}",
                            depth, done, total_states, checks, sols, elapsed_ms, eta_ms
                        ),
                    );
                }
            });

            let mut handles = Vec::with_capacity(active_workers);
            let frontier_ref = &frontier;
            let visited_ref = &visited_shards;
            let parents_ref = &parents;
            let gates_ref = &gates;
            let stop_ref = &stop_flag;

            for w_idx in 0..active_workers {
                let start_i = w_idx * chunk_size;
                if start_i >= total_states {
                    break;
                }
                let end_i = (start_i + chunk_size).min(total_states);

                handles.push(s.spawn(move || {
                    let mut buckets: Vec<Vec<([W; MAX_N], u32, u8)>> = if need_next_frontier {
                        (0..num_shards).map(|_| Vec::new()).collect()
                    } else {
                        Vec::new()
                    };

                    let mut local_done: usize = 0;
                    let mut local_checks: u64 = 0;

                    for idx in start_i..end_i {
                        if stop_ref.load(Ordering::Relaxed) {
                            break;
                        }
                        let state = frontier_ref[idx];
                        for (g_idx, &(c1_u8, c2_u8)) in gates_ref.iter().enumerate() {
                            let c1 = c1_u8 as usize;
                            let c2 = c2_u8 as usize;
                            let m1 = state[c1];
                            let m2 = state[c2];
                            let new_m1 = m1.bitor(m2);
                            let new_m2 = m1.bitand(m2);
                            local_checks += 1;

                            if cfg.prune_noops && new_m1 == m1 && new_m2 == m2 {
                                continue;
                            }

                            let mut new_state = state;
                            new_state[c1] = new_m1;
                            new_state[c2] = new_m2;

                            if depth >= cfg.start_size && is_descending(&new_state, cfg.num_cables) {
                                let prev = sf_ref.fetch_add(1, Ordering::Relaxed);
                                if prev < cfg.max_solutions {
                                    let seq = reconstruct_path(
                                        depth,
                                        idx as u32,
                                        g_idx as u8,
                                        parents_ref,
                                        gates_ref,
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
                                    buckets[sh].push((new_state, idx as u32, g_idx as u8));
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

                    if local_done > 0 {
                        sd_ref.fetch_add(local_done, Ordering::Relaxed);
                    }
                    if local_checks > 0 {
                        tc_ref.fetch_add(local_checks, Ordering::Relaxed);
                    }
                    buckets
                }));
            }

            let collected: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
            p1_ref.store(true, Ordering::Relaxed);
            let _ = reporter.join();
            collected
        });

        // Decharge immediatement la frontiere courante avant de construire la suivante
        frontier = Vec::new();

        let done_now = states_done.load(Ordering::Relaxed);
        let checks_now = total_checks.load(Ordering::Relaxed);
        let sols_now = sols_found.load(Ordering::Relaxed).min(cfg.max_solutions);
        let elapsed_ms = start_instant.elapsed().as_millis() as u64;
        emit_line(
            &stdout_lock,
            &format!(
                "PROGRESS {} {} {} {} {} {} 0",
                depth, done_now, total_states, checks_now, sols_now, elapsed_ms
            ),
        );

        if stop_flag.load(Ordering::Relaxed) || !need_next_frontier {
            break;
        }

        // Phase 2: Deduplication parallele par shard dans `visited_shards`
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

        // Libere les buckets temporaires des workers aussitot fusionnes
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

    // Decharge explicitement les structures lourdes avant la sortie
    drop(frontier);
    drop(parents);
    drop(visited_shards);

    let final_checks = total_checks.load(Ordering::Relaxed);
    let final_sols = sols_found.load(Ordering::Relaxed).min(cfg.max_solutions);
    let final_ms = start_instant.elapsed().as_millis() as u64;
    emit_line(
        &stdout_lock,
        &format!("DONE {} {} {}", final_checks, final_ms, final_sols),
    );
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

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--cables" => {
                i += 1;
                num_cables = args
                    .get(i)
                    .ok_or("Missing value for --cables")?
                    .parse()
                    .map_err(|_| "Invalid --cables")?;
            }
            "--start-size" => {
                i += 1;
                start_size = args
                    .get(i)
                    .ok_or("Missing value for --start-size")?
                    .parse()
                    .map_err(|_| "Invalid --start-size")?;
            }
            "--max-size" => {
                i += 1;
                max_size = args
                    .get(i)
                    .ok_or("Missing value for --max-size")?
                    .parse()
                    .map_err(|_| "Invalid --max-size")?;
            }
            "--max-solutions" => {
                i += 1;
                max_solutions = args
                    .get(i)
                    .ok_or("Missing value for --max-solutions")?
                    .parse()
                    .map_err(|_| "Invalid --max-solutions")?;
            }
            "--workers" => {
                i += 1;
                workers = args
                    .get(i)
                    .ok_or("Missing value for --workers")?
                    .parse()
                    .map_err(|_| "Invalid --workers")?;
            }
            "--no-prune" => {
                prune_noops = false;
            }
            "--help" | "-h" => {
                return Err(String::from(
                    "Usage: rust_solver --cables N --start-size S --max-size M --max-solutions K --workers W [--no-prune]",
                ));
            }
            other => {
                return Err(format!("Unknown argument: {}", other));
            }
        }
        i += 1;
    }

    if num_cables < 2 || num_cables > 7 {
        return Err(format!(
            "Unsupported --cables {} (Rust bitmask solver supports 2..=7 cables).",
            num_cables
        ));
    }
    if max_solutions < 1 {
        return Err(String::from("--max-solutions must be >= 1"));
    }
    if workers < 1 {
        workers = 1;
    }
    if max_size < start_size {
        return Err(String::from("--max-size must be >= --start-size"));
    }

    Ok(Config {
        num_cables,
        start_size,
        max_size,
        max_solutions,
        workers,
        prune_noops,
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
    } else {
        run_layer_bfs::<u128, 7>(&cfg);
    }
}
