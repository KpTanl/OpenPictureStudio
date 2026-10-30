"""Collect third-party license material for the packaged program folder.

Runs after PyInstaller and before the installation manifest. Every delivered file
is attributed to a component using build/bundle_toc.json (written by the spec)
and the build environment's package records. The script copies license texts to
``licenses/`` and writes LICENSE, GPL-3.0.txt, LGPL-3.0.txt, THIRD_PARTY_NOTICES.txt,
SOURCE.txt and components.json at the program root.

Any delivered file without an owner, any component without license text, and any
copyleft component without a corresponding-source entry stops the build.

Usage:
    python tools/collect_licenses.py <program-dir> --version=X.Y.Z --source-sha=<sha>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SUPPLEMENTS = ROOT / "packaging" / "licenses"
REPOSITORY = "https://github.com/KpTanl/OpenPictureStudio"
CONTENTS = "_internal"
OUTPUTS = ("LICENSE", "GPL-3.0.txt", "LGPL-3.0.txt", "THIRD_PARTY_NOTICES.txt", "SOURCE.txt", "components.json", "licenses")
NOTICE_NAME = re.compile(r"^(licen[cs]e|copying|notice|authors|copyright|thirdpartynotices|third_party_notices|patents)",
                         re.I)
COPYLEFT = re.compile(r"\b(A?L?GPL|GNU|MPL|Mozilla|CDDL|EUPL)", re.I)
MS_RUNTIME = re.compile(r"^(vcruntime\d+(_\d+)?|msvcp\d+(_\w+)?|concrt\d+|vcomp\d+|ucrtbase|api-ms-win-[\w.-]+)"
                        r"(-[0-9a-f]{8,})?\.dll$", re.I)
HOMEPAGE_LABELS = ("homepage", "home", "source", "source code", "repository")
CODE_SUFFIXES = {".py", ".pyc", ".pyi", ".pyd", ".dll", ".so", ".exe"}


@dataclass
class Component:
    key: str
    name: str
    version: str
    license: str
    homepage: str
    origin: str
    notices: dict[str, Path] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    modules: int = 0
    note: str = ""

    @property
    def folder(self) -> str:
        return re.sub(r"[^\w.+-]", "_", f"{self.name}-{self.version}")


def _key(path) -> str:
    """Comparable form of a path; short 8.3 names, subst drives and junctions resolve to the same key."""
    return os.path.normcase(os.path.realpath(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _license_of(meta) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 60:
        return text
    names = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License :: ")]
    return " / ".join(names) or "see license files"


def _homepage(meta, name, version) -> str:
    urls = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls[label.strip().lower()] = url.strip()
    for label in HOMEPAGE_LABELS:
        if urls.get(label):
            return urls[label]
    return meta.get("Home-page") or f"https://pypi.org/project/{name}/{version}/"


def _dist_notices(dist) -> dict[str, Path]:
    """License texts of a distribution: its .dist-info license files and notice files in its packages."""
    info = next((PurePosixPath(str(f)).parts[0] for f in dist.files or [] if str(f).endswith(".dist-info/METADATA")), "")
    declared = set(dist.metadata.get_all("License-File") or [])
    found = {}
    for entry in dist.files or []:
        parts = PurePosixPath(str(entry)).parts
        if parts[0] == info:
            rest = parts[1:]
            if rest[:1] == ("licenses",):
                rest, take = rest[1:], True
            else:
                take = bool(NOTICE_NAME.match(parts[-1])) or "/".join(rest) in declared
        else:
            rest = parts
            take = bool(NOTICE_NAME.match(parts[-1])) and PurePosixPath(parts[-1]).suffix.lower() not in CODE_SUFFIXES
        source = Path(dist.locate_file(entry))
        if take and rest and source.is_file():
            found["/".join(rest)] = source
    return found


class Collector:
    def __init__(self, program: Path, toc: Path, version: str, source_sha: str):
        self.program = program
        self.toc = json.loads(toc.read_text(encoding="utf-8"))
        self.version = version
        self.source_sha = source_sha
        self.manifest = json.loads((SUPPLEMENTS / "manifest.json").read_text(encoding="utf-8"))
        self.problems: list[str] = []
        self.components: dict[str, Component] = {}
        self.owners: dict[str, str] = {}
        self.folders: dict[str, set[str]] = {}
        self.sites: set[str] = set()
        for dist in metadata.distributions():
            name = dist.metadata["Name"]
            site = _key(dist.locate_file(""))
            self.sites.add(site)
            for entry in dist.files or []:
                path = os.path.normcase(os.path.normpath(os.path.join(site, str(entry))))
                self.owners[path] = name
                self.folders.setdefault(os.path.dirname(path), set()).add(name)
        for site in self.sites:
            self.folders.pop(site, None)
        self.base = _key(sys.base_prefix)
        self.root = _key(ROOT)

    # -- components ---------------------------------------------------------------------------
    def _component(self, key: str) -> Component:
        if key in self.components:
            return self.components[key]
        if key.startswith("python:"):
            dist = metadata.distribution(key[7:])
            meta = dist.metadata
            source = self.manifest["sources"].get(meta["Name"], {})
            component = Component(key, meta["Name"], dist.version, source.get("license") or _license_of(meta),
                                  _homepage(meta, meta["Name"], dist.version), "python package",
                                  _dist_notices(dist))
        elif key == "project":
            component = Component(key, "OpenPictureStudio", self.version, "MIT", REPOSITORY, "this project",
                                  {"LICENSE": ROOT / "LICENSE"})
        elif key == "tabler-icons":
            component = Component(key, "Tabler Icons", "bundled", "MIT", "https://github.com/tabler/tabler-icons",
                                  "bundled resource", {"LICENSE.txt": ROOT / "resources" / "icons" / "ui" / "LICENSE.txt"})
        elif key == "cpython":
            component = Component(key, "CPython", platform.python_version(), "PSF-2.0", "https://www.python.org/",
                                  "Python runtime", {"LICENSE.txt": Path(sys.base_prefix) / "LICENSE.txt"})
        elif key == "msvc-runtime":
            component = Component(key, "Microsoft Visual C++ and Universal C Runtime", "bundled",
                                  "Microsoft redistributable", "https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files",
                                  "redistributable runtime")
        else:
            raise KeyError(key)
        self._supplement(component)
        self.components[key] = component
        return component

    def _supplement(self, component: Component):
        entry = self.manifest["supplements"].get(component.key)
        if entry is None:
            return
        if entry.get("version") not in (None, component.version):
            self.problems.append(f"{component.name}: supplement is for version {entry['version']}, "
                                 f"bundled {component.version}")
            return
        for item in entry.get("files", []):
            path = SUPPLEMENTS / item["path"]
            if not path.is_file() or _text_sha256(path) != item["sha256"]:
                self.problems.append(f"{component.name}: supplement {item['path']} is missing or modified")
                continue
            component.notices.setdefault(PurePosixPath(item["path"]).name, path)
        # Notices the package ships under other names, such as a README that carries copyright lines.
        for relative in entry.get("package_files", []):
            path = Path(metadata.distribution(component.name).locate_file(relative))
            if not path.is_file():
                self.problems.append(f"{component.name}: notice file {relative} not found in the package")
                continue
            component.notices.setdefault(relative, path)

    def _owner(self, source: str) -> str | None:
        path = _key(source)
        if MS_RUNTIME.match(os.path.basename(path)):
            return "msvc-runtime"
        name = self.owners.get(path)
        if name is not None:
            return f"python:{name}"
        site = next((s for s in self.sites if path.startswith(s + os.sep)), None)
        if site is not None:
            # Files generated inside a package directory (such as numba caches) belong to that package.
            folder = os.path.dirname(path)
            while folder != site:
                names = self.folders.get(folder)
                if names:
                    return f"python:{next(iter(names))}" if len(names) == 1 else None
                folder = os.path.dirname(folder)
            return None
        if path.startswith(self.root + os.sep):
            relative = Path(path).relative_to(self.root).as_posix()
            if relative == "ops_accel.pyd":
                return "project"
            if relative.startswith("resources/icons/ui/"):
                return "tabler-icons"
            if relative.split("/")[0] in ("src", "resources", "main.py"):
                return "project"
            return None
        if path.startswith(self.base + os.sep):
            return "cpython"
        return None

    # -- attribution --------------------------------------------------------------------------
    def attribute(self):
        delivered = set()
        for kind in ("pure", "scripts", "binaries", "datas"):
            for dest, source, typecode in self.toc[kind]:
                if not source or source == "-" or not os.path.isfile(source):
                    continue
                owner = "cpython" if dest == "base_library.zip" else self._owner(source)
                if owner is None:
                    self.problems.append(f"no owner for {kind} entry {dest}")
                    continue
                component = self._component(owner)
                if kind in ("pure", "scripts"):
                    component.modules += 1
                else:
                    relative = PurePosixPath(CONTENTS, PurePosixPath(Path(dest).as_posix())).as_posix()
                    component.files.add(relative)
                    delivered.add(relative)
        bootloader = self._component("python:pyinstaller")
        bootloader.note = "bootloader of OpenPictureStudio.exe"
        bootloader.files.add("OpenPictureStudio.exe")
        delivered.add("OpenPictureStudio.exe")
        for path in sorted(p for p in self.program.rglob("*") if p.is_file()):
            relative = path.relative_to(self.program).as_posix()
            if relative.split("/")[0] in OUTPUTS:
                continue
            if relative not in delivered:
                self.problems.append(f"delivered file not in the bundle list: {relative}")
        for relative in sorted(delivered):
            if not (self.program / relative).is_file():
                self.problems.append(f"bundle list entry missing from the program folder: {relative}")

    def rust_crates(self):
        project = self._component("project")
        manifest = ROOT / "native" / "ops_accel" / "Cargo.toml"
        if f"{CONTENTS}/ops_accel.pyd" not in project.files:
            return
        result = subprocess.run(["cargo", "metadata", "--locked", "--offline", "--format-version", "1",
                                 "--manifest-path", str(manifest), "--filter-platform", "x86_64-pc-windows-msvc"],
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            self.problems.append(f"cargo metadata failed: {result.stderr.strip()}")
            return
        data = json.loads(result.stdout)
        packages = {p["id"]: p for p in data["packages"]}
        nodes = {n["id"]: n for n in data["resolve"]["nodes"]}
        root = data["resolve"]["root"]
        linked, build = {root}, set()
        stack = [(root, True)]
        while stack:
            current, runtime = stack.pop()
            for dep in nodes[current]["deps"]:
                kinds = {k["kind"] or "normal" for k in dep["dep_kinds"]} - {"dev"}
                if not kinds:
                    continue
                proc_macro = any("proc-macro" in t["kind"] for t in packages[dep["pkg"]]["targets"])
                child_runtime = runtime and "normal" in kinds and not proc_macro
                target = linked if child_runtime else build
                if dep["pkg"] not in target:
                    target.add(dep["pkg"])
                    stack.append((dep["pkg"], child_runtime))
        for pid in sorted((linked | build) - {root}, key=lambda i: packages[i]["name"]):
            crate = packages[pid]
            directory = Path(crate["manifest_path"]).parent
            notices = {f.name: f for f in directory.iterdir() if f.is_file() and NOTICE_NAME.match(f.name)}
            component = Component(f"crate:{crate['name']}@{crate['version']}", crate["name"], crate["version"],
                                  crate.get("license") or "see license files",
                                  crate.get("repository") or f"https://crates.io/crates/{crate['name']}",
                                  "Rust crate", notices,
                                  note=("compiled into _internal/ops_accel.pyd" if pid in linked
                                        else "build-time only (procedural macro or build script)"))
            self._supplement(component)
            self.components[component.key] = component

    # -- checks -------------------------------------------------------------------------------
    def check(self):
        sources = self.manifest["sources"]
        for component in self.components.values():
            if not component.notices:
                self.problems.append(f"{component.name} {component.version}: no license text "
                                     f"(add it under packaging/licenses and to manifest.json)")
            if component.key.startswith("python:"):
                entry = sources.get(component.name)
                if entry is None and COPYLEFT.search(component.license):
                    self.problems.append(f"{component.name} {component.version} ({component.license}): "
                                         "no corresponding-source entry in packaging/licenses/manifest.json")
                elif entry is not None and entry["version"] != component.version:
                    self.problems.append(f"{component.name}: source entry is for {entry['version']}, "
                                         f"bundled {component.version}")
    # -- output -------------------------------------------------------------------------------
    def write(self):
        for name in OUTPUTS:
            target = self.program / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        ordered = sorted(self.components.values(), key=lambda c: (c.key != "project", c.name.lower()))
        licenses = self.program / "licenses"
        for component in ordered:
            for relative, source in sorted(component.notices.items()):
                target = licenses / component.folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        shutil.copyfile(ROOT / "LICENSE", self.program / "LICENSE")
        for name in ("GPL-3.0.txt", "LGPL-3.0.txt"):
            shutil.copyfile(SUPPLEMENTS / "pyside" / name, self.program / name)
        self._write_notices(ordered)
        self._write_source()
        self._write_components(ordered)

    def _write_notices(self, ordered):
        lines = [
            f"OpenPictureStudio {self.version} - third-party components",
            "",
            "The original code of OpenPictureStudio is licensed under the MIT License (LICENSE).",
            "PySide6, Shiboken and Qt are distributed under their applicable open-source licenses.",
            "SOURCE.txt describes corresponding source and how to replace the libraries. The license",
            "texts of each component are in the licenses folder.",
            "",
            "OpenPictureStudio 原创代码采用 MIT 许可（LICENSE）。PySide6、Shiboken 和 Qt",
            "按各自适用的开源许可证分发；源码和替换说明见 SOURCE.txt，",
            "各组件的许可文本在 licenses 文件夹。",
            "",
            "Name | Version | License | Homepage | License texts",
            "",
        ]
        for c in ordered:
            note = f" ({c.note})" if c.note else ""
            lines.append(f"{c.name} | {c.version} | {c.license} | {c.homepage} | licenses/{c.folder}/{note}")
        models = self._catalog_entries()
        if models:
            lines += ["", "Models and runtimes downloaded on demand (not included in this program;",
                      "license texts of the models are in _internal/resources/models/licenses):", ""]
            lines += [f"{m['name']} | {m['version']} | {m['license']} | {m['source']}" for m in models]
        (self.program / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _catalog_entries(self):
        entries = []
        for catalog, key in (("models", "models"), ("runtimes", "runtimes")):
            path = ROOT / "resources" / catalog / "catalog.json"
            for item in json.loads(path.read_text(encoding="utf-8")).get(key, []):
                entries.append({"name": item["name"], "version": item.get("revision") or item.get("version", ""),
                                "license": item.get("license", ""), "source": item.get("source", "")})
        return entries

    def _tool_versions(self):
        versions = {"Python": platform.python_version(), "PyInstaller": metadata.version("pyinstaller")}
        for tool in ("rustc", "cargo"):
            try:
                versions[tool] = subprocess.run([tool, "-V"], capture_output=True, text=True,
                                                check=True).stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                versions[tool] = "unavailable"
        return versions

    def _write_source(self):
        archive = f"{REPOSITORY}/archive/{self.source_sha}.zip"
        lines = [
            f"Corresponding source for OpenPictureStudio {self.version}",
            "",
            "Application",
            f"  Repository: {REPOSITORY}",
            f"  Commit:     {self.source_sha}",
            f"  Archive:    {archive}",
            "  Build:      README.md (building the installer), requirements-build.lock.txt, build.ps1,",
            "              OpenPictureStudio.spec, packaging/OpenPictureStudio.iss and native/ops_accel.",
            "  License:    original code MIT (LICENSE); third-party licenses are listed separately.",
            "",
            "Build tools",
        ]
        lines += [f"  {name}: {value}" for name, value in self._tool_versions().items()]
        lines += [
            "",
            "Components under the GPL, LGPL or MPL. Each is included unmodified, as released by",
            "its upstream project; the source below is the release this program was built with.",
            "",
        ]
        bundled = {c.name for c in self.components.values()}
        for name, entry in sorted(self.manifest["sources"].items(), key=lambda item: item[0].lower()):
            if name not in bundled:
                continue
            lines.append(f"{name} {entry['version']} ({entry['license']})")
            if entry.get("note"):
                lines.append(f"  {entry['note']}")
            for item in entry["files"]:
                checks = ", ".join(f"{algorithm.upper()} {item[algorithm]}" for algorithm in ("sha256", "md5", "commit")
                                   if item.get(algorithm))
                lines.append(f"  {item['url']}")
                if checks:
                    lines.append(f"    {checks}")
            lines.append("")
        lines += [
            "PySide6 and shiboken6 Python modules, extension modules, and Qt shared libraries are",
            "separate files in _internal. Compatible replacement builds may be installed in the same",
            "locations. Use matching Python, PySide6, Shiboken and Qt versions and architectures.",
            "The application can be run with those replacement files in the program directory.",
            "",
            "The source is offered from the network locations above.",
            f"If a location is no longer available, please report it at {REPOSITORY}/issues.",
        ]
        (self.program / "SOURCE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_components(self, ordered):
        data = {
            "application": {"name": "OpenPictureStudio", "version": self.version, "commit": self.source_sha,
                            "repository": REPOSITORY},
            "components": [],
        }
        for c in ordered:
            data["components"].append({
                "name": c.name, "version": c.version, "license": c.license, "origin": c.origin,
                "homepage": c.homepage, "note": c.note, "license_texts": f"licenses/{c.folder}",
                "python_modules": c.modules,
                "files": [{"path": path, "sha256": _sha256(self.program / path)} for path in sorted(c.files)],
            })
        (self.program / "components.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                                       encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("program_dir", type=Path)
    parser.add_argument("--toc", type=Path, default=ROOT / "build" / "bundle_toc.json")
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        parser.error("--source-sha must be a full commit SHA")
    collector = Collector(args.program_dir.resolve(), args.toc, args.version, args.source_sha)
    collector.attribute()
    collector.rust_crates()
    collector.check()
    if collector.problems:
        for problem in collector.problems:
            print(f"license collection: {problem}", file=sys.stderr)
        print(f"license collection failed with {len(collector.problems)} problem(s)", file=sys.stderr)
        return 1
    collector.write()
    print(f"license collection: {len(collector.components)} components written to {args.program_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
