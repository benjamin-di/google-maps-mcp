# Changelog

All notable changes are recorded here. This project follows Semantic Versioning.

## [0.7.1] - 2026-11-26

### Fixed

- Declare `MAPS_MCP_DEPS_IMAGE` in global `Dockerfile` scope, exclude `scripts/smoke-image.sh` from public exports, and run offline `publish-public --check-head` in `make check`.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.7.0.

## [0.7.0] - 2026-11-23

### Changed

- Trim MCP server instructions to remove redundant tool/parameter re-listings and the raw error-table dump.
- Migrate CI checks and image promotion to native Jenkins `fleetPipeline` with `mainCandidate`.

### Added

- Verify immutable Maps vendor source and projection during CI checks and vendor refresh.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.3; stateless read-only server with no schema or persistent state changes.

## [0.6.3] - 2026-11-08

### Fixed

- Identify the producing version and source revision in map views and JSON diagnostics (#3).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.2.

[0.6.3]: https://github.com/benjamin-di/google-maps-mcp/releases/tag/v0.6.3

## [0.6.2] - 2026-10-31

_Security: require patched FastMCP; older releases pin an affected version._

### Security

- Require FastMCP 4.1.4 or a compatible 4.1 patch release to resolve component authorization and OAuth issuer-binding advisories.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.1, but restores its affected FastMCP dependency.

[0.6.2]: https://github.com/benjamin-di/google-maps-mcp/releases/tag/v0.6.2