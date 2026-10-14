# REVIEW.md

Line numbers refer to the original starter `app/main.py`.

| #   | Where (function / line) | What is wrong                                                                                                                                                              | How I notice it (test, input, or symptom)                                                     | How I fixed it                                                                                       |
| --- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 1   | `MongoClient(...)`, L22 | No `tz_aware=True`, so PyMongo returns naive datetimes. Subtracting them from time-zone-aware values raises `TypeError`, and naive values are silently read as local time. | A stored punch minus an IST-aware shift time gives a 500. `doc["punch_in"].tzinfo` is `None`. | `tz_aware=True`. Instants are UTC everywhere and converted to IST only for the date and shift rules. |

| 2 | L22-23 | Hard-coded fallback URI and database name. No server-selection timeout, so the default is 30 s. | Stop MongoDB: every request hangs about 30 s. | `os.environ[...]` with no fallback, and `serverSelectionTimeoutMS=5000`. |

| 3 | whole file | No indexes at all. Nothing guarantees a unique `emp_code` or a unique (`emp_code`, `date`), and every query is a collection scan. | `index_information()` shows only `_id_`. Explain shows `COLLSCAN`. | `ensure_indexes()` at startup (and retried by `/health`). Indexes are listed in DECISIONS.md. |

| 4 | `compute_late_minutes`, L33-35 | The shift start is built with `punch_in.replace(hour, minute)`, which uses the punch's own clock (UTC or naive), not IST on the attendance date. Overnight shifts use the wrong day. | Punch at 09:35 IST (04:05 UTC) on a 09:30 shift gives a nonsense value. | `shift_start_dt(day, "HH:MM")` builds the start in IST on the attendance date. |

| 5 | `compute_late_minutes`, L36-37 | `int(seconds/60)` is compared with `> 10`. A punch at 09:40:01 becomes 10 and then returns 0. The rule is strictly more than 600 seconds. Seconds are never cut off (R1). | Punch at 09:40:01 returns 0, expected 10. | Compare `seconds > 600`, then floor to whole minutes. `punched_at` is truncated to whole seconds. |

| 6 | `compute_work_hours`, L41 | `round()` is not half-up (for example `round(2.675, 2)` gives 2.67) and the inputs are not truncated to seconds. | 2.675 hours gives 2.67, expected 2.68. | `Decimal` with `ROUND_HALF_UP`, on whole-second inputs. |

| 7 | `compute_overtime`, L44-47 | The shift end is assumed to be on the record's date, so overnight shifts are wrong by a day. The 30-minute minimum (R3) is missing, so 5 minutes is returned as 5. | Day shift, punch-out 18:35: returns 5, expected 0. Overnight 22:00-06:00, punch-out 06:40: wrong. | `shift_end_dt` adds a day for overnight shifts. Minutes are returned only if they are 30 or more. |

| 8 | `EmployeeIn`, L53-60 | No validation: `emp_code` pattern, email, `HH:MM`, `shift_start == shift_end`, a real `joined_on` date, and lengths are all unchecked. | `{"emp_code":"x","joined_on":"banana","shift_start":"99:99"}` returns 201. | Field patterns and validators, so bad input returns 422. |

| 9 | `PunchInIn`, L63-66 | `status` is any string (`ABSENT` and `banana` are accepted). `punched_at` accepts `"123"`, `1.5`, and seconds-sized numbers. | Punch-in with `status: "ABSENT"` returns 201 with a punch time. | `Literal` for status. `punched_at` is a strict integer within a millisecond range. |

| 10 | `health`, L72-74 | Always returns ok, even when MongoDB is unreachable. | Stop MongoDB: `/health` still says 200. | Pings MongoDB and returns 503 on failure. |

| 11 | `create_employee`, L79-85 | Check-then-insert: two simultaneous requests both pass the check and create a duplicate. `datetime.now()` is naive local time. The response returns a datetime, not epoch milliseconds. | Two identical POSTs at the same instant give two 201s. | Unique index on `emp_code` and a catch for `DuplicateKeyError` (409). UTC time and epoch milliseconds in the response. |

| 12 | `list_employees`, L89-96 | `skip = page * page_size` skips the whole first page. `total` ignores the department filter. There is no sort, so pages are unstable. `page=0` or a negative `page_size` gives a 500. | `GET /employees` shows `total: 6` with empty `items` (seen). | `(page - 1) * page_size`, a filtered count, sort by `emp_code`, and `Query` bounds. |

| 13 | `punch_in`, L101 and L113 | An unknown `emp_code` makes `emp` `None`, so `emp["shift_start"]` raises `TypeError`. | POST `EMP9999` returns 500 (seen). | Returns 404. |

| 14 | `punch_in`, L102-103 | `fromtimestamp` uses the server's local time. `date` is the server's date, not the IST attendance day (R1), and the overnight rule is missing. `if body.punched_at` treats 0 as missing. | A punch at 23:30 UTC (05:00 IST the next day) gets the wrong date. | `ms_to_dt` in UTC and `attendance_date()` in IST. The check is `is not None`. |

| 15 | `punch_in`, L104-118 | Check-then-insert race: two identical punch-ins can both be stored. | Two identical requests at the same instant give two 201s. | Unique (`emp_code`, `date`) index. `DuplicateKeyError` becomes 409. |

| 16 | `punch_in`, L119-121 | The response leaks an `id` field (the ObjectId) that is not in the contract, and returns datetimes instead of epoch milliseconds. | The response contains `"id"` and a date string for `punch_in`. | `record_out()` builds exactly the contract shape. |

| 17 | `list_attendance`, L144-150 | Loads every matching record into Python, sorts there, and slices. `total` is `len()`. The sort has no `emp_code` tie-break. | Slow and memory-heavy at 100k records. Explain shows `COLLSCAN`. | Filter, sort, skip and limit run in MongoDB on indexes (`date_emp_code`, `emp_date_unique`, `status_date_emp_code`). `count_documents` for the total. |

| 18 | `list_attendance`, L125-132 | No validation: `status` is any string, dates may be fake or reversed, `page=0` gives a wrong slice, and `page_size` is unbounded. | `page=0` returns the wrong rows. `date_from` after `date_to` returns 200. | `Literal`, date patterns plus a real-date check, `Query` bounds, and 422 when `date_from` is after `date_to`. |

| 19 | `list_attendance`, L148-150 | Leaks `id`, returns datetimes, and records missing `history` or `half_day` (legacy) come back incomplete. | The legacy 14 July record has no `half_day` or `history`. | `record_out()` fills the defaults (false, empty list, 0). |

| 20 | L91, L134, L142 | Truthiness checks (`if department:`, `if emp_code:`, `if status:`) silently ignore an empty string. Minor. | `?emp_code=` returns every employee. | `is not None` checks. |

## Looked at and decided it is not a defect

- `load_dotenv()` (L20): by default it does not override real environment variables, which is the behaviour the environment contract asks for.

- Plain `def` endpoints with PyMongo: FastAPI runs them in a thread pool, so they do not block the event loop.

- Comparing `date` with `$gte` and `$lte` as strings (L139, L141): `YYYY-MM-DD` sorts correctly as text, so this is fine once the format is validated.

- `doc.pop("_id", None)` after `insert_one` (L84): `insert_one` adds `_id` to the dict, so removing it is correct and keeps it out of the response.

- `IST = timezone(timedelta(hours=5, minutes=30))` (L25): India has no daylight saving, so a fixed offset is correct.

- `joined_on` and `date` as plain strings: this matches DATA_MODEL.md, which says it is on purpose.
