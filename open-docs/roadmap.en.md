# Roadmap

[简体中文](roadmap.md) | [English](roadmap.en.md) | [Français](roadmap.fr.md)

[Back to README](../README.en.md)

## Project Vision

**Starting from professional photo processing, OPS will gradually bring together AI collaboration, automated workflows, and multi-mode image editing, to build an open AI image creation platform that creators control and developers extend together.**

OpenPictureStudio (OPS) aims to combine professional imaging capabilities with AI, so that people can manage photos, grade color professionally, retouch intelligently, automate processing, and take on a wider range of image creation tasks in one creative environment.

Everyday users can start from a photo and an idea;

professional creators can take deep control of color, light and shadow, and detail;

developers can bring new algorithms, models, and tools into the platform.

As workflows and external interfaces mature, AI agents will also be able to organize tool execution around a creative goal and hand the results back to the creator for review.

Human judgment, professional tools, and AI execution will collaborate on the same work. Creative experience can build up with a project and gradually become a method that can be reused, combined, and shared.

## Where We Are Now

OPS already offers a Windows Beta. The project currently covers photo browsing and culling, RAW decoding and color grading, local masks, non-destructive editing, AI image enhancement, an intelligent retouching assistant, photo group processing, project history, and batch export. For the specific features in each version, see the matching [release notes](https://github.com/KpTanl/OpenPictureStudio/releases).

- **Professional processing**: Built on linear image processing, RAW color grading, and local adjustments, and the creative process is saved through parameters, history, and projects.
- **AI collaboration**: The foundations are in place, including the retouching assistant, reference images, plan comparison, and named styles, and applicable adjustments go into an undoable edit history.
- **Group delivery**: Photo culling, group adjustments, and batch export already exist, building a base for continuous processing and reusing workflows.
- **Open extension**: Tool registration, workflow execution, and the project model already exist, and the development interfaces for third parties will keep being refined.

## The Four-Stage Roadmap

| Stage | Product goal | Main plans |
| --- | --- | --- |
| **Stage 1: Deepen AI creative collaboration** | Help AI understand user intent better and assist with professional image editing tasks | Expand the Agent's tool abilities, strengthen context understanding, reference image analysis, and style memory, and support multi-step adjustments, local refinement, and plan comparison |
| **Stage 2: Build automated workflows** | Turn repeated operations and creative experience into flows that can be reused, combined, and run | Build a configurable workflow system with step orchestration, parameter reuse, conditional execution, batch tasks, background scheduling, and calls from external AI agents |
| **Stage 3: Broaden all-round image creation** | Bring several ways of editing images into one project and widen the creative range | On top of non-destructive editing, gradually add layers, professional selections, pixel-level editing, and generative AI editing, with unified project saving and edit history |
| **Stage 4: Build an open extension ecosystem** | Let creators and developers extend the platform's abilities together | Stabilize the public API and plugin SDK, support third-party algorithms, models, and tools, improve plugin isolation and permissions, and grow the sharing and distribution of styles, workflows, and plugins |

### Stage 1: Deepen AI Creative Collaboration

Starting from the existing retouching assistant, let AI take part in a fuller creative process, drawing on the photo's content, the editing state, and the user's goal. Users can describe the mood they want, the details to keep, and the delivery requirements, then get closer to the goal step by step through preview, comparison, and parameter adjustment.

Main directions:

- **Cross-tool collaboration**: Expand the editing tools the assistant can call, connecting culling, global color grading, local adjustments, portrait processing, and export.
- **Creative context**: Draw up multi-step plans from the current photo, the project, the edit history, and the user's requests, and support changing the goal midway and carrying on adjusting.
- **Reference images and personal style**: Strengthen the analysis of the color, light, and style of reference images, so that users can build up, name, and reuse their own creative preferences.
- **Controlled execution**: Improve plan preview, local refinement, and comparison of several plans, so that key changes can be reviewed, adopted, undone, and edited further.

This stage aims to make natural language, professional parameters, and visual feedback one continuous creative process. Users can use AI to lower the cost of operating the tools, and can still go deep into the tools to control details at any time.

### Stage 2: Build Automated Workflows

Keep a processing method that worked once, so that it can be applied to the next photo, the next set of material, and the next task. Personal styles, parameter combinations, and delivery rules will gradually become creative flows that can be combined and run.

Main directions:

- **Flow orchestration**: Support combining processing steps and configuring inputs, parameters, and outputs, and gradually add conditional execution, multi-input processing, and reusable flow fragments.
- **Group processing**: Apply the matching flow based on culling results, photo groups, and differences in the material, to improve consistency and delivery efficiency for a set of works.
- **Background tasks**: Improve task progress, pause, cancel, retry on failure, and result management, so that long-running processing is easier to schedule and take over.
- **External Agent interface**: Gradually open up photo loading, processing, preview, project saving, and export, so that an external AI agent can organize tasks without opening the desktop interface.

This stage aims to save the time spent on repeated operations for creative judgment, so that individuals and small teams can reliably reuse their processing methods and turn daily experience into a workflow they can keep using.

### Stage 3: Broaden All-Round Image Creation

Building on the existing non-destructive editing, gradually connect photo processing, pixel editing, and generative creation. In one project, users can organize material, adjust the picture, and change local content, while keeping the state and history needed to continue creating.

| Way of editing | Direction |
| --- | --- |
| **Non-destructive editing** | Keep improving RAW, color, local masks, and parametric processing, and keep the adjustable steps and creative judgment |
| **Pixel and layer editing** | Gradually introduce layers, professional selections, compositing, and direct pixel changes, tied to undo, history, and project saving |
| **Generative editing** | Explore image generation, local repainting, and content changes, and combine generated results with the regular editing tools |

The several ways of editing will work together around one unified project: generated results can go on to color grading and local processing, pixel changes and parameter adjustments can both take part in making a work, and project saving can carry the material, the processing steps, and the resource references that are needed.

This stage aims to extend creation from fixing an existing picture to combining material, changing content, and building new visual expression.

### Stage 4: Build an Open Extension Ecosystem

Along the direction of Everything is Plugin, let new algorithms, models, tools, and creative methods keep entering the platform. Creators can choose the combination of abilities that suits them, and developers can contribute extensions around clear needs.

Main directions:

- **Public API and plugin SDK**: Gradually stabilize tool integration, parameter declarations, resource management, and the execution interface, and provide documentation and examples to lower the cost of third-party extensions.
- **Tool and model extensions**: Support new image algorithms, model adapters, and creative tools taking part in manual editing, workflow execution, and AI calls.
- **Isolation and permissions**: Keep improving plugin execution isolation, permission declarations, resource access, and version compatibility, so that what an extension does and where it can be used is clearer.
- **Sharing creative methods**: Grow the sharing and distribution of named styles, workflow templates, and plugins, so that professional experience can be learned, combined, and built on again.

In the long run, the hope is for an ecosystem where creators and developers support each other: real needs bring new tools, tools and flows carry new methods, and sharing in turn drives more creative practice.

## Ongoing Engineering

Performance, stability, compatibility, and engineering quality run through every stage and support the continued growth of the product.

### Native Image Computation Engine

Rust is the preferred native computation path, with a fallback to Python/NumPy kept for compatibility. Computation, data copying, and buffer reuse keep being optimized, and together with Rayon multithreading they improve the efficiency of large-image editing and batch processing.

CPUs that support AVX2 automatically enable the kernels already wired in, and other CPUs use the portable Rust path. Later work will explore further optimizations such as FMA, extending the acceleration within the limits of numerical precision and compatibility.

### Cross-Hardware Acceleration

On top of CPU processing power, we will explore a wgpu-based GPU image computation backend. We will first evaluate the real gains on Windows hardware such as Intel, AMD, and NVIDIA, and keep a reliable CPU execution path.

As the image operation interfaces and resource management mature, we will keep studying support for more hardware and platforms. DirectML, used for AI inference today, and the planned GPU image processing develop separately and together serve the editing experience.

### RAW and Color Processing

Keep optimizing full RAW decoding, preview switching, cache management, and high bit depth image processing, to improve color accuracy, processing speed, and memory efficiency. Using real photos and the usage scenarios of different devices, improve how large images, local adjustments, and group processing perform.

### Application Architecture and Quality

Keep improving the desktop experience on the PySide6 interface already adopted, move the architecture toward more modular designs, and strengthen quality checks, crash recovery, and project compatibility across versions. Improvements in performance and resource management will cover the whole process of loading, editing, background tasks, saving, and export.

Image computation, flow execution, and project data together form the long-term foundation:

| Foundation | Goal |
| --- | --- |
| **Image computation** | Provide consistent image operations, color, and output for manual editing, AI tools, and workflows |
| **Flow execution** | Connect operation orchestration, task scheduling, cancellation, and result delivery, and support interactive and background processing |
| **Project data** | Organize material, edit state, history, and resource references in one place, so that different tools can work together on the same work |

For the related technical directions, see [Architecture and Native Acceleration](architecture.en.md). For configuration and measured results, see [System Requirements and Performance](performance.en.md).

## Long-Term Direction and Getting Involved

OPS hopes to gradually build a unified foundation for image creation, in which manual editing, AI generation, and automated workflows work together, while users keep control over the creative process, their data, their models, and the final result.

Professional creators can build up their own styles and methods, everyday users can use the tools and AI to gradually form a personal expression, and developers can build new abilities around the open interfaces. Works, tools, and experience will connect with one another through continued use, and push the platform toward a wider range of image creation scenarios.

The project's original code currently uses the [MIT License](../LICENSE). Third-party components and models follow their own licenses, as described in [Licensing and Third-Party Components](licensing.en.md). To discuss specific use cases, tool needs, and suggestions for extensions, use [GitHub Issues](https://github.com/KpTanl/OpenPictureStudio/issues).

The stages advance according to how capabilities depend on each other, and some directions can run in parallel. The project will adjust priorities based on development progress, technical maturity, and community feedback. The features of each version are defined by the corresponding release notes.
