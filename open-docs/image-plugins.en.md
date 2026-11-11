# Image plugins

[简体中文](image-plugins.md) | [English](image-plugins.en.md) | [Français](image-plugins.fr.md)

Trusted Python packages register through `ToolRuntime.register_image_plugin(directory, trusted_hash=...)`. The host reviews the package and pins its SHA-256 before registration. The runtime keeps an independent snapshot and exposes standard `ToolReference` values. Project schema 4 records package digests and interface and implementation versions. Removing a plugin preserves project parameters and existing outputs; recomputation reports the missing implementation until the exact package returns.

`plugin.json` declares `manifest_version: "2.0"`, `runtime: "subprocess_python"`, a publisher-scoped `plugin_id`, and `declared_nodes` using `contract_version: "image/1"`. Nodes declare versioned parameters, image behavior, proxy support, timeout, caching, and optional named auxiliary capabilities. Numeric, integer, Boolean, string, and array parameters support defaults, ranges, and choices. Package resources carry SHA-256 digests.

The UTF-8 JSON request contains `protocol: "image/1"`, `api_version: 1`, `node_id`, `params`, `input`, `output`, and `auxiliary`. One primary image uses H×W×3 linear-sRGB float32 NPY data, with separate H×W float32 straight alpha. Input metadata includes actual and reference dimensions, proxy state, D65 white point, gamut, and color space. Load arrays with `allow_pickle=False`.

Write output arrays to the specified paths, then return `{"protocol":"image/1","status":"ok","alpha":true}` on standard output. The host validates dimensions, float32 data, finite values, alpha range, and declared alpha and geometry behavior. Cancellation and timeout end the subprocess. Files and buffers use host-managed budgets and workspaces. Packages declaring network permission are rejected by the current host.

The first interface version runs on the CPU, with host-managed compute threads and working memory. Subprocesses inherit the current user's system permissions. The network permission field controls package admission; the execution environment manages operating-system permissions and network access. `timeout_sec` accepts finite numbers from 1 to 300 seconds.

Caching policies are `deterministic`, `seeded` with a declared integer seed parameter, and `never`. All input, package, resource, auxiliary, parameter, and external-state identities participate. `never` disables cross-request reuse for the node and its downstream results; the canvas retains the current result for zooming and panning. `external_state` pins stable state beyond the input and parameters.

Hosts provide named `AuxiliaryResolver` capabilities at registration. Their context-managed `borrow(source, params, cancelled)` supplies read-only `ImagePluginAuxiliary` arrays with stable identity, semantic version, and reference dimensions. `mask-f32` carries H×W weights in 0–1; `faces-2d` carries normalized face×landmark×2 coordinates. Resolver identities and resources are execution dependencies. Resolvers own artifact leases and budget accounting.

Auxiliary results depend on the declared resources, resolver identity, current input, and parameters. Update the resolver identity and register the package again when models, external state, or semantics change. Use `cache: "never"` for auxiliary data that depends on continuously changing external content.

Legacy manifest `1.0`, contract `3.4.0` packages use the existing audited `PluginService` with an explicit sRGB8 PNG adapter. Installation management and distribution follow the [roadmap](roadmap.en.md).

The [sample package](../resources/image_plugins/org.openpicturestudio.examples) provides exposure, seeded grain, and uncached grain. The [complete registration example](image-plugins.md#注册与版本) uses the standard tool workflow node. `ToolRuntime.register_tool` also accepts the vignette definition in `src.core.tools.vignette`. Regional tools declare `output_size`, `region_input`, `execute_region`, and optional `region_memory`; their context preserves full dimensions and coordinate bounds.
