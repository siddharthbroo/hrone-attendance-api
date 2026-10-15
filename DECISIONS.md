# DECISIONS.md

1. **Indexes.** We created five indexes: `employees(emp_code)` (unique) ensures unique employees; `employees(department, emp_code)` speeds up filtered employee listings; `attendance_logs(emp_code, date)` (unique) prevents duplicate daily punches and speeds monthly analytics; `attendance_logs(date DESC, emp_code ASC)` backs `/attendance` pagination; and `attendance_logs(emp_code, punch_in DESC)` enables O(1) punch-out lookups. We rejected indexing `status` alone due to low cardinality and write overhead.

2. **Punch-in race.** If two identical punch-in requests arrive simultaneously, both may pass application checks. However, MongoDB's unique compound index `(emp_code, date)` serializes insertion: one insert succeeds (201), while the concurrent insert raises `DuplicateKeyError`, which we catch and return as HTTP 409 Conflict.

3. **Ties.** Standard competition ranking (1, 2, 2, 4) assigns tied totals equal rank and skips subsequent ranks via `$setWindowFields` with `$rank`. When ties cross the `limit` cutoff, the endpoint returns all candidates with `rank <= limit`, so returned rows can exceed `limit`. Ties order by `emp_code` ascending.

4. **Headcount.** The department summary aggregates from `employees` (filtering `joined_on <= month_end`) and left-joins `attendance_logs` using `$lookup`. Grouping on `employees` guarantees employees with zero logs are included in `headcount`.

5. **100x scale.** We would pre-aggregate daily records into monthly summary documents, eliminating runtime `$lookup` joins across millions of records.
