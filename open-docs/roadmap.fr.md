# Feuille de route | Roadmap

[简体中文](roadmap.md) | [English](roadmap.en.md) | [Français](roadmap.fr.md)

[Retour à l’accueil](../README.fr.md)

## Vision du projet

**À partir du traitement photo professionnel, fusionner peu à peu collaboration avec l’IA, flux de travail automatisés et retouche multimode, pour bâtir une plateforme ouverte de création d’images avec l’IA, maîtrisée par les créateurs et étendue ensemble avec les développeurs.**

OpenPictureStudio (OPS) veut associer la puissance du traitement d’image professionnel à l’IA, pour que chacun puisse, dans un seul environnement de création, gérer ses photos, étalonner les couleurs, retoucher de façon intelligente, automatiser les traitements et mener des tâches de création d’images plus larges.

Le grand public peut partir d’une photo et d’une idée.

Les créateurs professionnels peuvent contrôler en profondeur la couleur, la lumière et les détails.

Les développeurs peuvent apporter à la plateforme de nouveaux algorithmes, modèles et outils.

À mesure que les flux de travail et les interfaces externes se complètent, les agents IA pourront aussi organiser l’exécution d’outils autour d’un objectif de création, puis remettre le résultat au créateur pour relecture.

Le jugement humain, les outils professionnels et la capacité d’exécution de l’IA collaboreront autour d’une même œuvre. L’expérience de création s’accumule avec les projets et devient peu à peu une méthode que l’on peut réutiliser, combiner et partager.

## Bases actuelles

