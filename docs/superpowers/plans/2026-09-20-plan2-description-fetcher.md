# Plan 2: Job Description Fetcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before a posting is delivered, fetch its job description text — via the ATS's public JSON API where one exists, otherwise by extracting the page — store it on the job, measure per-host success, and fall back to the existing link-only message when the description cannot be read.

**Architecture:** A new `fetcher.py` turns an apply URL into `FetchResult(text | None, host, strategy, kind, error)`. The worker gains a `fetch` action that runs ahead of `deliver`: it picks the oldest job with `fetch_status=pending` that at least one open evaluation is waiting on, runs the fetcher, persists the result, and on permanent failure or exhausted budget marks the job failed and stamps `outcome=fetch_failed` on its evaluations. `deliver` is gated on the job's fetch being resolved. Nothing else in the pipeline changes: this plan still delivers link-only messages; Plan 3 will consume `job.description`.

**Tech Stack:** Python 3.12, requests, trafilatura 2.2.0 (HTML → text fallback), SQLAlchemy 2.0, pytest. Verified live on 2026-09-20: Greenhouse, Lever, Ashby, SmartRecruiters and Workday all answer plain unauthenticated GETs with JSON containing the description.

**Spec:** `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md` — sections "Worker thread", "Error classification and retries" (Fetch row), "Fetcher", "Build order" step 3. Deviation from spec, ruled during planning: Workday and SmartRecruiters get API handlers now (they were "later" in the spec) because both were verified to work with a plain GET; no headless browser is introduced.

## Global Constraints

- All datetimes naive UTC via `db.utcnow()` / the worker's injected `now()`; never `datetime.now()` in production code.
- Fetch error classes: `ok` | `transient` (timeout, connection error, 5xx, 429) | `permanent` (403, 404, 401, non-JSON where JSON was expected, extracted text under `MIN_DESCRIPTION_CHARS = 300`). Transient retries use the worker's existing `backoff()` schedule (`30, 60, 300, 900, 3600`). Budget: `FETCH_BUDGET = 5` attempts **or** `FETCH_MAX_AGE_HOURS = 24` since `fetch_first_attempt_at`, whichever first. Permanent fails immediately.
- On fetch failure (permanent or budget exhausted): `job.fetch_status = "failed"`, `job.fetch_error` set; every evaluation of that job with `stage != closed` and `outcome is None` gets `outcome = "fetch_failed"` (stage unchanged) — the existing `message_for` already renders the "(couldn't read the description)" note.
- `deliver` runs only for evaluations whose job has `fetch_status in ("ok", "failed")`. Jobs nobody is waiting on are never fetched.
- Description handling: store the **full** extracted text in `job.description`; set `job.description_truncated = len(text) > DESCRIPTION_CAP` (`DESCRIPTION_CAP = 12000`). Capping happens when the text is handed to the LLM (Plan 3), not here.
- Politeness: one fetch at a time (the worker is single-threaded), `FETCH_GAP_SECONDS = 2` between the end of one fetch and the start of the next, `FETCH_TIMEOUT = 15` seconds per HTTP call, browser-like User-Agent `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36`.
- Every fetch attempt logs exactly one INFO line: `fetch job=<id> host=<host> strategy=<strategy> outcome=<kind> chars=<n> requirements=<yes|no> [error=<...>]`.
- Schema: two new `jobs` columns, `fetch_strategy` (String, nullable) and `has_requirements` (Boolean, nullable). Existing databases get them via `db.ensure_columns()` at startup — `create_all` does not add columns to existing tables.
- `requirements=yes` heuristic: text matches `re.compile(r"\b(requirements?|qualifications?|what you.ll need|what we.re looking for|must[- ]haves?|minimum|basic qualifications|you have|you bring)\b", re.I)`.
- Tests import bare module names (`from fetcher import ...`, `patch("fetcher.requests.get")`). Mock HTTP at `fetcher.requests.get`; never hit the network in tests.
- Commit after every task. Do not push.

---

## File structure

| File | Responsibility |
|---|---|
| `src/fetcher.py` (new) | URL → `FetchResult`. Handler matching, ATS API calls, HTML → text, trafilatura fallback, error classification, requirements heuristic. Knows nothing about the DB. |
| `src/db.py` (modify) | `Job.fetch_strategy`, `Job.has_requirements`; `ensure_columns(engine)`. |
| `src/worker.py` (modify) | `fetch` action, gating of `deliver`, politeness gap, fetch budget, `outcome=fetch_failed` stamping. |
| `src/main.py` (modify) | call `ensure_columns` after `init_db`. |
| `requirements.txt` (modify) | add `trafilatura==2.2.0`. |
| `docs/ops.md` (new) | the two SQL queries that measure fetch success per host and per section. |
| `tests/test_fetcher.py` (new), `tests/test_db.py`, `tests/test_worker.py`, `tests/test_main.py` (modify) | Tests. |

Fetch strategies (the `strategy` label written to `job.fetch_strategy` and the log line): `greenhouse`, `greenhouse-embed`, `lever`, `ashby`, `smartrecruiters`, `workday`, `page` (trafilatura), `none` (no handler matched and the page could not be fetched/extracted).

Verified endpoint shapes (2026-09-20):

| ATS | Apply URL pattern | API | Description field |
|---|---|---|---|
| Greenhouse | `https://(job-)?boards.greenhouse.io/<board>/jobs/<id>` | `GET https://boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>` | `content` — HTML, **HTML-escaped** (`&lt;p&gt;`), unescape then strip |
| Greenhouse embed | any host, `?gh_jid=<id>`; board slug is in the page: `boards.greenhouse.io/embed/job_board/js?for=<board>` or `job_app?for=<board>` | same API | same |
| Lever | `https://jobs.lever.co/<company>/<uuid>[/apply]` | `GET https://api.lever.co/v0/postings/<company>/<uuid>` | `descriptionPlain` + each `lists[i].text` heading with `lists[i].content` (HTML) + `additionalPlain` |
| Ashby | `https://jobs.ashbyhq.com/<company>/<uuid>[/application]` | `GET https://api.ashbyhq.com/posting-api/job-board/<company>` → `jobs[]`; pick the one whose `jobUrl` contains `<uuid>` | `descriptionPlain` |
| SmartRecruiters | `https://jobs.smartrecruiters.com/<company>/<id>` | `GET https://api.smartrecruiters.com/v1/companies/<company>/postings/<id>` | `jobAd.sections.{jobDescription,qualifications,additionalInformation}.text` — HTML |
| Workday | `https://<tenant>.<wdN>.myworkdayjobs.com/[<ll-CC>/]<site>/job/<path...>` | `GET https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/job/<path...>` (locale segment dropped) | `jobPostingInfo.jobDescription` — HTML |

---

### Task 1: Schema additions and `ensure_columns`

**Files:**
- Modify: `src/db.py`
- Modify: `src/main.py:23-33` (`build`)
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Produces: `Job.fetch_strategy: str | None`, `Job.has_requirements: bool | None`; `db.ensure_columns(engine) -> list[str]` (returns the `table.column` names it added, empty when nothing was missing).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
from sqlalchemy import inspect, text

from db import ensure_columns


