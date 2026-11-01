"""Create a new checksum-verified R6 source/evidence review, preserving every prior ZIP."""

import hashlib
import json
import os
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"
REPORTS = (
    "ARCHITECTURAL_BOTTLENECK",
    "IMPLEMENTATION",
    "DIFFERENTIAL_CORRECTNESS",
    "MATCHED_BENCHMARKS",
    "10000_TARGET",
    "RESOURCE_REPORT",
    "FINAL_GO_NO_GO",
)


def clean(data: bytes, suffix: str):
    if suffix not in {".md", ".json", ".xml", ".log", ".txt"}:
        return data
    value = data.decode("utf-8")
    value = re.sub(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"]+", "<USER_HOME>", value, flags=re.IGNORECASE)
    host = os.environ.get("COMPUTERNAME")
    if host:
        value = value.replace(host, "REDACTED_HOST")
    if suffix == ".xml":
        value = re.sub(r'hostname="[^"]*"', 'hostname="REDACTED"', value)
    return value.encode("utf-8")


def main():
    baseline = json.loads((OUT / "baseline.json").read_text())
    for row in baseline["prior_archives"]:
        assert hashlib.sha256((ROOT.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
    contents = {}
    with ZipFile(ROOT.parent / "RAILSTORM_THUNDERBOLT_R5_REVIEW.zip") as zipped:
        manifest = json.loads(zipped.read("railstorm/SHA256_MANIFEST.json"))
        for name, sha in manifest.items():
            data = zipped.read("railstorm/" + name)
            assert hashlib.sha256(data).hexdigest() == sha
            contents[name] = data
    for folder in ("railstorm", "tests", "migrations", "scenarios", "scripts", "experiments"):
        for path in (ROOT / folder).rglob("*"):
            if (
                path.is_file()
                and not {"__pycache__", "reports", "node_modules"}.intersection(path.parts)
                and path.suffix not in {".pyc", ".pyo", ".log"}
            ):
                contents[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in (
        "README.md",
        "docs/IMPLEMENTATION_STATUS.md",
        "docs/THUNDERBOLT_R6_pytest.xml",
        "docs/THUNDERBOLT_R6_targeted.xml",
    ):
        path = ROOT / name
        contents[name] = clean(path.read_bytes(), path.suffix)
    for name in REPORTS:
        path = ROOT / ("docs/THUNDERBOLT_R6_" + name + ".md")
        assert path.stat().st_size > 300
        contents[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in OUT.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
            contents[path.relative_to(ROOT).as_posix()] = clean(path.read_bytes(), path.suffix)
    for path in (ROOT / ".work/r6/logs").glob("*.log"):
        contents["docs/r6_evidence/logs/" + path.name] = clean(path.read_bytes(), ".log")
    # Include the actual native R5 baseline without environments or private data.
    for path in (ROOT / ".work/r6/baseline").rglob("*"):
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            name = path.relative_to(ROOT / ".work/r6/baseline").as_posix()
            contents["docs/r6_evidence/frozen_r5_source/" + name] = path.read_bytes()
    for name, data in contents.items():
        assert not re.search(rb"[A-Za-z]:[\\/]+Users[\\/]+", data, re.IGNORECASE), name
        assert b"BEGIN " + b"PRIVATE KEY" not in data, name
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    target = ROOT.parent / "RAILSTORM_THUNDERBOLT_R6_REVIEW.zip"
    assert not target.exists(), "Never overwrite a review archive"
    with ZipFile(target, "w", ZIP_DEFLATED) as zipped:
        for name, data in sorted(contents.items()):
            zipped.writestr("railstorm/" + name, data)
        zipped.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(checksums, indent=2))
    with ZipFile(target) as zipped:
        assert zipped.testzip() is None
        for name, sha in checksums.items():
            assert hashlib.sha256(zipped.read("railstorm/" + name)).hexdigest() == sha
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(sha + "  " + target.name + "\n")
    print(
        json.dumps(
            {
                "archive": target.name,
                "files": len(checksums) + 1,
                "bytes": target.stat().st_size,
                "sha256": sha,
                "prior_archives_verified": len(baseline["prior_archives"]),
            }
        )
    )


if __name__ == "__main__":
    main()
