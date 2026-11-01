# Reviewing and contributing

This repository is publicly inspectable but **all rights reserved**, not open source. No reuse or
redistribution license is granted. Please open an issue to discuss a proposed contribution and its
permissions before submitting code. Do not upload private data or credentials.

Useful review reports include the exact commit, runtime versions, command, expected and actual
result, and a minimal synthetic reproduction. Financial changes need independent audit, replay,
idempotent retry and crash/recovery evidence. Do not remove checks to improve timings.

Use the maintained quality and test commands in docs/SETUP.md. Historical generator type errors
remain disclosed; do not claim whole-tree type cleanliness. Preserve original benchmark boundaries
and distinguish native, HTTP, browser, container and CI evidence.
