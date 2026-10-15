# DECISIONS.md

I create a unique `employees.emp_code` index because the API accepts customer codes, not Mongo IDs. The unique `(emp_code, date)` index guarantees one daily record and supports employee-month lookups. Date/code and status/date indexes serve attendance filtering and sorting; employee/punch-in supports punch-out. Join-date indexes support headcount lookups with or without a department filter. I avoid indexing every analytics field because pipelines enter through stored dates and employee codes.

For simultaneous identical punch-ins, both may read the employee; the unique attendance index accepts one insert and rejects the rest. The duplicate-key error becomes 409. Punch-out uses a conditional atomic update where `punch_out` is null.

The `$rank` leaderboard filters by rank. A tie at the cutoff returns every tied employee in employee-code order; the following rank is skipped.

Department summary starts from employees joined by month end, then looks up logs. Employees with no logs still count. For 100x the data, I would pre-aggregate daily department and employee summaries so monthly and trend queries scan fewer raw records.
