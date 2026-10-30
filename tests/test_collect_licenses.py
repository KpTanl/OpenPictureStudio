import importlib.metadata as metadata
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "0" * 40


def _load():
    spec = importlib.util.spec_from_file_location("collect_licenses", ROOT / "tools" / "collect_licenses.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collect_licenses = _load()


def _bundle(tmp_path, extra_datas=()):
    """A minimal program folder and bundle list made of real files from this environment."""
    qtcore = Path(metadata.distribution("PySide6_Essentials").locate_file("PySide6/QtCore.pyd"))
    icons = ROOT / "resources" / "icons" / "ui" / "LICENSE.txt"
    datas = [("resources/icons/ui/LICENSE.txt", str(icons), "DATA"), *extra_datas]
    toc = {
        "pure": [["src.utils.app_info", str(ROOT / "src" / "utils" / "app_info.py"), "PYMODULE"],
                 ["json", str(Path(sys.base_prefix) / "Lib" / "json" / "__init__.py"), "PYMODULE"]],
        "scripts": [],
        "binaries": [["PySide6/QtCore.pyd", str(qtcore), "EXTENSION"]],
        "datas": [list(entry) for entry in datas],
    }
    program = tmp_path / "program"
    for dest in ["OpenPictureStudio.exe"] + [f"_internal/{row[0]}" for row in toc["binaries"] + toc["datas"]]:
        (program / dest).parent.mkdir(parents=True, exist_ok=True)
        (program / dest).write_bytes(b"x")
    toc_path = tmp_path / "bundle_toc.json"
    toc_path.write_text(json.dumps(toc), encoding="utf-8")
    return program, toc_path


def _collector(program, toc_path):
    collector = collect_licenses.Collector(program, toc_path, "0.0.1", SHA)
    for name, entry in collector.manifest["sources"].items():
        try:
            entry["version"] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    collector.attribute()
    collector.rust_crates()
    collector.check()
    return collector


def test_writes_notices_source_and_components(tmp_path):
    program, toc = _bundle(tmp_path)
    collector = _collector(program, toc)
    assert collector.problems == []
    collector.write()

    notices = (program / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    assert "PySide6_Essentials | " in notices and "CPython | " in notices and "Tabler Icons | " in notices
    source = (program / "SOURCE.txt").read_text(encoding="utf-8")
    assert SHA in source and "pyside-setup-everywhere-src-6.11.3" in source.lower()
    assert "GNU GENERAL PUBLIC LICENSE" in (program / "GPL-3.0.txt").read_text(encoding="utf-8")[:400].upper()
    components = {c["name"]: c for c in json.loads((program / "components.json").read_text(encoding="utf-8"))["components"]}
    assert components["PySide6_Essentials"]["files"][0]["path"] == "_internal/PySide6/QtCore.pyd"
    assert components["OpenPictureStudio"]["python_modules"] == 1
    written = (program / "components.json").read_text(encoding="utf-8")
    for local in (ROOT, Path(sys.base_prefix), tmp_path):
        assert json.dumps(str(local))[1:-1] not in written
    for component in components.values():
        assert any((program / component["license_texts"]).iterdir()), component["name"]


def test_unlisted_file_in_program_folder_stops_the_build(tmp_path):
    program, toc = _bundle(tmp_path)
    (program / "_internal" / "stray.dll").write_bytes(b"x")
    assert "delivered file not in the bundle list: _internal/stray.dll" in _collector(program, toc).problems


def test_file_without_owner_stops_the_build(tmp_path):
    stray = tmp_path / "elsewhere" / "data.bin"
    stray.parent.mkdir()
    stray.write_bytes(b"x")
    program, toc = _bundle(tmp_path, [("data.bin", str(stray), "DATA")])
    assert "no owner for datas entry data.bin" in _collector(program, toc).problems


def test_modified_supplement_stops_the_build(tmp_path, monkeypatch):
    program, toc = _bundle(tmp_path)
    original = collect_licenses.Collector.__init__

    def tampered(self, *args):
        original(self, *args)
        self.manifest["supplements"]["msvc-runtime"]["files"][0]["sha256"] = "0" * 64

    monkeypatch.setattr(collect_licenses.Collector, "__init__", tampered)
    runtime = Path(sys.base_prefix) / "vcruntime140.dll"
    if not runtime.is_file():
        pytest.skip("no Microsoft runtime next to the Python runtime")
    program, toc = _bundle(tmp_path / "with_runtime", [("vcruntime140.dll", str(runtime), "DATA")])
    problems = _collector(program, toc).problems
    assert any("supplement msvc-runtime/NOTICE.txt is missing or modified" in p for p in problems)


def test_supplement_files_match_the_manifest():
    manifest = json.loads((collect_licenses.SUPPLEMENTS / "manifest.json").read_text(encoding="utf-8"))
    for key, supplement in manifest["supplements"].items():
        for item in supplement.get("files", []):
            path = collect_licenses.SUPPLEMENTS / item["path"]
            assert collect_licenses._text_sha256(path) == item["sha256"], (key, item["path"])


@pytest.mark.parametrize("name,expected", [
    ("vcruntime140.dll", True), ("vcruntime140_1.dll", True), ("msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll", True),
    ("api-ms-win-crt-convert-l1-1-0.dll", True), ("vcomp140.dll", True), ("Qt6Core.dll", False),
])
def test_microsoft_runtime_names(name, expected):
    assert bool(collect_licenses.MS_RUNTIME.match(name)) is expected


def test_copyleft_component_without_source_entry_stops_the_build(tmp_path):
    program, toc = _bundle(tmp_path)
    collector = collect_licenses.Collector(program, toc, "0.0.1", SHA)
    del collector.manifest["sources"]["PySide6_Essentials"]
    collector.attribute()
    collector.check()
    assert any(p.startswith("PySide6_Essentials ") and "no corresponding-source entry" in p for p in collector.problems)


def test_shiboken_license_and_replaceable_sources_are_included(tmp_path):
    shiboken = metadata.distribution("shiboken6")
    native = Path(shiboken.locate_file("shiboken6/Shiboken.pyd"))
    program, toc = _bundle(tmp_path, [("shiboken6/Shiboken.pyd", str(native), "DATA")])
    collector = _collector(program, toc)
    assert collector.problems == []
    collector.write()
    components = {c["name"]: c for c in json.loads((program / "components.json").read_text(encoding="utf-8"))["components"]}
    assert components["shiboken6"]["files"][0]["path"] == "_internal/shiboken6/Shiboken.pyd"
    licenses = program / components["shiboken6"]["license_texts"]
    assert (licenses / "LGPL-3.0.txt").is_file()
    assert (licenses / "PYTHON-3.7-LICENSE.txt").is_file()
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (program / "LGPL-3.0.txt").read_text(encoding="utf-8")[:400].upper()
    source = (program / "SOURCE.txt").read_text(encoding="utf-8")
    assert "shiboken6 6.11.3" in source and "qt-everywhere-src-6.11.3" in source
    assert "Compatible replacement builds" in source
