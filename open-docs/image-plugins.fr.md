# Plug-ins d’image

[简体中文](image-plugins.md) | [English](image-plugins.en.md) | [Français](image-plugins.fr.md)

Les paquets Python de confiance s’enregistrent avec `ToolRuntime.register_image_plugin(directory, trusted_hash=...)`. L’hôte examine le paquet et fixe son SHA-256 avant l’enregistrement. Le moteur conserve un instantané indépendant et fournit des références `ToolReference` standard. Le schéma de projet 4 enregistre les empreintes et les versions d’interface et d’implémentation. Le retrait d’un plug-in conserve les paramètres et les sorties existantes ; le recalcul signale l’implémentation manquante jusqu’au retour du paquet exact.

`plugin.json` déclare `manifest_version: "2.0"`, `runtime: "subprocess_python"`, un `plugin_id` avec espace de noms d’éditeur et des `declared_nodes` de contrat `image/1`. Chaque nœud déclare ses paramètres versionnés, le comportement de l’image, la compatibilité proxy, le délai maximal, le cache et les capacités auxiliaires nommées. Les paramètres acceptent nombres, entiers, booléens, chaînes et tableaux, avec valeurs par défaut, bornes et choix. Les ressources du paquet portent des empreintes SHA-256.

La requête JSON UTF-8 contient `protocol: "image/1"`, `api_version: 1`, `node_id`, `params`, `input`, `output` et `auxiliary`. L’image principale est un NPY float32 H×W×3 en sRGB linéaire, avec un alpha droit float32 H×W séparé. Les métadonnées indiquent dimensions réelles et de référence, état proxy, point blanc D65, gamut et espace colorimétrique. Les tableaux se lisent avec `allow_pickle=False`.

Le plug-in écrit les tableaux aux chemins demandés et renvoie `{"protocol":"image/1","status":"ok","alpha":true}` sur la sortie standard. L’hôte valide dimensions, float32, valeurs finies, alpha et comportement géométrique déclaré. Annulation et délai maximal arrêtent le sous-processus. Fichiers et tampons relèvent des budgets et espaces gérés par l’hôte. L’hôte actuel refuse les paquets déclarant une permission réseau.

La première version de l’interface s’exécute sur le CPU, avec les threads et la mémoire de travail gérés par l’hôte. Les sous-processus héritent des permissions système de l’utilisateur courant. Le champ réseau contrôle l’admission des paquets ; l’environnement d’exécution gère les permissions système et l’accès au réseau. `timeout_sec` accepte les nombres finis de 1 à 300 secondes.

Les politiques de cache sont `deterministic`, `seeded` avec paramètre entier de graine, et `never`. Toutes les identités d’entrée, de paquet, de ressources, de données auxiliaires, de paramètres et d’état externe participent au résultat. `never` désactive la réutilisation entre requêtes pour le nœud et ses descendants ; le canevas conserve le résultat courant pour le zoom et le déplacement. `external_state` fixe l’état stable externe.

L’hôte fournit des capacités `AuxiliaryResolver` nommées lors de l’enregistrement. Leur gestionnaire de contexte `borrow(source, params, cancelled)` livre des tableaux `ImagePluginAuxiliary` en lecture seule, avec identité stable, version sémantique et dimensions de référence. `mask-f32` représente des poids H×W entre 0 et 1 ; `faces-2d` représente des coordonnées normalisées visage×point×2. Identités et ressources du résolveur participent aux dépendances d’exécution. Le résolveur gère les baux et le budget des données.

Les résultats auxiliaires dépendent des ressources déclarées, de l’identité du résolveur, de l’entrée courante et des paramètres. Actualiser l’identité du résolveur et réenregistrer le paquet quand les modèles, l’état externe ou la sémantique changent. Utiliser `cache: "never"` pour les données auxiliaires issues de contenus externes continuellement variables.

Les anciens paquets de manifeste `1.0` et contrat `3.4.0` utilisent `PluginService`, son audit et un adaptateur PNG sRGB8 explicite. Installation et distribution suivent la [feuille de route](roadmap.fr.md).

Le [paquet d’exemple](../resources/image_plugins/org.openpicturestudio.examples) propose exposition, grain à graine fixe et grain sans cache. L’[exemple complet d’enregistrement](image-plugins.md#注册与版本) utilise le nœud d’outil standard. `ToolRuntime.register_tool` accepte également l’outil de vignettage de `src.core.tools.vignette`. Les outils régionaux déclarent `output_size`, `region_input`, `execute_region` et éventuellement `region_memory` ; le contexte conserve les dimensions complètes et les coordonnées des zones.
