# Roadmap

[简体中文](roadmap.md) | [English](roadmap.en.md) | [Français](roadmap.fr.md)

[Back to Home](../README.en.md) · [Documentation](README.en.md)

OpenPictureStudio brings professional image processing and AI collaboration together into an open image creation platform directed by creators and extensible by developers.

## Product Vision

Professional creators need predictable color, tonal precision, non-destructive history, and consistent delivery; everyday users want an approachable path from an initial idea to a finished photo; developers and AI agents need clear, composable, inspectable tool contracts. OPS evolves around a unified image and project model where human aesthetic judgment, native algorithms, and AI execution work together on the same asset.

## Current Baseline

OPS delivers a complete Windows desktop application (see individual [Release Notes](https://github.com/KpTanl/OpenPictureStudio/releases) for version-specific changes):

- **Viewing and non-destructive editing:** Standard formats and camera RAW decoding, rating/culling, linked comparison, non-destructive color grading and local masks, portrait retouching, edit history, group sync, and batch export.
- **GPU & native acceleration:** General-purpose wgpu/D3D12 image compute with out-of-VRAM GPU strip rendering (color grading, local masks, crop/rotation, portrait beauty, and qualified classic denoise), Rust/Rayon/AVX2 CPU acceleration, and optional DirectML model inference.
- **AI collaboration:** Multi-mode AI assistant with reference-image analysis, A/B candidate comparison, named style memory, and undoable history integration.
- **Extensibility:** Unified tool descriptors, workflow engine, `.opsproj` schema 4 project model, and trusted `image/1` subprocess image plugins.

## Four-Stage Product Evolution

```mermaid
flowchart LR
    S1["Stage 1 · AI Collaboration"] --> S2["Stage 2 · Automated Workflows"]
    S2 --> S3["Stage 3 · All-Round Creation"]
    S3 --> S4["Stage 4 · Open Plugin Ecosystem"]
```

### Stage 1: Deepen AI Creative Collaboration

Connect the assistant more closely with image content, editing context, and creative intent:

- **Cross-tool execution:** Expand tool coverage across culling, global grading, local masks, portrait retouching, and export.
- **Context & personal style:** Improve reference-image color/tone analysis and reusable named styles and preferences.
- **Controllable edits:** Refine multi-step planning, local refinement, and A/B candidate review with full undo/redo support.

### Stage 2: Build Automated Workflows

Turn successful manual adjustments into composable, reusable, schedulable workflows:

- **Flow orchestration:** Configurable node graphs, parameter reuse, conditional execution, multi-input steps, and reusable subflows.
- **Group delivery & background jobs:** Smart batch application across photo groups with pause, cancel, and retry controls.
- **External agent interface:** Expose loading, processing, previewing, project saving, and export contracts so external AI agents can orchestrate tasks headlessly.

### Stage 3: Broaden All-Round Image Creation

Combine parametric photo editing, pixel compositing, and generative AI in a single project format:

| Editing Paradigm | Direction |
| --- | --- |
| **Non-destructive adjustments** | Continue refining RAW color science, local masks, and high-precision parametric pipelines |
| **Layers & pixel editing** | Add layers, advanced selections, compositing, and direct pixel brushwork backed by unified history and `.opsproj` |
| **Generative editing** | Integrate image generation, inpainting, and localized synthesis alongside standard grading tools |

### Stage 4: Build an Open Extension Ecosystem

Advance the **Everything is Plugin** architecture for sustainable third-party extensibility:

- **Public API & plugin SDK:** Stabilize tool contracts, parameter schemas, resource management, and runtime documentation.
- **Sandboxing & permissions:** Strengthen plugin isolation, explicit permission boundaries, resource quotas, and version compatibility.
- **Community sharing:** Support distributing third-party algorithms, model adapters, named styles, and workflow templates.

## Continuous Engineering Tracks

| Engineering Track | Focus Areas |
| --- | --- |
| **Native CPU Engine** | Expand Rust + Rayon parallelism and buffer reuse, broaden AVX2 coverage, and evaluate FMA where precision permits while keeping portable x64 support |
| **Cross-Hardware GPU** | Qualify Intel, AMD, and NVIDIA hardware across more AI and image tools, refining shared wgpu/D3D12 and DirectML memory scheduling |
| **RAW & Color Management** | Improve full RAW decode throughput, preview responsiveness, and wide-gamut / high-bit-depth color fidelity |
| **Architecture & Quality** | Evolve modular PySide6 architecture, fault recovery, long-session stability, and backward-compatible project loading |

## Open Collaboration and License

Original OPS source code is licensed under the [MIT License](../LICENSE), with third-party libraries and models governed by their own licenses. Priorities adapt with technical readiness and community feedback—share ideas or feature requests via [GitHub Issues](https://github.com/KpTanl/OpenPictureStudio/issues).

## Related Documentation

- [Architecture and Native Acceleration](architecture.en.md)
- [Image Plugins](image-plugins.en.md)
- [Licensing and Third-Party Components](licensing.en.md)
