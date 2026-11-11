# 图像插件接口

[简体中文](image-plugins.md) | [English](image-plugins.en.md) | [Français](image-plugins.fr.md)

## 注册与版本

受信 Python 插件通过 `ToolRuntime.register_image_plugin(directory, trusted_hash=...)` 注册。宿主先审核插件包及其 SHA-256，再把已认可的摘要传入注册接口。运行时保存包的独立快照，工具通过标准 `ToolReference` 加入工作流。安装管理与分发见[路线图](roadmap.md)。

```python
from pathlib import Path
from src.application.plugin_service import PluginService
from src.application.tool_runtime import ToolRuntime
from src.core.contracts import WorkflowGraph, WorkflowNode

directory = Path("resources/image_plugins/org.openpicturestudio.examples")
manifest, entrypoint, digest = PluginService.inspect(directory)
# 审核文件及 digest 后，使用已认可的摘要。
runtime = ToolRuntime()
references = runtime.register_image_plugin(directory, trusted_hash=digest)
reference = next(item for item in references if item.tool_id.endswith(".exposure"))
workflow = WorkflowGraph(nodes=(WorkflowNode("exposure", "tool", reference.to_params({"stops": 0.5})),))
```

生产宿主固定审核后的摘要；参数、实现版本和资源摘要随工作流保存。项目文档版本 4 同时保存插件依赖清单。移除插件后，项目参数和已有输出仍可读取，重新计算报告缺少实现；恢复原包后继续使用原版本。`unregister_image_plugin(digest)` 移除当前注册并取消旧代请求。

## 包与图像契约

`plugin.json` 使用 `manifest_version: "2.0"`、`runtime: "subprocess_python"` 和带发布者范围的 `plugin_id`。每个 `declared_nodes` 条目声明：

| 字段 | 内容 |
| --- | --- |
| `node_id`、`contract_version` | 包内节点名、`image/1` |
| `params_version`、`parameters` | 参数版本及字段名称、类型、默认值、范围和选项 |
| `image` | `linear_srgb`、`float32`、Alpha 保留或变换、几何保留或声明输出尺寸 |
| `supports_proxy` | 图像与坐标语义能否用于代理 |
| `is_deterministic`、`cache` | 确定性声明与 `deterministic`／`seeded`／`never` 策略 |
| `seed_parameter` | 种子策略使用的整数参数名 |
| `external_state` | 参与结果身份的固定外部状态版本 |
| `auxiliary` | 具名辅助数据的名称、能力、类型和版本 |
| `timeout_sec` | 最长执行时间，1–300 秒范围内 |

参数类型支持 `integer`、`number`、`boolean`、`string`、`array`。`resources` 列出包内资源的相对路径及 SHA-256。声明网络权限的包由当前宿主拒绝注册。

接口第一版由 CPU 执行，计算线程和工作内存由宿主调度。子进程继承当前用户的系统权限；网络权限字段是包接入规则，操作系统权限和网络访问由运行环境管理。

接口第一版使用一个主图像输入和输出。标准输入为 UTF-8 JSON，包含 `protocol: "image/1"`、`api_version: 1`、`node_id`、`params`、`input`、`output` 和 `auxiliary`。主图像使用 H×W×3 的 float32 NPY，Alpha 使用独立 H×W float32 NPY；输入描述包含实际尺寸、完整参考尺寸、代理标志、颜色空间、D65 白点、色域和直通 Alpha。读取时使用 `allow_pickle=False`。

插件将结果写入请求指定的 `output.pixels` 和可选 `output.alpha`，然后向标准输出写入：

```json
{"protocol":"image/1","status":"ok","alpha":true}
```

输出尺寸、数据类型、有限数值、Alpha 范围及声明的 Alpha／几何行为由宿主校验。处理期间响应宿主的取消和超时，子进程退出后关闭。资源和临时文件纳入宿主预算与托管工作目录。

## 辅助产物

宿主通过注册参数 `auxiliaries` 提供 `AuxiliaryResolver`，按能力名解析声明的辅助数据。解析器的 `borrow(source, params, cancelled)` 返回上下文管理器，在借用期提供 `ImagePluginAuxiliary`。数据为只读 float32 数组，包含类型、能力、版本、稳定身份和参考尺寸。

- `mask-f32`：与输入实际尺寸相同的 H×W 权重，范围 0–1。
- `faces-2d`：人脸数×关键点数×2 的规范化坐标。

解析器负责产物租约和预算；其身份及所需资源进入执行依赖。产物只依赖声明的资源、版本、当前输入和参数。模型、外部状态或语义发生变化时更新解析器身份，并重新注册。辅助数据依赖持续变化的外部内容时，使用 `cache: "never"`。`resources` 中的 `(role, digest, path)` 固定辅助模型资源，随工具引用保存。

## 缓存与旧协议

确定性和种子策略只有在输入、参数、包、资源、辅助能力及外部状态一致时复用。`never` 禁用该节点及下游跨请求复用，并使用完整输入；自动画布保留本次结果用于放大和平移。提交或导出把本次结果写入既有产物事务。

`manifest_version: "1.0"`、`contract_version: "3.4.0"` 的旧插件通过原 `PluginService` 运行，输入输出转换明确使用 sRGB8 PNG。它们继续使用原包校验、审计、超时和取消机制。

## 暗角与区域扩展

`runtime.register_tool(src.core.tools.vignette.definition())` 注册暗角演练工具。强度变化复用几何权重，中心、半径或羽化变化更新权重，上游像素变化只更新应用阶段。

`ToolDefinition` 的 `output_size`、`region_input`、`execute_region` 和可选 `region_memory` 声明完整输出尺寸、区域输入范围、区域算法和额外内存。`execute_region` 上下文提供输入／输出范围、完整尺寸、线程数及取消回调。需要完整域的工具保留完整计算，区域结果通过独立身份缓存。

完整样例位于 [resources/image_plugins](../resources/image_plugins/org.openpicturestudio.examples)，涵盖曝光、固定种子颗粒与每次生成新结果的颗粒。
