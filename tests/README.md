# 测试说明

[简体中文](README.md) | [English](README.en.md)

测试按当前产品契约维护，全部离屏运行。自动化测试不能代替真实窗口、高 DPI、实际资源安装和安装包的人工检查。

## 运行

在仓库根目录、已安装依赖的虚拟环境中执行（发行构建使用 `requirements-build.lock.txt` 的版本）：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONUTF8 = '1'
python -m pytest tests -q -p no:cacheprovider -o faulthandler_timeout=300
```

`faulthandler_timeout` 让卡住超过 300 秒的运行导出所有线程的调用栈。Rust 原生模块 `ops_accel.pyd` 不存在时，依赖它的用例按原因跳过，其余用例走 Python 实现。

## 完整模型与 RAW 回归

CI 在测试前运行资源准备工具，校验模型目录与固定测试资源清单中的 SHA-256。权重、RAW 和 DCP 缓存在 Git 忽略目录，不随程序打包。

```powershell
python tools/prepare_ci_assets.py
$fixtures = Get-Content .pytest-tmp/fixtures/environment.json -Raw | ConvertFrom-Json
$env:OPS_RAW_FIXTURE = $fixtures.OPS_RAW_FIXTURE
$env:OPS_CI_DCP_PROFILE = $fixtures.OPS_CI_DCP_PROFILE
$env:OPS_DCP_DUMP_DIR = $fixtures.OPS_DCP_DUMP_DIR
python -m pytest tests -q -p no:cacheprovider -o faulthandler_timeout=300
```

模型使用[应用模型目录](../resources/models/catalog.json)中的 NAFNet Deblur 与 U2Net。RAW 和同机型 DCP 的固定来源与摘要见[测试资源清单](fixtures/ci_assets.json)。`OPS_CI_DCP_PROFILE` 只在 DCP 用例内启用，默认 RAW 路径保持独立。

NASA 样片 ISS030-E-122639：Image courtesy of the Earth Science and Remote Sensing Unit, NASA Johnson Space Center。来源与使用说明见 [NASA 图库](https://eol.jsc.nasa.gov/FAQ/)。Nikon D3S DCP 来自 RawTherapee，许可及固定来源见资源清单。

Windows 内存登记用例校验系统批准后的上下限及恢复；权限错误 1314 时校验登记被拒绝且原有限额不变。逐位比色验收继续按各 RAW 用例记录的 RawTherapee 对拍步骤执行。

## 覆盖职责

| 职责 | 主要入口 | 验证内容 |
| --- | --- | --- |
| 基础控件、主题、国际化 | `test_apple_*`、`test_toggle_switch_apple.py`、`test_semantic_colors.py`、`test_ui_buttons.py`、`test_ui_literals.py`、`test_message_dialog.py`、`test_ui_empty_state.py`、`test_i18n_files.py` | 用户编辑与静默回填、控件状态、主题取色、语言刷新、各语言翻译文件的键与占位符一致、界面代码里没有写死的颜色字号与圆角、消息与进度对话框、空状态与看图没有图片时的引导 |
| 主窗口与工作区 | `test_ui_top_bar.py`、`test_ui_canvas_toolbar.py`、`test_ui_filmstrip.py`、`test_ui_photo_info.py`、`test_ui_viewer_histogram.py`、`test_ui_tool_rail.py`、`test_ui_status_messages.py`、`test_ui_settings.py`、`test_ui_id_photo_tool.py`、`test_ui_canvas_tools.py`、`test_ui_window_chrome.py` | 统一动作、工具栏、胶片条、照片信息、看图的直方图、工具切换、提示条、轻提示、设置窗口与快捷键、证件照工具、画布工具的调整与结果 |
| 启动、图片列表与编辑协作 | `test_startup.py`、`test_studio_workspace_and_dual_workspaces.py`、`test_thumbnail_panel.py`、`test_refactor_plan_tool_sidebar.py`、`test_reuse_refactor.py` | 延迟装配、源身份、导航、草稿恢复、撤销重做、保存与服务复用 |
| 工具组件与资源契约 | `test_ops_tool_panels.py`、`test_phase1_draft_and_adapter.py`、`test_ui_enhance_panel.py`、`test_ui_beauty_panel.py`、`test_ui_color_panel.py`、`test_ui_editor_panels.py`、`test_ui_manual_preview.py`、模型／工具／运行环境相关测试 | 注册面板、参数版本、手动预览、缺模型限制、实际渲染、资源引用 |
| 项目、任务与保存安全 | `test_ops_review_regressions.py`、`test_phase0_core_api.py`、`test_transform_save_regressions.py`、项目事务相关测试 | 结构损坏拒绝、缺能力项目保留、历史分支、原子发布、取消、过期请求拒收 |
| 成像与资源预算 | DCP、E1、白平衡、蒙版、输出格式、图像资源与计算预算相关测试 | 数值合同、编码精度、输入只读、Alpha、内存与线程准入 |
| 蒙版渲染一致性 | `test_mask_render_consistency.py` | 原生／回退、处理版本、单／多线程、启用与禁用、采样、Alpha、取消和异常回退 |
| 存储与工作目录 | `test_storage_workspaces.py`、存储相关测试 | 工作目录在垃圾回收中只登记收尾，不重入线程池全局锁 |
| 历史成像格式 | `test_ops_identity_compatibility.py` | 历史显示方式、浮点产物、观感取值及调色参数契约继续可用 |
| Agent 与插件 | Agent／插件相关测试 | 后台服务合同 |

`PANEL_TYPES` 包含十种组件；工具栏 `TOOL_ENTRIES` 有八种编辑工具。旧降噪面板的独立组件测试属于兼容覆盖，产品降噪入口是画质增强；证件照是左侧工具栏的一个入口，版本 1 的旧节点用 `LegacyIDPhotoPanel` 打开。

## 准备与资源归属

- 根 `conftest.py` 依赖会话级 `qapp`，保证 Qt 应用存活到任务与控件释放之后。
- 每个用例的设置、草稿、评级、缓存、受管模型和运行环境目录落在 `tmp_path/app`；子进程使用所属临时根和 UTF-8。真实用户设置与安装目录由隔离入口保护。
- `window_factory` 按正常主窗口入口创建并登记关闭；`image_sources`、`tool_services`、`photo_service`、`panel_factory` 显式装配所需服务，按创建者释放任务、资源与订阅。
- `tests/support.py` 按当前路径、源身份、加载代次、参数状态及列表版本等待实际就绪；超时报出当前状态。切工具经所属工作区命令，工作区首次打开才触发主窗口装配。
- 模型就绪与目录发现是两个状态。真实输出前等待模型服务串行发现任务完成，避免验证途中仍有资源代次变化。控件能力测试可以在模型查询边界注入快照；真实输出使用实际算法与权重。
- `available_neural_models` 只读借用仓库 `models/deblurring_nafnet_2025may.onnx`；固定身份及摘要由模型目录校验。缺少该文件时明确跳过相关真实模型用例，不触发下载或安装。
- 三项 RAW 对拍测试需要 `OPS_RAW_FIXTURE` 指向一张 RAW 文件，其中 DCP 对拍另需相机 DCP 配置及 RawTherapee。未准备时按各用例的原因跳过。
