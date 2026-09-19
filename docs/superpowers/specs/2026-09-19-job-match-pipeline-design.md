# Job Match Pipeline — Design

Date: 2026-09-19 (revised after review)
Status: approved in brainstorm, awaiting implementation plan
Depends on: PR #1 (`fix/parser-url-identity`) — corrected URL identity, continuation rows, state migration

## Goal

Extend the internship tracker so that, for each new posting in a user's chosen feeds and sections, it fetches the job description, scores the user's fit against their master CV, and — when the score meets the user's threshold — generates a tailored one-page PDF resume and posts it to the user's Discord webhook alongside the job link.

Supports a small, hand-configured set of users (initially two). No login, no web UI.

An opportunity must never be hidden by a pipeline failure: if any stage exhausts its budget, the user still receives the job link with whatever was produced up to that point. The one accepted failure mode is a duplicate message: if the process crashes after Discord has accepted a send but before the row is marked closed, the message is sent again on restart. That is rare and harmless, and preferable to the alternative of marking rows closed before delivery is confirmed.

## Non-goals (v1)

- Self-serve sign-up, web UI, Authentik auth
- Email delivery
- Headless-browser (Playwright) or agent-based (Hermes) page fetching — added only if measured fetch failures justify it
- Rewriting bullet text — v1 selects and reorders original content only (see Tailor step)
- Notifications for below-threshold matches (near-miss digest)
- Backfilling postings the old tracker missed before PR #1
- Paperclip / Hermes orchestration — the LLM step is a plain HTTP call; an agent can be slotted behind the same interface later

## Decisions made

| Decision | Choice | Why |
|---|---|---|
| LLM runtime | OpenAI-compatible endpoint (existing LiteLLM on home server) | Already running; model swap is a config string |
| Models | Two slots: cheap scorer, stronger tailor (start: DeepSeek V4 Flash / DeepSeek V4 Pro) | Cost is a few dollars/month either way; keep the option to switch |
| Job description fetch | Redirect resolve → ATS JSON APIs → plain GET + trafilatura → bounded retries → link-only fallback | Covers server-rendered ATS hosts without a browser; failures still notify |
| Master CV format | YAML with stable IDs on every entry and bullet | Lets code verify the LLM only selected and reordered existing content |
| Tailoring scope | Selection and reordering only; summary fixed | Text checks cannot catch "assisted" becoming "led"; truthfulness must be structural |
| Output format | PDF only, via HTML template + WeasyPrint | No docx or LibreOffice needed |
| Delivery | Discord webhook per user, PDF as attachment | Matches existing setup |
| Users | `users.yaml` in the data dir | Two users; a DB table and UI are not needed |
| Job identity | Unique on `(feed, url_key)` | No URL appears in both feeds today; per-feed uniqueness is the simplest correct model |
| Process shape | One container, one process, poller thread + worker thread, SQLite queue | Polling never blocked by slow LLM calls; persisted stage state gives retries and restart recovery |

## Architecture

```
GitHub READMEs ──▶ poller thread ──▶ SQLite (feeds, jobs, evaluations)
                                          │
                                          ▼
                                    worker thread
        fetch description ─▶ score ─▶ tailor ─▶ render PDF ─▶ deliver
        (each stage persisted; each stage has its own retry budget and fallback)
```

### Poller thread

Every `POLL_INTERVAL_SECONDS`, for each configured feed:

