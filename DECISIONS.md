# DECISIONS.md

1.**Indexes.** Employees: unique `emp_code`, `(department, emp_code)` for listing, and `(department, joined_on)` for headcount. Attendance: unique `(emp_code, date)`, `(date desc, emp_code)` for lists and monthly scans, and `(status, date desc, emp_code)` for the status filter. I rejected an index on `late_minutes`: the leaderboard sums per employee over a month, so the date index already narrows the work.

2. **Punch-in race.** Both requests try to insert. The unique `(emp_code, date)` index lets the database accept one; the other raises a duplicate-key error, which I turn into 409. There is no check-then-insert.

3. **Ties.** Rows are sorted by total descending, then `emp_code`. The rank is the position where the total changes, so ties share a rank (1, 2, 2, 4). I keep everyone with rank at most `limit`, so tied rows at the cutoff are all returned.

4. **Headcount.** It comes from a separate aggregation on `employees` (joined on or before month end), not from the logs, so people with zero logs still count.

5. **100x data.** I would store a daily per-department rollup, updated at punch time, so analytics read a few rows instead of scanning logs and joining employees.