def test_job_has_fetch_strategy_and_has_requirements(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    session.add_all([feed, job])
    session.commit()
    assert job.fetch_strategy is None
    assert job.has_requirements is None
    job.fetch_strategy = "lever"
    job.has_requirements = True
    session.commit()
    assert session.get(Job, job.id).fetch_strategy == "lever"


def test_ensure_columns_adds_missing_columns_to_existing_db(tmp_path):
    from db import init_db, make_engine
    engine = make_engine(str(tmp_path / "old.db"))
    init_db(engine)
    # Simulate a database created before this plan: drop the two new columns.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN fetch_strategy"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN has_requirements"))
    assert "fetch_strategy" not in {c["name"] for c in inspect(engine).get_columns("jobs")}

    added = ensure_columns(engine)

    assert sorted(added) == ["jobs.fetch_strategy", "jobs.has_requirements"]
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"fetch_strategy", "has_requirements"} <= cols


def test_ensure_columns_is_noop_when_current(tmp_path):
    from db import init_db, make_engine
    engine = make_engine(str(tmp_path / "new.db"))
    init_db(engine)
    assert ensure_columns(engine) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_db.py -v -k "fetch_strategy or ensure_columns"`
Expected: FAIL — `ImportError: cannot import name 'ensure_columns' from 'db'`. (`ALTER TABLE … DROP COLUMN` needs SQLite ≥ 3.35; `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"` — Ubuntu 24.04 ships 3.45.)

- [ ] **Step 3: Implement**

In `src/db.py`, add to `Job` after `fetch_error`:

```python
    fetch_strategy: Mapped[str | None] = mapped_column(String, nullable=True)
    has_requirements: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
```

Add `from sqlalchemy import inspect, text` to the imports, and append:

```python
def ensure_columns(engine: Engine) -> list[str]:
    """Add columns that exist in the models but not in an existing database.

    create_all() only creates missing tables. Each plan that adds a column
    relies on this to upgrade a data dir that predates it. SQLite supports
    ADD COLUMN for nullable columns without a rebuild, which is all we need.
    """
    added: list[str] = []
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
            with engine.begin() as conn:
                conn.execute(text(ddl))
            added.append(f"{table.name}.{column.name}")
    return added
```

In `src/main.py` `build()`, right after `init_db(engine)`:

```python
    added = ensure_columns(engine)
    if added:
        log.info("Schema upgraded: added %s", ", ".join(added))
```

and add `ensure_columns` to the `from db import ...` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_db.py tests/test_main.py -v`
Expected: all pass (14 in test_db, 3 in test_main).

- [ ] **Step 5: Commit**

```bash
git add src/db.py src/main.py tests/test_db.py
git commit -m "feat: fetch_strategy/has_requirements columns and ensure_columns schema upgrade"
```

---

### Task 2: `fetcher.py` — pure helpers (HTML → text, URL matching, classification)

**Files:**
- Create: `src/fetcher.py`
- Create: `tests/test_fetcher.py`

**Interfaces:**
- Produces:
  - `fetcher.FetchResult(text: str | None, host: str, strategy: str, kind: str, error: str | None)` frozen dataclass; `kind in {"ok", "transient", "permanent"}`; property `ok`.
  - `fetcher.html_to_text(html: str) -> str` — unescape entities, turn block-level closers and `<br>` into newlines, strip tags, collapse blank runs.
  - `fetcher.has_requirements(text: str) -> bool`
  - `fetcher.match_ats(url: str) -> tuple[str, dict] | None` — `("greenhouse", {"board", "job_id"})`, `("lever", {"company", "uuid"})`, `("ashby", {"company", "uuid"})`, `("smartrecruiters", {"company", "posting_id"})`, `("workday", {"tenant", "wd", "site", "path"})`, or `None`.
  - `fetcher.classify_status(status: int) -> str` — `"ok"` for 2xx, `"transient"` for 429/5xx, `"permanent"` otherwise.
  - Constants: `MIN_DESCRIPTION_CHARS = 300`, `DESCRIPTION_CAP = 12000`, `FETCH_TIMEOUT = 15`, `USER_AGENT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fetcher.py`:

```python
from fetcher import (
    MIN_DESCRIPTION_CHARS,
    FetchResult,
    classify_status,
    has_requirements,
    html_to_text,
    match_ats,
)


# --- html_to_text ------------------------------------------------------------

def test_html_to_text_strips_tags_and_unescapes():
    assert html_to_text("<p>Hello &amp; <b>world</b></p>") == "Hello & world"


def test_html_to_text_turns_blocks_into_newlines():
    html = "<h3>About</h3><p>One</p><ul><li>a</li><li>b</li></ul><p>Two<br>Three</p>"
    assert html_to_text(html) == "About\nOne\na\nb\nTwo\nThree"


def test_html_to_text_collapses_blank_runs_and_whitespace():
    assert html_to_text("<p>  a  </p>\n\n\n<p></p><p>b</p>") == "a\nb"


def test_html_to_text_handles_double_escaped_greenhouse_content():
    # Greenhouse returns HTML that is itself entity-escaped.
    assert html_to_text("&lt;p&gt;Role &amp;amp; team&lt;/p&gt;") == "Role & team"


# --- has_requirements --------------------------------------------------------

def test_has_requirements_true_on_common_headings():
    assert has_requirements("About us\n\nQualifications\n- Python")
    assert has_requirements("What you'll need: 2 years")
    assert has_requirements("Basic Qualifications")


def test_has_requirements_false_without_keywords():
    assert not has_requirements("We are a fun company. Apply now.")


# --- match_ats ---------------------------------------------------------------

def test_match_greenhouse_both_hosts():
    assert match_ats("https://job-boards.greenhouse.io/togetherai/jobs/5211582007?utm_source=Simplify") == (
        "greenhouse", {"board": "togetherai", "job_id": "5211582007"})
    assert match_ats("https://boards.greenhouse.io/acme/jobs/123") == ("greenhouse", {"board": "acme", "job_id": "123"})


def test_match_lever_with_and_without_apply_suffix():
    u = "https://jobs.lever.co/steerbridge/718b3135-d15d-4cbc-9541-1cbb8a6f5ec5/apply?ref=Simplify"
    assert match_ats(u) == ("lever", {"company": "steerbridge", "uuid": "718b3135-d15d-4cbc-9541-1cbb8a6f5ec5"})
    assert match_ats("https://jobs.lever.co/weride/5a7cbc83-2381-482e-9d6d-e9c9d59ad63b")[1]["uuid"] == "5a7cbc83-2381-482e-9d6d-e9c9d59ad63b"


def test_match_ashby():
    u = "https://jobs.ashbyhq.com/meow/56e3b840-11a0-4e98-baca-44e8e26b5218/application?embed=true"
    assert match_ats(u) == ("ashby", {"company": "meow", "uuid": "56e3b840-11a0-4e98-baca-44e8e26b5218"})


def test_match_smartrecruiters():
    assert match_ats("https://jobs.smartrecruiters.com/GDMSI/744000145530335?utm_source=Simplify") == (
        "smartrecruiters", {"company": "GDMSI", "posting_id": "744000145530335"})


