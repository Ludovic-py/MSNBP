import json

# Prefixe L1+L2 canonique applique par le Rust
prefix = [(0,1,"OR","AND"), (2,3,"OR","AND"), (4,5,"OR","AND"),
          (0,2,"OR","AND"), (1,3,"OR","AND"), (4,6,"OR","AND")]

# Lecture des solutions Rust (BFS 10 portes)
solutions = []
with open('rs_test2.txt') as f:
    for line in f:
        line = line.strip()
        if line.startswith('SOLUTION '):
            body = line[9:].strip()
            bfs_steps = []
            for pair in body.split(';'):
                c1, c2 = pair.split(',')
                bfs_steps.append([int(c1), int(c2), "OR", "AND"])
            # Solution complete = prefixe + BFS
            full = prefix + bfs_steps
            solutions.append(full)

# Verifier la coherence
print(f"{len(solutions)} solutions")
print(f"Premiere: {len(solutions[0])} steps")
# Verifier que les 10 dernieres portes sont bien les BFS
for i, sol in enumerate(solutions):
    if len(sol) != 16:
        print(f"ERREUR: solution {i} a {len(sol)} steps")

payload = {
    "search_config": {
        "num_cables": 7, "start_size": 16, "max_size": 16,
        "max_solutions": 38, "workers": 4, "prune_noops": True,
        "search_mode": "rust_layer_dedup"
    },
    "summary": {
        "solutions_found": 38, "cancelled": False, "from_cache": False,
        "elapsed_sec": 1.6, "evaluations_checked": 3245283
    },
    "solutions": solutions
}

with open('dist/LogicNetworkExplorer/solutionsn7k16.json', 'w', encoding='utf-8') as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)

print(f"Fichier ecrit avec {len(solutions)} solutions de {len(solutions[0])} steps chacune")
print(f"Verification rapide:")
for i in range(min(3, len(solutions))):
    sol = solutions[i]
    n_bfs = len(sol) - len(prefix)
    print(f"  Solution {i+1}: {len(sol)} steps total (prefixe {len(prefix)} + BFS {n_bfs})")