"""Minimal, privacy-checked P2-A review archive with an internal hash manifest."""

import hashlib
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
target = root.parent / "RAILSTORM_P2_A_Chaos_Review.zip"
if target.exists():
    raise FileExistsError("Preserve existing archive before creating P2-A package")

files = []
for folder in (
    "railstorm",
    "migrations",
    "scenarios",
    "tests",
    "experiments",
    "scripts",
    "frontend/src",
    "frontend/dist",
):
    base = root / folder
    for path in base.rglob("*"):
        if path.is_file() and not {"__pycache__", "node_modules", ".pytest_cache", "work"}.intersection(
            path.parts
        ):
            if path.suffix in {".pyc", ".pyo", ".log"}:
                continue
            if folder == "experiments" and path.suffix != ".py":
                continue
            if folder == "tests" and path.suffix != ".py":
                continue
            files.append(path)
files += [
    root / name
    for name in (
        "pyproject.toml",
        "alembic.ini",
        "frontend/index.html",
        "Dockerfile",
        "docker-compose.yml",
        "README.md",
        "LICENSE",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "frontend/p2a.e2e.mjs",
        "docs/IMPLEMENTATION_STATUS.md",
    )
]
files += list((root / "docs").glob("P2_A_*.md"))
files += list((root / "docs").glob("P2_A_*.json"))
files += list((root / "docs/p2_a_evidence").glob("CH-*-seed20261030-n10.json"))
files += [root / "docs/p2_a_evidence/CH-01-seed20261030-n100.json"]
observed = json.loads((root / "docs/P2_A_browser_observed.json").read_text())
files += [root / "docs/p2_a_evidence" / f"browser-{row['runId']}.json" for row in observed["observed"]]
files += list((root / "docs/screenshots").glob("p2-a-*.png"))
files += [root / "docs/P2_A_pytest.xml"]

contents = {}
for path in sorted(set(files)):
    name = path.relative_to(root).as_posix()
    data = path.read_bytes()
    if name == "docs/P2_A_pytest.xml":
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    contents[name] = data
private_patterns = (b"C:" + b"\\Users\\", b"PRIVATE-HOSTNAME-PLACEHOLDER", b"BEGIN " + b"PRIVATE KEY")
for name, data in contents.items():
    if any(pattern in data for pattern in private_patterns):
        raise RuntimeError("PRIVATE_LOCAL_DATA: " + name)
manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for name, data in contents.items():
        archive.writestr("railstorm/" + name, data)
    archive.writestr("railstorm/SOURCE_MANIFEST_P2_A.json", json.dumps(manifest, indent=2).encode())
with ZipFile(target) as archive:
    assert archive.testzip() is None
print(
    json.dumps(
        {
            "archive": str(target),
            "files": len(manifest) + 1,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    )
)