def test_match_workday_with_and_without_locale():
    u1 = "https://toyota.wd503.myworkdayjobs.com/tmna/job/Plano-Texas/Software-Engineer_10325071?utm_source=Simplify"
    assert match_ats(u1) == ("workday", {"tenant": "toyota", "wd": "wd503", "site": "tmna",
                                         "path": "Plano-Texas/Software-Engineer_10325071"})
    u2 = "https://tms.wd3.myworkdayjobs.com/en-US/perseus-careers/job/Sharon-PA/Software-Engineer-I_R54341"
    assert match_ats(u2)[1] == {"tenant": "tms", "wd": "wd3", "site": "perseus-careers",
                                "path": "Sharon-PA/Software-Engineer-I_R54341"}


def test_match_returns_none_for_unknown_host():
    assert match_ats("https://careers.amd.com/careers-home/jobs/123") is None
    assert match_ats("https://stripe.com/jobs/search?gh_jid=8212508") is None  # embed handled elsewhere


# --- classify_status / FetchResult ------------------------------------------

def test_classify_status():
    assert classify_status(200) == "ok"
    assert classify_status(429) == "transient"
    assert classify_status(503) == "transient"
    assert classify_status(403) == "permanent"
    assert classify_status(404) == "permanent"


def test_fetch_result_ok_property():
    assert FetchResult("x" * MIN_DESCRIPTION_CHARS, "h", "lever", "ok", None).ok
    assert not FetchResult(None, "h", "none", "permanent", "404").ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_fetcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fetcher'`

- [ ] **Step 3: Implement the helpers in `src/fetcher.py`**

```python
import html as html_lib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
import trafilatura

log = logging.getLogger(__name__)

MIN_DESCRIPTION_CHARS = 300
DESCRIPTION_CAP = 12000
FETCH_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HEADERS_HTML = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
_HEADERS_JSON = {"User-Agent": USER_AGENT, "Accept": "application/json"}


@dataclass(frozen=True)
class FetchResult:
    text: str | None
    host: str
    strategy: str
    kind: str  # "ok" | "transient" | "permanent"
    error: str | None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


# --- text helpers ------------------------------------------------------------

_BLOCK_CLOSE = re.compile(r"</(p|div|li|h[1-6]|tr|ul|ol|section|article|blockquote)\s*>|<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_REQUIREMENTS = re.compile(
    r"\b(requirements?|qualifications?|what you.ll need|what we.re looking for|must[- ]haves?|minimum"
    r"|basic qualifications|you have|you bring)\b",
    re.I,
)


def html_to_text(html: str) -> str:
    """Rough HTML → plain text: entities unescaped (twice — Greenhouse double-escapes),
    block closers and <br> become newlines, tags dropped, whitespace normalised."""
    # Unescape twice: Greenhouse returns HTML that is itself entity-escaped, and a
    # second pass over already-plain text is a no-op.
    s = html_lib.unescape(html_lib.unescape(html))
    s = _BLOCK_CLOSE.sub("\n", s)
    s = _TAG.sub("", s)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def has_requirements(text: str) -> bool:
    return bool(_REQUIREMENTS.search(text))


def classify_status(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status == 429 or status >= 500:
        return "transient"
    return "permanent"


# --- URL matching ------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PATTERNS = [
    ("greenhouse", re.compile(r"^https?://(?:job-)?boards\.greenhouse\.io/(?P<board>[^/?#]+)/jobs/(?P<job_id>\d+)")),
    ("lever", re.compile(rf"^https?://jobs\.lever\.co/(?P<company>[^/?#]+)/(?P<uuid>{_UUID})")),
    ("ashby", re.compile(rf"^https?://jobs\.ashbyhq\.com/(?P<company>[^/?#]+)/(?P<uuid>{_UUID})")),
    ("smartrecruiters", re.compile(r"^https?://jobs\.smartrecruiters\.com/(?P<company>[^/?#]+)/(?P<posting_id>\d+)")),
    ("workday", re.compile(
        r"^https?://(?P<tenant>[^./]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
        r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)/job/(?P<path>[^?#]+)")),
]


def match_ats(url: str) -> tuple[str, dict] | None:
    for name, pattern in _PATTERNS:
        m = pattern.match(url)
        if m:
            return name, m.groupdict()
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_fetcher.py -v`
Expected: 14 passed.

- [ ] **Step 5: Add the dependency and commit**

Add `trafilatura==2.2.0` to `requirements.txt`, run `pip3 install -r requirements.txt` (`--break-system-packages` if pip insists), then:

```bash
git add src/fetcher.py tests/test_fetcher.py requirements.txt
git commit -m "feat: fetcher helpers — html_to_text, ATS URL matching, status classification"
```

---

### Task 3: ATS API handlers

**Files:**
- Modify: `src/fetcher.py`
- Modify: `tests/test_fetcher.py` (append)

**Interfaces:**
- Produces: `fetcher.fetch_via_api(name: str, params: dict) -> FetchResult` for `name in {"greenhouse", "lever", "ashby", "smartrecruiters", "workday"}`; internal `_get_json(url) -> tuple[str, dict | None, str | None]` returning `(kind, body, error)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetcher.py`:

