"""Create a new R6 campaign; never overwrite recorded measurements or databases."""

import hashlib
import json
import socket
import uuid
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"
FROZEN = ROOT / ".work/r6/baseline"
ARCHIVE = ROOT.parent / "RAILSTORM_THUNDERBOLT_R5_REVIEW.zip"
R5_SHA256 = "04e64071e916095b58fd7c77db8615b3f44395875406a927192e45beedcbc067"


def main():
    assert not OUT.exists(), "Use a fresh checkout; preserve supplied R6 evidence separately"
    assert not FROZEN.exists()
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == R5_SHA256
    with ZipFile(ARCHIVE) as zipped:
        assert zipped.testzip() is None
        manifest = json.loads(zipped.read("railstorm/SHA256_MANIFEST.json"))
        for name, sha in manifest.items():
            assert hashlib.sha256(zipped.read("railstorm/" + name)).hexdigest() == sha
            if name.startswith(("railstorm/", "migrations/", "scenarios/")):
                path = FROZEN / name
                assert path.resolve().is_relative_to(FROZEN.resolve())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zipped.read("railstorm/" + name))
    OUT.mkdir(parents=True)
    prior = [
        {"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(ROOT.parent.glob("*.zip"))
    ]
    source = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "railstorm").rglob("*.py")
    }
    (OUT / "baseline.json").write_text(
        json.dumps(
            {
                "source_sha256": source,
                "prior_archives": prior,
                "r5_archive_sha256": R5_SHA256,
                "database": "railstorm_r6_" + uuid.uuid4().hex[:8],
                "revision_note": "Reproduction checkout source; original pre-implementation revision remains in supplied evidence",
            },
            indent=2,
        )
    )
    new_baseline = json.loads((OUT / "baseline.json").read_text())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (OUT / "context.json").write_text(
        json.dumps(
            {
                "project": "railstorm-r6-" + new_baseline["database"][-8:],
                "container_port": port,
                "database": new_baseline["database"],
            },
            indent=2,
        )
    )
    print(json.dumps({"frozen_baseline": "R5", "new_context_created": True}))


if __name__ == "__main__":
    main()