1. Get the latest commit SHA for the feed's repo/branch (existing `github_client`).
2. If unchanged from `feeds.last_sha`, skip.
3. Fetch the README at that SHA, parse with `parser` (as fixed in PR #1) into sections → rows.
4. In **one transaction**: insert rows whose `(feed, url_key)` is not already in `jobs`; for each inserted job create one `evaluations` row per user whose `feeds` includes this feed and whose `sections` matches the job's section (substring match, as today); update `feeds.last_sha`. If the process dies mid-poll nothing is written and the poll repeats next interval.

First run for a feed (no `last_sha`): insert all rows as seeded — no `evaluations` rows are created — so no notification flood.

Feeds are defined in code:

```python
FEEDS = {
    "internships": ("SimplifyJobs/Summer2026-Internships", "dev"),
    "new-grad":    ("SimplifyJobs/New-Grad-Positions", "dev"),
}
```

The New-Grad README uses the same `## ` headings and `<tr><td>` table format (verified against the live feed); it differs in marking closed postings with 🔒 in the Application cell, which PR #1 handles.

### Worker thread

Loop forever, one item at a time:

1. Pick the oldest job with `fetch_status=pending`, `next_attempt_at <= now`, and at least one pending evaluation. Run the fetcher; persist the outcome. Jobs nobody subscribes to are never fetched.
2. Otherwise pick the oldest evaluation whose `stage` names a runnable action (`next_attempt_at <= now`, job fetch resolved, and neither the LLM service nor the user's Discord destination is paused). Run that one action; persist its output; set `stage` to the next action.
3. Otherwise sleep a few seconds.

`stage` always names the **next action to perform**, never the last one completed, so a restart needs no inference: a row at `stage=deliver` is delivered with whatever the row already holds, whether that is a PDF or only a score. Every action's output is written to the row before `stage` advances, so a restart never repeats an LLM call or a render that already succeeded.

Before an evaluation's first score call, the user's master CV is copied onto the row (`cv_snapshot`). Scoring retries, tailoring and rendering all read the snapshot, never the live file, so editing a CV between stages cannot orphan selected IDs or mix a score from one CV with a resume from another. An edit takes effect for evaluations created after the restart.

### Paused services and destinations

The worker keeps an in-memory map `paused: {name: resume_at | None}`:

- `llm` — set with a timestamp when the LLM endpoint refuses connections or returns 401/403; the worker probes again after the backoff. Rows waiting on the LLM are skipped, not failed, and `attempts` is not incremented.
- `discord:<user_id>` — set to `None` (suspended until restart) when the webhook returns 404 or 401; Discord's guidance is to stop using such a webhook. Set with a timestamp for 5xx/connection errors. While a destination is paused **every** row for that user waits at `stage=deliver`, so a broken webhook is hit once, not once per row.

The map is cleared on restart, which is also when `users.yaml` is reloaded, so correcting a webhook and restarting is the whole recovery procedure. Each pause is logged once at ERROR when it starts.

## Data model

SQLite at `$DATA_DIR/tracker.db`, SQLAlchemy.

```
feeds        id, name (unique), repo, branch, last_sha

jobs         id, feed_id, url_key, company, role, location, url, section,
             description (full extracted text, null until fetched),
             description_truncated (bool),
             fetch_status (pending | ok | failed), fetch_host, fetch_error,
             fetch_attempts, fetch_first_attempt_at, next_attempt_at, created_at
             unique (feed_id, url_key)

evaluations  id, job_id, user_id,
             stage (score | tailor | render | deliver | closed),
             outcome (null | matched | below_threshold | fetch_failed |
                      score_failed | tailor_failed | render_failed),
             cv_snapshot (json, the master CV as loaded when scoring began),
             score, reasoning, missing_confirmed (json), missing_unknown (json),
             tailored (json, validated selection), pdf_path, page_overflow (bool),
             attempts, next_attempt_at, last_error,
             delivery_attempts, delivery_error, updated_at
             unique (job_id, user_id)
```

`stage` is the next action to run. `outcome` is written once, at the moment a fallback decision is made (for example `tailor_failed` when the tailor budget runs out and the row moves to `stage=deliver`), and is never overwritten — so the delivered message and the database both say why the resume is missing. Delivery has its own attempt counter and error column so a failed send never disturbs the outcome or the stage budget.

A row reaches `closed` in exactly two ways: Discord confirms the final message (match, match-without-PDF, or link-only fallback), or the score is below the user's threshold (`outcome=below_threshold`, nothing sent). A crash between deciding to notify and Discord confirming leaves the row at `stage=deliver` and it is retried.

### Migration from current state files

PR #1 leaves `$DATA_DIR/known_urls.json` (version-2 keys) and `last_sha.txt`. On first boot with an empty `jobs` table:

- Insert every key in `known_urls.json` as a seeded job on the `internships` feed (no evaluations, `fetch_status=ok`, empty description).
- Copy `last_sha.txt` into `feeds.last_sha` for `internships`.

The next poll then diffs from that SHA, so postings added between the old tracker's last poll and the deploy are still notified. The `new-grad` feed has no prior state and seeds on its first poll. Before the import, `migrate_state` from PR #1 runs (and retries until it succeeds) so a deployment that skipped PR #1's image still gets version-2 keys. A data directory with no old state files at all is a fresh install and needs no import.

## Users

`$DATA_DIR/users.yaml`:

```yaml
- id: ron
  cv: /data/cvs/ron.yaml
  discord_webhook: https://discord.com/api/webhooks/...
  feeds: [internships, new-grad]
  sections: [software engineering, data science]
  threshold: 60
- id: cousin
  cv: /data/cvs/cousin.yaml
  discord_webhook: https://discord.com/api/webhooks/...
  feeds: [new-grad]
  sections: [software engineering]
  threshold: 65
```

Loaded and validated (Pydantic) at startup. A change requires a restart.

## Error classification and retries

Every external call is classified before deciding what to do:

| Class | Examples | Handling |
|---|---|---|
| Transient | timeout, connection error, 5xx, 429 | Retry with exponential backoff (30 s → 1 min → 5 min → 15 min → 1 h), honoring `Retry-After` when present. Counts against the stage's budget. |
| Service unavailable | LLM endpoint refuses connections or returns 401/403 | Pause that service: log at ERROR once, set `next_attempt_at` 15 min ahead on the affected row, **do not** increment `attempts`. Queued evaluations wait for the service to come back. |
| Item invalid | LLM response fails schema validation (after the client's single in-call re-ask), PDF render throws on this input | Counts as one failed attempt against the stage budget. |
| Item gone | ATS API 404 for this posting | Not an error for the stage: fall through to the next fetch strategy. |
| Destination gone | Discord webhook returns 404 or 401 | Pause `discord:<user_id>` until restart (see Paused services). Does not count against delivery attempts. |

Attempt accounting: one stage run = one attempt, whatever happens inside it. The LLM client's re-ask on a malformed response happens inside that single attempt and is not counted separately. Stage budgets below are in attempts.

Per-stage budgets and fallbacks:

| Stage | Budget | On exhaustion |
|---|---|---|
| Fetch description | 5 attempts or 24 h elapsed since first attempt, whichever first | `fetch_status=failed`; every pending evaluation for the job → deliver link-only message, then `closed` / `fetch_failed` |
| Score | 3 attempts | deliver link-only message ("couldn't score"), `closed` / `score_failed` |
| Tailor | 3 attempts | deliver score + link, no PDF, `closed` / `tailor_failed` |
| Render | 3 attempts | deliver score + link, no PDF, `closed` / `render_failed` |
| Deliver | 10 attempts (`delivery_attempts`), honoring Discord `Retry-After` | row stays at `stage=deliver` with `delivery_error` set and a 1 h `next_attempt_at`; the existing PDF is reused on every retry; logged at ERROR |

Delivery failures never discard the PDF, the score or the outcome: the row keeps everything and retries the send with the same content.

## Fetcher

`fetcher.fetch_description(url) -> FetchResult(text: str | None, host: str, error: str | None)`

1. Resolve redirects with `requests` (`simplify.jobs` wrapper links redirect to the real ATS URL). Record the final host.
2. If the final URL matches a known ATS pattern, call its public JSON API:
   - Greenhouse: `boards.greenhouse.io/<board>/jobs/<id>` **and** `job-boards.greenhouse.io/<board>/jobs/<id>` (the latter is five times more common in the live feeds) → `boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>`, field `content` (HTML-escaped; unescape and strip tags). Greenhouse embeds (`?gh_jid=<id>` on a company domain) → same API, board slug from the `boards.greenhouse.io` link or the page's embed script; if the board cannot be determined, fall through.
   - Lever: `jobs.lever.co/<company>/<uuid>` → `api.lever.co/v0/postings/<company>/<uuid>`; description is `descriptionPlain` plus each entry in `lists[]` (requirements often live only in the lists).
   - Ashby: `jobs.ashbyhq.com/<company>/<uuid>` → the public job-board API returns every posting for the board; select the one whose `jobUrl` contains the UUID. Field names verified against Ashby's public API docs during planning.
   A 404 from any of these falls through to step 3.
3. Otherwise plain GET and `trafilatura.extract()`. A result under 300 characters is treated as a failure (JavaScript shell page).
4. Store the full extracted text. When handing it to the LLM, cap at 12,000 characters and set `description_truncated` if the cap applied.

Politeness: one fetch at a time, 2 s pause between fetches, 15 s timeout, browser-like User-Agent.

Every fetch logs one line with host and outcome. Before deciding on a headless browser, measure — for the subscribed sections only — how many fetched descriptions actually contain a requirements section, not merely non-empty text. Workday, Oracle Cloud, TikTok and SmartRecruiters together outnumber Greenhouse + Lever + Ashby in the live feeds; SmartRecruiters has a documented Posting API and is the first candidate for an additional handler.

## Master CV schema

`cvs/<user>.yaml`, validated by a Pydantic model at load. Every entry and bullet carries a unique `id`.

```yaml
name: Ronak Patel
contact: {email: ..., phone: ..., linkedin: ..., github: ...}
summary: "..."                       # rendered as written; the LLM does not touch it
education:
  - id: edu1
    school: ...
    degree: ...
    dates: ...
    details: ["..."]
experience:
  - id: exp1
    company: ...
    title: ...
    dates: ...
    location: ...
    bullets:
      - {id: exp1.b1, text: "..."}
      - {id: exp1.b2, text: "..."}
projects:
  - id: proj1
    name: ...
    tech: [...]
    link: ...
    bullets:
      - {id: proj1.b1, text: "..."}
skills:
  languages: [...]
  frameworks: [...]
  tools: [...]
```

The YAML is the source of truth from now on; the old master docx/pdf is not read by the system.

## LLM client

`llm.py` talks to an OpenAI-compatible chat endpoint. Config: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_SCORE_MODEL`, `LLM_TAILOR_MODEL`. Requests use JSON mode; responses are validated with Pydantic; one retry on a parse failure, then the step fails with class "item invalid".

### Score step

Input: job description text (capped) and the CV snapshot rendered as plain text. The score measures **demonstrated** fit: how well what the CV shows matches what the posting asks for. A requirement the CV does not address either way (work authorisation, graduation year, security clearance, a technology never mentioned) is reported as unknown and does not lower the score — the user judges those, the model cannot. The prompt carries a fixed rubric so scores are comparable across jobs:

- 90–100: demonstrates every stated hard requirement and most preferred ones
- 70–89: demonstrates the hard requirements; some preferred ones not shown
- 50–69: one hard requirement confirmed missing, otherwise a fit
- below 50: multiple hard requirements confirmed missing, or the role is a different discipline

Output:

```json
{
  "score": 0-100,
  "reasoning": "two or three sentences",
  "missing_confirmed": ["requirement the CV clearly does not meet", "..."],
  "missing_unknown":   ["requirement the CV does not mention either way", "..."]
}
```

`missing_confirmed` is what the CV shows the user lacks; `missing_unknown` is what the CV is silent on. Both are stored and both appear in the Discord message. Score below the user's threshold → `closed` / `below_threshold`, no notification.

Before the worker is wired to real users, run the score prompt over a labelled sample (about 20 real postings the user has hand-rated as apply / maybe / skip, deliberately including several whose only gaps are unknowns) and adjust the rubric or threshold until the ordering agrees. This is a planning task, not a code path.

### Tailor step (score ≥ threshold)

Input: job description and the CV snapshot including IDs. Output is a selection, never text:

```json
{
  "experience": [{"id": "exp1", "bullets": ["exp1.b2", "exp1.b1"]}],
  "projects":   [{"id": "proj3", "bullets": ["proj3.b1"]}],
  "skills": {"languages": [...], "frameworks": [...], "tools": [...]}
}
```

Validation in code before rendering:

- Every entry ID must exist in the master CV; unknown entries are dropped with a warning.
- Every bullet ID must exist **and belong to the entry it is listed under**; a bullet listed under the wrong employer is dropped.
- Each skills list must be a subset of the master list; extras are dropped.
- Education and summary are always rendered in full from the master; the LLM does not control them.
- At most `MAX_BULLETS_PER_ENTRY` (default 4) bullets per entry; extras beyond the limit are dropped in the order given.
- If validation leaves fewer than two experience or project entries, fall back to the master CV's own order, capped to the bullet limit, and log a warning.

The validated selection is stored on the evaluation (`tailored`) so a re-render after a crash needs no LLM call. Validation and rendering resolve IDs against `cv_snapshot`, never the live YAML. Rephrasing bullets is deferred to v2 and, when added, is delivered as a draft for review rather than as a finished resume.

### Renderer

`render.py`: CV snapshot + validated selection → Jinja2 HTML template → WeasyPrint → `$DATA_DIR/output/<user>/<job_id>-<company>.pdf`. One HTML template and one CSS file shared by all users, styled to resemble the current CV layout.

One page is the target, enforced by content limits rather than trimming loops: the bullet cap and a cap on rendered entries (default 4 experience + 3 projects, the LLM's order deciding which survive). After rendering, check the page count; if it is still more than one page, keep the PDF, set `page_overflow=true`, and mention it in the Discord message. The user decides whether to trim by hand.

## Delivery

Per-user Discord webhook. Contract:

- Every send uses `?wait=true`. Success is HTTP 200 with a message object in the body; only then does the row advance to `closed`. The default `wait=false` returns 204 before the message is stored and can hide failures, so the current `send_notification` (which accepts 204) is replaced.
- The PDF, when present, is attached as a multipart file.
- Message content is capped at 2,000 characters. The gaps and not-on-CV lists are truncated first (with an ellipsis), then the reasoning, so an oversized list never turns into a permanently rejected request.
- 429 is retried after `Retry-After`. 5xx and connection errors are transient. 404 and 401 pause the destination until restart.

Match with PDF:

```
🎯 82% — Company — Role
📍 Location
🔗 https://...
✅ Why: <reasoning>
⚠️ Gaps: <missing_confirmed, comma-separated>
❓ Not on CV: <missing_unknown, comma-separated>
(📄 Resume ran over one page — trim before sending)   ← only when page_overflow
```

Match without PDF (tailor or render budget exhausted): same message, plus `⚠️ Couldn't generate resume — apply with your master CV.`

Fetch or score failed (no score, no PDF):

```
🆕 Company — Role (couldn't read the description)
📍 Location
🔗 https://...
```

Nothing is sent for below-threshold scores.

## Configuration

`.env`:

```
DATA_DIR=/data
POLL_INTERVAL_SECONDS=300
GITHUB_TOKEN=
LLM_BASE_URL=http://litellm:4000/v1
LLM_API_KEY=
LLM_SCORE_MODEL=deepseek-v4-flash
LLM_TAILOR_MODEL=deepseek-v4-pro
MAX_BULLETS_PER_ENTRY=4
```

`DISCORD_WEBHOOK_URL` and `FILTER_SECTIONS` are removed; both now live per user in `users.yaml`.

## Module layout

```
src/
  main.py            entrypoint: load config + users, run migration, start poller and worker threads
  config.py          env parsing, FEEDS
  db.py              SQLAlchemy models, session, migration from known_urls.json + last_sha.txt
  users.py           users.yaml loading + validation
  poller.py          feed polling → jobs/evaluations (one transaction per feed per poll)
  worker.py          stage loop, error classification, budgets, fallbacks
  github_client.py   unchanged
  parser.py          unchanged (fixed in PR #1)
  fetcher.py         redirect resolve, ATS handlers, trafilatura fallback
  cv.py              master CV schema, selection validation
  llm.py             OpenAI-compatible client, score and tailor prompts, rubric
  render.py          HTML template → PDF, page-count check
  discord_client.py  extended: attachment support, message variants, Retry-After
  templates/resume.html, resume.css
```

## Testing

pytest, matching the existing test style (mocked `requests`, temp dirs, in-memory SQLite). Required coverage:

- `parser`: fixture from the New-Grad README parses sections and rows (already in PR #1)
- `fetcher`: each ATS handler with mocked responses, including `job-boards.greenhouse.io` and Lever `lists[]`; redirect resolution; ATS 404 falls through to plain GET; trafilatura fallback; short result treated as failure; truncation flag set at 12k
- `llm`: mocked endpoint; valid JSON parsed; malformed JSON retried once then classed invalid; 401 classed service-unavailable; 429 with `Retry-After` classed transient with the given delay
- `cv` validation: unknown entry dropped; bullet under wrong entry dropped; foreign skill dropped; education and summary untouched; bullet cap enforced; too-few-entries fallback
- `render`: produces a valid PDF; entry caps applied; two-page output sets `page_overflow`
- `discord_client`: `wait=true` and 200-with-body required for success, 204 not accepted; multipart with attachment; 429 honored; 404 classed destination-gone; content capped at 2,000 characters with lists truncated first; each message variant
- `poller`: new rows inserted with evaluations for matching users only; seeded first run creates no evaluations; a failure mid-poll leaves no partial rows and does not advance `last_sha`; same `url_key` in two feeds yields two jobs
- `worker`: end to end with all externals mocked: job → score → tailor → render → deliver → closed; restart after each stage resumes at the recorded `stage` without repeating the previous action; fetch budget exhausted → link-only sent → closed/fetch_failed; tailor fails three times → `outcome=tailor_failed`, `stage=deliver`, score+link sent; fallback delivery fails → restart → only delivery is retried and `outcome` is unchanged; delivery fails → PDF kept and resent on retry; LLM 401 pauses without consuming attempts; webhook 404 pauses every row for that user and other users keep flowing; CV edited on disk mid-evaluation → tailor and render still use `cv_snapshot`; below-threshold closes without any send; row is not `closed` until Discord confirms the fallback message
- `db` migration: `known_urls.json` + `last_sha.txt` imported as seeded jobs with the SHA preserved; import skipped when `jobs` is non-empty; empty data dir starts clean

## Deployment

Dockerfile stays on `python:3.12-slim` and adds WeasyPrint's system dependencies (pango, cairo, gdk-pixbuf). CI adds a smoke test that renders a sample PDF inside the built image so font/library gaps are caught before deploy. Compose joins the Docker network LiteLLM is on so `LLM_BASE_URL` resolves by container name. The data directory now holds `users.yaml`, `cvs/`, `output/`, and `tracker.db`; `.gitignore` already excludes `data/*` (PR #1).

## Build order

1. Ingestion and identity — done in PR #1.
2. Durable queue: `db`, `users`, `poller`, `worker` with stage persistence, error classes, budgets, and link-only fallbacks. Deliverable: link-only notifications flowing for both users from both feeds, nothing lost across restarts.
3. Description fetching with per-host measurement.
4. Scoring, with the labelled-sample calibration before enabling threshold suppression.
5. Tailoring, rendering, PDF delivery.

Each step is deployable on its own; the pipeline degrades to the previous step's behaviour when a later stage is absent or failing.

## Open items for planning

- Verify the exact Greenhouse (including `gh_jid` embed board discovery), Lever and Ashby response shapes against current docs before writing mocks.
- Draft the score and tailor prompts and check DeepSeek Flash/Pro output against the JSON schemas with a real CV and a handful of real job descriptions.
- Assemble the ~20-posting labelled sample for score calibration.
- Decide the extraction path for the initial CV → YAML conversion (LLM-assisted draft, then hand-corrected).
