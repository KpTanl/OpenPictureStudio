# Développement et compilation

[简体中文](building.md) | [English](building.en.md) | [Français](building.fr.md)

[Retour à l’accueil](../README.fr.md) · [Documentation](README.fr.md)

Ce guide décrit l’exécution d’OpenPictureStudio depuis les sources, la compilation du module d’accélération natif en Rust et la création de l’installateur Windows.

## Prérequis et exécution depuis les sources

Le développement et les builds de publication utilisent **Python 3.13** (Python 3.12 minimum). Les dépendances sont organisées par couches :

```powershell
# 1. Cloner le dépôt et créer un environnement virtuel
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. Installer les dépendances de base (visualisation, couleur, formats, export)
pip install -r requirements.txt

# 3. (Facultatif) Installer les dépendances IA locale et beauté portrait
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. Lancer l’application
python main.py
```

En exécution depuis les sources sans module natif compilé, les calculs basculent automatiquement sur Python/NumPy. Consultez [tests/README.md](../tests/README.md) pour exécuter les tests.

## Compilation du module d’accélération natif

`ops_accel.pyd` est facultatif depuis les sources et requis pour l’installateur officiel. La compilation nécessite la **chaîne d’outils Rust 1.95.0** (figée dans `native/ops_accel/rust-toolchain.toml` et sélectionnée automatiquement par `rustup`), **MSVC C++ Build Tools** et le **Windows SDK** ; le compilateur DXC statique activé par défaut exige également le composant **« C++ ATL pour x64/x86 »** (la première compilation télécharge et vérifie l’archive statique DXC figée) :

```powershell
cd native\ops_accel
cargo build --release --locked
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

| Variante de compilation | Usage |
| --- | --- |
| Variable `OPS_ATL_LIB` | Indiquer directement un dossier contenant `atls.lib` |
| `cargo build --release --locked --no-default-features` | Compiler un module natif FXC uniquement sans dépendance ATL |

Les builds de publication ciblent un jeu x86_64 générique avec détection AVX2 à l’exécution. Pour tester ou comparer le comportement sans AVX2, lancez avec `OPS_CPU_KERNEL=portable` (voir [Calcul CPU et compatibilité](architecture.fr.md#calcul-cpu-et-compatibilité)).

## Création de l’installateur Windows

L’empaquetage requiert **Inno Setup 6.7.1**, Rust et les dépendances verrouillées dans [requirements-build.lock.txt](../requirements-build.lock.txt) (qui fige `PySide6_Essentials==6.11.3` et `shiboken6==6.11.3` en excluant PyQt5/PyQt6/PySide2) :

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

> **Remarque :** `build.ps1` utilise `.venv\Scripts\python.exe`, exige un dépôt Git propre avec toutes les sources validées, et supprime `build/` et `dist/` au démarrage. Assurez-vous que les sources Cargo locales sont en cache (`cargo fetch --locked`) avant la collecte des licences.

- **Fichiers générés :** le dossier d’application complet est produit dans `dist/OpenPictureStudio/`, et l’installateur EXE avec son fichier `.exe.sha256` dans `dist/installer/`.
- **Réutiliser le module natif :** ajoutez `-SkipNativeBuild` lorsque `ops_accel.pyd` à la racine correspond déjà exactement aux sources.

## Intégration continue et builds automatisés

GitHub Actions exécute en parallèle les vérifications Python (`ruff`, `compileall`, `pytest`) et Rust (`clippy`, `cargo test`) ainsi que la compilation du module natif. L’envoi d’une étiquette de version produit l’installateur Windows et son fichier `.sha256` via le workflow public, puis crée une version brouillon (Draft Release) en attente de publication.

## Documentation associée

- [Architecture et accélération native](architecture.fr.md)
- [Plug-ins d’image](image-plugins.fr.md)
- [Licences et composants tiers](licensing.fr.md)