```python
from unittest.mock import Mock, patch

import requests

from fetcher import fetch_via_api

LONG = "Responsibilities: build things. " * 20  # > MIN_DESCRIPTION_CHARS


def _resp(status, body=None, text=""):
    r = Mock()
    r.status_code = status
    r.headers = {}
    r.text = text
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


def test_greenhouse_handler_unescapes_content():
    body = {"content": "&lt;h3&gt;Qualifications&lt;/h3&gt;&lt;p&gt;" + LONG + "&lt;/p&gt;"}
    with patch("fetcher.requests.get", return_value=_resp(200, body)) as get:
        r = fetch_via_api("greenhouse", {"board": "togetherai", "job_id": "5211582007"})
    assert get.call_args.args[0] == "https://boards-api.greenhouse.io/v1/boards/togetherai/jobs/5211582007"
    assert r.ok and r.strategy == "greenhouse" and r.host == "boards-api.greenhouse.io"
    assert r.text.startswith("Qualifications\nResponsibilities")


def test_lever_handler_joins_description_lists_and_additional():
    body = {
        "descriptionPlain": LONG,
        "lists": [{"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}],
        "additionalPlain": "EEO statement.",
    }
    with patch("fetcher.requests.get", return_value=_resp(200, body)) as get:
        r = fetch_via_api("lever", {"company": "steerbridge", "uuid": "718b3135-d15d-4cbc-9541-1cbb8a6f5ec5"})
    assert get.call_args.args[0] == "https://api.lever.co/v0/postings/steerbridge/718b3135-d15d-4cbc-9541-1cbb8a6f5ec5"
    assert r.ok
    assert "Requirements\nPython\nSQL" in r.text
    assert r.text.endswith("EEO statement.")


def test_ashby_handler_picks_posting_by_uuid_in_joburl():
    body = {"jobs": [
        {"jobUrl": "https://jobs.ashbyhq.com/meow/other-uuid", "descriptionPlain": "wrong"},
        {"jobUrl": "https://jobs.ashbyhq.com/meow/56e3b840-11a0-4e98-baca-44e8e26b5218", "descriptionPlain": LONG},
    ]}
    with patch("fetcher.requests.get", return_value=_resp(200, body)) as get:
        r = fetch_via_api("ashby", {"company": "meow", "uuid": "56e3b840-11a0-4e98-baca-44e8e26b5218"})
    assert get.call_args.args[0] == "https://api.ashbyhq.com/posting-api/job-board/meow"
    assert r.ok and r.text == LONG.strip()


def test_ashby_handler_permanent_when_uuid_not_on_board():
    body = {"jobs": [{"jobUrl": "https://jobs.ashbyhq.com/meow/other", "descriptionPlain": LONG}]}
    with patch("fetcher.requests.get", return_value=_resp(200, body)):
        r = fetch_via_api("ashby", {"company": "meow", "uuid": "56e3b840-11a0-4e98-baca-44e8e26b5218"})
    assert r.kind == "permanent" and "not on board" in r.error


def test_smartrecruiters_handler_joins_sections_in_order():
    body = {"jobAd": {"sections": {
        "companyDescription": {"text": "<p>About us</p>"},
        "jobDescription": {"text": "<p>" + LONG + "</p>"},
        "qualifications": {"text": "<ul><li>Degree</li></ul>"},
        "additionalInformation": {"text": "<p>Extra</p>"},
    }}}
    with patch("fetcher.requests.get", return_value=_resp(200, body)) as get:
        r = fetch_via_api("smartrecruiters", {"company": "GDMSI", "posting_id": "744000145530335"})
    assert get.call_args.args[0] == "https://api.smartrecruiters.com/v1/companies/GDMSI/postings/744000145530335"
    assert r.ok
    assert r.text.index("Responsibilities") < r.text.index("Degree") < r.text.index("Extra")
    assert "About us" not in r.text


def test_workday_handler_builds_cxs_url_and_reads_job_description():
    body = {"jobPostingInfo": {"jobDescription": "<p><b>Overview</b></p><p>" + LONG + "</p>"}}
    with patch("fetcher.requests.get", return_value=_resp(200, body)) as get:
        r = fetch_via_api("workday", {"tenant": "toyota", "wd": "wd503", "site": "tmna",
                                      "path": "Plano-Texas/Software-Engineer_10325071"})
    assert get.call_args.args[0] == (
        "https://toyota.wd503.myworkdayjobs.com/wday/cxs/toyota/tmna/job/Plano-Texas/Software-Engineer_10325071")
    assert r.ok and r.strategy == "workday" and r.host == "toyota.wd503.myworkdayjobs.com"
    assert r.text.startswith("Overview\nResponsibilities")


def test_api_404_is_permanent():
    with patch("fetcher.requests.get", return_value=_resp(404, None, "nope")):
        r = fetch_via_api("greenhouse", {"board": "x", "job_id": "1"})
    assert r.kind == "permanent" and "404" in r.error and r.text is None


def test_api_503_is_transient():
    with patch("fetcher.requests.get", return_value=_resp(503, None)):
        assert fetch_via_api("lever", {"company": "x", "uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12}).kind == "transient"


def test_api_timeout_is_transient():
    with patch("fetcher.requests.get", side_effect=requests.Timeout("slow")):
        r = fetch_via_api("workday", {"tenant": "t", "wd": "wd1", "site": "s", "path": "p"})
    assert r.kind == "transient" and "slow" in r.error


def test_api_non_json_200_is_permanent():
    with patch("fetcher.requests.get", return_value=_resp(200, None, "<html>login</html>")):
        assert fetch_via_api("smartrecruiters", {"company": "x", "posting_id": "1"}).kind == "permanent"


def test_api_short_description_is_permanent():
    with patch("fetcher.requests.get", return_value=_resp(200, {"descriptionPlain": "Short.", "lists": []})):
        r = fetch_via_api("lever", {"company": "x", "uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12})
    assert r.kind == "permanent" and "too short" in r.error


def test_api_calls_use_timeout_and_user_agent():
    with patch("fetcher.requests.get", return_value=_resp(200, {"content": LONG})) as get:
        fetch_via_api("greenhouse", {"board": "b", "job_id": "1"})
    assert get.call_args.kwargs["timeout"] == 15
    assert "Mozilla" in get.call_args.kwargs["headers"]["User-Agent"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_fetcher.py -v -k "handler or api_"`
Expected: FAIL — `ImportError: cannot import name 'fetch_via_api' from 'fetcher'`

- [ ] **Step 3: Implement the handlers**

Append to `src/fetcher.py`:

```python
# --- ATS API handlers --------------------------------------------------------

def _get_json(url: str) -> tuple[str, dict | None, str | None]:
    """GET a JSON endpoint. Returns (kind, body, error)."""
    try:
        resp = requests.get(url, headers=_HEADERS_JSON, timeout=FETCH_TIMEOUT)
    except requests.RequestException as e:
        return "transient", None, f"{type(e).__name__}: {e}"
    kind = classify_status(resp.status_code)
    if kind != "ok":
        return kind, None, f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return "permanent", None, "non-JSON response"
    if not isinstance(body, dict):
        return "permanent", None, "unexpected JSON shape"
    return "ok", body, None


def _finish(text: str, host: str, strategy: str) -> FetchResult:
    text = text.strip()
    if len(text) < MIN_DESCRIPTION_CHARS:
        return FetchResult(None, host, strategy, "permanent", f"too short ({len(text)} chars)")
    return FetchResult(text, host, strategy, "ok", None)


def _greenhouse(board: str, job_id: str, strategy: str = "greenhouse") -> FetchResult:
    host = "boards-api.greenhouse.io"
    kind, body, err = _get_json(f"https://{host}/v1/boards/{board}/jobs/{job_id}")
    if kind != "ok":
        return FetchResult(None, host, strategy, kind, err)
    return _finish(html_to_text(body.get("content", "")), host, strategy)


def _lever(company: str, uuid: str) -> FetchResult:
    host = "api.lever.co"
    kind, body, err = _get_json(f"https://{host}/v0/postings/{company}/{uuid}")
    if kind != "ok":
        return FetchResult(None, host, "lever", kind, err)
    parts = [body.get("descriptionPlain", "")]
    for section in body.get("lists", []) or []:
        parts.append(section.get("text", ""))
        parts.append(html_to_text(section.get("content", "")))
    parts.append(body.get("additionalPlain", ""))
    return _finish("\n".join(p for p in parts if p), host, "lever")


def _ashby(company: str, uuid: str) -> FetchResult:
    host = "api.ashbyhq.com"
    kind, body, err = _get_json(f"https://{host}/posting-api/job-board/{company}")
    if kind != "ok":
        return FetchResult(None, host, "ashby", kind, err)
    for job in body.get("jobs", []) or []:
        if uuid in (job.get("jobUrl") or ""):
            return _finish(job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", "")), host, "ashby")
    return FetchResult(None, host, "ashby", "permanent", f"posting {uuid} not on board {company}")


def _smartrecruiters(company: str, posting_id: str) -> FetchResult:
    host = "api.smartrecruiters.com"
    kind, body, err = _get_json(f"https://{host}/v1/companies/{company}/postings/{posting_id}")
    if kind != "ok":
        return FetchResult(None, host, "smartrecruiters", kind, err)
    sections = (body.get("jobAd") or {}).get("sections") or {}
    parts = [html_to_text((sections.get(k) or {}).get("text", ""))
             for k in ("jobDescription", "qualifications", "additionalInformation")]
    return _finish("\n".join(p for p in parts if p), host, "smartrecruiters")


def _workday(tenant: str, wd: str, site: str, path: str) -> FetchResult:
    host = f"{tenant}.{wd}.myworkdayjobs.com"
    kind, body, err = _get_json(f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}")
    if kind != "ok":
        return FetchResult(None, host, "workday", kind, err)
    return _finish(html_to_text((body.get("jobPostingInfo") or {}).get("jobDescription", "")), host, "workday")


_HANDLERS = {
    "greenhouse": lambda p: _greenhouse(p["board"], p["job_id"]),
    "lever": lambda p: _lever(p["company"], p["uuid"]),
    "ashby": lambda p: _ashby(p["company"], p["uuid"]),
    "smartrecruiters": lambda p: _smartrecruiters(p["company"], p["posting_id"]),
    "workday": lambda p: _workday(p["tenant"], p["wd"], p["site"], p["path"]),
}


def fetch_via_api(name: str, params: dict) -> FetchResult:
    return _HANDLERS[name](params)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_fetcher.py -v`
