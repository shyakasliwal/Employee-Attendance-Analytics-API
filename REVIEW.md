# Starter review

| Location | Defect | How to reproduce / symptom | Fix |
|---|---|---|---|
| `compute_late_minutes` | Uses `punch_in.replace(...)`, which retains the punch-in date/time zone and has fragile overnight semantics; integer truncation is not explicitly enforced. | Test 09:40:01 for a 09:30 shift and an overnight shift after midnight. | Convert instants to UTC/IST at the boundary, truncate to whole seconds, and build the shift start from the attendance date. Apply the strict >600-second grace rule. |
| `compute_work_hours` | Python `round` uses ties-to-even rather than the contract's half-up rounding. | Construct a duration whose hour value is exactly halfway at two decimal places. | Use `Decimal` and `ROUND_HALF_UP` after calculating elapsed whole seconds. |
| `compute_overtime` | Always builds shift end on `date_str`; overnight shifts end the next day. It also counts any positive minute rather than enforcing the 30-minute threshold. | Shift 22:00–06:00, punch out 06:20 or 06:40 the following day. | Add a day for overnight shifts and return overtime only at 30 whole minutes or more. |
| `GET /health` | Always returns healthy without checking MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB; return 503 on failure. |
| Employee creation | Duplicate check then insert is racy; `created_at` is a naive local datetime; input constraints and unequal shift validation are missing. | Send simultaneous creates with the same `emp_code`; use malformed codes/dates or equal shifts. | Enforce a unique database index, translate duplicate-key errors to 409, validate fields, and store UTC timestamps. |
| `GET /employees` | `skip = page * page_size` skips the first page; total ignores department filter; results have no deterministic sort. | Request page 1 and a department filter. | Use `(page - 1) * page_size`, count the filtered query, sort by `emp_code`, and validate pagination. |
| `POST /attendance/punch-in` | Unknown employees cause a 500; status and timestamp validation are weak; uses local machine time; date calculation ignores overnight shift rules; check-then-insert is racy; response exposes a synthetic `id`. | Unknown `emp_code`, seconds instead of milliseconds, invalid status, overnight punch, or parallel identical requests. | Validate input and employee existence, use UTC/IST conversion and whole-second truncation, rely on a unique `(emp_code,date)` index, return 404/409/422 as appropriate, and omit Mongo IDs. |
| `GET /attendance` | Loads all matching documents into memory, sorts in Python, lacks date validation, and doesn't sort ties deterministically. | Query a large dataset or pass `date_from > date_to`. | Use indexed MongoDB filters, count the filtered set, sort by date descending then employee code ascending, and paginate in MongoDB. |
| Missing endpoints | Punch-out, regularization, monthly/department analytics, leaderboard, trend and explain endpoints are not implemented. | Request any contract path marked “You”. | Implemented the missing routes, including atomic updates, aggregation pipelines, ranking and explain output. |
| Serialization | BSON datetimes and Mongo `_id` are not consistently converted/removed. | Inspect timestamps/history and records from seeded data. | Convert all API instants to epoch milliseconds and remove internal IDs. |

## Reviewed and retained

- `emp_code` is the business key, not MongoDB `_id`.
- Calendar dates remain `YYYY-MM-DD` strings; instants are stored as BSON UTC datetimes.
- Analytics use the stored derived fields rather than recalculating them from punch times.
