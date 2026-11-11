import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            value.update(chunk)
    return value.hexdigest()


def run(command, cwd, log, *, environment=None):
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", **(environment or {})})
    if completed.returncode:
        raise RuntimeError(f"构建失败，日志：{log}")


def main():
    parser = argparse.ArgumentParser(description="从固定的工作区快照构建独立候选安装包")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native-sources", type=Path, required=True)
    parser.add_argument("--native-artifacts", type=Path, required=True)
    args = parser.parse_args()
    native_sources = json.loads(args.native_sources.read_text(encoding="utf-8"))
    native_artifacts = json.loads(args.native_artifacts.read_text(encoding="utf-8"))
    if any(digest(ROOT/item["path"]) != item["sha256"] for item in native_sources):
        raise ValueError("原生源码与 release 构建记录不一致")
    native_hash = digest(ROOT/"ops_accel.pyd")
    if not any(Path(item["path"]).resolve() == (ROOT/"ops_accel.pyd").resolve()
               and item["sha256"] == native_hash for item in native_artifacts):
        raise ValueError("原生模块与 release 构建记录不一致")
    version = re.search(r'APP_VERSION\s*=\s*"([\d.]+)"', (ROOT/"src/utils/app_info.py").read_text(encoding="utf-8"))[1]
    output = (args.output or ROOT/"dist"/f"candidate-{version}").resolve()
    if (not any(output.is_relative_to(ROOT/name) for name in ("dist", "scratch"))
            or output.exists() and any(output.iterdir())):
        raise ValueError("候选目录必须是工作区 dist 或 scratch 下的独立空目录")
    for parent in (output, *output.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("候选目录不允许重解析点")
    output.mkdir(parents=True, exist_ok=True)
    source = output/"source"
    source.mkdir()
    shutil.copyfile(args.native_sources, output/"native-sources.json")
    shutil.copyfile(args.native_artifacts, output/"native-artifacts.json")
    base = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=ROOT, text=True).strip()
    names = subprocess.check_output(("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"), cwd=ROOT).decode("utf-8").split("\0")
    records = []
    for name in sorted(set(filter(None, names))):
        path = ROOT/name
        if not path.is_file():
            continue
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"源码包含重解析点：{name}")
        target = source/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        records.append({"path": name, "sha256": digest(target), "bytes": target.stat().st_size})
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    module = ROOT/"ops_accel.pyd"
    shutil.copyfile(module, source/"ops_accel.pyd")
    native = digest(module)
    report = {"version": version, "base_commit": base, "source_snapshot": fingerprint,
        "native_sha256": native, "files": records, "manual_acceptance": "待手动验收",
        "dependencies": sorted(({"name": dist.metadata["Name"], "version": dist.version}
            for dist in metadata.distributions()), key=lambda item: item["name"].lower())}
    archive = output/f"OpenPictureStudio-{version}-source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for record in records:
            entry = zipfile.ZipInfo(record["path"], date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(entry, (source/record["path"]).read_bytes())
    (output/"source-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"源码快照已固定：{fingerprint}", flush=True)
    run((sys.executable, "-B", "-m", "PyInstaller", "--noconfirm", "--clean", "OpenPictureStudio.spec"),
        source, output/"pyinstaller.log", environment={
            "PYINSTALLER_CONFIG_DIR": str(output/"pyinstaller-cache"),
            "PYTHONUSERBASE": str(output/"python-user"),
            "NUMBA_DISABLE_JIT": "1",
        })
    program = source/"dist/OpenPictureStudio"
    if digest(program/"_internal/ops_accel.pyd") != native:
        raise ValueError("候选原生模块摘要不一致")
    print("程序目录已构建，整理许可与源码。", flush=True)
    run((sys.executable, "-B", "tools/collect_licenses.py", str(program), f"--version={version}", f"--source-sha={base}"),
        source, output/"licenses.log")
    shipped_source = program/"source"/archive.name
    shipped_source.parent.mkdir()
    shutil.copyfile(archive, shipped_source)
    archive_hash = digest(archive)
    source_notice = program/"SOURCE.txt"
    text = source_notice.read_text(encoding="utf-8")
    text = text.replace(f"  Commit:     {base}", f"  Base commit: {base}\n  Snapshot:    {fingerprint}")
    text = text.replace(f"  Archive:    https://github.com/KpTanl/OpenPictureStudio/archive/{base}.zip",
        f"  Archive:    source/{archive.name}\n  SHA-256:    {archive_hash}")
    source_notice.write_text(text, encoding="utf-8")
    components_path = program/"components.json"
    components = json.loads(components_path.read_text(encoding="utf-8"))
    application = components["application"]
    application.pop("commit", None)
    application.update(base_commit=base, source_snapshot=fingerprint, source_archive=archive.name, candidate=True)
    project = next(item for item in components["components"] if item["name"] == "OpenPictureStudio")
    project["files"].append({"path": "source/"+archive.name, "sha256": archive_hash})
    components_path.write_text(json.dumps(components, ensure_ascii=False, indent=1)+"\n", encoding="utf-8")
    run((sys.executable, "-B", "tools/install_manifest.py", str(program), f"--version={version}", f"--source-sha={base}"),
        source, output/"install-manifest.log")
    iscc = next((path for path in (Path(os.environ["LOCALAPPDATA"])/"Programs/Inno Setup 6/ISCC.exe",
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"), Path("C:/Program Files/Inno Setup 6/ISCC.exe")) if path.is_file()), None)
    if iscc is None:
        raise FileNotFoundError("Inno Setup 编译器不存在")
    print("编译候选安装包。", flush=True)
    run((str(iscc), f"/DAppVersion={version}", f"/DRepoRoot={source}", "/DReviewCandidate=1",
        str(source/"packaging/OpenPictureStudio.iss")), source, output/"inno.log")
    installer = source/f"dist/installer/OpenPictureStudio-{version}-candidate-setup.exe"
    installer_hash = digest(installer)
    delivered = output/installer.name
    shutil.copyfile(installer, delivered)
    (output/(installer.name+".sha256")).write_text(installer_hash+"  "+installer.name+"\n", encoding="utf-8")
    if any(digest(source/record["path"]) != record["sha256"] for record in records):
        raise ValueError("固定源码在构建期间改变")
    report.update(installer=str(delivered), installer_sha256=installer_hash, source_archive_sha256=archive_hash,
        program=str(program), program_bytes=sum(path.stat().st_size for path in program.rglob("*") if path.is_file()))
    (output/"artifacts.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("installer", "installer_sha256", "source_snapshot", "program_bytes")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
