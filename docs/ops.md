# Operations notes

## Fetch success by host (last 7 days)

Run on the server: `sqlite3 data/tracker.db` then

```sql
SELECT fetch_host, fetch_strategy, fetch_status,
       COUNT(*) AS n,
       SUM(has_requirements) AS with_requirements
FROM jobs
WHERE created_at >= datetime('now', '-7 days') AND fetch_status != 'pending'
GROUP BY 1, 2, 3
ORDER BY n DESC;
```

## Usable descriptions for subscribed sections

"Usable" = fetched and the text mentions requirements/qualifications.

```sql
SELECT j.section,
       COUNT(*)                                        AS jobs,
       SUM(j.fetch_status = 'ok')                      AS fetched,
       SUM(j.fetch_status = 'ok' AND j.has_requirements) AS usable
FROM jobs j
WHERE EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id)
  AND j.created_at >= datetime('now', '-7 days')
GROUP BY 1;
```

Decide on a headless browser only if `usable / jobs` for the sections you care about stays under ~70% after two weeks, and the misses concentrate on a host without a JSON API.

## Queue state

```sql
SELECT stage, outcome, COUNT(*) FROM evaluations GROUP BY 1, 2;
SELECT fetch_status, COUNT(*) FROM jobs GROUP BY 1;
```

## Retry a failed fetch by hand

```sql
UPDATE jobs SET fetch_status='pending', fetch_attempts=0, fetch_first_attempt_at=NULL,
       next_attempt_at=datetime('now') WHERE id = <job id>;
UPDATE evaluations SET outcome=NULL WHERE job_id = <job id> AND stage != 'closed';
```
