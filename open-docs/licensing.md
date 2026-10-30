# 许可证与第三方组件

[简体中文](licensing.md) | [English](licensing.en.md) | [Français](licensing.fr.md)

[返回首页](../README.md)

OpenPictureStudio 的原创代码采用 [MIT License](../LICENSE) 开源。第三方依赖、模型及资源遵循各自的许可证。界面图标取自 [Tabler Icons](https://github.com/tabler/tabler-icons)，采用 MIT 许可，许可文本随图标存放于 [resources/icons/ui/LICENSE.txt](../resources/icons/ui/LICENSE.txt)。

安装包和便携版的程序目录附带以下材料，由构建时运行的 [tools/collect_licenses.py](../tools/collect_licenses.py) 按实际打包内容生成：

- `THIRD_PARTY_NOTICES.txt`：每个组件的名称、版本、许可和主页，以及按需下载的模型与运行环境；
- `licenses/`：各组件的许可文本和版权声明；
- `SOURCE.txt`：本程序对应的提交与源码地址，以及 GPL、LGPL、MPL 组件对应源码的地址和校验值；
- `components.json`：组件与交付文件的对应关系；
- `LICENSE`（MIT）、`GPL-3.0.txt` 与 `LGPL-3.0.txt`。

上游包缺少的许可文本和需要提供源码的组件登记在 [packaging/licenses](../packaging/licenses)。任一交付文件找不到所属组件、组件缺少许可文本，或 GPL、LGPL、MPL 组件没有登记对应源码时，构建会停止。

本项目使用 PySide6 Essentials、Shiboken 和 Qt。项目原创代码采用 MIT 许可；各第三方组件适用其各自的许可证，许可文本和对应源码信息随程序提供。

PySide6、Shiboken 的 Python 模块、扩展模块及 Qt 动态库以独立文件分发，可用兼容版本替换。分发安装包或便携版时，应保留版权声明与许可文本，并提供相关组件的对应源码和构建信息。具体要求见 [Qt 开源许可](https://doc.qt.io/qt-6/licensing.html)和 [Qt LGPL 分发义务](https://www.qt.io/development/open-source-lgpl-obligations)。

README 中的标志、截图、流程图与图标来源见 [素材来源](../.github/assets/readme/SOURCES.md)。

## 从 PyQt6 迁移到 PySide6

自 0.17.0 起，界面组件由 PyQt6 改为 PySide6，安装包不再整体按 GPLv3 分发。0.16.1 及更早版本的许可以该版本随附文件为准。[PyQt6 提供 GPLv3 和商业许可](https://www.riverbankcomputing.com/software/pyqt/intro)；[PySide6 所属的 Qt for Python 提供 LGPLv3／GPL 及商业许可选项](https://doc.qt.io/qtforpython-6/)。项目原创代码继续采用 MIT 许可，PySide6、Shiboken、Qt 及其他依赖分别遵循各自的条款。

采用 LGPL 条件分发的组件保留许可与版权声明、对应源码信息，以及用户替换兼容库的权利。安装包已包含运行所需的界面组件，用户按[安装与升级](installation.md)覆盖安装即可。各版本的具体组件与许可见该版本随附的 `THIRD_PARTY_NOTICES.txt`、`licenses/` 和 `SOURCE.txt`。

## Qt 组件与分发

PySide6／Shiboken 提供 LGPLv3、GPLv3 与商业许可选项，Qt 模块的可用许可取决于实际组件。当前发行清单登记 PySide6 Essentials、Shiboken 与 Qt 6.11.3；新增 Qt 模块时，应重新核对该模块及其第三方依赖的许可。

按 LGPL 条件分发时，应提供适用的许可与版权声明、对应源码及必要构建信息，并保留用户替换相关库和调试其修改的权利。当前打包配置将 PySide6／Shiboken 模块与 Qt 动态库保留为独立文件。具体要求以 [Qt 官方分发义务](https://www.qt.io/development/open-source-lgpl-obligations)和 [Qt for Python 第三方许可清单](https://doc.qt.io/qtforpython-6/licenses.html)为准。

## 依赖与模型入口

- [组件来源与对应源码登记](../packaging/licenses/manifest.json)
- [Qt 第三方声明](../packaging/licenses/qt/QT_THIRD_PARTY_NOTICES.txt)
- [AI 模型目录与许可](../resources/models/catalog.json)
- [可选运行环境目录](../resources/runtimes/catalog.json)
- [界面图标来源](../resources/icons/ui/SOURCE.txt)与[许可](../resources/icons/ui/LICENSE.txt)
