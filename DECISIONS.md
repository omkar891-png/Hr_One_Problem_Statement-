# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
We created: (1) `employees.emp_code` (unique) to enforce identity uniqueness during concurrent creation; (2) `employees.(joined_on, department)` to enable index scans during department summary headcount filtering; (3) `attendance_logs.(emp_code, date)` (unique) to guarantee at most one record per employee per day and power natural-key PATCH lookups; (4) `attendance_logs.(date, emp_code)` to serve paginated listing in `date` descending and `emp_code` ascending order without in-memory sorting; (5) `attendance_logs.(emp_code, punch_in)` to accelerate reverse punch-in lookups during punch-out; and (6) `attendance_logs.(date, late_minutes, emp_code)` for IXSCAN in leaderboard queries. We considered indexing `history` but rejected it because `history` is an append-only audit trail never queried by filters.

2. **Punch-in race.**
Both requests pass application validation and attempt `insert_one`. Because `(emp_code, date)` carries a unique database index, MongoDB atomically executes the first write and yields 201 Created. The duplicate insert raises `pymongo.errors.DuplicateKeyError`, which our exception handler intercepts to immediately return 409 Conflict.

3. **Ties.**
Our aggregation uses MongoDB's `$rank` window function, implementing standard competition ranking (1, 2, 2, 4). Filtering with `rank <= limit` returns all tied employees who share qualifying ranks, even if total returned items exceed `limit`. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.**
We root the aggregation in `employees` (filtered by `joined_on <= month_end`), grouping by department so all active personnel count toward headcount. We then `$lookup` their month's attendance logs; employees with zero logs yield an empty array, preserving their headcount contribution while adding zero to presence or late totals.

5. **One thing you would change** if this had to serve 100x the data.
We would maintain pre-aggregated daily/monthly departmental rollups (updated asynchronously via MongoDB change streams) and shard `attendance_logs` by hashed `emp_code` with a compound `date` key.
