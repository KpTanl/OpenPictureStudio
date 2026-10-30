# Configuration requise et performances

[简体中文](performance.md) | [English](performance.en.md) | [Français](performance.fr.md)

[Retour à l’accueil](../README.fr.md)

## Configuration recommandée

| Élément | Usage de base | Configuration recommandée | Pour aller plus loin |
| --- | --- | --- | --- |
| Système | Windows 10/11, 64 bits | Windows 10/11, 64 bits | Windows 10/11, 64 bits |
| CPU | x64, CPU multicœur récent | Intel Core i5 de 7e génération ou AMD Ryzen de 1re génération, ou mieux | Processeur Intel de 12e génération ou AMD Ryzen de 5e génération, ou mieux |
| Mémoire | 8 Go, adaptés à la visualisation et à la retouche légère | 16 Go, adaptés aux RAW et à la retouche avec plusieurs outils | 32 Go ou plus, adaptés aux images haute résolution, aux masques nombreux et aux traitements par lots |
| GPU | Carte graphique intégrée | GTX 950 ou mieux | RTX 2060 ou mieux |
| Stockage | 3 Go d’espace libre | 6 Go d’espace libre, SSD conseillé | 12 Go d’espace libre, SSD conseillé |

La version installée embarque ses dépendances d’exécution ; les modèles IA se téléchargent à la demande ; l’assistant de retouche IA demande de configurer un service en ligne.

## Performances du traitement d’image

Ordinateur de test : Intel Core i5-12600KF, 32 Go de mémoire, Windows 11. Plus le temps est court, mieux c’est.

### Python et Rust

#### Conversion des couleurs RAW

À partir du résultat de décodage d’un même RAW Nikon Z 50, on compare le temps de conversion des couleurs entre Python/NumPy et Rust sur 8 threads ; après préchauffage, 9 mesures alternées, et on retient la médiane. Date du test : 5 octobre 2026.

| Traitement | Taille de l’image | Python/NumPy | Rust |
| --- | --- | ---: | ---: |
| Conversion des couleurs RAW | 1864×2800 | 95,0 ms | 7,7 ms |
| Conversion des couleurs RAW | 3728×5600 | 371,3 ms | 29,4 ms |

#### Retouche de portrait

Même portrait, mêmes paramètres de retouche, un visage traité par image, Rust sur 8 threads. Python/NumPy est mesuré sur 10 images et Rust sur 30 ; le tableau donne la médiane, du traitement jusqu’à la génération de l’image d’aperçu. Date du test : 25 octobre 2026.

| Traitement | Taille de l’image | Python/NumPy | Rust |
| --- | --- | ---: | ---: |
| Retouche courante | 3840×2560 | 1,956 s | 1,148 s |
| Retouche et forme du visage | 3840×2560 | 2,652 s | 1,854 s |
| Retouche courante | 6720×4480 | 6,211 s | 3,411 s |
| Retouche et forme du visage | 6720×4480 | 7,425 s | 4,692 s |

#### Masques locaux

Image synthétique de 1600×1067, avec des réglages combinés de tonalité, de couleur et de détail, Rust sur 10 threads. Ces données viennent d’un prototype de masques : pour Python, un seul résultat après préchauffage ; pour Rust, la médiane de 30 mesures avec 1, 3 et 8 masques, et un seul résultat avec 128 masques. Date du test : 19 octobre 2026.

| Nombre de masques | Python | Rust |
| --- | ---: | ---: |
| 1 | 2419,629 ms | 70,750 ms |
| 3 | 2747,778 ms | 94,494 ms |
| 8 | 3121,199 ms | 119,038 ms |
| 128 | 13245,931 ms | 2984,170 ms |

### Rust et Rust + AVX2

Image synthétique de 6000×4000, même traitement mesuré sur 8 threads, avec des mesures répétées dont on retient la médiane. Date du test : 29 octobre 2026.

| Traitement | Rust | Rust + AVX2 |
| --- | ---: | ---: |
| Conversion pour l’affichage | 30,39 ms | 18,95 ms |
| Réglages de couleur de base | 101,63 ms | 64,35 ms |
| Courbes et mélange des couleurs | 382,15 ms | 296,51 ms |
| Réglages de couleur spatiaux | 1459,20 ms | 633,68 ms |
| Réglages locaux avec trois masques | 516,26 ms | 415,92 ms |
| Conversion des couleurs RAW | 23,03 ms | 17,07 ms |

La vitesse réelle varie selon l’ordinateur, les photos et les réglages. AVX2 est déjà implémenté dans les sources ; l’installateur correspondant attend sa validation avant publication.
