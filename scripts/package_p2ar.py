"""Create a compact, redacted P2-A-R review archive and verify its manifest."""

import hashlib
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
target = root.parent / "RAILSTORM_P2_A_R_Review.zip"
if target.exists():
    raise FileExistsError(target)

files = []
for folder in ("railstorm", "migrations", "scenarios", "tests", "experiments", "scripts", "frontend/src"):
    base = root / folder
    for path in base.rglob("*"):
        if not path.is_file() or {"__pycache__", "node_modules", ".pytest_cache", "work", "reports"} & set(
            path.parts
        ):
            continue
        if path.suffix in {".pyc", ".pyo", ".log"}:
            continue
        if folder in {"experiments", "tests"} and path.suffix != ".py":
            continue
        files.append(path)
files += [
    root / name
    for name in (
        "pyproject.toml",
        "alembic.ini",
        "Dockerfile",
        "docker-compose.yml",
        "README.md",
        "LICENSE",
        "frontend/index.html",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "docs/IMPLEMENTATION_STATUS.md",
        "docs/P2_A_FINAL_GO_NO_GO.md",
        "docs/P2_A_SCENARIO_REGISTRY.md",
        "docs/P2_A_pytest.xml",
    )
]
files += list((root / "docs").glob("P2_A_R_*.md"))
files += list((root / "docs").glob("P2_A_R_*.json"))
files += [root / "docs/P2_A_R_pytest.xml"]
contents = {}
for path in sorted(set(files)):
    name = path.relative_to(root).as_posix()
    data = path.read_bytes()
    if name.endswith("pytest.xml"):
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    if any(
        pattern in data
        for pattern in (b"C:" + b"\\Users\\", b"PRIVATE-HOSTNAME-PLACEHOLDER", b"BEGIN " + b"PRIVATE KEY")
    ):
        raise RuntimeError(f"PRIVATE_LOCAL_DATA: {name}")
    contents[name] = data
manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for name, data in contents.items():
        archive.writestr("railstorm/" + name, data)
    archive.writestr("railstorm/SOURCE_MANIFEST_P2_A_R.json", json.dumps(manifest, indent=2).encode())
with ZipFile(target) as archive:
    assert archive.testzip() is None
    archived_manifest = json.loads(archive.read("railstorm/SOURCE_MANIFEST_P2_A_R.json"))
    assert archived_manifest == manifest
    for name, expected in manifest.items():
        assert hashlib.sha256(archive.read("railstorm/" + name)).hexdigest() == expected
    required = {
        f"railstorm/docs/P2_A_R_{name}.md"
        for name in (
            "BASELINE_AND_SCOPE",
            "CONCURRENCY_VERIFICATION",
            "CRASH_RECOVERY",
            "MESSAGE_ORDERING",
            "PERFORMANCE_PROFILE",
            "FINANCIAL_SAFETY",
            "TEST_REPORT",
            "FINAL_GO_NO_GO",
        )
    }
    assert required <= set(archive.namelist())
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