Expected: 26 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fetcher.py tests/test_fetcher.py
git commit -m "feat: Greenhouse, Lever, Ashby, SmartRecruiters and Workday description handlers"
```

---

### Task 4: `fetch_description` — orchestration, Greenhouse embeds, page fallback

**Files:**
- Modify: `src/fetcher.py`
- Modify: `tests/test_fetcher.py` (append)

**Interfaces:**
- Produces: `fetcher.fetch_description(url: str) -> FetchResult` — the only function the worker calls. Order: `match_ats(url)` → handler; else GET the page (follow redirects); if the final URL matches an ATS → handler; else if the page embeds a Greenhouse board (`?gh_jid=` in the URL or `greenhouse.io/embed/job_board` in the HTML) → `greenhouse-embed` handler; else `trafilatura.extract` → `page`. Page GET failures are classified by status; extracted text under `MIN_DESCRIPTION_CHARS` is `permanent` with strategy `page`; a page that could not be fetched at all reports strategy `none`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetcher.py`:

```python
from fetcher import fetch_description

PAGE_HTML = "<html><body><main><h1>Software Engineer</h1><h2>Qualifications</h2><p>" + LONG + "</p></main></body></html>"


def _page(status=200, text=PAGE_HTML, url="https://careers.example.com/job/1"):
    r = _resp(status, None, text)
    r.url = url
    return r


def test_fetch_description_uses_api_handler_without_fetching_page():
    with patch("fetcher.requests.get", return_value=_resp(200, {"content": LONG})) as get:
        r = fetch_description("https://job-boards.greenhouse.io/togetherai/jobs/5211582007?utm_source=Simplify")
    assert r.ok and r.strategy == "greenhouse"
    assert get.call_count == 1
    assert "boards-api.greenhouse.io" in get.call_args.args[0]


def test_fetch_description_falls_back_to_trafilatura_for_unknown_host():
    with patch("fetcher.requests.get", return_value=_page()) as get, \
         patch("fetcher.trafilatura.extract", return_value="Qualifications\n" + LONG) as extract:
        r = fetch_description("https://careers.example.com/job/1?utm_source=Simplify")
    assert r.ok and r.strategy == "page" and r.host == "careers.example.com"
    assert get.call_args.kwargs["allow_redirects"] is True
    assert get.call_args.kwargs["timeout"] == 15
    extract.assert_called_once()


def test_fetch_description_short_page_text_is_permanent():
    with patch("fetcher.requests.get", return_value=_page()), \
         patch("fetcher.trafilatura.extract", return_value="Apply now."):
        r = fetch_description("https://careers.example.com/job/1")
    assert r.kind == "permanent" and r.strategy == "page" and "too short" in r.error


def test_fetch_description_none_from_trafilatura_is_permanent():
    with patch("fetcher.requests.get", return_value=_page()), \
         patch("fetcher.trafilatura.extract", return_value=None):
        r = fetch_description("https://careers.example.com/job/1")
    assert r.kind == "permanent" and r.strategy == "page"


def test_fetch_description_page_403_is_permanent_strategy_none():
    with patch("fetcher.requests.get", return_value=_page(403, "")):
        r = fetch_description("https://careers.example.com/job/1")
    assert r.kind == "permanent" and r.strategy == "none" and "403" in r.error


def test_fetch_description_page_timeout_is_transient():
    with patch("fetcher.requests.get", side_effect=requests.ConnectionError("dns")):
        r = fetch_description("https://careers.example.com/job/1")
    assert r.kind == "transient" and r.strategy == "none" and r.host == "careers.example.com"


def test_fetch_description_redirect_to_ats_uses_handler():
    # A wrapper URL redirects to Lever; the page GET reveals the final URL, then the API is used.
    page = _page(url="https://jobs.lever.co/acme/718b3135-d15d-4cbc-9541-1cbb8a6f5ec5")
    api = _resp(200, {"descriptionPlain": LONG, "lists": []})
    with patch("fetcher.requests.get", side_effect=[page, api]) as get:
        r = fetch_description("https://apply.acme.com/go/123")
    assert r.ok and r.strategy == "lever"
    assert get.call_count == 2


def test_fetch_description_greenhouse_embed_discovers_board_from_page():
    html = '<html><script src="https://boards.greenhouse.io/embed/job_board/js?for=stripe"></script></html>'
    page = _page(text=html, url="https://stripe.com/jobs/search?gh_jid=8212508")
    api = _resp(200, {"content": "&lt;p&gt;" + LONG + "&lt;/p&gt;"})
    with patch("fetcher.requests.get", side_effect=[page, api]) as get:
        r = fetch_description("https://stripe.com/jobs/search?gh_jid=8212508&utm_source=Simplify")
    assert r.ok and r.strategy == "greenhouse-embed"
    assert get.call_args.args[0] == "https://boards-api.greenhouse.io/v1/boards/stripe/jobs/8212508"


def test_fetch_description_greenhouse_embed_without_board_falls_back_to_page():
    page = _page(text=PAGE_HTML, url="https://stripe.com/jobs/search?gh_jid=8212508")
    with patch("fetcher.requests.get", return_value=page), \
         patch("fetcher.trafilatura.extract", return_value="Qualifications\n" + LONG):
        r = fetch_description("https://stripe.com/jobs/search?gh_jid=8212508")
    assert r.ok and r.strategy == "page"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_fetcher.py -v -k fetch_description`
Expected: FAIL — `ImportError: cannot import name 'fetch_description' from 'fetcher'`

- [ ] **Step 3: Implement**

Append to `src/fetcher.py`:

