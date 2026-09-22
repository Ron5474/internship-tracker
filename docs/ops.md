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

The worker only fetches jobs that still have an open (non-`closed`) evaluation. If the job's evaluations were already delivered, the reset has no effect.

## Scoring

Per-attempt log line: `score ev=<id> user=<id> model=<m> outcome=<matched|below_threshold|transient|unavailable|invalid> score=<n|-> ms=<t> tokens=<in>/<out>`.

Score distribution and outcomes (last 7 days):

```sql
SELECT user_id, outcome, COUNT(*) AS n, ROUND(AVG(score),1) AS avg_score, MIN(score), MAX(score)
FROM evaluations
WHERE updated_at >= datetime('now', '-7 days') AND score IS NOT NULL
GROUP BY 1, 2;
```

Token spend by model:

```sql
SELECT score_model, COUNT(*) AS calls,
       SUM(json_extract(score_usage, '$.prompt_tokens'))     AS prompt_tokens,
       SUM(json_extract(score_usage, '$.completion_tokens')) AS completion_tokens
FROM evaluations WHERE score_usage IS NOT NULL GROUP BY 1;
```

### Calibration week

Set `notify_below_threshold: true` for yourself in `users.yaml` and restart. Every scored posting arrives with 🎯 (≥ threshold) or 📉 (below). Read a week of them; when a 📉 should have been a 🎯 or vice versa, note the evaluation id from the log line and the score. Adjust the rubric in `src/prompts.py` or your `threshold`, then set `notify_below_threshold: false`.

Re-score one evaluation by hand (e.g. after a prompt change):

```sql
UPDATE evaluations SET stage='score', outcome=NULL, score=NULL, attempts=0, next_attempt_at=datetime('now')
WHERE id = <evaluation id>;
```
