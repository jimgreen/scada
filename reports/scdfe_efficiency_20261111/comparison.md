# scdfe_c / scdfe_s / scdfe_t efficiency comparison (2026-11-11)

This round keeps only changes with a measured benefit.

| Area | Before | After | Result |
|---|---:|---:|---|
| Runtime status read: 2400 snapshots vs current 50-channel page | 24.918 ms | 3.306 ms | 86.73% lower |
| scdfe_t /api/trans/status median | 0.268 ms | 0.264 ms | 1.49% lower |
| scdfe_t status payload | 3468 B | 2760 B | 20.42% smaller |
| scdfe_c /api/scdfe_c/status median | 1.079 ms | 1.076 ms | essentially unchanged |
| scdfe_s /api/monitor/communications median | 14.433 ms | 13.242 ms | same code path, local variance |
| T page periodic point requests | status refresh also reloaded points | status refresh skips points | fewer redundant DB/API reads |
| S test page same-RTU overlapping refreshes | possible | coalesced | fewer duplicate requests |

The four-way parallel RTU option pagination candidate was measured and removed: five pages took 10.893 ms serially and 32.448 ms with four concurrent requests in local SQLite/WAL.

Validation: 33 focused tests passed; Python compilation, JavaScript syntax, git diff --check, and health checks on 8877/8878/8879 passed. The broad suite reached 458 passed and 3 skipped, but the live scdfe_c daemon held the global instance lock, so lock-dependent tests failed and left 3 teardown errors; those are environment boundaries rather than this change set.