```python
# --- orchestration -----------------------------------------------------------

_GH_EMBED_BOARD = re.compile(r"greenhouse\.io/embed/(?:job_board|job_app)[^\"']*?[?&]for=([A-Za-z0-9_-]+)")
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")


def fetch_description(url: str) -> FetchResult:
    """Apply URL → description text. API handlers first, page extraction last."""
    matched = match_ats(url)
    if matched:
        return fetch_via_api(*matched)

    host = urlparse(url).netloc
    try:
        resp = requests.get(url, headers=_HEADERS_HTML, timeout=FETCH_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return FetchResult(None, host, "none", "transient", f"{type(e).__name__}: {e}")
    kind = classify_status(resp.status_code)
    if kind != "ok":
        return FetchResult(None, host, "none", kind, f"HTTP {resp.status_code}")

    final_url = resp.url or url
    final_host = urlparse(final_url).netloc or host
    matched = match_ats(final_url)
    if matched:
        return fetch_via_api(*matched)

    jid = _GH_JID.search(final_url) or _GH_JID.search(url)
    board = _GH_EMBED_BOARD.search(resp.text or "")
    if jid and board:
        return _greenhouse(board.group(1), jid.group(1), strategy="greenhouse-embed")

    extracted = trafilatura.extract(resp.text or "", include_comments=False, include_tables=True)
    if not extracted:
        return FetchResult(None, final_host, "page", "permanent", "no extractable text")
    return _finish(extracted, final_host, "page")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_fetcher.py -v`
Expected: 35 passed.

- [ ] **Step 5: Live spot-check (manual, not committed)**

```bash
PYTHONPATH=src python3 -c "
from fetcher import fetch_description as f
for u in ['https://job-boards.greenhouse.io/togetherai/jobs/5211582007',
          'https://jobs.lever.co/steerbridge/718b3135-d15d-4cbc-9541-1cbb8a6f5ec5/apply',
          'https://jobs.ashbyhq.com/meow/56e3b840-11a0-4e98-baca-44e8e26b5218/application',
          'https://jobs.smartrecruiters.com/GDMSI/744000145530335',
          'https://toyota.wd503.myworkdayjobs.com/tmna/job/Plano-Texas/Software-Engineer--Early-Career-Professional-_10325071']:
    r = f(u); print(r.strategy, r.kind, r.host, len(r.text or ''), r.error)
"
```
Expected: five lines, each `<strategy> ok <host> <n> None` with n in the thousands. (Postings close over time; a `permanent HTTP 404` on one line is the posting, not the code — pick a fresh URL from the live README.)

- [ ] **Step 6: Commit**

```bash
git add src/fetcher.py tests/test_fetcher.py
git commit -m "feat: fetch_description orchestration with Greenhouse embed discovery and page fallback"
```

---

### Task 5: Worker `fetch` action, delivery gating, budget, politeness

**Files:**
- Modify: `src/worker.py`
- Modify: `tests/test_worker.py` (append)

**Interfaces:**
- Consumes: `fetcher.fetch_description`, `fetcher.DESCRIPTION_CAP`, `fetcher.has_requirements`, `db.Job`, `db.FETCH_*`.
- Produces:
  - `worker.FETCH_BUDGET = 5`, `worker.FETCH_MAX_AGE_HOURS = 24`, `worker.FETCH_GAP_SECONDS = 2`
  - `Worker.__init__(..., fetch=fetch_description, ...)` — injectable like `send`
  - `Worker.run_once()` now tries `fetch` before `deliver`
  - `Worker.fetch(session, job) -> None`
  - `Worker._next_fetchable(session) -> Job | None`
  - `Worker._next_deliverable` additionally requires `job.fetch_status != "pending"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker.py`:

```python
from db import FETCH_FAILED, FETCH_OK, FETCH_PENDING
from fetcher import FetchResult
from worker import FETCH_BUDGET, FETCH_GAP_SECONDS, FETCH_MAX_AGE_HOURS

FETCH_OK_RESULT = FetchResult("Qualifications\n" + "x" * 400, "api.lever.co", "lever", "ok", None)
FETCH_TRANSIENT = FetchResult(None, "api.lever.co", "lever", "transient", "HTTP 503")
FETCH_PERMANENT = FetchResult(None, "careers.x.com", "page", "permanent", "HTTP 404")


class FakeFetcher:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self.results.pop(0) if self.results else FETCH_OK_RESULT


def _worker_f(session_factory, fetcher, clock, sender=None, users=(RON, COUSIN)):
    return Worker(session_factory, list(users), send=sender or FakeSender(), fetch=fetcher, now=clock)


def test_new_jobs_are_pending_fetch_and_not_delivered_yet(session_factory, session, clock):
    ev = _seed(session)
    assert ev.job.fetch_status == FETCH_PENDING
    sender = FakeSender()
    w = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT), clock, sender)
    w.run_once()                              # fetch attempt, transient
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert sender.calls == []                 # gated on fetch


def test_fetch_ok_stores_description_and_then_delivers(session_factory, session, clock):
    ev = _seed(session)
    fetcher = FakeFetcher(FETCH_OK_RESULT)
    sender = FakeSender(OK)
    w = _worker_f(session_factory, fetcher, clock, sender)
    assert w.run_once() is True               # fetch
    session.refresh(ev); session.refresh(ev.job)
    job = ev.job
    assert job.fetch_status == FETCH_OK
    assert job.description.startswith("Qualifications")
    assert job.description_truncated is False
    assert job.fetch_host == "api.lever.co"
    assert job.fetch_strategy == "lever"
    assert job.has_requirements is True
    assert job.fetch_attempts == 1
    assert fetcher.calls == [job.url]
    clock.advance(FETCH_GAP_SECONDS)
    assert w.run_once() is True               # deliver
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED and ev.outcome is None
    assert "couldn't read" not in sender.calls[0][1]


def test_fetch_marks_truncated_when_over_cap(session_factory, session, clock):
    from fetcher import DESCRIPTION_CAP
    ev = _seed(session)
    big = FetchResult("Requirements " + "y" * (DESCRIPTION_CAP + 10), "h", "page", "ok", None)
    w = _worker_f(session_factory, FakeFetcher(big), clock)
    w.run_once()
    session.refresh(ev.job)
    assert ev.job.description_truncated is True
    assert len(ev.job.description) > DESCRIPTION_CAP     # full text kept


def test_fetch_transient_backs_off_and_records_first_attempt(session_factory, session, clock):
    ev = _seed(session)
    w = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT), clock)
    w.run_once()
    session.refresh(ev.job)
    job = ev.job
    assert job.fetch_status == FETCH_PENDING
    assert job.fetch_attempts == 1
    assert job.fetch_first_attempt_at == T0
    assert job.fetch_error == "HTTP 503"
    assert job.next_attempt_at == T0 + timedelta(seconds=30)


def test_fetch_permanent_fails_job_and_stamps_evaluations(session_factory, session, clock):
    ev_ron = _seed(session, "ron")
    ev_cousin = _seed(session, "cousin")     # different job; untouched
    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock)
    w.run_once()
    session.refresh(ev_ron); session.refresh(ev_ron.job); session.refresh(ev_cousin)
    assert ev_ron.job.fetch_status == FETCH_FAILED
    assert ev_ron.job.fetch_error == "HTTP 404"
    assert ev_ron.outcome == "fetch_failed" and ev_ron.stage == STAGE_DELIVER
    assert ev_cousin.outcome is None


def test_fetch_failed_delivers_link_only_with_note(session_factory, session, clock):
    ev = _seed(session)
    sender = FakeSender(OK)
    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock, sender)
    w.run_once()                              # fetch → failed
    clock.advance(FETCH_GAP_SECONDS)
    w.run_once()                              # deliver
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED and ev.outcome == "fetch_failed"
    assert "(couldn't read the description)" in sender.calls[0][1]


def test_fetch_budget_exhausted_by_attempts(session_factory, session, clock):
    ev = _seed(session)
    w = _worker_f(session_factory, FakeFetcher(*[FETCH_TRANSIENT] * FETCH_BUDGET), clock)
    for _ in range(FETCH_BUDGET):
        assert w.run_once() is True
        clock.advance(3600)
    session.refresh(ev); session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_FAILED
    assert ev.job.fetch_attempts == FETCH_BUDGET
    assert "budget" in ev.job.fetch_error
    assert ev.outcome == "fetch_failed"


def test_fetch_budget_exhausted_by_age(session_factory, session, clock):
    ev = _seed(session)
    w = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT, FETCH_TRANSIENT), clock)
    w.run_once()                              # attempt 1 at T0
    clock.advance(FETCH_MAX_AGE_HOURS * 3600 + 1)
    w.run_once()                              # attempt 2, now older than the age limit
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_FAILED
    assert ev.job.fetch_attempts == 2


def test_fetch_skips_jobs_nobody_is_waiting_on(session_factory, session, clock):
    ev = _seed(session, stage=STAGE_CLOSED)   # only evaluation already closed
    fetcher = FakeFetcher()
    w = _worker_f(session_factory, fetcher, clock)
    assert w.run_once() is False
    assert fetcher.calls == []
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_PENDING


def test_fetch_respects_gap_between_fetches(session_factory, session, clock):
    _seed(session, "ron")
    _seed(session, "cousin")                  # two jobs pending
    fetcher = FakeFetcher(FETCH_OK_RESULT, FETCH_OK_RESULT)
    w = _worker_f(session_factory, fetcher, clock)
    assert w.run_once() is True               # fetch #1
    assert w.run_once() is True               # deliver #1 (fetch #2 must wait for the gap)
    assert len(fetcher.calls) == 1
    clock.advance(FETCH_GAP_SECONDS)
    assert w.run_once() is True               # fetch #2
    assert len(fetcher.calls) == 2


def test_fetcher_exception_is_a_transient_attempt(session_factory, session, clock):
    ev = _seed(session)

    def boom(url):
        raise RuntimeError("parser exploded")

    w = _worker_f(session_factory, boom, clock)
    w.run_once()
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_PENDING
    assert ev.job.fetch_attempts == 1
    assert "RuntimeError" in ev.job.fetch_error


def test_restart_resumes_pending_fetch(session_factory, session, clock):
    ev = _seed(session)
    w1 = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT), clock)
    w1.run_once()
    clock.advance(30)
    w2 = _worker_f(session_factory, FakeFetcher(FETCH_OK_RESULT), clock)   # restart
    assert w2.run_once() is True
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_OK and ev.job.fetch_attempts == 2
```

