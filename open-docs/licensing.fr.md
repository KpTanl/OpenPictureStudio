# Licences et composants tiers

[简体中文](licensing.md) | [English](licensing.en.md) | [Français](licensing.fr.md)

[Retour à l’accueil](../README.fr.md)

Le code original d’OpenPictureStudio est publié sous [licence MIT](../LICENSE). Les dépendances tierces, les modèles et les ressources suivent chacun leur propre licence. Les icônes de l’interface viennent de [Tabler Icons](https://github.com/tabler/tabler-icons), sous licence MIT ; le texte de la licence est rangé avec les icônes, dans [resources/icons/ui/LICENSE.txt](../resources/icons/ui/LICENSE.txt).

Le dossier du programme de l’installateur et de la version portable contient les éléments suivants, générés à la compilation par [tools/collect_licenses.py](../tools/collect_licenses.py) d’après le contenu réellement empaqueté :

- `THIRD_PARTY_NOTICES.txt` : le nom, la version, la licence et la page d’accueil de chaque composant, ainsi que les modèles et les moteurs d’exécution téléchargés à la demande ;
- `licenses/` : le texte de licence et les mentions de droits d’auteur de chaque composant ;
- `SOURCE.txt` : le commit et l’adresse des sources correspondant à ce programme, ainsi que l’adresse et la somme de contrôle des sources correspondant aux composants GPL, LGPL et MPL ;
- `components.json` : la correspondance entre les composants et les fichiers livrés ;
- `LICENSE` (MIT), `GPL-3.0.txt` et `LGPL-3.0.txt`.

Les textes de licence absents des paquets amont et les composants dont les sources doivent être fournies sont enregistrés dans [packaging/licenses](../packaging/licenses). La compilation s’arrête si un fichier livré n’a pas de composant d’origine, si un composant n’a pas de texte de licence, ou si un composant GPL, LGPL ou MPL n’a pas de sources correspondantes enregistrées.

Ce projet utilise PySide6 Essentials, Shiboken et Qt. Le code original du projet est sous licence MIT ; chaque composant tiers relève de sa propre licence, et les textes de licence et les informations sur les sources correspondantes sont fournis avec le programme.

Les modules Python de PySide6 et de Shiboken, les modules d’extension et les bibliothèques dynamiques de Qt sont distribués comme fichiers indépendants, que l’on peut remplacer par une version compatible. Pour distribuer l’installateur ou la version portable, conservez les mentions de droits d’auteur et les textes de licence, et fournissez les sources correspondantes et les informations de compilation des composants concernés. Les exigences précises figurent dans la [licence open source de Qt](https://doc.qt.io/qt-6/licensing.html) et les [obligations de distribution LGPL de Qt](https://www.qt.io/development/open-source-lgpl-obligations).

L’origine des logos, captures d’écran, schémas et icônes du README est indiquée dans les [sources des ressources](../.github/assets/readme/SOURCES.en.md).

## Migration de PyQt6 vers PySide6

Depuis la version 0.17.0, les composants d’interface sont passés de PyQt6 à PySide6, et l’installateur n’est plus distribué dans son ensemble sous GPLv3. Pour la version 0.16.1 et les versions antérieures, la licence est celle des fichiers fournis avec cette version. [PyQt6 est proposé sous GPLv3 et sous licence commerciale](https://www.riverbankcomputing.com/software/pyqt/intro) ; [Qt for Python, dont fait partie PySide6, propose LGPLv3/GPL et une option de licence commerciale](https://doc.qt.io/qtforpython-6/). Le code original du projet reste sous licence MIT ; PySide6, Shiboken, Qt et les autres dépendances suivent chacun leurs propres conditions.

Pour les composants distribués selon les conditions de la LGPL, on conserve la licence et les mentions de droits d’auteur, les informations sur les sources correspondantes, et le droit de l’utilisateur de remplacer les bibliothèques compatibles. L’installateur contient déjà les composants d’interface nécessaires à l’exécution : il suffit d’installer par-dessus, comme décrit dans [Installation et mises à niveau](installation.fr.md). Les composants et licences précis de chaque version se trouvent dans les `THIRD_PARTY_NOTICES.txt`, `licenses/` et `SOURCE.txt` fournis avec cette version.

## Composants Qt et distribution

PySide6/Shiboken sont proposés sous LGPLv3, sous GPLv3 et avec une option de licence commerciale ; les licences disponibles pour un module Qt dépendent du composant réellement utilisé. La liste de distribution actuelle enregistre PySide6 Essentials, Shiboken et Qt 6.11.3 ; si l’on ajoute un module Qt, il faut revérifier la licence de ce module et de ses dépendances tierces.

Distribuer selon les conditions de la LGPL demande de fournir les licences et mentions de droits d’auteur applicables, les sources correspondantes et les informations de compilation nécessaires, et de conserver le droit de l’utilisateur de remplacer les bibliothèques concernées et de déboguer ses modifications. La configuration d’empaquetage actuelle garde les modules PySide6/Shiboken et les bibliothèques dynamiques de Qt comme fichiers indépendants. Les exigences précises sont celles des [obligations de distribution officielles de Qt](https://www.qt.io/development/open-source-lgpl-obligations) et de la [liste des licences tierces de Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html).

## Dépendances et modèles

- [Registre des origines des composants et des sources correspondantes](../packaging/licenses/manifest.json)
- [Mentions tierces de Qt](../packaging/licenses/qt/QT_THIRD_PARTY_NOTICES.txt)
- [Catalogue et licences des modèles IA](../resources/models/catalog.json)
- [Catalogue des moteurs d’exécution facultatifs](../resources/runtimes/catalog.json)
- [Origine](../resources/icons/ui/SOURCE.txt) et [licence](../resources/icons/ui/LICENSE.txt) des icônes de l’interface
