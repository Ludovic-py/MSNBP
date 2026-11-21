# MSNBP — Réseau de tri Binaire de Taille minimale
*(Minimal Sorting Network with Binary Principle)*

Outil de recherche, vérification et simulation visuelle de réseaux de tri / câblages booléens minimaux, avec interface **Tkinter** en français, moteur **masques de bits** et solveur natif **Rust** multi-cœurs.

## Fonctionnalités
- **Recherche de câblages minimaux :**
  - *Couches BFS — Moteur Rust (Multi-cœurs)* : recherche par profondeur avec masques de bits (`u64` / `u128` / `u256`) et déduplication d'états.
  - *Couches BFS + Dédup. d'états (Python)* : repli portable sans binaire Rust.
  - *Permutations de sous-ensembles (Parallèle)* : exploration parallèle sur plusieurs workers.
- **Vérificateur & Simulateur interactif :** table de vérité complète, détection des entrées en échec, simulation pas à pas et **mode édition à la souris** (ajout/suppression de connexions directement sur le schéma).
- **Mode portable & cache local :** stocke les réglages et résultats dans `logic_network_data/` à côté de l'exécutable/script (avec repli mémoire si lecture seule).
- **API HTTP locale** : contrôle à distance depuis l'agent AI pour piloter l'interface, lancer des analyses en autopilot qui demande plus qu'une automatisation simple, ainsi que tenir le journal des decouvertes et des methodes utiles.
- **Double panneau d'analyse** : compare deux collections côte-à-côte avec surbrillance des différences.
- **Serveur de préfixes L1/L2** : extrait les motifs d'ouverture les plus fréquents et les réinjecte comme préfixe de recherche.
- **Solveur Rust étendu** : support de N=8 avec type `U256` (2 × u128).
- **Support des préfixes de recherche** : option `--prefix` pour fixer les premières couches et réduire l'espace d'état.
- **Coupe de commutation Foata** : option `--no-foata` pour désactiver.
- **Filtre d'écart final** : option `--no-final-span-1` pour explorer des motifs de fermeture variés.

## Résultats de recherche

| N | Comparateurs optimaux | Solutions trouvées | Type de masque |
|---|----------------------|-------------------|----------------|
| 3 | 3 | 5 | `u64` |
| 4 | 5 | 5 | `u64` |
| 5 | 9 | 340 | `u64` |
| 6 | 12 | 1 725 | `u64` |
| 7 | 16 | **515** (5 familles de préfixes) | `u128` |
| 8 | 19 | **726** (4 familles de préfixes) | `U256` (2 × u128) |

## Utilisation rapide
- **Exécutable portable :** lancer `LogicNetworkExplorer.exe` (avec `rust_solver.exe` dans le même dossier pour bénéficier du moteur Rust).
- **Compilation du moteur Rust (si besoin) :**
  ```powershell
  rustc -O -C target-cpu=native rust_solver.rs -o rust_solver.exe
  ```
- **Utilisation en ligne de commande (solveur seul) :**
  ```powershell
  rust_solver.exe --cables 8 --start-size 11 --max-size 11 --max-solutions 30 --workers 6 --prefix 0,1,2,3,4,5,6,7,0,2,1,3,4,6,5,7
  ```
- **API HTTP distante :** l'application expose un serveur sur `127.0.0.1:39227` (token: `dsh-lnetwork-2024`).

## Journal des modifications

### 21 novembre 2026 — Refonte majeure

#### Solveur Rust (`rust_solver.rs`)
- **Compactage RAM des `worker_buckets`** : les tuples `([W; MAX_N], u32, u8)` de 128 octets ont été remplacés par `(u32, u8)` de 8 octets (le nouvel état est recalculé en Phase 2 depuis l'état parent). Permet de passer de 46,9 Go à ~10 Go au pic sur N=7.
- **Option `--prefix`** : fixe les premières portes (L1, L1+L2) avant le BFS. Réduit la frontière de couche 7 de 27 M états à 15 279 pour N=7 avec L1.
- **Coupe de commutation Foata** (`--no-foata`) : évite d'évaluer des permutations équivalentes de portes disjointes.
- **Filtre d'écart final** (`--no-final-span-1`) : à `depth == max_size`, ne tester que les paires adjacentes (écart 1).
- **Support N=8** : ajout du type `U256` (2 × u128) pour les masques de bits à 8 câbles. Les niveaux N≤6 et N=7 continuent d'utiliser `u64` et `u128`.
- **Correction d'index** : les index de portes stockés dans `parents` référencent toujours la liste complète, même quand la couche finale utilise les portes adjacentes uniquement.

#### Interface Python (`logic_network_gui.py`)
- **Ajout d'une API HTTP locale** (`127.0.0.1:39227`, token `dsh-lnetwork-2024`) pour piloter l'application à distance. Endpoints : `/status`, `/collections`, `/solutions`, `/search`, `/set-prefix`, `/import-solutions`, `/switch-tab`, `/run-analysis`, `/select-solution`, `/select-primary`, `/stop-search`, `/send-to-mso`.
- **Architecture thread-safe** : les appels Tkinter sont marshallés vers le thread principal via `root.after()`.
- **Double panneau d'analyse** dans l'onglet Analyse : panneau gauche (jeu principal), panneau central (comparaison A↔B avec surbrillance), panneau droit (jeu croisé).
- **Nouvelles métriques statistiques** : distribution de la dernière porte E_K, patterns L1, profondeur parallèle ASAP, symétries par réflexion, projection N→N-1.
- **Workflow L1/L2** : les boutons "Extraire L1" et "Envoyer L1+L2 → MSO" analysent les patterns d'ouverture les plus fréquents et les réinjectent dans l'onglet MSO.
- **Champ Préfixe** dans l'onglet MSO : zone de texte pour saisir les portes de préfixe (transmises au solveur Rust via `--prefix` ou appliquées directement dans le BFS Python).
- **Options de coupe** : cases à cocher pour la coupe Foata et le filtre d'écart final.
- **Auto-chargement** des fichiers `solutionsn*.json` au démarrage.
- **Chargement automatique des collections** : restauration des jeux de solutions N=5, N=6, N=7, N=8 à l'ouverture.
- **Corrections de bugs** : résolution du deadlock Tkinter lors des appels API, correction des variables `diff_a`/`diff_b` non définies.

#### Exécutables
- **Compilation en mode `--onedir`** (remplace `--onefile`) : évite l'extraction temporaire dans `%TEMP%` qui causait l'erreur `INTERNAL ERROR: cannot create temporary directory!`.
- **Distribution** : `dist/LogicNetworkExplorer/` contient l'application, le solveur Rust, les fichiers de solutions et les données.

#### Solutions
- **N=7 : 515 solutions à 16 comparateurs** (38 originales + 487 par réflexion et commutation), réparties en 5 familles de préfixes L1+L2.
- **N=8 : 726 solutions à 19 comparateurs** (52 originales + 674 par réflexion et commutation), réparties en 4 familles de préfixes.
- **Projection N→N-1 validée** : 100% des solutions N=8 et N=7 se projettent correctement sur N-1.
- **100% des solutions fiables** : chaque solution est vérifiée via `run_full_verification()`.

## OEIS
- **suite :** https://oeis.org/A003075
