"""Inspect local app build artifacts; this is not provider qualification."""

import argparse
import hashlib
import json
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

APP_PATH = "services/cell-runtime/integrations/zapier/private-app"
ROOT_FILES = {"adapter.js", "definition.json", "index.js", "package.json", "zapierwrapper.js"}


def members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        result = {}
        for info in archive.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if (
                not parts
                or name.startswith("/")
                or ".." in parts
                or "\\" in name
                or name != str(PurePosixPath(name))
                or info.is_dir()
                or stat.S_ISLNK(info.external_attr >> 16)
                or name in result
            ):
                raise ValueError("unsafe or duplicate archive member")
            result[name] = archive.read(info)
        return result


def check_runtime(files: dict[str, bytes], packages: dict[str, dict[str, object]]) -> list[str]:
    observed = set()
    for name in files:
        if name in ROOT_FILES:
            continue
        parts = name.split("/")
        if parts[0] != "node_modules":
            raise ValueError("unexpected app runtime member")
        boundaries = [index for index, part in enumerate(parts) if part == "node_modules"]
        index = boundaries[-1]
        if len(parts) <= index + 2:
            raise ValueError("unmapped runtime package")
        width = 3 if parts[index + 1].startswith("@") else 2
        if len(parts) <= index + width:
            raise ValueError("unmapped runtime package")
        package = "/".join(parts[: index + width])
        record = packages.get(package)
        if not record or record.get("dev") or record.get("link"):
            raise ValueError("developer-only or unmapped runtime package")
        observed.add(package)
    if not ROOT_FILES.issubset(files) or not observed:
        raise ValueError("incomplete app runtime archive")
    return sorted(observed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-directory", type=Path, required=True)
    args = parser.parse_args()

    def git(*arguments: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(args.repository), *arguments])

    commit = git("rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    prefix = f"{commit}:{APP_PATH}"
    source = members(args.build_directory / "source.zip")
    tracked = git("ls-tree", "-rz", prefix).split(b"\0")
    expected = {}
    for entry in filter(None, tracked):
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("source contains a link or unsupported entry")
        expected[name.decode()] = git("cat-file", "blob", object_id.decode())
    if not expected or source != expected:
        raise ValueError("source archive differs from the exact committed app")
    runtime = members(args.build_directory / "build.zip")
    packages = json.loads(expected["package-lock.json"])["packages"]
    observed = check_runtime(runtime, packages)
    for app_name in ("adapter.js", "index.js", "package.json"):
        if runtime[app_name] != expected[app_name]:
            raise ValueError("runtime app differs from source archive")
    receipt = {
        "source_head": commit,
        "source_members": len(source),
        "runtime_members": len(runtime),
        "runtime_package_directories": len(observed),
        "developer_package_files": 0,
        "unmapped_package_files": 0,
        "archives": {
            name: hashlib.sha256((args.build_directory / name).read_bytes()).hexdigest()
            for name in ("build.zip", "source.zip")
        },
        "runtime_member_digest": hashlib.sha256(
            json.dumps(
                {name: hashlib.sha256(data).hexdigest() for name, data in sorted(runtime.items())},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
