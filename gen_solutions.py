import json

# Lecture des solutions Rust
solutions = []
with open('rs_test2.txt') as f:
    for line in f:
        line = line.strip()
        if line.startswith('SOLUTION '):
            body = line[9:].strip()
            steps = []
            for pair in body.split(';'):
                c1, c2 = pair.split(',')
                steps.append([int(c1), int(c2), "OR", "AND"])
            solutions.append(steps)

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

print(f"{len(solutions)} solutions ecrites")
print(f"Premiere: {len(solutions[0])} steps")
print(f"Derniere: {len(solutions[-1])} steps")