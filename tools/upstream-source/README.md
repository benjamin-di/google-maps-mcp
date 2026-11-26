# Standalone source verifier

`upstream-source` and its adjacent `upstream_source.py` are exact snapshots of
the portable source verifier. `SOURCE.toml` records the immutable source commit,
original paths and SHA-256 hashes. The command uses Python's standard library
(Python 3.12+) and Git; no package installation or external checkout is needed.

The source adapter selects an explicit `UPSTREAM_SOURCE` executable first,
otherwise it selects this bundled copy, independent of PATH. The bundled files
must match their recorded hashes and the command must be executable. A missing
explicit override always refuses verification.

Update both snapshot files and the source record together from one reviewed
source commit. Do not edit the snapshot files locally. Run `make check` after
an update; it exercises source reconstruction as well as distribution failures.