OPS propose déjà une version bêta pour Windows. Le projet offre aujourd’hui la visualisation et le tri des photos, le décodage et l’étalonnage des RAW, les masques locaux, la retouche non destructive, l’amélioration de la qualité par l’IA, un assistant de retouche intelligent, le traitement de photos par lot, l’historique des projets et l’export par lot. Les fonctions précises de chaque version figurent dans les [notes de version](https://github.com/KpTanl/OpenPictureStudio/releases) correspondantes.

- **Traitement professionnel** : fondé sur le traitement d’image linéaire, l’étalonnage RAW et les réglages locaux, le processus de création est conservé par les paramètres, l’historique et le projet.
- **Collaboration avec l’IA** : l’assistant de retouche, les images de référence, la comparaison de propositions et les styles nommés existent déjà, et les réglages concernés entrent dans l’historique de retouche, où ils restent annulables.
- **Livraison par lot** : le tri des photos, les réglages par lot et l’export par lot sont en place, et préparent le traitement continu et la réutilisation des flux.
- **Extension ouverte** : l’enregistrement des outils, l’exécution des flux de travail et le modèle de projet existent, et les interfaces de développement destinées aux tiers continueront d’être complétées.

## Feuille de route en quatre étapes

| Étape | Objectif produit | Priorités |
| --- | --- | --- |
| **Étape 1 : approfondir la collaboration créative avec l’IA** | Que l’IA comprenne mieux l’intention de l’utilisateur et l’aide dans les tâches de retouche professionnelles | Étendre les outils de l’Agent, renforcer la compréhension du contexte, l’analyse des images de référence et la mémoire des styles, et prendre en charge les réglages en plusieurs étapes, la retouche locale fine et la comparaison de propositions |
| **Étape 2 : mettre en place des flux de travail automatisés** | Transformer les opérations répétées et l’expérience créative en flux réutilisables, composables et exécutables | Construire un système de flux de travail configurable, avec orchestration des étapes, réutilisation des paramètres, exécution conditionnelle, tâches par lot, planification en arrière-plan et appel par des agents IA externes |
| **Étape 3 : élargir à la création d’images complète** | Réunir plusieurs modes de retouche dans un même projet et repousser les limites de la création | Sur la base de la retouche non destructive, introduire progressivement calques, sélections professionnelles, retouche au pixel et retouche générative par IA, avec un enregistrement de projet et un historique unifiés |
| **Étape 4 : construire un écosystème d’extensions ouvert** | Étendre ensemble les capacités de la plateforme, créateurs et développeurs | Stabiliser l’API publique et le SDK de plug-ins, prendre en charge l’intégration d’algorithmes, de modèles et d’outils tiers, compléter l’isolation et les permissions des plug-ins, et développer le partage et la distribution de styles, de flux de travail et de plug-ins |

### Étape 1 : approfondir la collaboration créative avec l’IA

En partant de l’assistant de retouche actuel, l’IA tient compte du contenu de la photo, de l’état de la retouche et de l’objectif de l’utilisateur, et prend part à un processus de création plus complet. L’utilisateur peut exprimer l’ambiance voulue, les détails à préserver et les exigences de livraison, puis s’approcher de son objectif par la prévisualisation, la comparaison et le réglage des paramètres.

Axes principaux :

- **Collaboration entre outils** : étendre les outils de retouche que l’assistant peut appeler, et relier le tri des photos, l’étalonnage global, les réglages locaux, le traitement de portrait et l’export.
- **Contexte de création** : définir des plans de traitement en plusieurs étapes à partir de la photo en cours, du projet, de l’historique de retouche et des demandes de l’utilisateur, avec la possibilité de changer d’objectif en cours de route et de continuer à ajuster.
- **Images de référence et style personnel** : renforcer l’analyse de la couleur, de la lumière et des traits de style d’une image de référence, pour que l’utilisateur puisse accumuler, nommer et réutiliser ses préférences de création.
- **Exécution maîtrisée** : compléter la prévisualisation des plans, la retouche locale fine et la comparaison de plusieurs propositions, pour que les changements importants puissent être relus, adoptés, annulés et poursuivis.

Cette étape veut faire du langage naturel, des paramètres professionnels et du retour visuel un processus de création continu. L’utilisateur peut s’appuyer sur l’IA pour réduire l’effort de manipulation, et revenir à tout moment aux outils pour contrôler les détails.

### Étape 2 : mettre en place des flux de travail automatisés

Conserver une méthode de traitement réussie, pour qu’elle serve à la photo suivante, au lot suivant et à la tâche suivante. Les styles personnels, les combinaisons de paramètres et les règles de livraison deviendront peu à peu des flux de création que l’on peut combiner et exécuter.

Axes principaux :

- **Orchestration des flux** : prendre en charge la combinaison d’étapes de traitement et la configuration des entrées, des paramètres et des sorties, avec peu à peu l’exécution conditionnelle, le traitement à plusieurs entrées et des fragments de flux réutilisables.
- **Traitement par lots** : appliquer le flux adapté selon le résultat du tri, le groupe de photos et les différences entre les sources, pour améliorer la cohérence et l’efficacité de livraison d’une série.
- **Tâches en arrière-plan** : compléter la progression, la pause, l’annulation, la reprise après échec et la gestion des résultats, pour que les traitements longs soient plus faciles à planifier et à reprendre.
- **Interface pour agents externes** : ouvrir peu à peu le chargement des photos, le traitement, la prévisualisation, l’enregistrement du projet et l’export, pour que les agents IA externes puissent organiser des tâches sans ouvrir l’interface de bureau.

Cette étape veut consacrer à la décision créative le temps passé sur les gestes répétés, permettre aux individus et aux petites équipes de réutiliser leurs méthodes de traitement de façon fiable, et transformer l’acquis quotidien en flux de travail durables.

### Étape 3 : élargir à la création d’images complète

Sur la base de la retouche non destructive actuelle, relier peu à peu le traitement photo, la modification de pixels et la création générative. Dans un même projet, l’utilisateur peut organiser ses éléments, ajuster l’image, modifier des parties du contenu, et conserver l’état et l’historique nécessaires pour poursuivre.

| Mode de retouche | Direction |
| --- | --- |
| **Retouche non destructive** | Continuer à perfectionner le RAW, la couleur, les masques locaux et le traitement paramétrique, en gardant des étapes ajustables et le jugement créatif |
| **Retouche de pixels et de calques** | Introduire peu à peu calques, sélections professionnelles, composition et modification directe des pixels, en lien avec l’annulation, l’historique et l’enregistrement du projet |
| **Retouche générative** | Explorer la génération d’images, la retouche locale par régénération et la modification du contenu, et combiner les résultats générés avec les outils de retouche habituels |

Les différents modes de retouche collaboreront autour d’un projet unifié : un résultat généré peut recevoir d’autres réglages de couleur et de traitement local, la modification de pixels et le réglage des paramètres peuvent contribuer ensemble à l’œuvre, et l’enregistrement du projet peut porter les éléments, les étapes de traitement et les références de ressources nécessaires.

Cette étape veut faire passer la création de la retouche d’une image existante à la composition d’éléments, à la modification du contenu et à la construction de nouvelles expressions visuelles.

### Étape 4 : construire un écosystème d’extensions ouvert

Dans la direction d’Everything is Plugin, des algorithmes, des modèles, des outils et des méthodes de création nouveaux peuvent entrer en continu dans la plateforme. Les créateurs choisissent l’ensemble de capacités qui leur convient, et les développeurs contribuent avec des extensions autour de besoins précis.

Axes principaux :

- **API publique et SDK de plug-ins** : stabiliser peu à peu l’intégration des outils, la déclaration des paramètres, la gestion des ressources et l’interface d’exécution, avec documentation et exemples, pour réduire le coût d’intégration des extensions tierces.
- **Extensions d’outils et de modèles** : permettre à de nouveaux algorithmes d’image, adaptateurs de modèles et outils de création de participer à la retouche manuelle, à l’exécution des flux de travail et aux appels de l’IA.
- **Isolation et permissions** : continuer à compléter l’isolation de l’exécution des plug-ins, la déclaration des permissions, l’accès aux ressources et la compatibilité des versions, pour que le comportement et le périmètre d’usage d’une extension soient plus clairs.
- **Partage des méthodes de création** : développer le partage et la distribution des styles nommés, des modèles de flux de travail et des plug-ins, pour que l’expérience professionnelle puisse être apprise, combinée et réutilisée dans de nouvelles créations.

À long terme, l’objectif est un écosystème où créateurs et développeurs se soutiennent : des besoins réels font naître de nouveaux outils, les outils et les flux portent de nouvelles méthodes, et le partage nourrit davantage de pratiques de création.

## Travaux d’ingénierie continus

Performance, stabilité, compatibilité et qualité d’ingénierie accompagnent toutes les étapes et soutiennent le développement continu des capacités du produit.

### Moteur natif de calcul d’image

Rust est le chemin de calcul natif prioritaire, avec un repli Python/NumPy compatible. On continue d’optimiser le calcul, la copie des données et la réutilisation des tampons, et le multithread de Rayon améliore l’efficacité de la retouche de grandes images et du traitement par lots.

Les CPU qui prennent en charge AVX2 activent automatiquement les noyaux d’accélération déjà branchés ; les autres CPU utilisent le chemin Rust générique. Par la suite, on explorera d’autres optimisations comme FMA, en étendant le périmètre d’accélération dans le respect des exigences de précision et de compatibilité.

### Accélération sur plusieurs matériels

En complément de la puissance du CPU, on explore un moteur de calcul d’image GPU fondé sur wgpu, en évaluant d’abord le gain réel sur les environnements Windows Intel, AMD et NVIDIA, tout en gardant un chemin d’exécution CPU fiable.

À mesure que les interfaces d’opérations d’image et la gestion des ressources se complètent, on poursuivra l’étude de l’adaptation à davantage de matériels et de plateformes. DirectML, aujourd’hui utilisé pour l’inférence IA, et le traitement d’image GPU prévu évoluent séparément, et servent ensemble l’expérience de retouche.

### RAW et traitement des couleurs

On continue d’optimiser le décodage RAW complet, le passage d’un aperçu à l’autre, la gestion des caches et le traitement des images à haute profondeur de bits, pour améliorer la précision des couleurs, la vitesse de traitement et l’efficacité de la mémoire. À partir de photos réelles et d’appareils variés, on améliore le comportement sur les grandes images, les réglages locaux et le traitement par lots.

### Architecture applicative et qualité

Sur l’interface PySide6 déjà adoptée, on continue de perfectionner l’expérience de bureau, de faire évoluer l’architecture vers la modularité, et de renforcer la validation de la qualité, la reprise après erreur et la compatibilité des projets entre versions. Les améliorations de performance et de gestion des ressources couvriront tout le cycle : chargement, retouche, tâches en arrière-plan, enregistrement et export.

Le calcul d’image, l’exécution des flux et les données de projet forment ensemble la base à long terme :

| Capacité de base | Objectif |
| --- | --- |
| **Calcul d’image** | Offrir à la retouche manuelle, aux outils IA et aux flux de travail des opérations d’image, une couleur et une sortie cohérentes |
| **Exécution des flux** | Relier l’orchestration des opérations, la planification des tâches, l’annulation et la remise des résultats, pour le traitement interactif comme en arrière-plan |
| **Données de projet** | Organiser de façon unifiée les éléments, l’état de retouche, l’historique et les références de ressources, pour que des outils différents collaborent autour d’une même œuvre |

Pour les directions techniques concernées, voir [Architecture et accélération native](architecture.fr.md) ; pour la configuration et les mesures, voir [Configuration requise et performances](performance.fr.md).

## Perspectives à long terme et participation

OPS veut former peu à peu une base unifiée de création d’images, où la retouche manuelle, la génération par IA et les flux de travail automatisés fonctionnent ensemble, tout en laissant à l’utilisateur le contrôle du processus de création, des données, des modèles et du résultat final.

Les créateurs professionnels peuvent y déposer leur style et leurs méthodes, le grand public peut s’appuyer sur les outils et l’IA pour former peu à peu son expression personnelle, et les développeurs peuvent bâtir de nouvelles capacités autour d’interfaces ouvertes. Les œuvres, les outils et l’expérience se relieront au fil de l’usage, et la plateforme pourra s’ouvrir à des scénarios de création d’images toujours plus larges.

Le code original du projet est actuellement sous [licence MIT](../LICENSE) ; les composants tiers et les modèles relèvent chacun de leur licence, voir [Licences et composants tiers](licensing.fr.md). Vos cas d’usage, besoins d’outils et suggestions d’extension sont les bienvenus dans [GitHub Issues](https://github.com/KpTanl/OpenPictureStudio/issues).

Les étapes avancent selon les dépendances entre capacités, et certaines directions peuvent se mener en parallèle. Le projet ajustera ses priorités selon l’avancement du développement, la maturité technique et les retours de la communauté ; les fonctions de chaque version sont celles décrites dans les notes de version correspondantes.
