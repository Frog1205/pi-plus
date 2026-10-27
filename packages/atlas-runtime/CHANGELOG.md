# Changelog

## [Unreleased]

### Added

- Controlled offline Atlas Worker with an explicit two-tool registry, host receipts, and a reviewed pi-durable source pin.
- Fixture-only P2.0 HTTP adapter transport probe with framed IPC, durable request identity, cancellation, response replay, and process-crash tests.
- Governed P2.1 fixture Host with immutable task profiles, reconstructed model context, transactional request reservations, shared batch budgets, migration backups, and conservative unknown-outcome recovery.

### Fixed

- Serialize governed Host deadline and completion reads with model ledger transactions; surface storage failures through the bounded Worker error path.
- Preserve incremental P2.1 test output and exclude temporary artifact snapshots from the relative TypeScript import check.
