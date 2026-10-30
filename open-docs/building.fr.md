# Développement et compilation

[简体中文](building.md) | [English](building.en.md) | [Français](building.fr.md)

[Retour à l’accueil](../README.fr.md)

## Exécution depuis les sources

Python 3.12 ou plus est requis (le développement et les versions de distribution utilisent 3.13). Les dépendances sont organisées par fonction : vous pouvez n’installer que ce dont vous avez besoin.

```powershell
# 1. Récupérer les sources
git clone https://github.com/KpTanl/OpenPictureStudio.git
cd OpenPictureStudio
python -m venv .venv
.\.venv\Scripts\activate

# 2. Installer les dépendances de base (visualisation, couleur, traitement des formats et sortie)
pip install -r requirements.txt

# 3. (Facultatif) Installer les dépendances IA et retouche de portrait
pip install -r requirements-ai.txt
pip install -r requirements-beauty.txt

# 4. Lancer l’application
python main.py
```

Le module d’accélération Rust est facultatif. Si vous en avez besoin, installez la chaîne d’outils Rust, compilez, puis copiez le résultat à la racine du dépôt :

```powershell
cd native\ops_accel
cargo build --release --locked
copy target\release\ops_accel.dll ..\..\ops_accel.pyd
cd ..\..
```

## Compilation

### Construire l’installateur

Il faut Inno Setup, la chaîne d’outils Rust et toutes les dépendances (programme, IA, retouche de portrait et empaquetage). `requirements-build.lock.txt` fige les versions testées, et les versions de distribution s’installent avec lui ; `requirements-build.txt` en est la source. Avant la compilation, le script vérifie la version de Python, que les dépendances sont complètes et qu’il n’y a qu’une seule distribution d’OpenCV dans l’environnement.

```powershell
pip install -r requirements-build.lock.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

### CI et publication

La CI exécute, dans des jobs indépendants et dans l’ordre, les contrôles de code avec la précompilation Python, la compilation du module natif, les tests, puis la construction de l’installateur. Une PR va jusqu’aux tests ; les push sur `dev`/`main` et les lancements manuels continuent jusqu’à l’empaquetage.

Une étiquette de version exécute les mêmes étapes, vérifie que le commit est déjà dans `main` et que l’étiquette correspond à `APP_VERSION`, puis crée un brouillon de Release. Le mainteneur termine la publication à la main.

La compilation native téléverse `ops_accel.pyd`, et les tests comme l’empaquetage téléchargent ce même artefact. L’empaquetage réutilise ce module avec `build.ps1 -SkipNativeBuild` et prépare les sources des dépendances Cargo pour la collecte des licences. Les jobs en aval choisissent l’artefact d’après l’ID d’artefact du job producteur.

### Exécuter les tests

Les instructions de test se trouvent dans [tests/README.en.md](../tests/README.en.md).

## PySide6 et module natif

Les dépendances de base figent `PySide6_Essentials==6.11.3` et `shiboken6==6.11.3`. Pour la distribution, la référence est [requirements-build.lock.txt](../requirements-build.lock.txt), et les liaisons Qt utilisent PySide6. `build.ps1` utilise toujours `.venv\Scripts\python.exe` à la racine du dépôt, et exige que toutes les sources de la distribution soient validées (commit) et que l’espace de travail soit propre. Une exécution ordinaire depuis les sources peut se faire dans un espace de travail de développement.

AVX2 est détecté à l’exécution ; les versions de distribution gardent une cible CPU générique. Si le module natif est absent, les calculs concernés peuvent passer par Python/NumPy pour une exécution depuis les sources ; une distribution complète exige que `ops_accel.pyd` soit généré avec succès ou réutilisé explicitement.

Le script de compilation nettoie `build/` et `dist/`, après avoir sauvegardé ce qui doit être conservé. `-SkipNativeBuild` ne convient qu’à un module natif dont la correspondance avec les sources candidates est confirmée. La collecte des licences demande les sources locales des dépendances Cargo, que vous pouvez récupérer d’abord avec `cargo fetch --locked`.

Résultats : le dossier du programme `dist/OpenPictureStudio/`, ainsi que l’EXE et le `.exe.sha256` dans `dist/installer/`. Avant la publication, effectuez la validation manuelle ci-dessous et vérifiez que le module natif du dossier du programme correspond à cette compilation.

## Migration vers PySide6 et validation de la distribution

Les sources et la configuration d’empaquetage actuelles utilisent PySide6 Essentials et Shiboken 6.11.3, et excluent PyQt5, PyQt6 et PySide2. Avant la publication, vérifiez à la main les points suivants :

1. Installez la version précédente sur un compte de test, notez la version, le dossier d’installation et le dossier de données ; sauvegardez les paramètres, les modèles IA, les projets `.opsproj` et les données personnalisées.
2. Fermez toutes les instances d’OPS, puis installez la version candidate par-dessus, dans le même dossier. Vérifiez que le programme démarre, que les anciens fichiers PyQt sont supprimés d’après le manifeste d’installation et que PySide6 et Shiboken se chargent normalement.
3. Ouvrez des projets existants et vérifiez l’historique de retouche, les références de modèles et l’export ; vérifiez que les paramètres et les dossiers de données personnalisés restent utilisables.
4. Dans les paramètres, confirmez que les modèles et les moteurs d’exécution sont toujours reconnus, puis ouvrez successivement la visualisation, le RAW, les masques, la retouche de portrait et l’assistant.
5. Annulez une installation de test, puis lancez l’installateur complet de la même version ; traitez la sauvegarde et les conflits selon les indications de l’installateur, et vérifiez que le programme démarre après la réparation.
6. Sur une machine avec AVX2 et sur une machine sans AVX2, vérifiez séparément le démarrage, les réglages de couleur et l’export ; `portable` permet de vérifier les noyaux génériques, mais les relevés de compatibilité matérielle réelle doivent venir des machines concernées.

Lors de la validation, notez la version de l’installateur, le SHA256, la version de Windows, le dossier de données et le résultat.
