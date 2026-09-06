"""Create a deterministic source-inclusive Decky ZIP from an explicit allowlist."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package():
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    project = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not version == project["version"] == lock["version"] == lock["packages"][""]["version"]:
        raise ValueError("Version metadata is inconsistent.")
    if manifest["name"] != "SpotiDeck" or project["name"] != "spotideck" or lock["name"] != "spotideck" or lock["packages"][""]["name"] != "spotideck":
        raise ValueError("SpotiDeck name metadata is inconsistent.")
    required = ["main.py", "plugin.json", "package.json", "package-lock.json", "rollup.config.js",
                "tsconfig.json", "README.md", "SpotiDeck.png", "CHANGELOG.md", "LICENSE", "NOTICE", "dist/index.js", "dist/index.js.map", "scripts/package.py", "scripts/deploy_live.py", "scripts/deploy_migration.py"]
    files = [(ROOT / name, "SpotiDeck/" + name) for name in required]
    for directory in ("backend", "src"):
        files.extend((path, "SpotiDeck/" + path.relative_to(ROOT).as_posix()) for path in sorted((ROOT / directory).rglob("*"))
                     if path.is_file() and path.suffix in (".py", ".ts", ".tsx"))
    api = ROOT / "node_modules" / "@decky" / "api"
    metadata = json.loads((api / "package.json").read_text(encoding="utf-8"))
    if metadata["version"] != "1.1.3":
        raise ValueError("The Decky API version does not match NOTICE.")
    files.append((api / "LICENSE", "SpotiDeck/THIRD_PARTY_LICENSES/decky-api-LGPL-2.1.txt"))
    for dependency, license_file in (("qrcode", "license"), ("dijkstrajs", "LICENSE.md")):
        files.append((ROOT / "node_modules" / dependency / license_file,
                      "SpotiDeck/THIRD_PARTY_LICENSES/" + dependency + "-MIT.txt"))
    for path in sorted(api.rglob("*")):
        if path.is_file() and "node_modules" not in path.relative_to(api).parts:
            files.append((path, "SpotiDeck/THIRD_PARTY_SOURCES/decky-api-1.1.3/" + path.relative_to(api).as_posix()))
    for path, _ in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or unsafe package input: {path.name}")
    bundle = (ROOT / "dist/index.js").read_text(encoding="utf-8")
    if "Midnight City Lights" in bundle or "previewClient" in bundle:
        raise ValueError("Preview fixtures leaked into the production build.")
    output = ROOT / "artifacts" / f"SpotiDeck-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, name in sorted(files, key=lambda entry: entry[1]):
            info = zipfile.ZipInfo(name, (2026, 9, 6, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity verification failed.")
        names = archive.namelist()
        if any("/preview/" in name or "account.json" in name or "__pycache__" in name for name in names):
            raise ValueError("Unexpected private or preview file in package.")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"Packaged {len(files)} files: {output}")
    print(f"SHA-256: {digest}")


if __name__ == "__main__":
    try:
        package()
    except (OSError, ValueError, KeyError) as error:
        print(f"Package failed: {error}", file=sys.stderr)
        sys.exit(1)