Two edits to the existing helpers in `tests/test_worker.py`:

1. Jobs now go through the worker's clock too, and the model default `next_attempt_at=utcnow()` is real time (later than `T0`), so `_next_fetchable` would never pick them. In both `_seed` and `_seed_second_ron`, add `next_attempt_at=T0` to the `Job(...)` constructor arguments.
2. Every pre-existing deliver-stage test assumes its job is already fetch-resolved. Add this helper and wrap the `_seed(...)` calls in those tests (`test_run_once_delivers_and_closes` through `test_restart_resumes_pending_delivery`, and inside `_seed_second_ron`) as `_resolve(session, _seed(...))`. The new fetch tests in this task use plain `_seed(...)` and start from `fetch_status=pending`.

```python
def _resolve(session, ev):
    ev.job.fetch_status = FETCH_OK
    session.commit()
    return ev
```


- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_worker.py -v`
Expected: the new tests FAIL with `ImportError: cannot import name 'FETCH_BUDGET' from 'worker'`; after adding the `_resolve` wrapper the old tests still pass once the implementation lands.

- [ ] **Step 3: Implement**

In `src/worker.py`:

Imports — add:
```python
from db import FETCH_FAILED, FETCH_OK, FETCH_PENDING, STAGE_CLOSED, STAGE_DELIVER, Evaluation, Job, utcnow
from fetcher import DESCRIPTION_CAP, FetchResult, fetch_description, has_requirements
```
(replace the existing `from db import ...` line.)

Constants — add after `DELIVERY_BUDGET`:
```python
FETCH_BUDGET = 5
FETCH_MAX_AGE_HOURS = 24
FETCH_GAP_SECONDS = 2
```

`__init__` — add a `fetch` parameter and a politeness timestamp:
```python
    def __init__(
        self,
        session_factory: sessionmaker,
        users: list[User],
        send: Callable[..., DeliveryResult] = send_message,
        fetch: Callable[[str], FetchResult] = fetch_description,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        self._send = send
        self._fetch = fetch
        self._now = now
        self._fetch_not_before: datetime | None = None
        self.paused: dict[str, datetime | None] = {}
```

`run_once` — fetch before deliver:
```python
    def run_once(self) -> bool:
        with self._sessions() as session:
            job = self._next_fetchable(session)
            if job is not None:
                self.fetch(session, job)
                session.commit()
                return True
            ev = self._next_deliverable(session)
            if ev is None:
                return False
            self.deliver(session, ev)
            session.commit()
            return True

    def _next_fetchable(self, session: Session) -> Job | None:
        now = self._now()
        if self._fetch_not_before is not None and now < self._fetch_not_before:
            return None
        waiting = session.query(Evaluation.job_id).filter(Evaluation.stage != STAGE_CLOSED)
        return (
            session.query(Job)
            .filter(Job.fetch_status == FETCH_PENDING, Job.next_attempt_at <= now, Job.id.in_(waiting))
            .order_by(Job.next_attempt_at, Job.id)
            .first()
        )
```

`_next_deliverable` — gate on fetch resolution (add the join and filter):
```python
        candidates = (
            session.query(Evaluation)
            .join(Job)
            .filter(
                Evaluation.stage == STAGE_DELIVER,
                Evaluation.next_attempt_at <= now,
                Job.fetch_status != FETCH_PENDING,
            )
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
```

New `fetch` action (place before `deliver`):
```python
    # -- fetch stage --------------------------------------------------------

    def fetch(self, session: Session, job: Job) -> None:
        now = self._now()
        if job.fetch_first_attempt_at is None:
            job.fetch_first_attempt_at = now
        job.fetch_attempts += 1
        try:
            result = self._fetch(job.url)
        except Exception as e:  # noqa: BLE001 — a fetcher bug is a failed attempt, not a dead worker
            log.exception("Fetcher raised for job %d", job.id)
            result = FetchResult(None, job.fetch_host or "", "none", "transient", f"{type(e).__name__}: {e}")
        self._fetch_not_before = self._now() + timedelta(seconds=FETCH_GAP_SECONDS)

        job.fetch_host = result.host
        job.fetch_strategy = result.strategy
        chars = len(result.text or "")
        reqs = has_requirements(result.text) if result.text else False
        log.info("fetch job=%d host=%s strategy=%s outcome=%s chars=%d requirements=%s%s",
                 job.id, result.host, result.strategy, result.kind, chars, "yes" if reqs else "no",
                 f" error={result.error}" if result.error else "")

        if result.ok:
            job.description = result.text
            job.description_truncated = chars > DESCRIPTION_CAP
            job.has_requirements = reqs
            job.fetch_status = FETCH_OK
            job.fetch_error = None
            return

        job.fetch_error = result.error
        age = now - job.fetch_first_attempt_at
        over_budget = job.fetch_attempts >= FETCH_BUDGET or age >= timedelta(hours=FETCH_MAX_AGE_HOURS)
        if result.kind == "permanent" or over_budget:
            if result.kind != "permanent":
                job.fetch_error = f"budget exhausted after {job.fetch_attempts} attempts: {result.error}"
            self._fail_fetch(session, job)
            return
        job.next_attempt_at = now + timedelta(seconds=backoff(job.fetch_attempts))

    def _fail_fetch(self, session: Session, job: Job) -> None:
        job.fetch_status = FETCH_FAILED
        for ev in job.evaluations:
            if ev.stage != STAGE_CLOSED and ev.outcome is None:
                ev.outcome = "fetch_failed"
        log.warning("Job %d (%s — %s) description unavailable: %s", job.id, job.company, job.role, job.fetch_error)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_worker.py -v` then `python3 -m pytest -q -W error`
Expected: all pass (worker: 12 new + existing), whole suite green, no warnings.

- [ ] **Step 5: Commit**

```bash
git add src/worker.py tests/test_worker.py
git commit -m "feat: worker fetch stage with budget, politeness gap and fetch_failed fallback"
```

---

### Task 6: End-to-end test, ops queries, deploy

**Files:**
- Modify: `tests/test_main.py` (append)
- Create: `docs/ops.md`
- Modify: `Dockerfile` (verify only), `.github/workflows/docker-publish.yml` (no change expected)

- [ ] **Step 1: Write the failing e2e test**

Append to `tests/test_main.py` (reuse the helpers already in that file — `_readme`, `Settings`, `User`, the `patch` targets; if the earlier test defines its own inline README strings, copy the two-row variant):

```python
def test_poll_fetch_deliver_end_to_end(tmp_path):
    from fetcher import FetchResult
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok")
    users = [User(id="ron", cv="/x", discord_webhook="https://d/ron", feeds=["internships"],
                  sections=["software engineering"])]
    session_factory, worker = main.build(settings, users)
    worker._fetch = lambda url: FetchResult("Qualifications\n" + "z" * 400, "api.lever.co", "lever", "ok", None)

    with patch("main.get_latest_sha", return_value="s1"), \
         patch("main.get_readme_content", side_effect=lambda repo, sha, token=None: README_V1 if "Internships" in repo else ""):
        main.poll_all(session_factory, users, settings)
    with patch("main.get_latest_sha", return_value="s2"), \
         patch("main.get_readme_content", side_effect=lambda repo, sha, token=None: README_V2 if "Internships" in repo else ""):
        main.poll_all(session_factory, users, settings)

    post = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: {"id": "1"}))
    with patch("discord_client.requests.post", post):
        assert worker.run_once() is True      # fetch
        assert post.call_count == 0
        worker._fetch_not_before = None       # skip the politeness gap
        assert worker.run_once() is True      # deliver
    assert post.call_count == 1

    with session_factory() as s:
        ev = s.query(Evaluation).one()
        assert ev.stage == "closed"
        assert ev.job.fetch_status == "ok"
        assert ev.job.fetch_strategy == "lever"
        assert ev.job.has_requirements is True
