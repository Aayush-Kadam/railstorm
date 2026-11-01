"""Package reviewable source/evidence only; exclude environments and private config."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--name", default="RAILSTORM_P0_1_Hardened.zip")
args = parser.parse_args()
if Path(args.name).name != args.name or not args.name.endswith(".zip"):
    raise ValueError("Archive name must be a simple .zip filename")
target = root.parent / args.name
if target.exists():
    raise FileExistsError("Preserve existing evidence; choose a new archive name")
excluded = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    "work",
}
files = []
for path in sorted(root.rglob("*")):
    if not path.is_file():
        continue
    relative = path.relative_to(root)
    if excluded.intersection(relative.parts) or path.name in {".env", "SOURCE_MANIFEST.json"}:
        continue
    if path.suffix in {".pyc", ".pyo", ".log"} or path.name.endswith("-progress.json"):
        continue
    files.append(path)
manifest = {
    str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in files
}
manifest_path = root / "SOURCE_MANIFEST.json"
manifest_path.write_text(json.dumps(manifest, indent=2))
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for path in [*files, manifest_path]:
        archive.write(path, "railstorm/" + str(path.relative_to(root)).replace("\\", "/"))
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert not any(excluded.intersection(Path(name).parts) for name in archive.namelist())
print(
    json.dumps(
        {
            "archive": target.name,
            "files": len(files) + 1,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    )
)
