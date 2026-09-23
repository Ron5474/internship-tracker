# scripts/

On-demand diagnostics. Nothing here is started by the daemon; everything runs through
`docker compose exec`, inside the container, where the LLM endpoint and `tracker.db` are both
reachable and the `.env` is already loaded.

## ab_score.py — does this change move the scores?

Re-scores evaluations that already have a score, so each row carries its own ground truth.
Reads the database read-only.

```bash
cd ~/deployed-projects/internship-tracker
docker compose exec -T internship-tracker python3 /app/scripts/ab_score.py \
  --limit 8 --variant '{}' --variant '{"reasoning_effort":"none"}'
```

Output is one row per evaluation — stored score beside each variant's score — then a summary
line per variant giving mean and maximum drift, how many postings crossed the 60-point
threshold, average latency, and average reasoning tokens.

**Always include `--variant '{}'`.** That re-runs the request unchanged and shows how much the
scores move on their own between two identical calls. A variant that drifts less than that
baseline has not been shown to change anything.

Useful flags:

- `--ids 165,171,177,183` — re-score specific evaluations rather than the most recent ones.
  Pick a mix you have opinions about: some that matched correctly and some that did not.
- `--model <alias>` — try a different model without touching `.env`.
- `--limit N` — how many recent evaluations (default 8). Each variant costs one call per row,
  so `--limit 8` with two variants is 16 calls.

### Reading the result

What matters is not whether individual numbers move — they will — but whether they move
*across your threshold*. A posting going 85 → 82 changes nothing you would do. A posting
going 85 → 55, or 40 → 70, changes which jobs reach you. The flip count is the number to
watch, and the `{}` row tells you how many flips are just noise.
