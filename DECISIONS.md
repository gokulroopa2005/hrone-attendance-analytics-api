# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
On `employees`: unique `emp_code` (uniqueness/lookups), `(department, emp_code)` (listing), `(department, joined_on)` and `joined_on` (headcount). On `attendance_logs`: unique `(emp_code, date)` (one log daily), `(date: -1, emp_code: 1)` (listing sort), `(emp_code: 1, date: -1)` (monthly), `(status, date, emp_code)` (status filter), `(date, late_minutes)` (leaderboard), and `(emp_code: 1, punch_in: -1)` (punch-out). Rejected standalone `date` on logs because it cannot satisfy compound listing sorts, causing in-memory sorts.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests compute the attendance date and execute `insert_one`. The unique compound index on `(emp_code, date)` enforces write atomicity: the first succeeds with HTTP 201; the second raises a MongoDB `DuplicateKeyError`, caught to return HTTP 409 Conflict without creating duplicate records.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
Using `$setWindowFields` with `$rank`, tied employees receive identical competition ranks (1, 2, 2, 4). The query matches `rank <= limit`. Tied employees at the cutoff are both included, ordered deterministically by `emp_code` ascending.

4. **Headcount & Analytics Aggregations.** How does your department summary make sure employees with zero logs are counted?
The pipeline starts on `employees` (`joined_on <= period_end`), uses `$lookup` to `attendance_logs`, and `$unwind` with `preserveNullAndEmptyArrays: true`. Headcount uses `$size` of `$addToSet: "$emp_code"`, including zero-log employees. All metrics are aggregated directly in MongoDB, avoiding 16MB document limits.

5. **One thing you would change** if this had to serve 100x the data.
Shard `attendance_logs` by `(emp_code, date)` and pre-aggregate daily department statistics via change streams into roll-up collections, eliminating expensive on-demand `$lookup` and `$unwind` aggregations over 10M+ documents.
