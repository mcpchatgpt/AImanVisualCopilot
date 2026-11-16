## 0.9.1

- Added Migration 11 to complete the semantic trust reset while preserving raw observation/evidence.
- Added deterministic `semanticizer.py --reseed` recovery from retained raw Frames.
- Removed overlapping legacy Sessions and embedded keyword-anomaly summaries after reseed.
- Fixed Session Episode counts beyond the 100-ID drill-down window.
- Added production-copy migration/reseed regression coverage and an explicit reseed unit test.

# Changelog

## 0.9.0

- Replaced query-time semantic rebuilds with transactional incremental state updates.
- Added reliable `current_world_state`, engine cursors and idempotent database migrations.
- Reset untrustworthy 0.8 derived anomaly/causality rows during migration while preserving raw observations.
- Restricted anomaly detection to structured dialogs, OS state, HTTP/network/dev signals and low-information structure.
- Added DPI/browser-toolbar/scroll coordinate calibration and same-name disambiguation to Scene Graph fusion.
- Added an explicit fusion status so missing page-level UIA is not misreported as a failed match.
- Required explicit mouse, key, submit or navigation evidence for causal hints.
- Added a budgeted visual-memory worker with priority filtering, SHA-256 deduplication and durable summaries.
- Added regression tests for migration ordering, state idempotence, anomaly accounting and visual queue consumption.

## 0.8.0

- Public baseline release preserved on `release/0.8.0`.
