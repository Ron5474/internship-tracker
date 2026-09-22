# Plan 3: LLM Fit Scoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every fetched posting against the user's master CV with an LLM (via the LiteLLM endpoint), deliver a rich Discord message for matches at or above the user's threshold, close below-threshold rows silently — with a per-user switch to deliver those too during the calibration week.

**Architecture:** A new `score` action runs between `fetch` and `deliver`. New evaluations start at `stage=score`. The worker snapshots the user's CV onto the row, leases the attempt, calls `llm.score(description, cv_text)` (OpenAI-compatible chat completion in JSON mode, validated with Pydantic, one in-call re-ask), stores score/reasoning/gaps, and moves the row to `deliver` (outcome `matched`, or `below_threshold` when the user opted in) or `closed` (`below_threshold`). LLM outages pause the `llm` service key in the existing pause map without consuming attempts; a 3-attempt budget falls back to link-only delivery with outcome `score_failed`. `deliver` renders a match message when a score exists. Tailoring and PDFs are Plan 4.

**Tech Stack:** Python 3.12, requests (to LiteLLM's `/v1/chat/completions`), Pydantic 2, PyYAML, SQLAlchemy 2.0, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md` — "Worker thread" (cv_snapshot), "Paused services and destinations" (`llm` key), "Error classification and retries" (Score row), "Master CV schema", "LLM client", "Score step", "Delivery" (match message), "Configuration", "Build order" step 4. Corrected after external review before execution: strict score validation (no clamping/coercion), retry deadlines computed after the call returns, budget checked before leasing, shared `llm` cooldown on transient errors, deliver-before-score ordering, `contact.location` kept in the CV text; the equivalent fetch-stage fixes already landed in PR #4. Deviations ruled during planning: (1) no labelled-sample calibration — instead `notify_below_threshold` per user delivers below-threshold rows with their score for a calibration week; (2) link-only note for `score_failed` is "couldn't score"; (3) `demo` is an optional project field in the CV schema.

## Global Constraints

- Stage values after this plan: `score | deliver | closed`. `Evaluation.stage` column default becomes `"score"`; the poller sets it explicitly. Rows already at `deliver` in the live DB are untouched.
- `_fail_fetch` must move open rows at `stage=score` to `stage=deliver` (nothing to score) in addition to stamping `outcome=fetch_failed`.
- `score` runs only when: `stage == score`, `next_attempt_at <= now`, the job's `fetch_status == "ok"`, and `llm` is not paused. Pause key is exactly `"llm"`.
- `ScoreResponse` validation is **strict**: `score` must be a JSON integer 0–100 (booleans, strings, floats and out-of-range values are rejected → re-ask → `invalid`); `reasoning`, `missing_confirmed`, `missing_unknown` are required fields (lists may be empty).
- Before the first LLM call for a row, `cv_snapshot` is set to the user's master CV (a plain dict) and committed. Every later use reads the snapshot, never the file.
- Lease: `attempts += 1` and a provisional `next_attempt_at = now + backoff(attempts)` are committed **before** the LLM call (same pattern as fetch).
- LLM result classes: `ok` | `transient` (timeout, 5xx, 429 — honour `Retry-After`) | `unavailable` (connection refused/reset, 401, 403, 404 — a config or outage problem) | `invalid` (schema validation failed even after the one re-ask, or other 4xx). `unavailable` → undo the lease increment, pause `llm` for `LLM_PAUSE_SECONDS = 900`, set the row's `next_attempt_at` to the resume time. `transient` → count the attempt, back off the row, **and pause `llm` until that row's retry time** (one 429 or timeout means the shared endpoint is unwell; `invalid` is per-row and does not pause). `transient`/`invalid` count against `SCORE_BUDGET = 3`; on exhaustion `outcome = "score_failed"`, `stage = deliver`.
- **Budget is checked before leasing**: a row that already carries `attempts >= SCORE_BUDGET` (e.g. after crashes) goes straight to `score_failed` without another LLM call. (The fetch stage already does this since PR #4.)
- **Retry deadlines are computed from the time the call returns**, not from before it: `after = self._now()` once the LLM/fetch call has come back, and every `next_attempt_at`/pause time is `after + delay`. LLM calls can take the full `LLM_TIMEOUT_SECONDS`.
- `run_once` order: **deliver → fetch → score**. Ready messages never wait behind slow scoring.
- On `ok`: store `score`, `reasoning`, `missing_confirmed`, `missing_unknown`, `score_model`, `score_usage`; `score >= user.threshold` → `outcome = "matched"`, `stage = deliver`; else `outcome = "below_threshold"` and `stage = deliver` if `user.notify_below_threshold` else `closed`.
- Description handed to the LLM is `job.description[:DESCRIPTION_CAP]` (12,000 chars, from `fetcher`).
- Rubric and JSON schema exactly as the spec's "Score step". Temperature 0. `response_format={"type": "json_object"}`.
- Match message (`format_match`): header `🎯 <score>% — **Company** — Role` (`📉` when below threshold), `📍 Location`, `🔗 url`, `✅ Why: <reasoning>`; then list lines `⚠️ Gaps: …` and `❓ Not on CV: …` (omitted when empty); capped by `cap_content` so lists truncate first. Total ≤ 2,000 chars.
- Config (env): `LLM_BASE_URL` (required, e.g. `http://litellm:4000/v1`), `LLM_API_KEY` (optional), `LLM_SCORE_MODEL` (required), `LLM_TIMEOUT_SECONDS` (default 120). Startup fails with a clear message if a required one is missing.
- `users.yaml`: new optional `notify_below_threshold: bool = false`. Each user's `cv` path must load and validate at startup; a bad CV file fails startup naming the path and the error.
- One INFO line per score attempt: `score ev=<id> user=<id> model=<m> outcome=<kind|matched|below_threshold> score=<n|-> ms=<t> tokens=<in>/<out>`.
- New `evaluations` columns (nullable, added by `ensure_columns`): `score_model` (String), `score_usage` (JSON).
- All datetimes via the worker's injected `now()`; tests use bare module imports; no network in tests (`patch("llm.requests.post")`). Worker tests that assert deadlines use a fake LLM/fetcher that **advances the clock during the call** so pre-call vs post-call timing is actually tested.
- Commit after every task. Do not push.

---

## File structure

| File | Responsibility |
|---|---|
| `src/cv.py` (new) | `MasterCV` Pydantic schema (ids unique, bullet ids prefixed by parent), `load_cv(path)`, `cv_to_text(cv)`. |
| `src/prompts.py` (new) | `SCORE_SYSTEM` and `score_user_message(description, cv_text)` — the rubric and JSON contract. |
| `src/llm.py` (new) | `ScoreResponse`, `LLMResult`, `LLMClient(base_url, api_key, model, timeout)` with `.score()`; HTTP + classification + one re-ask. |
| `src/db.py` (modify) | `STAGE_SCORE`, default stage, `score_model`, `score_usage`. |
| `src/config.py` (modify) | LLM settings. |
| `src/users.py` (modify) | `notify_below_threshold`. |
| `src/poller.py` (modify) | explicit `stage=STAGE_SCORE`. |
| `src/discord_client.py` (modify) | `format_match`. |
| `src/worker.py` (modify) | `score` action, `_next_scoreable`, `llm` pause, `message_for` for scored rows, `_fail_fetch` stage move, `cvs`/`llm` constructor seams. |
| `src/main.py` (modify) | build `LLMClient`, load CVs, pass to `Worker`. |
| `.env.example`, `data/users.example.yaml`, `docs/ops.md` (modify) | config + calibration notes + score queries. |
| `tests/test_cv.py`, `tests/test_llm.py` (new); `tests/test_db.py`, `tests/test_config.py`, `tests/test_users.py`, `tests/test_poller.py`, `tests/test_discord_client.py`, `tests/test_worker.py`, `tests/test_main.py`, `tests/fixtures/cv_sample.yaml` (modify/new) | Tests. |

---

### Task 1: Stage `score`, new columns, poller + `_fail_fetch` adjustments

**Files:**
- Modify: `src/db.py`, `src/poller.py`, `src/worker.py`
- Test: `tests/test_db.py`, `tests/test_poller.py`, `tests/test_worker.py`

**Interfaces:**
- Produces: `db.STAGE_SCORE = "score"`; `Evaluation.stage` default `STAGE_SCORE`; `Evaluation.score_model: str | None`, `Evaluation.score_usage: dict | None`. Poller creates evaluations with `stage=STAGE_SCORE`. `_fail_fetch` moves `score` rows to `deliver`.

- [ ] **Step 1: Write the failing tests**

`tests/test_db.py` — append:

```python
from db import STAGE_SCORE


def test_evaluation_defaults_to_score_stage_with_score_columns(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    ev = Evaluation(job=job, user_id="ron")
    session.add_all([feed, job, ev])
    session.commit()
    assert ev.stage == STAGE_SCORE
    assert ev.score_model is None
    assert ev.score_usage is None
```

Update the existing `test_evaluation_defaults` assertion `assert ev.stage == STAGE_DELIVER` → `assert ev.stage == STAGE_SCORE` (and adjust its import).

`tests/test_poller.py` — in `test_new_row_creates_job_and_evaluation_for_matching_user`, change `assert ev.stage == STAGE_DELIVER` to `assert ev.stage == STAGE_SCORE` (import `STAGE_SCORE` from `db`).

`tests/test_worker.py`:
- In `_seed`, make the default stage explicit so the existing deliver-stage tests keep their meaning: change the `Evaluation(...)` line to `Evaluation(job=job, user_id=user_id, **{"next_attempt_at": T0, "stage": STAGE_DELIVER, **ev_kwargs})`. Same in `_seed_second_ron` (`stage=STAGE_DELIVER`).
- Append:

```python
from db import STAGE_SCORE


def test_fail_fetch_moves_score_rows_to_deliver(session_factory, session, clock):
    ev = _seed(session, stage=STAGE_SCORE)
    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "fetch_failed"
    assert ev.stage == STAGE_DELIVER

```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_db.py tests/test_poller.py tests/test_worker.py -q`
Expected: failures — `ImportError: cannot import name 'STAGE_SCORE' from 'db'`.

- [ ] **Step 3: Implement**

`src/db.py`: add `STAGE_SCORE = "score"` next to the other stage constants; change `stage` default to `STAGE_SCORE` and its comment to `# Next action to run: score → deliver → closed (tailor/render arrive in Plan 4).`; add to `Evaluation` after `missing_unknown`:

```python
    score_model: Mapped[str | None] = mapped_column(String, nullable=True)
    score_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

`src/poller.py`: import `STAGE_SCORE`; in `_evaluations_for` construct `Evaluation(job=job, user_id=u.id, stage=STAGE_SCORE)`.

`src/worker.py`: import `STAGE_SCORE`; in `_fail_fetch`:

```python
        for ev in job.evaluations:
            if ev.stage == STAGE_CLOSED:
                continue
            if ev.outcome is None:
                ev.outcome = "fetch_failed"
            if ev.stage == STAGE_SCORE:
                ev.stage = STAGE_DELIVER   # nothing to score; deliver the link
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest -q -W error` — all green. (`test_main.py`'s e2e tests will start failing here because new evaluations no longer start at `deliver`; that is expected until Task 7. Run `python3 -m pytest -q -W error --deselect tests/test_main.py::test_poll_all_then_run_once_delivers_new_posting --deselect tests/test_main.py::test_poll_fetch_deliver_end_to_end` for this task and note it in the report.)

- [ ] **Step 5: Commit**

```bash
git add src/db.py src/poller.py src/worker.py tests/test_db.py tests/test_poller.py tests/test_worker.py
git commit -m "feat: evaluations start at stage=score; score_model/score_usage columns"
```

---

### Task 2: `cv.py` — master CV schema, loader, plain-text rendering

**Files:**
- Create: `src/cv.py`, `tests/fixtures/cv_sample.yaml`
- Test: `tests/test_cv.py`

**Interfaces:**
- Produces: `cv.MasterCV` (Pydantic; fields `name`, `contact`, `summary`, `education`, `experience`, `projects`, `skills`), `cv.load_cv(path: str) -> MasterCV` (raises `ValueError` with the path on any problem), `cv.cv_to_text(cv: MasterCV) -> str`, `cv.all_ids(cv) -> set[str]`.

- [ ] **Step 1: Write the fixture and failing tests**

Create `tests/fixtures/cv_sample.yaml`:

```yaml
name: Test Person
contact:
  email: t@example.com
  phone: "+1 555 0100"
  location: San Jose, CA
  linkedin: https://www.linkedin.com/in/test/
  github: https://github.com/test
summary: null
education:
  - id: edu1
    school: Test University
    degree: BS Computer Science
    dates: 2021 – 2025
    details: ["GPA 3.9"]
experience:
  - id: exp1
    company: Acme
    title: SWE Intern
    dates: 2024
    location: Remote
    bullets:
      - {id: exp1.b1, text: "Built a FastAPI service handling 1k rps."}
      - {id: exp1.b2, text: "Wrote SQLAlchemy migrations."}
projects:
  - id: proj1
    name: Tracker
    dates: Jun 2026
    tech: [Python, SQLite]
    link: https://github.com/test/tracker
    demo: https://tracker.example.com
    bullets:
      - {id: proj1.b1, text: "Polls GitHub and posts to Discord."}
skills:
  languages: [Python, Go]
  frameworks: [FastAPI]
  tools: [Docker, SQLite]
```

Create `tests/test_cv.py`:

```python
from pathlib import Path

import pytest
import yaml

from cv import MasterCV, all_ids, cv_to_text, load_cv

FIXTURE = str(Path(__file__).parent / "fixtures" / "cv_sample.yaml")


def _write(tmp_path, data):
    p = tmp_path / "cv.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return str(p)


def test_load_cv_parses_fixture():
    cv = load_cv(FIXTURE)
    assert cv.name == "Test Person"
    assert cv.experience[0].bullets[1].id == "exp1.b2"
    assert cv.projects[0].demo == "https://tracker.example.com"
    assert cv.skills["tools"] == ["Docker", "SQLite"]


def test_all_ids_covers_entries_and_bullets():
    assert all_ids(load_cv(FIXTURE)) == {"edu1", "exp1", "exp1.b1", "exp1.b2", "proj1", "proj1.b1"}


def test_numeric_dates_are_coerced_to_str(tmp_path):
    data = yaml.safe_load(Path(FIXTURE).read_text())
    data["experience"][0]["dates"] = 2024
    cv = load_cv(_write(tmp_path, data))
    assert cv.experience[0].dates == "2024"


def test_duplicate_ids_rejected(tmp_path):
    data = yaml.safe_load(Path(FIXTURE).read_text())
    data["projects"][0]["id"] = "exp1"
    with pytest.raises(ValueError, match="duplicate id"):
        load_cv(_write(tmp_path, data))


def test_bullet_id_must_be_prefixed_by_parent(tmp_path):
    data = yaml.safe_load(Path(FIXTURE).read_text())
    data["experience"][0]["bullets"][0]["id"] = "proj1.b9"
    with pytest.raises(ValueError, match="exp1"):
        load_cv(_write(tmp_path, data))


def test_missing_file_raises_value_error_with_path(tmp_path):
    with pytest.raises(ValueError, match="nope.yaml"):
        load_cv(str(tmp_path / "nope.yaml"))


def test_invalid_schema_raises_value_error_with_path(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name: X\n")
    with pytest.raises(ValueError, match="bad.yaml"):
        load_cv(str(p))


def test_cv_to_text_renders_every_section():
    text = cv_to_text(load_cv(FIXTURE))
    assert text.startswith("Test Person\n")
    assert "Education:\n- BS Computer Science, Test University (2021 – 2025) — GPA 3.9" in text
    assert "Experience:\n- SWE Intern, Acme (2024)\n  • Built a FastAPI service handling 1k rps.\n  • Wrote SQLAlchemy migrations." in text
    assert "Projects:\n- Tracker (Jun 2026) [Python, SQLite]\n  • Polls GitHub and posts to Discord." in text
    assert "Skills:\n- languages: Python, Go\n- frameworks: FastAPI\n- tools: Docker, SQLite" in text
    assert "Location: San Jose, CA\n" in text   # relevant to location requirements
    assert "http" not in text and "t@example.com" not in text and "555" not in text   # links/contact are noise


def test_cv_to_text_includes_summary_when_present():
    cv = load_cv(FIXTURE).model_copy(update={"summary": "Backend engineer."})
    assert "Summary: Backend engineer.\n" in cv_to_text(cv)


def test_master_cv_roundtrips_through_dict():
    cv = load_cv(FIXTURE)
    assert MasterCV.model_validate(cv.model_dump()) == cv
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_cv.py -q`
Expected: `ModuleNotFoundError: No module named 'cv'`

- [ ] **Step 3: Implement `src/cv.py`**

```python
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="forbid")


class Bullet(_Model):
    id: str
    text: str


class Contact(_Model):
    email: str
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None


class Education(_Model):
    id: str
    school: str
    degree: str
    dates: str
    details: list[str] = Field(default_factory=list)


class Experience(_Model):
    id: str
    company: str
    title: str
    dates: str
    location: str | None = None
    bullets: list[Bullet] = Field(min_length=1)


class Project(_Model):
    id: str
    name: str
    dates: str | None = None
    tech: list[str] = Field(default_factory=list)
    link: str | None = None
    demo: str | None = None
    bullets: list[Bullet] = Field(min_length=1)


class MasterCV(_Model):
    name: str
    contact: Contact
    summary: str | None = None
    education: list[Education] = Field(min_length=1)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ids_unique_and_prefixed(self) -> "MasterCV":
        seen: set[str] = set()

        def check(i: str) -> None:
            if i in seen:
                raise ValueError(f"duplicate id {i!r}")
            seen.add(i)

        for e in self.education:
            check(e.id)
        for entry in [*self.experience, *self.projects]:
            check(entry.id)
            for b in entry.bullets:
                if not b.id.startswith(entry.id + "."):
                    raise ValueError(f"bullet id {b.id!r} must start with {entry.id!r}.")
                check(b.id)
        return self


def all_ids(cv: MasterCV) -> set[str]:
    ids = {e.id for e in cv.education}
    for entry in [*cv.experience, *cv.projects]:
        ids.add(entry.id)
        ids.update(b.id for b in entry.bullets)
    return ids


def load_cv(path: str) -> MasterCV:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"{path}: file not found")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
        return MasterCV.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise ValueError(f"{path}: {e}") from e


def cv_to_text(cv: MasterCV) -> str:
    """Plain-text CV for the scoring prompt. Location stays (it bears on location requirements);
    email, phone and links are noise."""
    lines = [cv.name]
    if cv.contact.location:
        lines.append(f"Location: {cv.contact.location}")
    if cv.summary:
        lines.append(f"Summary: {cv.summary}")
    lines.append("Education:")
    for e in cv.education:
        extra = f" — {'; '.join(e.details)}" if e.details else ""
        lines.append(f"- {e.degree}, {e.school} ({e.dates}){extra}")
    if cv.experience:
        lines.append("Experience:")
        for x in cv.experience:
            lines.append(f"- {x.title}, {x.company} ({x.dates})")
            lines.extend(f"  • {b.text}" for b in x.bullets)
    if cv.projects:
        lines.append("Projects:")
        for p in cv.projects:
            dates = f" ({p.dates})" if p.dates else ""
            tech = f" [{', '.join(p.tech)}]" if p.tech else ""
            lines.append(f"- {p.name}{dates}{tech}")
            lines.extend(f"  • {b.text}" for b in p.bullets)
    if cv.skills:
        lines.append("Skills:")
        lines.extend(f"- {k}: {', '.join(v)}" for k, v in cv.skills.items())
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cv.py -v` — 10 passed. Then validate the real CV: `PYTHONPATH=src python3 -c "from cv import load_cv; c=load_cv('data/cvs/ron.yaml'); print(len(c.experience), len(c.projects))"` → `2 9`. (If `data/cvs/ron.yaml` is absent in this checkout, skip and say so.)

- [ ] **Step 5: Commit**

```bash
git add src/cv.py tests/test_cv.py tests/fixtures/cv_sample.yaml
git commit -m "feat: master CV schema, loader and plain-text rendering"
```

---

### Task 3: Config and users — LLM settings, `notify_below_threshold`

**Files:**
- Modify: `src/config.py`, `src/users.py`, `.env.example`, `data/users.example.yaml`
- Test: `tests/test_config.py`, `tests/test_users.py`

**Interfaces:**
- Produces: `config.Settings` gains `llm_base_url: str`, `llm_api_key: str | None`, `llm_score_model: str`, `llm_timeout: int`; `load_settings` raises `ValueError` naming the missing variable. `users.User.notify_below_threshold: bool = False`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py` — replace `test_load_settings_defaults` and `test_load_settings_reads_env` bodies so every call passes the two required LLM vars, and append:

```python
LLM = {"LLM_BASE_URL": "http://litellm:4000/v1", "LLM_SCORE_MODEL": "deepseek-v4-flash"}


def test_load_settings_defaults():
    s = load_settings(LLM)
    assert s.data_dir == "/data"
    assert s.poll_interval == 300
    assert s.github_token is None
    assert s.llm_base_url == "http://litellm:4000/v1"
    assert s.llm_api_key is None
    assert s.llm_score_model == "deepseek-v4-flash"
    assert s.llm_timeout == 120


def test_load_settings_reads_env():
    s = load_settings({**LLM, "DATA_DIR": "/tmp/x", "POLL_INTERVAL_SECONDS": "60", "GITHUB_TOKEN": "ghp_1",
                       "LLM_API_KEY": "sk-1", "LLM_TIMEOUT_SECONDS": "30"})
    assert s.data_dir == "/tmp/x" and s.poll_interval == 60 and s.github_token == "ghp_1"
    assert s.llm_api_key == "sk-1" and s.llm_timeout == 30


def test_load_settings_strips_trailing_slash_from_llm_base_url():
    assert load_settings({**LLM, "LLM_BASE_URL": "http://h:4000/v1/"}).llm_base_url == "http://h:4000/v1"


def test_load_settings_requires_llm_base_url():
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        load_settings({"LLM_SCORE_MODEL": "m"})


def test_load_settings_requires_llm_score_model():
    with pytest.raises(ValueError, match="LLM_SCORE_MODEL"):
        load_settings({"LLM_BASE_URL": "http://h"})
```

Update the other existing tests in the file (`test_load_settings_treats_blank_token_as_none`, `test_load_settings_rejects_non_integer_interval`) to pass `{**LLM, ...}`.

`tests/test_users.py` — append:

```python
def test_notify_below_threshold_defaults_false():
    assert load_users(_write(VALID))[0].notify_below_threshold is False


def test_notify_below_threshold_parsed():
    text = VALID.replace("  threshold: 60\n", "  threshold: 60\n  notify_below_threshold: true\n")
    assert load_users(_write(text))[0].notify_below_threshold is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py tests/test_users.py -q`
Expected: `AttributeError: 'Settings' object has no attribute 'llm_base_url'` and the users tests failing on the missing field.

- [ ] **Step 3: Implement**

`src/config.py`:

```python
@dataclass(frozen=True)
class Settings:
    data_dir: str
    poll_interval: int
    github_token: str | None
    llm_base_url: str
    llm_api_key: str | None
    llm_score_model: str
    llm_timeout: int


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required (set it in .env)")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    return Settings(
        data_dir=env.get("DATA_DIR", "/data"),
        poll_interval=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        github_token=env.get("GITHUB_TOKEN") or None,
        llm_base_url=_required(env, "LLM_BASE_URL").rstrip("/"),
        llm_api_key=env.get("LLM_API_KEY") or None,
        llm_score_model=_required(env, "LLM_SCORE_MODEL"),
        llm_timeout=int(env.get("LLM_TIMEOUT_SECONDS", "120")),
    )
```

`src/users.py`: add `notify_below_threshold: bool = False` after `threshold`.

`.env.example` — append:

```
# LLM (OpenAI-compatible; LiteLLM). Model names are whatever LiteLLM exposes.
LLM_BASE_URL=http://litellm:4000/v1
LLM_API_KEY=
LLM_SCORE_MODEL=deepseek-v4-flash
LLM_TIMEOUT_SECONDS=120
```

`data/users.example.yaml` — add after `threshold: 60`:

```yaml
  # Calibration: true delivers below-threshold postings too, with their score, so you can
  # judge the scoring. Set false once you trust it.
  notify_below_threshold: false
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py tests/test_users.py -v` — all pass. `tests/test_main.py` constructs `Settings(...)` positionally/by keyword in three places; update those calls to include `llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_timeout=5` so the file imports (its e2e tests remain deselected until Task 7).

- [ ] **Step 5: Commit**

```bash
git add src/config.py src/users.py .env.example data/users.example.yaml tests/test_config.py tests/test_users.py tests/test_main.py
git commit -m "feat: LLM settings and per-user notify_below_threshold"
```

---

### Task 4: `prompts.py` and `llm.py` — scoring client

**Files:**
- Create: `src/prompts.py`, `src/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `prompts.SCORE_SYSTEM: str`; `prompts.score_user_message(description: str, cv_text: str) -> str`
  - `llm.ScoreResponse` (Pydantic: `score: int` 0–100, `reasoning: str`, `missing_confirmed: list[str]`, `missing_unknown: list[str]`)
  - `llm.LLMResult(kind: str, data: ScoreResponse | None, error: str | None, retry_after: float | None, model: str | None, usage: dict | None, ms: int)` with `.ok`
  - `llm.LLMClient(base_url, api_key, model, timeout)` with `score(description, cv_text) -> LLMResult`
  - `llm.classify_status(status) -> str` (`ok` 2xx; `transient` 429/5xx; `unavailable` 401/403/404; `invalid` other 4xx)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm.py`:

```python
import json
from unittest.mock import Mock, patch

import requests

from llm import LLMClient, LLMResult, ScoreResponse, classify_status
from prompts import SCORE_SYSTEM, score_user_message

GOOD = {"score": 82, "reasoning": "Strong Python and FastAPI match.", "missing_confirmed": ["Kubernetes"],
        "missing_unknown": ["work authorization"]}


def _resp(status, content=None, headers=None, usage=None, raw=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    body = raw if raw is not None else {
        "choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}],
        "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 80},
        "model": "deepseek-v4-flash",
    }
    r.json.return_value = body
    r.text = json.dumps(body) if isinstance(body, dict) else str(body)
    return r


def _client():
    return LLMClient("http://llm:4000/v1", "sk-test", "deepseek-v4-flash", timeout=7)


# --- prompts -----------------------------------------------------------------

def test_system_prompt_carries_rubric_and_schema():
    flat = " ".join(SCORE_SYSTEM.split())
    assert "90–100" in flat and "below 50" in flat
    assert '"missing_unknown"' in flat
    assert "does not lower the score" in flat


def test_user_message_contains_both_inputs_in_order():
    m = score_user_message("JOB TEXT", "CV TEXT")
    assert m.index("JOB TEXT") < m.index("CV TEXT")


# --- classify ----------------------------------------------------------------

def test_classify_status():
    assert classify_status(200) == "ok"
    assert classify_status(429) == "transient" and classify_status(503) == "transient"
    assert classify_status(401) == "unavailable" and classify_status(403) == "unavailable" and classify_status(404) == "unavailable"
    assert classify_status(400) == "invalid" and classify_status(422) == "invalid"


# --- score -------------------------------------------------------------------

def test_score_posts_json_mode_with_auth_and_returns_parsed():
    with patch("llm.requests.post", return_value=_resp(200, GOOD)) as post:
        r = _client().score("JD", "CV")
    assert r.ok and isinstance(r.data, ScoreResponse) and r.data.score == 82
    assert r.model == "deepseek-v4-flash" and r.usage["prompt_tokens"] == 1200 and r.ms >= 0
    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == "http://llm:4000/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["timeout"] == 7
    body = kwargs["json"]
    assert body["model"] == "deepseek-v4-flash" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": SCORE_SYSTEM}
    assert body["messages"][1]["role"] == "user" and "JD" in body["messages"][1]["content"]


def test_score_without_api_key_sends_no_auth_header():
    with patch("llm.requests.post", return_value=_resp(200, GOOD)) as post:
        LLMClient("http://llm/v1", None, "m", timeout=5).score("JD", "CV")
    assert "Authorization" not in post.call_args.kwargs["headers"]


def test_score_tolerates_code_fenced_json():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    with patch("llm.requests.post", return_value=_resp(200, fenced)):
        assert _client().score("JD", "CV").data.score == 82


def test_score_rejects_non_integer_or_out_of_range_scores():
    # Each of these must be re-asked and, when repeated, classed invalid — never coerced.
    for bad in [140, -1, "82", 82.5, True, None]:
        with patch("llm.requests.post", side_effect=[_resp(200, {**GOOD, "score": bad})] * 2) as post:
            r = _client().score("JD", "CV")
        assert r.kind == "invalid", bad
        assert post.call_count == 2, bad


def test_score_requires_reasoning_and_gap_fields():
    for missing in ["reasoning", "missing_confirmed", "missing_unknown"]:
        body = {k: v for k, v in GOOD.items() if k != missing}
        with patch("llm.requests.post", side_effect=[_resp(200, body)] * 2):
            assert _client().score("JD", "CV").kind == "invalid", missing


def test_score_accepts_empty_lists():
    with patch("llm.requests.post", return_value=_resp(200, {**GOOD, "missing_confirmed": [], "missing_unknown": []})):
        assert _client().score("JD", "CV").ok


def test_score_reasks_once_on_invalid_json_then_succeeds():
    with patch("llm.requests.post", side_effect=[_resp(200, "not json at all"), _resp(200, GOOD)]) as post:
        r = _client().score("JD", "CV")
    assert r.ok
    assert post.call_count == 2
    msgs = post.call_args.kwargs["json"]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": "not json at all"}
    assert msgs[-1]["role"] == "user" and "JSON" in msgs[-1]["content"]


def test_score_invalid_after_reask_is_invalid():
    with patch("llm.requests.post", side_effect=[_resp(200, "nope"), _resp(200, {"score": "high"})]) as post:
        r = _client().score("JD", "CV")
    assert r.kind == "invalid" and r.data is None and post.call_count == 2
    assert "score" in r.error


def test_score_missing_choices_is_invalid():
    with patch("llm.requests.post", return_value=_resp(200, raw={"error": "weird"})):
        assert _client().score("JD", "CV").kind == "invalid"


def test_score_429_is_transient_with_retry_after():
    with patch("llm.requests.post", return_value=_resp(429, raw={}, headers={"Retry-After": "12"})):
        r = _client().score("JD", "CV")
    assert r.kind == "transient" and r.retry_after == 12.0


def test_score_5xx_is_transient():
    with patch("llm.requests.post", return_value=_resp(502, raw={})):
        assert _client().score("JD", "CV").kind == "transient"


def test_score_timeout_is_transient():
    with patch("llm.requests.post", side_effect=requests.Timeout("slow")):
        r = _client().score("JD", "CV")
    assert r.kind == "transient" and "slow" in r.error


def test_score_connection_error_is_unavailable():
    with patch("llm.requests.post", side_effect=requests.ConnectionError("refused")):
        r = _client().score("JD", "CV")
    assert r.kind == "unavailable" and "refused" in r.error


def test_score_401_is_unavailable():
    with patch("llm.requests.post", return_value=_resp(401, raw={"error": {"message": "bad key"}})):
        r = _client().score("JD", "CV")
    assert r.kind == "unavailable" and "bad key" in r.error


def test_score_400_is_invalid():
    with patch("llm.requests.post", return_value=_resp(400, raw={"error": {"message": "context length"}})):
        assert _client().score("JD", "CV").kind == "invalid"


def test_llm_result_ok_property():
    assert LLMResult("ok", ScoreResponse(**GOOD), None, None, "m", None, 1).ok
    assert not LLMResult("transient", None, "x", None, None, None, 1).ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_llm.py -q`
Expected: `ModuleNotFoundError: No module named 'llm'`

- [ ] **Step 3: Implement `src/prompts.py`**

```python
SCORE_SYSTEM = """\
You evaluate how well a candidate's CV demonstrates fit for one job posting.

Score the DEMONSTRATED fit only: what the CV shows versus what the posting asks for.
A requirement the CV does not address either way (work authorization, graduation year,
security clearance, a technology never mentioned) goes in "missing_unknown" and does not
lower the score — the candidate judges those, you cannot.

Rubric (use the whole range; be consistent across postings):
- 90–100: demonstrates every stated hard requirement and most preferred ones
- 70–89: demonstrates the hard requirements; some preferred ones not shown
- 50–69: one hard requirement confirmed missing, otherwise a fit
- below 50: multiple hard requirements confirmed missing, or the role is a different discipline

"Hard requirements" are what the posting says is required / must-have / minimum.
"Confirmed missing" means the CV shows the candidate lacks it (e.g. the posting requires 5+
years of professional experience and the CV shows internships only), not merely that the CV
is silent on it.

Reply with ONLY a JSON object, no prose, no code fences:
{
  "score": <integer 0-100>,
  "reasoning": "<two or three sentences on the strongest evidence for and against>",
  "missing_confirmed": ["<requirement the CV clearly does not meet>", ...],
  "missing_unknown": ["<requirement the CV does not mention either way>", ...]
}
Keep each list item under 12 words. Empty lists are fine.
"""

_REASK = (
    "That reply was not a valid JSON object matching the schema. "
    "Reply again with ONLY the JSON object — no prose, no code fences."
)


def score_user_message(description: str, cv_text: str) -> str:
    return f"JOB POSTING:\n{description}\n\n---\n\nCANDIDATE CV:\n{cv_text}"


def reask_message() -> str:
    return _REASK
```

- [ ] **Step 4: Implement `src/llm.py`**

```python
import json
import logging
import re
import time
from dataclasses import dataclass

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prompts import SCORE_SYSTEM, reask_message, score_user_message

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class ScoreResponse(BaseModel):
    """Strict on purpose: a score of `false`, "82" or 140 is a malformed reply, not a datum.
    Coercing it could silently suppress a real match; rejecting it triggers the re-ask."""
    model_config = ConfigDict(strict=True, extra="ignore")

    score: int = Field(ge=0, le=100)
    reasoning: str
    missing_confirmed: list[str]
    missing_unknown: list[str]


@dataclass(frozen=True)
class LLMResult:
    kind: str  # "ok" | "transient" | "unavailable" | "invalid"
    data: ScoreResponse | None
    error: str | None
    retry_after: float | None
    model: str | None
    usage: dict | None
    ms: int

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def classify_status(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status == 429 or status >= 500:
        return "transient"
    if status in (401, 403, 404):
        return "unavailable"   # bad key, blocked, or unknown model: config, not this item
    return "invalid"


def _strip_fence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def _error_message(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
    except ValueError:
        pass
    return (resp.text or "")[:200]


class LLMClient:
    def __init__(self, base_url: str, api_key: str | None, model: str, timeout: int = 120) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._model = model
        self._timeout = timeout

    # -- public -------------------------------------------------------------

    def score(self, description: str, cv_text: str) -> LLMResult:
        messages = [
            {"role": "system", "content": SCORE_SYSTEM},
            {"role": "user", "content": score_user_message(description, cv_text)},
        ]
        started = time.monotonic()
        kind, content, error, retry_after, model, usage = self._chat(messages)
        if kind == "ok":
            parsed, perr = self._parse(content)
            if parsed is None:
                # One re-ask, carrying the bad reply so the model sees what it did.
                messages += [{"role": "assistant", "content": content}, {"role": "user", "content": reask_message()}]
                kind, content, error, retry_after, model2, usage2 = self._chat(messages)
                model, usage = model2 or model, usage2 or usage
                if kind == "ok":
                    parsed, perr = self._parse(content)
                    if parsed is None:
                        kind, error = "invalid", f"schema validation failed after re-ask: {perr}"
            if kind == "ok":
                return LLMResult("ok", parsed, None, None, model, usage, self._ms(started))
        return LLMResult(kind, None, error, retry_after, model, usage, self._ms(started))

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _chat(self, messages: list[dict]):
        """Returns (kind, content, error, retry_after, model, usage)."""
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout)
        except requests.ConnectionError as e:
            return "unavailable", None, f"{type(e).__name__}: {e}", None, None, None
        except requests.RequestException as e:
            return "transient", None, f"{type(e).__name__}: {e}", None, None, None

        kind = classify_status(resp.status_code)
        if kind != "ok":
            retry_after = None
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
            return kind, None, f"HTTP {resp.status_code}: {_error_message(resp)}", retry_after, None, None

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return "invalid", None, f"unexpected response shape: {type(e).__name__}", None, None, None
        return "ok", content or "", None, None, body.get("model"), body.get("usage")

    @staticmethod
    def _parse(content: str) -> tuple[ScoreResponse | None, str | None]:
        try:
            return ScoreResponse.model_validate(json.loads(_strip_fence(content))), None
        except (ValueError, ValidationError) as e:  # json.JSONDecodeError is a ValueError
            return None, str(e)[:300]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_llm.py -v` — 19 passed.

- [ ] **Step 6: Commit**

```bash
git add src/prompts.py src/llm.py tests/test_llm.py
git commit -m "feat: LLM scoring client with rubric prompt, JSON mode and one re-ask"
```

---

### Task 5: Match message and `message_for`

**Files:**
- Modify: `src/discord_client.py`, `src/worker.py`
- Test: `tests/test_discord_client.py`, `tests/test_worker.py`

**Interfaces:**
- Produces: `discord_client.format_match(company, role, location, url, score, reasoning, missing_confirmed, missing_unknown, matched: bool) -> str`. `worker.message_for(ev)` returns the match message when `ev.score is not None and ev.outcome in ("matched", "below_threshold")`, else the link-only message; `_LINK_ONLY_NOTES["score_failed"] = "couldn't score"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_discord_client.py` — append:

```python
from discord_client import format_match


def test_format_match_full():
    msg = format_match("Stripe", "SWE Intern", "SF", "https://s/j?gh_jid=1", 82, "Strong Python match.",
                       ["Kubernetes"], ["work authorization"], matched=True)
    assert msg == ("🎯 82% — **Stripe** — SWE Intern\n📍 SF\n🔗 https://s/j?gh_jid=1\n✅ Why: Strong Python match.\n"
                   "⚠️ Gaps: Kubernetes\n❓ Not on CV: work authorization")


def test_format_match_below_threshold_icon_and_omits_empty_lists():
    msg = format_match("Stripe", "SWE Intern", "SF", "https://s", 41, "Different discipline.", [], [], matched=False)
    assert msg.startswith("📉 41% — **Stripe**")
    assert "Gaps" not in msg and "Not on CV" not in msg


def test_format_match_caps_at_2000_truncating_lists_first():
    gaps = [f"requirement number {i}" for i in range(300)]
    msg = format_match("S", "R", "L", "https://s", 70, "Why.", gaps, [], matched=True)
    assert len(msg) <= MAX_CONTENT and "✅ Why: Why." in msg and "…" in msg
```

`tests/test_worker.py` — append:

```python
def test_message_for_matched_row_uses_match_format(session):
    ev = _seed(session, outcome="matched", score=82, reasoning="Good.", missing_confirmed=["K8s"], missing_unknown=[])
    msg = message_for(ev)
    assert msg.startswith("🎯 82% — **Stripe** — SWE Intern") and "⚠️ Gaps: K8s" in msg


def test_message_for_below_threshold_row_uses_down_icon(session):
    ev = _seed(session, outcome="below_threshold", score=40, reasoning="Meh.")
    assert message_for(ev).startswith("📉 40% — **Stripe**")


def test_message_for_score_failed_is_link_only_with_note(session):
    ev = _seed(session, outcome="score_failed")
    assert "(couldn't score)" in message_for(ev) and "🎯" not in message_for(ev)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_discord_client.py tests/test_worker.py -q -k "format_match or message_for"`
Expected: `ImportError: cannot import name 'format_match'` and the `score_failed` note test failing on the old wording.

- [ ] **Step 3: Implement**

`src/discord_client.py` — append:

```python
def format_match(
    company: str, role: str, location: str, url: str,
    score: int, reasoning: str, missing_confirmed: list[str], missing_unknown: list[str],
    matched: bool,
) -> str:
    icon = "🎯" if matched else "📉"
    header = f"{icon} {score}% — **{company}** — {role}\n📍 {location}\n🔗 {url}"
    if reasoning:
        header += f"\n✅ Why: {reasoning}"
    lists = []
    if missing_confirmed:
        lists.append("⚠️ Gaps: " + ", ".join(missing_confirmed))
    if missing_unknown:
        lists.append("❓ Not on CV: " + ", ".join(missing_unknown))
    return cap_content(header, lists)
```

`src/worker.py`:

```python
_LINK_ONLY_NOTES = {
    "fetch_failed": "couldn't read the description",
    "score_failed": "couldn't score",
}


def message_for(ev: Evaluation) -> str:
    job = ev.job
    if ev.score is not None and ev.outcome in ("matched", "below_threshold"):
        return format_match(job.company, job.role, job.location, job.url, ev.score, ev.reasoning or "",
                            ev.missing_confirmed or [], ev.missing_unknown or [], matched=ev.outcome == "matched")
    return format_link_only(job.company, job.role, job.location, job.url, note=_LINK_ONLY_NOTES.get(ev.outcome))
```

(import `format_match` from `discord_client`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_discord_client.py tests/test_worker.py -q` — all pass. If `test_format_match_caps_at_2000_truncating_lists_first` fails because `cap_content` treats a long list line as a single unit, check the current `cap_content` implementation: it truncates each list line to the remaining budget with an ellipsis, which is what the test expects.

- [ ] **Step 5: Commit**

```bash
git add src/discord_client.py src/worker.py tests/test_discord_client.py tests/test_worker.py
git commit -m "feat: match message format; scored rows render score, reasoning and gaps"
```

---

### Task 6: Worker `score` action

**Files:**
- Modify: `src/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `llm.LLMClient.score`, `llm.LLMResult`, `cv.MasterCV`, `cv.cv_to_text`, `fetcher.DESCRIPTION_CAP`, `db.STAGE_SCORE`, `users.User.threshold`, `users.User.notify_below_threshold`.
- Produces: `worker.SCORE_BUDGET = 3`, `worker.LLM_PAUSE_SECONDS = 900`; `Worker(session_factory, users, cvs: dict[str, dict] | None = None, llm=None, send=..., fetch=..., now=...)`; `Worker.run_once()` order deliver → fetch → score; `Worker._next_scoreable(session) -> Evaluation | None`; `Worker.score(session, ev) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker.py`:

```python
from cv import load_cv
from llm import LLMResult, ScoreResponse
from worker import LLM_PAUSE_SECONDS, SCORE_BUDGET

CV_DICT = load_cv("tests/fixtures/cv_sample.yaml").model_dump()
CVS = {"ron": CV_DICT, "cousin": CV_DICT}

def _llm_ok(score=82):
    return LLMResult("ok", ScoreResponse(score=score, reasoning="Because.", missing_confirmed=["K8s"], missing_unknown=["visa"]),
                     None, None, "deepseek-v4-flash", {"prompt_tokens": 10, "completion_tokens": 5}, 42)

LLM_TRANSIENT = LLMResult("transient", None, "HTTP 503", None, None, None, 5)
LLM_INVALID = LLMResult("invalid", None, "schema", None, None, None, 5)
LLM_DOWN = LLMResult("unavailable", None, "ConnectionError: refused", None, None, None, 5)


class FakeLLM:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def score(self, description, cv_text):
        self.calls.append((description, cv_text))
        return self.results.pop(0) if self.results else _llm_ok()


def _worker_s(session_factory, llm, clock, sender=None, users=(RON, COUSIN)):
    return Worker(session_factory, list(users), cvs=CVS, llm=llm, send=sender or FakeSender(), fetch=FakeFetcher(), now=clock)


def _seed_scoreable(session, user_id="ron", description="Requirements\nPython, FastAPI.", **kw):
    ev = _seed(session, user_id, stage=STAGE_SCORE, **kw)
    ev.job.fetch_status = FETCH_OK
    ev.job.description = description
    session.commit()
    return ev


def test_score_snapshots_cv_before_calling_llm(session_factory, session, clock):
    ev = _seed_scoreable(session)
    llm = FakeLLM(_llm_ok())
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.cv_snapshot == CV_DICT
    assert "Test Person" in llm.calls[0][1] and "Requirements" in llm.calls[0][0]


def test_score_match_stores_fields_and_moves_to_deliver(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(_llm_ok(82)), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "matched" and ev.stage == STAGE_DELIVER
    assert ev.score == 82 and ev.reasoning == "Because."
    assert ev.missing_confirmed == ["K8s"] and ev.missing_unknown == ["visa"]
    assert ev.score_model == "deepseek-v4-flash" and ev.score_usage["prompt_tokens"] == 10
    assert ev.attempts == 1 and ev.last_error is None


def test_score_below_threshold_closes_silently(session_factory, session, clock):
    ev = _seed_scoreable(session)
    sender = FakeSender()
    w = _worker_s(session_factory, FakeLLM(_llm_ok(59)), clock, sender)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "below_threshold" and ev.stage == STAGE_CLOSED and ev.score == 59
    assert w.run_once() is False and sender.calls == []


def test_score_below_threshold_delivers_when_user_opted_in(session_factory, session, clock):
    ev = _seed_scoreable(session)
    opted = RON.model_copy(update={"notify_below_threshold": True})
    sender = FakeSender(OK)
    w = _worker_s(session_factory, FakeLLM(_llm_ok(59)), clock, sender, users=(opted, COUSIN))
    w.run_once()                       # score
    session.refresh(ev)
    assert ev.outcome == "below_threshold" and ev.stage == STAGE_DELIVER
    w.run_once()                       # deliver
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED and sender.calls[0][1].startswith("📉 59%")


def test_score_uses_user_threshold(session_factory, session, clock):
    ev = _seed_scoreable(session, "cousin")
    strict = COUSIN.model_copy(update={"threshold": 90})
    w = _worker_s(session_factory, FakeLLM(_llm_ok(82)), clock, users=(RON, strict))
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "below_threshold"


def test_score_at_threshold_is_a_match(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(_llm_ok(60)), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "matched"


def test_score_uses_capped_description(session_factory, session, clock):
    from fetcher import DESCRIPTION_CAP
    ev = _seed_scoreable(session, description="R" * (DESCRIPTION_CAP + 500))
    llm = FakeLLM(_llm_ok())
    _worker_s(session_factory, llm, clock).run_once()
    assert len(llm.calls[0][0]) == DESCRIPTION_CAP


class SlowLLM(FakeLLM):
    """Advances the fake clock during the call, like a real 120 s request would."""
    def __init__(self, clock, seconds, *results):
        super().__init__(*results)
        self._clock, self._seconds = clock, seconds

    def score(self, description, cv_text):
        self._clock.advance(self._seconds)
        return super().score(description, cv_text)


def test_score_transient_leases_attempt_backs_off_and_cools_down_llm(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(LLM_TRANSIENT), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_SCORE and ev.attempts == 1 and ev.last_error == "HTTP 503"
    assert ev.next_attempt_at == T0 + timedelta(seconds=30)
    assert w.paused["llm"] == T0 + timedelta(seconds=30)      # shared cooldown


def test_score_transient_cooldown_holds_other_rows(session_factory, session, clock):
    _seed_scoreable(session, "ron")
    _seed_scoreable(session, "cousin")
    llm = FakeLLM(LLMResult("transient", None, "429", 60.0, None, None, 1), _llm_ok(), _llm_ok())
    w = _worker_s(session_factory, llm, clock)
    w.run_once()                        # ron: 429, Retry-After 60
    assert w.run_once() is False        # cousin must NOT hit the endpoint during the cooldown
    assert len(llm.calls) == 1
    clock.advance(60)
    assert w.run_once() is True
    assert len(llm.calls) == 2


def test_score_final_attempt_429_still_cools_down_llm(session_factory, session, clock):
    ev_a = _seed_scoreable(session, "ron", attempts=SCORE_BUDGET - 1)   # third attempt is the last
    _seed_scoreable(session, "cousin")
    llm = FakeLLM(LLMResult("transient", None, "429", 60.0, None, None, 1), _llm_ok())
    sender = FakeSender(OK)
    w = _worker_s(session_factory, llm, clock, sender)
    assert w.run_once() is True         # A: 429 on its final attempt → score_failed, cooldown recorded
    session.refresh(ev_a)
    assert ev_a.outcome == "score_failed" and ev_a.stage == STAGE_DELIVER
    assert w.paused["llm"] == T0 + timedelta(seconds=60)
    assert w.run_once() is True         # A is delivered link-only right away
    assert len(sender.calls) == 1 and "(couldn't score)" in sender.calls[0][1]
    assert w.run_once() is False        # B waits out the cooldown
    assert len(llm.calls) == 1
    clock.advance(60)
    assert w.run_once() is True and len(llm.calls) == 2


def test_score_invalid_does_not_cool_down_llm(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(LLM_INVALID), clock)
    w.run_once()
    assert "llm" not in w.paused


def test_score_deadlines_are_measured_from_after_the_call(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, SlowLLM(clock, 120, LLMResult("transient", None, "429", 7.0, None, None, 1)), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.next_attempt_at == T0 + timedelta(seconds=120 + 7)   # not 113 s in the past
    assert w.paused["llm"] == T0 + timedelta(seconds=120 + 7)


def test_score_unavailable_resume_is_measured_from_after_the_call(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, SlowLLM(clock, 120, LLM_DOWN), clock)
    w.run_once()
    session.refresh(ev)
    assert w.paused["llm"] == T0 + timedelta(seconds=120 + LLM_PAUSE_SECONDS)
    assert ev.next_attempt_at == w.paused["llm"]


def test_score_over_budget_on_restart_goes_straight_to_fallback(session_factory, session, clock):
    ev = _seed_scoreable(session, attempts=SCORE_BUDGET)   # three crashed attempts persisted
    llm = FakeLLM(_llm_ok())
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True
    assert llm.calls == []
    session.refresh(ev)
    assert ev.outcome == "score_failed" and ev.stage == STAGE_DELIVER and ev.attempts == SCORE_BUDGET


def test_ready_deliveries_go_before_scoring(session_factory, session, clock):
    ready = _resolve(session, _seed(session, "ron", stage=STAGE_DELIVER))
    _seed_scoreable(session, "cousin")
    llm = FakeLLM(_llm_ok())
    sender = FakeSender(OK)
    w = _worker_s(session_factory, llm, clock, sender)
    assert w.run_once() is True
    assert len(sender.calls) == 1 and llm.calls == []      # delivered first
    session.refresh(ready)
    assert ready.stage == STAGE_CLOSED
    assert w.run_once() is True and len(llm.calls) == 1    # then scored


def test_score_budget_exhausted_falls_back_to_link_only(session_factory, session, clock):
    ev = _seed_scoreable(session)
    sender = FakeSender(OK)
    w = _worker_s(session_factory, FakeLLM(*[LLM_INVALID] * SCORE_BUDGET), clock, sender)
    for _ in range(SCORE_BUDGET):
        assert w.run_once() is True
        clock.advance(3600)
    session.refresh(ev)
    assert ev.outcome == "score_failed" and ev.stage == STAGE_DELIVER and ev.attempts == SCORE_BUDGET
    w.run_once()                       # deliver
    assert "(couldn't score)" in sender.calls[0][1]


def test_score_unavailable_pauses_llm_without_consuming_attempt(session_factory, session, clock):
    ev_ron = _seed_scoreable(session, "ron")
    ev_cousin = _seed_scoreable(session, "cousin")
    llm = FakeLLM(LLM_DOWN, _llm_ok(), _llm_ok())
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True         # ron: unavailable
    session.refresh(ev_ron)
    assert ev_ron.attempts == 0 and ev_ron.stage == STAGE_SCORE
    assert w.paused["llm"] == T0 + timedelta(seconds=LLM_PAUSE_SECONDS)
    assert ev_ron.next_attempt_at == T0 + timedelta(seconds=LLM_PAUSE_SECONDS)
    assert w.run_once() is False        # cousin waits too; nothing deliverable
    assert len(llm.calls) == 1
    clock.advance(LLM_PAUSE_SECONDS)
    assert w.run_once() is True         # scoring resumes
    assert len(llm.calls) == 2


def test_score_waits_for_fetch(session_factory, session, clock):
    ev = _seed(session, stage=STAGE_SCORE)         # fetch still pending
    llm = FakeLLM()
    w = _worker_s(session_factory, llm, clock)
    w._fetch = FakeFetcher(FETCH_TRANSIENT)
    w.run_once()                        # fetch attempt, not a score
    assert llm.calls == []
    session.refresh(ev)
    assert ev.stage == STAGE_SCORE


def test_score_llm_exception_is_transient_attempt(session_factory, session, clock):
    ev = _seed_scoreable(session)

    class Boom:
        def score(self, d, c):
            raise RuntimeError("kaboom")

    w = _worker_s(session_factory, Boom(), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.attempts == 1 and "RuntimeError" in ev.last_error and ev.stage == STAGE_SCORE


def test_score_lease_survives_crash(session_factory, session, clock):
    ev = _seed_scoreable(session)

    class Crash:
        def score(self, d, c):
            raise _Crash()

    w1 = _worker_s(session_factory, Crash(), clock)
    with pytest.raises(_Crash):
        w1.run_once()
    session.refresh(ev)
    assert ev.attempts == 1 and ev.cv_snapshot == CV_DICT and ev.next_attempt_at == T0 + timedelta(seconds=30)


def test_score_reuses_snapshot_on_retry_even_if_cv_changes(session_factory, session, clock):
    ev = _seed_scoreable(session)
    llm = FakeLLM(LLM_TRANSIENT, _llm_ok())
    w = _worker_s(session_factory, llm, clock)
    w.run_once()
    w._cvs["ron"] = {**CV_DICT, "name": "Someone Else"}   # "edited on disk" between attempts
    clock.advance(30)
    w.run_once()
    assert "Test Person" in llm.calls[1][1] and "Someone Else" not in llm.calls[1][1]


def test_score_unknown_user_pauses_destination(session_factory, session, clock):
    ev = _seed_scoreable(session, "ghost")
    llm = FakeLLM()
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True          # the pause is the unit of work
    assert llm.calls == []
    assert w.paused == {"discord:ghost": None}
    assert w.run_once() is False         # paused rows are skipped from now on
    session.refresh(ev)
    assert ev.stage == STAGE_SCORE and ev.attempts == 0


def test_score_without_llm_configured_is_skipped(session_factory, session, clock):
    _seed_scoreable(session)
    w = Worker(session_factory, [RON, COUSIN], cvs=CVS, llm=None, send=FakeSender(), fetch=FakeFetcher(), now=clock)
    assert w.run_once() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_worker.py -q -k score`
Expected: `ImportError: cannot import name 'LLM_PAUSE_SECONDS' from 'worker'`.

- [ ] **Step 3: Implement in `src/worker.py`**

Imports — add `STAGE_SCORE` to the `db` import, and:
```python
from cv import MasterCV, cv_to_text
from llm import LLMResult
```

Constants:
```python
SCORE_BUDGET = 3
LLM_PAUSE_SECONDS = 900
```

Constructor — new params after `users`:
```python
        cvs: dict[str, dict] | None = None,
        llm=None,
```
and store `self._cvs = dict(cvs or {})`, `self._llm = llm`.

`run_once` — new order **deliver → fetch → score** (ready messages first, then the cheap fetch, then the slow LLM call):
```python
    def run_once(self) -> bool:
        with self._sessions() as session:
            ev = self._next_deliverable(session)
            if ev is not None:
                self.deliver(session, ev)
                session.commit()
                return True
            job = self._next_fetchable(session)
            if job is not None:
                self.fetch(session, job)
                session.commit()
                return True
            ev = self._next_scoreable(session)
            if ev is not None:
                self.score(session, ev)
                session.commit()
                return True
            return False
```
Existing Plan 2 tests that assumed fetch-before-deliver (`test_fetch_respects_gap_between_fetches`: "fetch #1, deliver #1, fetch #2") still hold with this order — re-check each `run_once()` sequence in `tests/test_worker.py` and adjust comments/assertions where the order of units changed; do not weaken what they prove.

`_next_scoreable`:
```python
    def _next_scoreable(self, session: Session) -> Evaluation | None:
        if self._llm is None or self.is_paused("llm"):
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .join(Job)
            .filter(Evaluation.stage == STAGE_SCORE, Evaluation.next_attempt_at <= now, Job.fetch_status == FETCH_OK)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None
```

`score`:
```python
    # -- score stage --------------------------------------------------------

    def score(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None or ev.user_id not in self._cvs:
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml / no CV loaded")
            return

        now = self._now()
        if ev.attempts >= SCORE_BUDGET:
            # Crashed attempts can leave the row at the budget with no result: no further call.
            self._give_up_scoring(ev, now, f"budget exhausted after {ev.attempts} attempts")
            return
        if ev.cv_snapshot is None:
            ev.cv_snapshot = self._cvs[ev.user_id]
        # Lease the attempt before the (slow, crash-prone) call, like fetch.
        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()

        description = (ev.job.description or "")[:DESCRIPTION_CAP]
        cv_text = cv_to_text(MasterCV.model_validate(ev.cv_snapshot))
        try:
            result = self._llm.score(description, cv_text)
        except Exception as e:  # noqa: BLE001 — a client bug is a failed attempt, not a dead worker
            log.exception("LLM client raised for evaluation %d", ev.id)
            result = LLMResult("transient", None, f"{type(e).__name__}: {e}", None, None, None, 0)
        after = self._now()   # deadlines below are measured from when the call came back

        usage = result.usage or {}
        log.info("score ev=%d user=%s model=%s outcome=%s score=%s ms=%d tokens=%s/%s%s",
                 ev.id, ev.user_id, result.model or self._llm_model_name(), self._score_outcome(result, user),
                 result.data.score if result.ok else "-", result.ms,
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                 f" error={result.error}" if result.error else "")

        if result.ok:
            data = result.data
            ev.score, ev.reasoning = data.score, data.reasoning
            ev.missing_confirmed, ev.missing_unknown = data.missing_confirmed, data.missing_unknown
            ev.score_model, ev.score_usage = result.model, result.usage
            ev.last_error = None
            if data.score >= user.threshold:
                ev.outcome, ev.stage = "matched", STAGE_DELIVER
            else:
                ev.outcome = "below_threshold"
                ev.stage = STAGE_DELIVER if user.notify_below_threshold else STAGE_CLOSED
            ev.next_attempt_at = after
            return

        if result.kind == "unavailable":
            # Outage or config problem: not this row's fault. Give the lease back and hold every row.
            ev.attempts -= 1
            resume = after + timedelta(seconds=LLM_PAUSE_SECONDS)
            ev.next_attempt_at = resume
            self._pause("llm", resume, result.error or "unavailable")
            return

        ev.last_error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(ev.attempts)
        if result.kind == "transient":
            # A 429/5xx/timeout says the shared endpoint is unwell: hold every other row for the
            # backoff — even when this row is about to give up and be delivered link-only.
            self._pause("llm", after + timedelta(seconds=delay), result.error or result.kind)
        if ev.attempts >= SCORE_BUDGET:
            self._give_up_scoring(ev, after, result.error or result.kind)
            return
        ev.next_attempt_at = after + timedelta(seconds=delay)

    def _give_up_scoring(self, ev: Evaluation, when: datetime, why: str) -> None:
        ev.last_error = why
        ev.outcome, ev.stage = "score_failed", STAGE_DELIVER
        ev.next_attempt_at = when
        log.warning("Evaluation %d: scoring gave up after %d attempts: %s", ev.id, ev.attempts, why)

    def _llm_model_name(self) -> str:
        return getattr(self._llm, "_model", "?")

    @staticmethod
    def _score_outcome(result: LLMResult, user: User) -> str:
        if not result.ok:
            return result.kind
        return "matched" if result.data.score >= user.threshold else "below_threshold"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_worker.py -v` then `python3 -m pytest -q -W error --deselect tests/test_main.py::test_poll_all_then_run_once_delivers_new_posting --deselect tests/test_main.py::test_poll_fetch_deliver_end_to_end`
Expected: all pass, no warnings.

- [ ] **Step 5: Commit**

```bash
git add src/worker.py tests/test_worker.py
git commit -m "feat: worker score stage with CV snapshot, lease, budget and llm pause"
```

---

### Task 7: `main.py` wiring, e2e test, ops doc

**Files:**
- Modify: `src/main.py`, `tests/test_main.py`, `docs/ops.md`

**Interfaces:**
- Produces: `main.build(settings, users, llm=None)` — constructs `LLMClient` from settings when `llm` is not given, loads every user's CV via `cv.load_cv(user.cv)` (failing fast with the path), and passes `cvs=` and `llm=` to `Worker`.

- [ ] **Step 1: Update `main.build`**

```python
from cv import load_cv
from llm import LLMClient


def build(settings: Settings, users: list[User], llm=None):
    engine = make_engine(os.path.join(settings.data_dir, "tracker.db"))
    init_db(engine)
    added = ensure_columns(engine)
    if added:
        log.info("Schema upgraded: added %s", ", ".join(added))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        ensure_feeds(session, FEEDS.values())
        seeded = import_legacy_state(session, settings.data_dir)
        session.commit()
    if seeded:
        log.info("Imported %d jobs from legacy known_urls.json", seeded)

    cvs = {u.id: load_cv(u.cv).model_dump() for u in users}   # fails fast naming the bad file
    if llm is None:
        llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_score_model, settings.llm_timeout)
    log.info("Scoring with %s at %s", settings.llm_score_model, settings.llm_base_url)
    return session_factory, Worker(session_factory, users, cvs=cvs, llm=llm)
```

Keep the existing shape of the rest of `main.py`.

- [ ] **Step 2: Fix the two existing e2e tests and add the scored flow**

In `tests/test_main.py`:
- Both existing e2e tests build users with `cv="/x"`. Point them at the fixture: `cv="tests/fixtures/cv_sample.yaml"`.
- Both call `main.build(settings, users)`; pass `llm=FakeLLM(...)`. Define at module top:

```python
from llm import LLMResult, ScoreResponse


class FakeLLM:
    def __init__(self, score=82):
        self._score = score
        self.calls = 0

    def score(self, description, cv_text):
        self.calls += 1
        return LLMResult("ok", ScoreResponse(score=self._score, reasoning="Fits.", missing_confirmed=[], missing_unknown=[]),
                         None, None, "fake", {"prompt_tokens": 1, "completion_tokens": 1}, 1)
```

- `test_poll_all_then_run_once_delivers_new_posting`: the post-poll assertion `assert ev.stage == STAGE_DELIVER` (currently `tests/test_main.py:77`) becomes `assert ev.stage == STAGE_SCORE` (import it from `db`); after marking the job `fetch_status=ok`, expect **two** `run_once()` calls (score, then deliver); assert the POST content starts with `🎯 82%` and the row is `closed` with `outcome == "matched"`.
- `test_poll_fetch_deliver_end_to_end`: rename to `test_poll_fetch_score_deliver_end_to_end`; after the fetch `run_once()`, add a score `run_once()` (assert `post.call_count == 0` still), then the deliver `run_once()`; assert `ev.score == 82`, `ev.cv_snapshot["name"] == "Test Person"`, `ev.score_model == "fake"`, message starts with `🎯 82%`.
- Add `test_build_fails_fast_on_missing_cv(tmp_path)`: user with `cv=str(tmp_path / "missing.yaml")` → `pytest.raises(ValueError, match="missing.yaml")` from `main.build(...)`.
- Add `test_below_threshold_end_to_end_closes_without_post(tmp_path)`: same as the scored flow with `FakeLLM(score=30)` and default `notify_below_threshold`; after score `run_once()`, `run_once()` returns `False`, no POST, row `closed` / `below_threshold`.

- [ ] **Step 3: `docs/ops.md` — append**

````markdown
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
````

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q -W error` — all green, nothing deselected. `PYTHONPATH=src python3 -c "import main"` silent.

- [ ] **Step 5: Live check against LiteLLM (manual, not committed)**

With `.env` pointing at the real LiteLLM (or `LLM_BASE_URL` exported), score one real description from the live DB:

```bash
PYTHONPATH=src python3 - <<'EOF'
import os, sqlite3
from dotenv import load_dotenv; load_dotenv()
from config import load_settings
from cv import load_cv, cv_to_text
from llm import LLMClient
s = load_settings(os.environ)
cv_text = cv_to_text(load_cv("data/cvs/ron.yaml"))
desc = sqlite3.connect(os.path.join(s.data_dir, "tracker.db")).execute(
    "select description from jobs where fetch_status='ok' and length(description)>1000 limit 1").fetchone()[0]
r = LLMClient(s.llm_base_url, s.llm_api_key, s.llm_score_model, s.llm_timeout).score(desc[:12000], cv_text)
print(r.kind, r.ms, "ms", r.usage); print(r.data)
EOF
```

Expected: `ok`, a few seconds, a plausible score with non-empty reasoning. If `unavailable`, the model name or URL is wrong for your LiteLLM. Record the model name that worked in the report.

- [ ] **Step 6: Commit**

```bash
git add src/main.py tests/test_main.py docs/ops.md
git commit -m "feat: wire LLM scoring into main; scored end-to-end tests; ops notes"
```

---

### Task 8: Deploy checklist (manual)

- [ ] **Every user in `users.yaml` needs a CV file before this image starts** — `main.build` loads all of them and refuses to start otherwise. Copy `data/cvs/ron.yaml` to the server: `~/deployed-projects/internship-tracker/data/cvs/ron.yaml` (create `data/cvs/`); the `cv:` path in `users.yaml` is `/data/cvs/ron.yaml` inside the container. If your cousin is listed, provision their CV the same way first, or remove their entry until it exists.
- [ ] Add to the server `.env`: `LLM_BASE_URL`, `LLM_API_KEY` (if LiteLLM needs one), `LLM_SCORE_MODEL` (the name LiteLLM exposes for DeepSeek Flash), optionally `LLM_TIMEOUT_SECONDS`. If LiteLLM runs as a container on a Docker network, add that network to `docker-compose.yml` (`networks:`) and use the container name in the URL; if it is on the host, use `http://host.docker.internal:4000/v1` with `extra_hosts: ["host.docker.internal:host-gateway"]`.
- [ ] Set `notify_below_threshold: true` in `users.yaml` for **every user who is calibrating** (you, and your cousin if present) before the first scored posting arrives — suppression starts on the first score otherwise.
- [ ] Verify the endpoint from inside the container network before merging: `docker compose run --rm internship-tracker python -c "import os,requests;print(requests.get(os.environ['LLM_BASE_URL']+'/models',headers={'Authorization':'Bearer '+os.environ.get('LLM_API_KEY','')},timeout=10).status_code)"` — expect 200, and `LLM_SCORE_MODEL` must appear in that `/models` list.
- [ ] Merge; `docker compose pull && docker compose up -d`; check logs for `Schema upgraded: added evaluations.score_model, evaluations.score_usage`, `Scoring with <model> at <url>`, then `score ev=…` lines as postings arrive.
- [ ] Watch the first `score ev=` line: `outcome=matched|below_threshold` is healthy; `outcome=invalid` on every row means the model is not honouring the strict schema (read `error=`); `Pausing llm (3 consecutive invalid replies…)` is the breaker. See "Deploying Plan 3" in `docs/ops.md`.
- [ ] If the first score line says `outcome=unavailable`, the worker pauses `llm` for 15 min and retries; fix `.env` and restart rather than waiting.
- [ ] After a week: review 🎯/📉 messages, adjust prompt/threshold, set `notify_below_threshold: false`, restart.

---

## Self-review

**Spec coverage (Build order step 4, "Score step", "LLM client", "Paused services", retry table Score row, Delivery match message, Configuration):**
- CV snapshot before first call, snapshot reused on retry — Task 6 (+ test with a changed CV).
- Rubric, demonstrated-fit policy, JSON schema, unknowns not penalised — Task 4 prompt; message shows both lists — Task 5.
- JSON mode, Pydantic validation, one in-call re-ask, then `invalid` — Task 4.
- Error classes: transient (timeout/5xx/429 w/ Retry-After), unavailable (connection/401/403/404 → pause `llm`, no attempt), invalid → budget 3 → `score_failed` link-only — Task 6.
- `score >= threshold` → matched → deliver; below → closed, or deliver when opted in (calibration ruling) — Task 6.
- Match message format and 2,000-char cap — Task 5.
- Config: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_SCORE_MODEL`, timeout — Task 3. `LLM_TAILOR_MODEL` and `MAX_BULLETS_PER_ENTRY` belong to Plan 4.
- Description capped at 12k on the way in — Task 6.
- `stage` default and poller explicit stage — Task 1 (closes the Plan 2 follow-up note).
- Not in scope: tailor, render, PDF (Plan 4).

**Placeholder scan:** none.

**Type consistency:** `LLMResult(kind, data, error, retry_after, model, usage, ms)` positional order identical in Task 4 tests, Task 6 fakes and `worker.score`'s exception path. `Worker(session_factory, users, cvs=, llm=, send=, fetch=, now=)` used by `_worker_s` and `main.build`. `FakeLLM.score(description, cv_text)` matches `LLMClient.score`. `format_match(..., matched: bool)` called with keyword in `message_for`.

**Known follow-ups:** `_score_outcome` recomputes the threshold decision for the log line — small duplication, acceptable. `cvs` are loaded once at startup; editing a CV needs a restart (spec says so). The `llm` pause is worker-memory only, cleared on restart (spec says so).