```

(`README_V1`/`README_V2`: if `tests/test_main.py` does not already define them, import from `tests/test_poller.py` via `from test_poller import README_V1, README_V2` — pytest's `pythonpath` includes `.`, so `tests` is importable as a package because it has `__init__.py`; use `from tests.test_poller import README_V1, README_V2`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_main.py -v -k end_to_end`
Expected: FAIL — the first `run_once()` delivers instead of fetching only if Task 5 is missing; with Task 5 present it should pass immediately. If it passes immediately, that is acceptable here: this test's job is to lock the integration, and each unit it covers had its own RED in Tasks 1–5. Note that in the report.

- [ ] **Step 3: Write `docs/ops.md`**

```markdown
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
```

- [ ] **Step 4: Verify the image still builds with trafilatura**

Run: `docker build -t internship-tracker:local . && docker run --rm internship-tracker:local python -c "import main, trafilatura; print(trafilatura.__version__)"`
Expected: `2.2.0`. `python:3.12-slim` has manylinux wheels for lxml; no apt packages should be needed. If the build fails on lxml, add `RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxslt1.1 && rm -rf /var/lib/apt/lists/*` before the `pip install` line and report it.

- [ ] **Step 5: Full suite, commit**

Run: `python3 -m pytest -q -W error` — all green.

```bash
git add tests/test_main.py docs/ops.md
git commit -m "test: poll → fetch → deliver end to end; ops queries for fetch measurement"
```

---

### Task 7: Deploy checklist (manual)

- [ ] Merge; CI builds and pushes `:latest`.
- [ ] On the server: `docker compose pull && docker compose up -d`, then `docker compose logs --tail 20 internship-tracker`. Expect `Schema upgraded: added jobs.fetch_strategy, jobs.has_requirements` on first boot, then normal poll lines.
- [ ] Jobs that already exist with open evaluations (there should be none after Plan 1 has been delivering) are fetched first; new postings are fetched before delivery, so the first message after deploy may arrive a few seconds later than before.
- [ ] After a few days, run the two queries in `docs/ops.md`; paste the per-host table into the Plan 3 planning notes.

---

## Self-review

**Spec coverage (Build order step 3 + "Fetcher" + fetch row of the retry table):**
- Redirect resolve, ATS handlers (Greenhouse both hosts + embed, Lever with `lists[]`, Ashby by `jobUrl`), plain GET + trafilatura, short-text = failure — Tasks 2–4. SmartRecruiters and Workday added beyond spec (ruled; both verified live).
- Full text stored, truncation flag at 12k — Task 5 (`description_truncated`), cap applied later by Plan 3.
- Politeness (one at a time, 2 s gap, 15 s timeout, UA) — Tasks 2 and 5.
- One log line per fetch with host and outcome; "has requirements" measurement — Task 5 log line + `has_requirements` column + `docs/ops.md`.
- Fetch budget 5 attempts / 24 h, permanent fails immediately, `fetch_failed` outcome + link-only delivery — Task 5. Existing `message_for` renders the note (Plan 1).
- Jobs nobody subscribes to never fetched — `_next_fetchable` joins on open evaluations.
- Deliver gated on fetch resolved — `_next_deliverable` filter.
- Schema upgrade for existing databases — Task 1 `ensure_columns`.
- Not in scope: LLM pause key, scoring — Plan 3.

**Placeholder scan:** none.

**Type consistency:** `FetchResult(text, host, strategy, kind, error)` positional order used identically in Tasks 2–6 and in every test constructor. `Worker(session_factory, users, send=, fetch=, now=)` — `fetch` keyword used by `_worker_f` and the e2e test's `worker._fetch` override. `backoff()` reused for fetch retries. `STAGE_CLOSED`/`FETCH_*` from `db`.

**Known follow-ups:** `fetch_via_api` is looked up by name each call — fine. `_next_fetchable` uses a subquery `IN`; at a few thousand rows this is instant. The Greenhouse double-unescape in `html_to_text` is the one fiddly bit; the Step 4 note in Task 2 gives the fallback.
