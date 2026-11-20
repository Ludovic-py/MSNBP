# MSNBP — Réseau de tri Binaire de Taille minimale
*(Minimal Sorting Network with Binary Principle)*

Outil de recherche, vérification et simulation visuelle de réseaux de tri / câblages booléens minimaux, avec interface **Tkinter** en français, moteur **masques de bits** et solveur natif **Rust** multi-cœurs.

## Fonctionnalités
- **Recherche de câblages minimaux :**
  - *Couches BFS — Moteur Rust (Multi-cœurs)* : recherche par profondeur avec masques de bits (`u64` / `u128`) et déduplication d'états.
  - *Couches BFS + Dédup. d'états (Python)* : repli portable sans binaire Rust.
  - *Permutations de sous-ensembles (Parallèle)* : exploration parallèle sur plusieurs workers.
- **Vérificateur & Simulateur interactif :** table de vérité complète, détection des entrées en échec, simulation pas à pas et **mode édition à la souris** (ajout/suppression de connexions directement sur le schéma).
- **Mode portable & cache local :** stocke les réglages et résultats dans `logic_network_data/` à côté de l'exécutable/script (avec repli mémoire si lecture seule).

## Utilisation rapide
- **Exécutable portable :** lancer `LogicNetworkExplorer.exe` (avec `rust_solver.exe` dans le même dossier pour bénéficier du moteur Rust).
- **Compilation du moteur Rust (si besoin) :**
  ```powershell
  rustc -O -C target-cpu=native rust_solver.rs -o rust_solver.exe
