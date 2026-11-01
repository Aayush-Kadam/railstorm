"""New source/evidence archive with every internal hash checked; prior ZIPs untouched."""

import hashlib
import json
import os
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"
NAMES = (
    "PERFORMANCE_ROOT_CAUSE",
    "ARCHITECTURAL_DESIGN",
    "IMPLEMENTATION_AND_DIFF",
    "CORRECTNESS_VERIFICATION",
    "BENCHMARK_RESULTS",
    "RESOURCE_PROFILE",
    "10K_GO_NO_GO",
    "FINAL_VERDICT",
)


def clean(data, suffix):
    if suffix not in {".json", ".xml", ".md", ".log", ".txt", ".diff"}:
        return data
    value = data.decode("utf-8")
    value = re.sub(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"]+", "<USER_HOME>", value, flags=re.IGNORECASE)
    host = os.environ.get("COMPUTERNAME")
    if host:
        value = value.replace(host, "REDACTED_HOST")
    user = os.environ.get("USERNAME")
    if user and suffix in {".json", ".xml", ".log"}:
        value = re.sub(r"\b" + re.escape(user) + r"\b", "LOCAL_USER", value, flags=re.IGNORECASE)
    if suffix == ".xml":
        value = re.sub(r'hostname="[^"]*"', 'hostname="REDACTED"', value)
    return value.encode("utf-8")


def main():
    baseline = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    for prior in baseline["prior_archives"]:
        assert hashlib.sha256((ROOT.parent / prior["name"]).read_bytes()).hexdigest() == prior["sha256"]
    verdict = json.loads((OUT / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["financial"] == verdict["concurrency"] == "PASS"
    contents = {}
    with ZipFile(ROOT.parent / "RAILSTORM_THUNDERBOLT_R6_REVIEW.zip") as zipped:
        manifest = json.loads(zipped.read("railstorm/SHA256_MANIFEST.json"))
        for name, sha in manifest.items():
            data = zipped.read("railstorm/" + name)
            assert hashlib.sha256(data).hexdigest() == sha
            contents[name] = data
    for folder in ("railstorm", "tests", "scripts", "experiments", "migrations", "scenarios"):
        for path in (ROOT / folder).rglob("*"):
            if (
                path.is_file()
                and not {"__pycache__", "reports", "node_modules"}.intersection(path.parts)
                and path.suffix not in {".pyc", ".pyo", ".log"}
            ):
                contents[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ("README.md", "docs/IMPLEMENTATION_STATUS.md", "docs/R7_pytest.xml"):
        path = ROOT / name
        contents[name] = clean(path.read_bytes(), path.suffix)
    for name in NAMES:
        path = ROOT / ("docs/R7_" + name + ".md")
        assert path.stat().st_size > 300
        contents[path.relative_to(ROOT).as_posix()] = clean(path.read_bytes(), ".md")
    for path in OUT.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
            contents[path.relative_to(ROOT).as_posix()] = clean(path.read_bytes(), path.suffix)
    for path in (ROOT / ".work/r7/logs").glob("*.log"):
        contents["docs/r7_evidence/logs/" + path.name] = clean(path.read_bytes(), ".log")
    for path in (ROOT / ".work/r7/baseline").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            contents[
                "docs/r7_evidence/frozen_r6_source/" + path.relative_to(ROOT / ".work/r7/baseline").as_posix()
            ] = path.read_bytes()
    checkpoint = ROOT / ".work/r7/prechange_source.zip"
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == baseline["checkpoint_sha256"]
    with ZipFile(checkpoint) as zipped:
        assert zipped.testzip() is None
        for name in zipped.namelist():
            assert not re.search(rb"[A-Za-z]:[\\/]+Users[\\/]+", zipped.read(name), re.IGNORECASE), name
    contents["docs/r7_evidence/prechange_source.zip"] = checkpoint.read_bytes()
    for name, data in contents.items():
        assert not re.search(rb"[A-Za-z]:[\\/]+Users[\\/]+", data, re.IGNORECASE), name
        assert b"BEGIN " + b"PRIVATE KEY" not in data, name
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    target = ROOT.parent / "RAILSTORM_THUNDERBOLT_R7_PERF_REVIEW.zip"
    assert not target.exists(), "Never overwrite a review archive"
    with ZipFile(target, "w", ZIP_DEFLATED) as zipped:
        for name, data in sorted(contents.items()):
            zipped.writestr("railstorm/" + name, data)
        zipped.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(hashes, indent=2))
    with ZipFile(target) as zipped:
        assert zipped.testzip() is None
        for name, sha in hashes.items():
            assert hashlib.sha256(zipped.read("railstorm/" + name)).hexdigest() == sha
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(sha + "  " + target.name + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "archive": target.name,
                "files": len(hashes) + 1,
                "bytes": target.stat().st_size,
                "sha256": sha,
                "prior_archives_verified": len(baseline["prior_archives"]),
            }
        )
    )


if __name__ == "__main__":
    main()
