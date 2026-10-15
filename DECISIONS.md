# Decisions (review and rewrite in your own words before submission)

1. **Indexes.** A unique index on `employees.emp_code` enforces the business identifier, while the unique compound index on `(emp_code, date)` makes one attendance row per employee/day safe under concurrency. Department/code and department/join-date indexes support employee filtering and headcount lookups. Attendance indexes cover date-ordered listing, date/status filtering, and finding the most recent punch-in for an employee.

2. **Concurrent punch-ins.** Both requests may reach MongoDB, but the unique compound index permits only one insert. The winner receives 201; the duplicate-key error is translated to 409. A preliminary existence check alone would not provide this guarantee.

3. **Leaderboard ties.** `$rank` assigns equal total-late-minute values the same rank and skips the next rank. The rank filter is applied after ranking, so every employee tied at or above the cutoff rank is returned; ties are displayed by employee code.

4. **Employees without logs.** The department summary starts with employees joined by the month end and uses a lookup for that month's logs. Preserving employees with an empty log array keeps them in headcount, while later joiners are excluded. Departments with zero headcount are omitted.

5. **100× data.** I would inspect execution statistics and index selectivity, consider a replica set and workload-specific compound indexes, cache or precompute stable monthly summaries, and benchmark aggregation memory use. I would keep analytics server-side and avoid loading full collections into application memory.
