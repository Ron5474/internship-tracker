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
UPDATE evaluations SET outcome=NULL, stage='score', attempts=0,
       tailored=NULL, pdf_path=NULL, page_overflow=0, resume_error=NULL,
       next_attempt_at=datetime('now')
WHERE job_id = <job id> AND stage != 'closed';
```

The evaluation has to go back to `score`: a row left at `deliver` would be sent link-only as soon as the re-fetch lands, so it must be re-queued for scoring to benefit from the new description. Clearing `tailored`/`pdf_path`/`page_overflow`/`resume_error` matters too: a row already tailored and rendered (waiting at `deliver` behind a paused webhook, say) still has a PDF from the old description, and re-scoring below threshold must not ship it — see "Calibration week" below for the full statement and why.

The worker only fetches jobs that still have an open (non-`closed`) evaluation. If the job's evaluations were already delivered, the reset has no effect.

## Deploying Plan 3

1. **Set `notify_below_threshold: true` for every calibrating user in `users.yaml` before pulling the image.** Suppression starts on the first scored row, so a user switched on afterwards silently loses the below-threshold postings scored in between.

2. **Verify the endpoint from inside the container network** (not from the host — the URL differs):

```
docker compose run --rm internship-tracker python -c "import os,requests;print(requests.get(os.environ['LLM_BASE_URL']+'/models',headers={'Authorization':'Bearer '+os.environ.get('LLM_API_KEY','')},timeout=10).status_code)"
```

Expect `200`, and the model alias in `LLM_SCORE_MODEL` must appear in that `/models` list — an alias LiteLLM does not expose comes back as a 404, which the worker classes `unavailable` and pauses on.

3. **Tail the logs for the first `score ev=` line:**

| What you see | What it means |
| --- | --- |
| `outcome=matched` / `outcome=below_threshold` | healthy |
| `outcome=invalid` on every row | the model is not honouring the strict schema — read `error=` for the failing field |
| `outcome=unavailable` and `Pausing llm` every 15 min | URL, key or model alias is wrong; fix `.env` and restart rather than waiting |
| `Pausing llm (3 consecutive invalid replies…)` | the breaker: three invalid replies in a row, same cause as above |
| `outcome=unusable` | the model says the fetched text is not a posting; the row is delivered link-only |

4. **Bulk re-score for recovery**, once the endpoint is fixed:

```sql
UPDATE evaluations SET stage='score', outcome=NULL, score=NULL, attempts=0, next_attempt_at=datetime('now')
WHERE outcome='score_failed' AND stage != 'closed';
```

Rows already delivered "(couldn't score)" are `closed` and cannot be recovered — the message is out.

## Scoring

Per-attempt log line: `score ev=<id> user=<id> model=<m> outcome=<matched|below_threshold|unusable|transient|unavailable|invalid> score=<n|-> ms=<t> tokens=<in>/<out>`.

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

Find the evaluation behind a message (by company):

```sql
SELECT e.id, e.score, e.outcome, e.reasoning
FROM evaluations e JOIN jobs j ON j.id = e.job_id
WHERE j.company LIKE '%X%' ORDER BY e.id DESC;
```

**Re-scoring a row (after a CV, prompt or rubric change) must clear every downstream artifact.** A bare `stage='score'` leaves the previous run's selection and PDF attached, and if tailoring then fails the row ships the old resume under the new score:

```sql
UPDATE evaluations SET stage='score', outcome=NULL, attempts=0,
       cv_snapshot=NULL, tailored=NULL, pdf_path=NULL, page_overflow=0, resume_error=NULL,
       score=NULL, reasoning=NULL, missing_confirmed=NULL, missing_unknown=NULL,
       next_attempt_at=datetime('now') WHERE id = <evaluation id>;
```

`cv_snapshot=NULL` is what makes the re-score read the edited CV; leaving it set re-scores against the old one — harmless after a prompt change (same CV, new rubric), wrong after a CV edit.

## Deploying Plan 4

1. **Server prep.** Add `LLM_TAILOR_MODEL=deepseek-v4-pro` and `MAX_BULLETS_PER_ENTRY=4` to `~/deployed-projects/internship-tracker/.env`; the container creates `data/output/<user>/` itself. Nothing prunes `data/output/`: rendered PDFs accumulate there indefinitely, including orphans left behind once `pdf_path` is cleared (a re-tailor, a below-threshold re-score, a manual re-render). Confirm the alias exists:

```bash
curl -s -H "Authorization: Bearer $LLM_API_KEY" $LLM_BASE_URL/models | python3 -c "import json,sys; print([m['id'] for m in json.load(sys.stdin)['data']])"
```

2. **Expected first-boot lines:**

```
Schema upgraded: added evaluations.resume_error, evaluations.tailor_model
Scoring with deepseek-v4-flash, tailoring with deepseek-v4-pro at ...
```

3. **Watch the logs:**

```bash
docker compose logs -f internship-tracker | grep -E "tailor ev=|render ev=|Delivered|ERROR"
```

4. **Measurement queries:**

```sql
-- how often a match actually ships with a resume
SELECT CASE WHEN pdf_path IS NOT NULL THEN 'with pdf' ELSE 'no pdf' END AS kind,
       COUNT(*) FROM evaluations WHERE outcome='matched' GROUP BY kind;

-- why resumes are missing
SELECT substr(resume_error,1,60), COUNT(*) FROM evaluations
WHERE resume_error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;

-- one-page rate
SELECT page_overflow, COUNT(*) FROM evaluations WHERE pdf_path IS NOT NULL GROUP BY 1;

-- tailoring spend
SELECT tailor_model, COUNT(*) FROM evaluations WHERE tailor_model IS NOT NULL GROUP BY 1;
```

5. **Rolling back to a Plan 3 image strands in-flight rows.** A Plan 3 worker has no `tailor` or `render` selector at all, so rows sitting at those stages are invisible to it — the drain that would rescue them lives in Plan 4's code and cannot help from an older image. **Order matters — stop the worker first**, or an in-flight `run_once()` will lease a row and write its stage back over the reset:

```bash
docker compose stop internship-tracker
```

```sql
UPDATE evaluations SET stage='deliver', attempts=0, pdf_path=NULL, page_overflow=0,
       resume_error='rolled back before the resume was built', next_attempt_at=datetime('now')
WHERE stage IN ('tailor','render');
```

```bash
docker compose up -d      # now on the Plan 3 image
```

6. **Calibration week note.** The tailor prompt is separate from `SCORE_SYSTEM`, so calibration-week rubric changes (above) do not touch it.

7. **Manual re-render of one row** (after fixing a template) — the stored selection is reused, no LLM call:

```sql
UPDATE evaluations SET stage='render', attempts=0, pdf_path=NULL, page_overflow=0,
       resume_error=NULL, next_attempt_at=datetime('now') WHERE id = <id>;
```

8. **Re-scoring a row (after a CV or rubric change) must clear every downstream artifact**, not just `stage='score'` — see "Calibration week" above for why and the exact statement.
