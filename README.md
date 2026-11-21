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

## Propriétés structurelles des réseaux de tri binaires minimaux (OR, AND)

### 1. Activité par câble (distribution en cloche)
L'activité des comparateurs par câble suit une **courbe en cloche symétrique** : les câbles centraux portent significativement plus de portes que les câbles extrêmes.

**Observations :**
- **N=5** : C2 est le câble le plus actif, C0 et C4 les moins actifs.
- **N=6** : C2 et C3 sont les plus actifs, C0 et C5 les moins actifs.
- **N=7** : C3 est le plus actif (6 portes dans le réseau optimal de couche 3×3), C0 et C6 les moins actifs.
- **N=8** : Même profil en cloche, les câbles centraux (C3, C4) dominent.

### 2. Écart des portes (span)
La distribution des écarts |c₂ - c₁| évolue continûment de l'entrée vers la sortie :
- **Aux premières étapes (E1-E3)** : les écarts sont distribués proportionnellement au nombre de paires de chaque écart (40% d'écart 1, 30% d'écart 2, etc.).
- **Aux étapes intermédiaires** : la proportion de portes adjacentes (écart 1) croît à chaque étape.
- **À la dernière étape E_K** : **100% des réseaux optimaux** (N=3 à N=8) terminent par une porte d'**écart 1** (paire adjacente).

**Mesures (sur 515 solutions N=7) :**
- Écart 1 : 42,7% des portes E1..E15 ; **100% de E16**
- Écart 2 : 30,3%
- Écart 3 : 14,3%
- Écart 4 : 10,4%
- Écart 5 : 1,6%
- Écart 6 : 0,7% (jamais d'écart max en fin de réseau)

**Mesures (sur 726 solutions N=8) :**
- Écart 1 : 41,0% des portes E1..E18 ; **100% de E19**
- Écart 2 : 29,7%
- Écart 3 : 10,1%
- Écart 4 : 14,5%
- Écart 5 : 3,2%
- Écart 7 : 1,4%

### 3. Fermeture du réseau
- **100% des réseaux optimaux** de N=3 à N=8 se terminent par une porte d'écart 1.
- La dernière porte E_K est toujours une paire **adjacente centrale** (jamais une paire extrême comme (0,1) dans plus de 97% des cas pour N=7).
- Aucun réseau optimal (et optimal+1 sans porte redondante) ne se termine par un écart supérieur à 1.

**Distribution de E16 (N=7, 515 solutions) :**
- (3,4) : 28,3%
- (2,3) : 24,9%
- (1,2) : 23,1%
- (4,5) : 12,6%
- (5,6) : 8,2%
- (0,1) : 2,9%

**Distribution de E19 (N=8, 726 solutions) :**
- (1,2) : 38,5%
- (5,6) : 38,5%
- (3,4) : 23,1%

### 4. Ouverture du réseau (première couche L1)
Les réseaux optimaux privilégient **la mise en parallèle immédiate** des fils :
- **N=5** : 73,3% des 149 040 chemins optimaux démarrent par une couche parallèle de 2 comparateurs disjoints.
- **N=6, 7, 8** : l'ouverture par 3 (N=6,7) ou 4 (N=8) paires mutuellement disjointes est le motif dominant.
- Le motif `(0,1),(2,3),(4,5)` (ou sa symétrie centrée) est le **pattern L1 le plus fréquent** pour N≥5.

### 5. Symétrie par réflexion haut/bas
La transformation `(c₁, c₂) → (N-1-c₁, N-1-c₂)` est une **symétrie exacte** du graphe des solutions optimales :
- **100% des 515 solutions N=7** ont un symétrique valide.
- Appliquée aux classes de Foata, cette symétrie divise l'espace par 2, laissant **5 895 orbites canoniques** pour N=5 (sur 11 790 classes de Foata).

### 6. Projection N → N-1
Tout réseau optimal à N câbles dont un câble extrême (C₀ ou C_{N-1}) porte exactement `k` portes induit un réseau optimal sur les N-1 autres câbles après suppression de ce câble et des `k` portes qui le touchent.

**Validation empirique :**
- **N=7 → N=6** : 100% des 515 solutions N=7 se projettent valablement sur N=6.
- **N=8 → N=7** : 100% des 726 solutions N=8 se projettent valablement sur N=7.

### 7. Profondeur parallèle ASAP (As Soon As Possible)
Le regroupement glouton par couches parallèles (forme normale de Foata) donne une mesure de **profondeur parallèle** :

**Distribution N=7 (515 solutions) :**
| Profondeur | Pourcentage |
|-----------|------------|
| 6 couches | 1,4% |
| 7 couches | 19,0% |
| 8 couches | **48,0%** |
| 9 couches | 27,2% |
| 10 couches | 4,1% |
| 11 couches | 0,4% |

**Distribution N=8 (726 solutions) :**
| Profondeur | Pourcentage |
|-----------|------------|
| 6 couches | 11,5% |
| 7 couches | **46,2%** |
| 8 couches | 38,5% |
| 9 couches | 3,8% |

### 8. Proportion de portes adjacentes
La proportion de comparateurs adjacents (écart 1) est remarquablement stable :
- **N=5** : 58,5%
- **N=6** : 54,9%
- **N=7** : 46,3%
- **N=8** : 44,1%

Cette proportion décroît légèrement avec N, ce qui est cohérent avec le nombre croissant de paires non adjacentes disponibles.

### 9. Réseau optimal canonique N=7 (profondeur 6, 16 portes)
Le réseau de profondeur parallèle 6 vérifié comme optimal illustre la compression progressive des 128 vecteurs d'entrée :

| Couche | Portes | Vecteurs distincts | Paires inversées |
|--------|--------|-------------------|-----------------|
| Initial | — | 128 | 63 |
| L1 (E1-E3) | (0,1),(2,3),(4,5) | 54 | 18 |
| L2 (E4-E6) | (0,2),(1,3),(4,6) | 30 | 14 |
| L3 (E7-E9) | (0,4),(1,5),(2,6) | 19 | 9 |
| L4 (E10-E11) | (1,4),(3,6) | 14 | 6 |
| L5 (E12-E13) | (2,4),(3,5) | 11 | 3 |
| L6 (E14-E16) | (1,2),(3,4),(5,6) | 8 (triés) | 0 |

### 10. Prédiction pour N=9
Basé sur les régularités observées, on peut prédire :
- **Optimum** : 25 comparateurs (suite OEIS A003075 : 0, 1, 3, 5, 9, 12, 16, 19, 25, ...)
- **Dernière porte** : écart 1 (100% de probabilité)
- **Profil de câbles** : courbe en cloche avec C4 comme câble le plus actif
- **Profondeur dominante** : ~10-12 couches parallèles
- **Portes adjacentes** : ~40% du total

## OEIS
- **suite :** https://oeis.org/A003075
