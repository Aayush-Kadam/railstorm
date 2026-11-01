# Third-party dependency review

RAILSTORM's own source remains all rights reserved. Dependencies retain their separate upstream
licenses; the project LICENSE does not relicense third-party components.

The public repository contains lockfiles and imports, not copied installed dependency directories,
database binaries or frontend bundles. Python and frontend dependencies are obtained by reviewers
from their package registries during installation. Their declared license inventories are retained:

- [Python locked-package metadata](evidence/python-dependency-license-inventory.json)
- [Frontend installed-package metadata](evidence/frontend-dependency-license-inventory.json)

The inspected declarations include permissive MIT/BSD/Apache licenses and the LGPL terms of
psycopg. No dependency declaration was found to prevent publication of this source snapshot.
Retain upstream notices and comply with the applicable component terms if distributing built
images or bundles. This campaign publishes source; it does not upload a prebuilt Docker image.

PostgreSQL, Python and Node base images are fetched by Docker at build time. The Dockerfile pins
the uv binary image by digest and the Python/frontend package versions by lockfile; some base
image tags can resolve to later patch releases. Fresh acceptance records the actual image IDs
and installed versions, rather than claiming a bit-for-bit reproducible OCI image from mutable tags.

No external fonts, stock photography or third-party visual assets were introduced. Screenshots
show the executed synthetic application. Registry license metadata is an inventory, not a claim
of a complete legal or security certification.
