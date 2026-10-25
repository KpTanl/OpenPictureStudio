# Test Instructions

[简体中文](README.md) | [English](README.en.md)

Tests track the current product contracts and run offscreen. Real windows, high DPI, actual resource installation, and installers also require manual checks.

## Running

Run from the repository root in a virtual environment with the dependencies installed. Release builds use the versions in `requirements-build.lock.txt`.

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONUTF8 = '1'
python -m pytest tests -q -p no:cacheprovider -o faulthandler_timeout=300
```

`faulthandler_timeout` dumps the stacks of all threads when a run stalls for more than 300 seconds. If the Rust native module `ops_accel.pyd` is absent, tests requiring it are skipped with a reason; the remaining tests use the Python implementation.

## Full Model and RAW Regression

CI prepares test assets before running tests and verifies their SHA-256 hashes against the model catalog and pinned fixture manifest. Model weights, RAW files, and DCP profiles are cached in Git-ignored directories and are excluded from the application bundle.

```powershell
python tools/prepare_ci_assets.py
$fixtures = Get-Content .pytest-tmp/fixtures/environment.json -Raw | ConvertFrom-Json
$env:OPS_RAW_FIXTURE = $fixtures.OPS_RAW_FIXTURE
$env:OPS_CI_DCP_PROFILE = $fixtures.OPS_CI_DCP_PROFILE
$env:OPS_DCP_DUMP_DIR = $fixtures.OPS_DCP_DUMP_DIR
python -m pytest tests -q -p no:cacheprovider -o faulthandler_timeout=300
```

The models are NAFNet Deblur and U2Net from the [application model catalog](../resources/models/catalog.json). Pinned RAW and matching camera DCP sources and hashes are recorded in the [fixture manifest](fixtures/ci_assets.json). `OPS_CI_DCP_PROFILE` is enabled only within the DCP test, keeping the default RAW path independent.

NASA image ISS030-E-122639: Image courtesy of the Earth Science and Remote Sensing Unit, NASA Johnson Space Center. See the [NASA image collection](https://eol.jsc.nasa.gov/FAQ/) for usage information. The Nikon D3S DCP comes from RawTherapee; its pinned source and license are linked in the fixture manifest.

The Windows working-set test checks limits and restoration when registration succeeds. For privilege error 1314, it checks that registration is refused and the previous limits remain unchanged. Pixel-exact color acceptance follows the RawTherapee comparison steps documented by the RAW tests.

## Coverage Responsibilities

| Area | Main entry points | Coverage |
| --- | --- | --- |
| Base controls, themes, and localization | `test_apple_*`, `test_toggle_switch_apple.py`, `test_semantic_colors.py`, `test_ui_buttons.py`, `test_ui_literals.py`, `test_message_dialog.py`, `test_ui_empty_state.py`, `test_i18n_files.py` | User edits and silent programmatic updates; control states; theme colors; language refresh; matching translation keys and placeholders across languages; UI code free of hardcoded colors, font sizes, and corner radii; message and progress dialogs; empty states and guidance when no image is open |
| Main window and workspaces | `test_ui_top_bar.py`, `test_ui_canvas_toolbar.py`, `test_ui_filmstrip.py`, `test_ui_photo_info.py`, `test_ui_viewer_histogram.py`, `test_ui_tool_rail.py`, `test_ui_status_messages.py`, `test_ui_settings.py`, `test_ui_id_photo_tool.py`, `test_ui_canvas_tools.py`, `test_ui_window_chrome.py` | Shared actions, toolbars, filmstrip, photo information, viewer histogram, tool switching, banners, toasts, settings dialogs and shortcuts, ID photo tools, and canvas tool adjustments and results |
| Startup, image lists, and editing coordination | `test_startup.py`, `test_studio_workspace_and_dual_workspaces.py`, `test_thumbnail_panel.py`, `test_refactor_plan_tool_sidebar.py`, `test_reuse_refactor.py` | Deferred assembly, source identity, navigation, draft restoration, undo/redo, saving, and service reuse |
| Tool components and resource contracts | `test_ops_tool_panels.py`, `test_phase1_draft_and_adapter.py`, `test_ui_enhance_panel.py`, `test_ui_beauty_panel.py`, `test_ui_color_panel.py`, `test_ui_editor_panels.py`, `test_ui_manual_preview.py`, and model/tool/runtime tests | Registered panels, parameter versions, manual preview, restrictions when models are missing, actual rendering, and resource references |
| Projects, tasks, and save safety | `test_ops_review_regressions.py`, `test_phase0_core_api.py`, `test_transform_save_regressions.py`, and project transaction tests | Rejection of structurally damaged data, preservation of projects with unavailable capabilities, history branches, atomic publication, cancellation, and rejection of stale requests |
| Imaging and resource budgets | DCP, E1, white balance, mask, output format, image resource, and compute budget tests | Numerical contracts, encoding precision, read-only inputs, Alpha, and memory and thread admission |
| Mask rendering consistency | `test_mask_render_consistency.py` | Native and fallback paths, processing versions, single and multiple threads, enabled and disabled masks, sampling, Alpha, cancellation, and fallback on exceptions |
| Storage and working directories | `test_storage_workspaces.py` and storage tests | Working directory garbage collection only queues cleanup, without reentering the global thread-pool lock |
| Legacy imaging formats | `test_ops_identity_compatibility.py` | Continued support for legacy display modes, floating-point artifacts, look values, and color parameter contracts |
| Agents and plugins | Agent and plugin tests | Background service contracts |

`PANEL_TYPES` contains ten components; the toolbar's `TOOL_ENTRIES` contains eight editing tools. Standalone component tests for the old denoising panel cover compatibility; the product's denoising entry point is Image Enhancement. ID Photo is an entry in the left toolbar, and legacy version 1 nodes open with `LegacyIDPhotoPanel`.

## Setup and Resource Ownership

- The root `conftest.py` depends on the session-scoped `qapp`, keeping the Qt application alive until tasks and widgets have been released.
- Each test stores settings, drafts, ratings, caches, managed models, and runtimes under `tmp_path/app`. Subprocesses use their assigned temporary root and UTF-8. Isolation entry points protect real user settings and installation directories.
- `window_factory` creates main windows through the normal entry point and registers them for closure. `image_sources`, `tool_services`, `photo_service`, and `panel_factory` explicitly assemble the required services. Creators release their tasks, resources, and subscriptions.
- `tests/support.py` waits for actual readiness based on the current path, source identity, load generation, parameter state, and list version, and reports the current state on timeout. Tool switching uses commands belonging to the workspace; main-window assembly is triggered only when the workspace first opens.
- Model readiness and catalog discovery are separate states. Before producing real output, wait for the model service's serial discovery task to finish so that resource generations remain stable during verification. Widget capability tests can inject snapshots at the model query boundary; real output uses actual algorithms and weights.
- `available_neural_models` borrows the repository's `models/deblurring_nafnet_2025may.onnx` read-only. The model catalog validates its pinned identity and checksum. If the file is missing, the relevant real-model tests are explicitly skipped without triggering a download or installation.
- Three RAW comparison tests require `OPS_RAW_FIXTURE` to point to a RAW file. The DCP comparison additionally requires a camera DCP profile and RawTherapee. Tests skip with their respective reasons when these prerequisites are unavailable.
