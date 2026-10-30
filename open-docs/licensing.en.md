# Licensing and Third-Party Components

[简体中文](licensing.md) | [English](licensing.en.md) | [Français](licensing.fr.md)

[Back to README](../README.en.md)

The original code of OpenPictureStudio is open source under the [MIT License](../LICENSE). Third-party dependencies, models, and assets follow their own licenses. The UI icons come from [Tabler Icons](https://github.com/tabler/tabler-icons) under the MIT License, and the license text is stored with the icons in [resources/icons/ui/LICENSE.txt](../resources/icons/ui/LICENSE.txt).

The program folder of the installer and the portable version includes the following materials, generated from what is actually packaged by [tools/collect_licenses.py](../tools/collect_licenses.py), which runs during the build:

- `THIRD_PARTY_NOTICES.txt`: the name, version, license, and homepage of each component, plus the models and runtimes that are downloaded as needed;
- `licenses/`: the license text and copyright notice of each component;
- `SOURCE.txt`: the commit and source address of this program, plus the address and checksum of the corresponding source for GPL, LGPL, and MPL components;
- `components.json`: how components map to the distributed files;
- `LICENSE` (MIT), `GPL-3.0.txt`, and `LGPL-3.0.txt`.

License texts missing from upstream packages, and components that need their source provided, are registered in [packaging/licenses](../packaging/licenses). The build stops if any distributed file has no component it belongs to, if a component has no license text, or if a GPL, LGPL, or MPL component has no registered corresponding source.

This project uses PySide6 Essentials, Shiboken, and Qt. The project's original code is under the MIT License. Each third-party component follows its own license, and the license texts and corresponding source information ship with the program.

The PySide6 and Shiboken Python modules, the extension modules, and the Qt dynamic libraries are distributed as separate files and can be replaced with compatible versions. When distributing the installer or the portable version, keep the copyright notices and license texts, and provide the corresponding source and build information for the relevant components. For the exact requirements, see [Qt open source licensing](https://doc.qt.io/qt-6/licensing.html) and [Qt LGPL distribution obligations](https://www.qt.io/development/open-source-lgpl-obligations).

For the sources of the logo, screenshots, diagrams, and icons in the README, see [Asset sources](../.github/assets/readme/SOURCES.en.md).

## Migrating from PyQt6 to PySide6

Starting with 0.17.0, the UI components changed from PyQt6 to PySide6, and the installer is no longer distributed as a whole under GPLv3. For 0.16.1 and earlier, the license follows the files shipped with that version. [PyQt6 is offered under GPLv3 and a commercial license](https://www.riverbankcomputing.com/software/pyqt/intro), and [Qt for Python, which includes PySide6, is offered under LGPLv3/GPL and commercial license options](https://doc.qt.io/qtforpython-6/). The project's original code stays under the MIT License. PySide6, Shiboken, Qt, and the other dependencies each follow their own terms.

Components distributed under LGPL conditions keep their license and copyright notices, the corresponding source information, and the user's right to replace compatible libraries. The installer already includes the UI components needed to run, so users just upgrade in place as described in [Installation and Upgrades](installation.en.md). For the exact components and licenses of each version, see the `THIRD_PARTY_NOTICES.txt`, `licenses/`, and `SOURCE.txt` that ship with that version.

## Qt Components and Distribution

PySide6/Shiboken are offered under LGPLv3, GPLv3, and commercial license options, and which licenses a Qt module offers depends on the actual component. The current release manifest records PySide6 Essentials, Shiboken, and Qt 6.11.3. When a new Qt module is added, check again the license of that module and of its third-party dependencies.

When distributing under LGPL conditions, provide the applicable license and copyright notices, the corresponding source, and the necessary build information, and keep the user's right to replace the relevant libraries and debug their modifications. The current packaging configuration keeps the PySide6/Shiboken modules and the Qt dynamic libraries as separate files. For the exact requirements, the [official Qt distribution obligations](https://www.qt.io/development/open-source-lgpl-obligations) and the [Qt for Python third-party license list](https://doc.qt.io/qtforpython-6/licenses.html) apply.

## Dependency and Model References

- [Component sources and corresponding-source records](../packaging/licenses/manifest.json)
- [Qt third-party notices](../packaging/licenses/qt/QT_THIRD_PARTY_NOTICES.txt)
- [AI model catalog and licenses](../resources/models/catalog.json)
- [Optional runtime catalog](../resources/runtimes/catalog.json)
- [UI icon sources](../resources/icons/ui/SOURCE.txt) and [license](../resources/icons/ui/LICENSE.txt)
