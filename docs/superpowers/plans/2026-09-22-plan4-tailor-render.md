# Plan 4 — Tailored Resume: Selection, Render, PDF Delivery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a posting scores at or above the user's threshold, ask the LLM which existing CV items to keep, validate that selection against the CV snapshot, render a one-page PDF, and attach it to the Discord message with the job link.

**Architecture:** Two new worker stages sit between `score` and `deliver`. `tailor` calls the LLM with the job description and the ID-annotated CV snapshot and stores a *validated selection* (IDs only — the model never writes prose). `render` turns snapshot + selection into HTML via Jinja2 and a PDF via WeasyPrint, recording the page count. Delivery attaches the PDF. Every failure in either stage degrades to the Plan 3 behaviour: the score message still goes out, with a line saying the resume is missing.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (SQLite), Pydantic 2 (strict), Jinja2 3.1.6, WeasyPrint 70.0, requests, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md` — sections "Data model", "Master CV schema", "LLM client / Tailor step", "Renderer", "Delivery", "Configuration", "Testing", "Deployment".

## Global Constraints

- The LLM returns **a selection of IDs, never text.** Rephrasing bullets is out of scope (spec: v2, and then only as a labelled draft).
- Validation and rendering resolve IDs against `evaluations.cv_snapshot`, **never** the live YAML on disk.
- Education and summary are always rendered in full from the snapshot; the model does not control them.
- `MAX_BULLETS_PER_ENTRY` default **4**; rendered-entry caps default **4 experience + 3 projects**, the model's order deciding which survive.
- One page is a target enforced by content caps, **not** a trimming loop. Over one page: keep the PDF, set `page_overflow=true`, say so in the message.
- `stage` is always *the next action to run*. `outcome` is written once and never overwritten.
- Every send uses `?wait=true`; success is HTTP 200 **with a message body**. 204 is not success. Content capped at 2,000 characters.
- No secrets in the repo. `data/` stays git-ignored — CVs, webhooks, `tracker.db` and generated PDFs are never committed.
- Tests: pytest, all externals mocked, in-memory SQLite, no network.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Rulings that bind this plan

These resolve gaps or conflicts in the spec. They are decisions, not suggestions.

1. **`outcome` stays `matched`; a new nullable column `resume_error` records why a PDF is missing.**
   The spec lists `tailor_failed` / `render_failed` as `outcome` values *and* says `outcome` is written once. Those contradict: `score` already writes `matched` before tailoring begins, so a tailor failure would have to overwrite it. Keeping `matched` and adding `resume_error` satisfies the write-once rule literally, needs no rewrite of Plan 3's semantics, and `ensure_columns` adds the nullable column on deploy with no migration. Spec amendment recorded in Task 8.
2. **`evaluations.attempts` means "attempts at the current stage" and resets to 0 on every stage transition.** One counter, three budgets (`SCORE_BUDGET=3`, `TAILOR_BUDGET=3`, `RENDER_BUDGET=2`). No new columns.
3. **Pause keys split by cause.** A `transient` LLM failure (429/5xx/timeout) says the *endpoint* is unwell and pauses `llm`, holding both stages. An `unavailable` failure (401/403/404) is a config/model problem and pauses `llm:<model>` only, so a wrong `LLM_TAILOR_MODEL` alias cannot stop scoring. Both stages check `llm` and their own `llm:<model>`.
4. **`run_once()` order is `deliver → fetch → render → score → tailor`** — ready messages first, then cheap network, then local CPU, then the cheap LLM call, then the expensive one.
5. **Tailoring is optional at runtime, and queued rows drain when it is off.** `LLM_TAILOR_MODEL` stays **required** — no new disable flag. But `Worker` can still be built without a tailor client or an output directory (every Plan 1–3 test does exactly that), so two things must hold: `score` sends a match straight to `deliver`, *and* rows an earlier process left at `tailor` or `render` are moved to `deliver` at startup with `resume_error` set. Without the second, "never parked at a stage nothing can run" is a claim this plan would break rather than keep.
6. **The too-few-entries fallback is per section.** If validation leaves fewer than two experience entries and the snapshot has more to offer, experience falls back to the snapshot's own order (capped); projects are decided independently.

## File Structure

| File | Responsibility |
|---|---|
| `src/config.py` (modify) | add `llm_tailor_model`, `max_bullets_per_entry` |
| `src/db.py` (modify) | `STAGE_TAILOR`, `STAGE_RENDER`, `evaluations.resume_error` |
| `src/cv.py` (modify) | `cv_to_id_text`, `validate_selection`, entry/bullet caps |
| `src/prompts.py` (modify) | `TAILOR_SYSTEM`, `tailor_user_message` |
| `src/llm.py` (modify) | `TailorResponse`, `LLMClient.tailor()` |
| `src/render.py` (create) | selection + snapshot → HTML → PDF, page count |
| `src/templates/resume.html`, `resume.css` (create) | the one shared layout |
| `src/discord_client.py` (modify) | message variants, attachment robustness |
| `src/worker.py` (modify) | tailor and render stages, pause-key split, delivery integration |
| `src/main.py` (modify) | second LLM client, output directory |
| `Dockerfile`, `requirements.txt`, `.github/workflows/docker-publish.yml` (modify) | WeasyPrint system deps + render smoke test |
| `docs/ops.md` (modify) | Plan 4 runbook and measurement queries |

## Local setup note

This machine's Python is PEP 668 "externally managed"; the project's existing deps live in `~/.local`. Install the two new ones with:

```bash
pip install --break-system-packages --user Jinja2==3.1.6 weasyprint==70.0
```

The pango/cairo system libraries are already present on this WSL host (verified with `ldconfig -p`). The container installs them explicitly in Task 4.

---

### Task 1: Configuration and schema

**Files:**
- Modify: `src/config.py`
- Modify: `src/db.py:26-31` (stage constants), `src/db.py:105` (Evaluation columns)
- Test: `tests/test_config.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: nothing from later tasks.
- Produces: `Settings.llm_tailor_model: str`, `Settings.max_bullets_per_entry: int`; `db.STAGE_TAILOR = "tailor"`, `db.STAGE_RENDER = "render"`; `Evaluation.resume_error: str | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, alongside the existing ones:

```python
def test_tailor_model_required():
    env = {"LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "flash"}
    with pytest.raises(ValueError, match="LLM_TAILOR_MODEL"):
        load_settings(env)


def test_tailor_model_and_bullet_cap_parsed():
    s = load_settings({
        "LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "flash",
        "LLM_TAILOR_MODEL": "pro", "MAX_BULLETS_PER_ENTRY": "3",
    })
    assert s.llm_tailor_model == "pro"
    assert s.max_bullets_per_entry == 3


@pytest.mark.parametrize("bad", ["0", "-2"])
def test_bullet_cap_must_be_positive(bad):
    with pytest.raises(ValueError, match="MAX_BULLETS_PER_ENTRY"):
        load_settings({"LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "f",
                       "LLM_TAILOR_MODEL": "p", "MAX_BULLETS_PER_ENTRY": bad})


def test_bullet_cap_defaults_to_four():
    s = load_settings({"LLM_BASE_URL": "http://x/v1", "LLM_SCORE_MODEL": "f", "LLM_TAILOR_MODEL": "p"})
    assert s.max_bullets_per_entry == 4
```

In `tests/test_db.py`:

```python
def test_ensure_columns_adds_resume_error(tmp_path):
    # A database written before Plan 4 gains the column without a migration.
    path = str(tmp_path / "old.db")
    engine = make_engine(path)
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE evaluations DROP COLUMN resume_error"))
    assert "evaluations.resume_error" in ensure_columns(engine)
    assert "resume_error" in {c["name"] for c in inspect(engine).get_columns("evaluations")}
```

(Import `text` and `inspect` from `sqlalchemy` at the top of the test file if not already there.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_config.py tests/test_db.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'llm_tailor_model'`, and the DROP COLUMN test failing because the column never existed.

- [ ] **Step 3: Implement**

In `src/config.py`, add to `Settings` (after `llm_score_model`) and to `load_settings`:

```python
    llm_tailor_model: str
    max_bullets_per_entry: int
```

```python
        llm_tailor_model=_required(env, "LLM_TAILOR_MODEL"),
        max_bullets_per_entry=_positive_int(env, "MAX_BULLETS_PER_ENTRY", 4),
```

with the helper beside `_required`:

```python
def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """A cap of zero is not "no cap": it would disable validate_selection's stopping condition
    while still emptying the fallback slice. Refuse it at startup, not at the first tailor call."""
    value = int(env.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1 (got {value})")
    return value
```

Keep field order consistent between the dataclass and the constructor call; `llm_timeout` stays last.

In `src/db.py`, extend the stage constants:

```python
STAGE_SCORE = "score"
STAGE_TAILOR = "tailor"
STAGE_RENDER = "render"
STAGE_DELIVER = "deliver"
STAGE_CLOSED = "closed"
```

and add one column to `Evaluation`, next to `pdf_path`:

```python
    # Why a match went out without a PDF. Diagnostic; `outcome` stays "matched".
    resume_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Update the comment above `stage` to read `score → tailor → render → deliver → closed`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (265 existing + the new ones). Any existing test that builds `Settings(...)` positionally must be updated for the two new fields.

- [ ] **Step 5: Commit**

```bash
git add src/config.py src/db.py tests/test_config.py tests/test_db.py
git commit -m "feat(config,db): tailor model, bullet cap, tailor/render stages, resume_error column"
```

---

### Task 2: Selection validation in `cv.py`

**Files:**
- Modify: `src/cv.py`
- Test: `tests/test_cv.py`

**Interfaces:**
- Consumes: `MasterCV` from Task 1's unchanged schema.
- Produces:
  - `cv.MAX_EXPERIENCE_ENTRIES = 4`, `cv.MAX_PROJECT_ENTRIES = 3`
  - `cv_to_id_text(cv: MasterCV) -> str` — the prompt's view of the CV, every entry and bullet prefixed with its ID.
  - `validate_selection(cv: MasterCV, raw: dict, max_bullets: int = 4) -> tuple[dict, list[str]]` — returns `({"experience": [{"id": str, "bullets": [str]}], "projects": [...], "skills": {str: [str]}}, warnings)`.

**Why this shape:** the returned dict is what gets stored in `evaluations.tailored` and replayed by the renderer after a crash, so it must be plain JSON, fully validated, and independent of the live YAML.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cv.py`. The existing fixture is `tests/fixtures/cv_sample.yaml`; load it with a path built from `Path(__file__).parent` so the test does not depend on the working directory.

```python
from pathlib import Path

from cv import MAX_EXPERIENCE_ENTRIES, MAX_PROJECT_ENTRIES, cv_to_id_text, load_cv, validate_selection

# A CV with room to choose from: the 1-experience/1-project `cv_sample.yaml` cannot distinguish
# "the validator filtered correctly" from "the too-few-entries fallback replaced the selection".
FIXTURE = str(Path(__file__).parent / "fixtures" / "cv_tailor.yaml")


def _cv():
    return load_cv(FIXTURE)


def test_id_text_labels_every_entry_and_bullet():
    text = cv_to_id_text(_cv())
    cv = _cv()
    for entry in [*cv.experience, *cv.projects]:
        assert f"[{entry.id}]" in text
        for b in entry.bullets:
            assert f"[{b.id}]" in text
    # Education and the summary are rendered from the master; the model must not select them.
    assert "[edu" not in text


def test_unknown_entry_is_dropped_with_a_warning():
    cv = _cv()
    good = cv.experience[0]
    sel, warnings = validate_selection(cv, {
        "experience": [{"id": "nope", "bullets": []}, {"id": good.id, "bullets": [good.bullets[0].id]}],
        "projects": [], "skills": {},
    })
    assert [e["id"] for e in sel["experience"]] == [good.id]
    assert any("nope" in w for w in warnings)


def test_bullet_under_the_wrong_entry_is_dropped():
    # Two entries, so the too-few-entries fallback stays out of the way and the filtering is visible.
    cv = _cv()
    a, b = cv.experience[0], cv.experience[1]
    sel, warnings = validate_selection(cv, {
        "experience": [{"id": a.id, "bullets": [b.bullets[0].id, a.bullets[0].id]},
                       {"id": b.id, "bullets": [b.bullets[0].id]}],
        "projects": [], "skills": {},
    })
    assert sel["experience"][0]["bullets"] == [a.bullets[0].id]
    assert any(b.bullets[0].id in w for w in warnings)


def test_model_order_is_preserved():
    cv = _cv()
    a, b = cv.experience[0], cv.experience[1]
    ids = [x.id for x in a.bullets][:2]
    sel, _ = validate_selection(cv, {
        "experience": [{"id": b.id, "bullets": [b.bullets[0].id]},
                       {"id": a.id, "bullets": list(reversed(ids))}],
        "projects": [], "skills": {}})
    assert [e["id"] for e in sel["experience"]] == [b.id, a.id]
    assert sel["experience"][1]["bullets"] == list(reversed(ids))


def test_bullet_cap_keeps_the_first_n_in_the_given_order():
    cv = _cv()
    fat = max(cv.projects, key=lambda p: len(p.bullets))
    other = next(p for p in cv.projects if p.id != fat.id)
    ids = [b.id for b in fat.bullets]
    sel, _ = validate_selection(cv, {
        "experience": [],
        "projects": [{"id": fat.id, "bullets": ids}, {"id": other.id, "bullets": [other.bullets[0].id]}],
        "skills": {}}, max_bullets=2)
    assert sel["projects"][0]["bullets"] == ids[:2]


def test_entry_caps_applied():
    cv = _cv()
    sel, _ = validate_selection(cv, {
        "experience": [{"id": e.id, "bullets": [e.bullets[0].id]} for e in cv.experience],
        "projects": [{"id": p.id, "bullets": [p.bullets[0].id]} for p in cv.projects],
        "skills": {},
    })
    assert len(sel["experience"]) <= MAX_EXPERIENCE_ENTRIES
    assert len(sel["projects"]) <= MAX_PROJECT_ENTRIES


def test_foreign_skill_dropped_and_group_subset_enforced():
    cv = _cv()
    group = next(iter(cv.skills))
    real = cv.skills[group][0]
    sel, warnings = validate_selection(cv, {
        "experience": [], "projects": [],
        "skills": {group: [real, "COBOL-on-Mars"], "invented_group": ["x"]},
    })
    assert sel["skills"] == {group: [real]}
    assert any("COBOL-on-Mars" in w for w in warnings)
    assert any("invented_group" in w for w in warnings)


def test_too_few_experience_entries_falls_back_to_master_order():
    cv = _cv()
    sel, warnings = validate_selection(cv, {"experience": [], "projects": [], "skills": {}}, max_bullets=2)
    assert [e["id"] for e in sel["experience"]] == [e.id for e in cv.experience][:MAX_EXPERIENCE_ENTRIES]
    assert sel["experience"][0]["bullets"] == [b.id for b in cv.experience[0].bullets][:2]
    assert any("experience" in w for w in warnings)


def test_projects_fall_back_independently_of_experience():
    cv = _cv()
    keep = [{"id": e.id, "bullets": [e.bullets[0].id]} for e in cv.experience[:2]]
    sel, _ = validate_selection(cv, {"experience": keep, "projects": [], "skills": {}})
    assert [e["id"] for e in sel["experience"]] == [e["id"] for e in keep]   # untouched
    assert len(sel["projects"]) >= min(2, len(cv.projects))                   # fell back on its own


def test_duplicate_ids_are_collapsed():
    cv = _cv()
    a, b = cv.experience[0], cv.experience[1]
    bid = a.bullets[0].id
    sel, warnings = validate_selection(cv, {
        "experience": [{"id": a.id, "bullets": [bid, bid]}, {"id": a.id, "bullets": [bid]},
                       {"id": b.id, "bullets": [b.bullets[0].id]}],
        "projects": [], "skills": {}})
    assert [e["id"] for e in sel["experience"]] == [a.id, b.id]
    assert sel["experience"][0]["bullets"] == [bid]
    assert any("duplicate" in w for w in warnings)


def test_a_single_surviving_entry_triggers_the_fallback():
    # The other side of the coin the tests above avoid: one entry is not a resume section.
    cv = _cv()
    a = cv.experience[0]
    sel, warnings = validate_selection(cv, {"experience": [{"id": a.id, "bullets": [a.bullets[0].id]}],
                                            "projects": [], "skills": {}}, max_bullets=2)
    assert [e["id"] for e in sel["experience"]] == [e.id for e in cv.experience][:MAX_EXPERIENCE_ENTRIES]
    assert any("fell back" in w for w in warnings)


def test_selection_is_json_round_trippable():
    import json
    cv = _cv()
    sel, _ = validate_selection(cv, {"experience": [], "projects": [], "skills": {}})
    assert json.loads(json.dumps(sel)) == sel


def test_extra_key_in_cv_yaml_is_rejected():
    # Guards `extra="forbid"` on the CV schema — carried over from Plan 3's review.
    import pytest
    import yaml
    raw = yaml.safe_load(Path(FIXTURE).read_text())
    raw["favourite_colour"] = "blue"
    from cv import MasterCV
    with pytest.raises(Exception):
        MasterCV.model_validate(raw)
```

**Create `tests/fixtures/cv_tailor.yaml`** — do not touch `cv_sample.yaml`, which has one experience and one project and is what the Plan 1–3 tests are written against. The new fixture needs **3 experience entries** (ids `exp1`–`exp3`, each with 3+ bullets), **4 projects** (ids `proj1`–`proj4`, at least one with 4+ bullets, at least one with a `link` and a `demo`), a `summary`, one education entry, and a `skills` mapping with 2+ groups. Keep it valid under `MasterCV` (bullet ids prefixed by their entry id).

Why a second fixture rather than a bigger `cv_sample.yaml`: with one entry per section, every selection test also trips the too-few-entries fallback, and a passing assertion cannot tell filtering from fallback. Tasks 6 and 7 use this fixture too.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_cv.py -q`
Expected: FAIL — `ImportError: cannot import name 'validate_selection' from 'cv'`.

- [ ] **Step 3: Implement**

Append to `src/cv.py`:

```python
MAX_EXPERIENCE_ENTRIES = 4
MAX_PROJECT_ENTRIES = 3


def cv_to_id_text(cv: MasterCV) -> str:
    """The tailor prompt's view: every selectable item carries the ID the model must quote back.
    Education and the summary are omitted — they are always rendered from the master."""
    lines: list[str] = []
    if cv.experience:
        lines.append("EXPERIENCE:")
        for x in cv.experience:
            where = f", {x.location}" if x.location else ""
            lines.append(f"[{x.id}] {x.title}, {x.company}{where} ({x.dates})")
            lines.extend(f"  [{b.id}] {b.text}" for b in x.bullets)
    if cv.projects:
        lines.append("PROJECTS:")
        for p in cv.projects:
            tech = f" [{', '.join(p.tech)}]" if p.tech else ""
            lines.append(f"[{p.id}] {p.name}{tech}")
            lines.extend(f"  [{b.id}] {b.text}" for b in p.bullets)
    if cv.skills:
        lines.append("SKILLS (group: options):")
        lines.extend(f"  {k}: {', '.join(v)}" for k, v in cv.skills.items())
    return "\n".join(lines)


def _validate_entries(allowed, raw_list, max_bullets, max_entries, kind, warnings):
    """Keep the model's order; drop anything that is not in `allowed`."""
    out, seen = [], set()
    for item in raw_list or []:
        if not isinstance(item, dict):
            warnings.append(f"{kind}: ignored non-object entry {item!r}")
            continue
        entry_id = item.get("id")
        entry = allowed.get(entry_id)
        if entry is None:
            warnings.append(f"{kind}: unknown entry id {entry_id!r} dropped")
            continue
        if entry_id in seen:
            warnings.append(f"{kind}: duplicate entry id {entry_id!r} dropped")
            continue
        seen.add(entry_id)
        own = {b.id for b in entry.bullets}
        bullets, seen_bullets = [], set()
        for bid in item.get("bullets") or []:
            if bid not in own:
                warnings.append(f"{kind}: bullet {bid!r} does not belong to {entry_id!r}; dropped")
                continue
            if bid in seen_bullets:
                continue
            seen_bullets.add(bid)
            bullets.append(bid)
            if len(bullets) == max_bullets:
                break
        if not bullets:   # an entry with no usable bullets is an empty block on the page
            bullets = [b.id for b in entry.bullets][:max_bullets]
            warnings.append(f"{kind}: {entry_id!r} had no usable bullets; used the master's first {len(bullets)}")
        out.append({"id": entry_id, "bullets": bullets})
        if len(out) == max_entries:
            break
    return out


def _fallback_entries(entries, max_bullets, max_entries):
    return [{"id": e.id, "bullets": [b.id for b in e.bullets][:max_bullets]} for e in entries[:max_entries]]


def validate_selection(cv: MasterCV, raw: dict, max_bullets: int = 4) -> tuple[dict, list[str]]:
    """Turn the model's reply into a selection that is safe to render.

    Only IDs that exist in THIS CV survive, a bullet must belong to the entry it is listed
    under, skills must be a subset of the master's, and the caps are enforced. The model's
    ordering is respected for everything that survives. Returns (selection, warnings).
    """
    warnings: list[str] = []
    raw = raw or {}

    exp = _validate_entries({e.id: e for e in cv.experience}, raw.get("experience"),
                            max_bullets, MAX_EXPERIENCE_ENTRIES, "experience", warnings)
    proj = _validate_entries({p.id: p for p in cv.projects}, raw.get("projects"),
                             max_bullets, MAX_PROJECT_ENTRIES, "projects", warnings)

    # Per-section fallback: a near-empty section is worse than the master's own order.
    if len(exp) < 2 and len(cv.experience) > len(exp):
        exp = _fallback_entries(cv.experience, max_bullets, MAX_EXPERIENCE_ENTRIES)
        warnings.append("experience: fewer than two entries survived; fell back to the master order")
    if len(proj) < 2 and len(cv.projects) > len(proj):
        proj = _fallback_entries(cv.projects, max_bullets, MAX_PROJECT_ENTRIES)
        warnings.append("projects: fewer than two entries survived; fell back to the master order")

    skills: dict[str, list[str]] = {}
    for group, chosen in (raw.get("skills") or {}).items():
        master = cv.skills.get(group)
        if master is None:
            warnings.append(f"skills: unknown group {group!r} dropped")
            continue
        kept = [s for s in (chosen or []) if s in master]
        for s in (chosen or []):
            if s not in master:
                warnings.append(f"skills: {s!r} is not in the master {group!r} list; dropped")
        if kept:
            skills[group] = kept
    if not skills:
        skills = {k: list(v) for k, v in cv.skills.items()}
        if cv.skills:
            warnings.append("skills: nothing usable selected; kept the master's skills")

    return {"experience": exp, "projects": proj, "skills": skills}, warnings
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_cv.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cv.py tests/test_cv.py tests/fixtures/cv_sample.yaml
git commit -m "feat(cv): ID-annotated prompt text and selection validation with per-section fallback"
```

---

### Task 3: Tailor prompt and LLM call

**Files:**
- Modify: `src/prompts.py`, `src/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `cv.cv_to_id_text` (Task 2).
- Produces:
  - `prompts.TAILOR_SYSTEM: str`, `prompts.tailor_user_message(description: str, cv_id_text: str, max_bullets: int) -> str`
  - `llm.TailorResponse` (strict Pydantic), `LLMClient.tailor(description: str, cv_id_text: str, max_bullets: int) -> LLMResult`
- `LLMResult.data` is typed `ScoreResponse | TailorResponse | None` after this task. The tailor client is a **second `LLMClient` instance** constructed with `LLM_TAILOR_MODEL`; `_chat`, classification, the re-ask and usage summing are reused unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm.py`, following the existing mocking style in that file:

```python
from llm import TailorResponse

TAILOR_JSON = json.dumps({
    "experience": [{"id": "exp1", "bullets": ["exp1.b2", "exp1.b1"]}],
    "projects": [{"id": "proj3", "bullets": ["proj3.b1"]}],
    "skills": {"languages": ["Python"]},
})


def test_tailor_parses_a_valid_selection(monkeypatch):
    client = _client(model="pro")                      # existing helper in this file
    _mock_ok(monkeypatch, TAILOR_JSON, model="pro")    # existing helper
    result = client.tailor("JD text", "[exp1] ...", 4)
    assert result.ok
    assert isinstance(result.data, TailorResponse)
    assert result.data.experience[0].bullets == ["exp1.b2", "exp1.b1"]
    assert result.model == "pro"


def test_tailor_sends_the_tailor_system_prompt_and_the_bullet_cap(monkeypatch):
    seen = {}
    _mock_capture(monkeypatch, seen, TAILOR_JSON)      # existing helper capturing the payload
    _client(model="pro").tailor("JD text", "[exp1] a bullet", 3)
    messages = seen["payload"]["messages"]
    assert messages[0]["content"] == TAILOR_SYSTEM
    assert "[exp1] a bullet" in messages[1]["content"]
    assert "3" in messages[1]["content"]
    assert seen["payload"]["response_format"] == {"type": "json_object"}


def test_tailor_free_text_instead_of_ids_is_invalid_after_one_reask(monkeypatch):
    bad = json.dumps({"experience": [{"id": "exp1", "bullets": [{"text": "I rewrote this"}]}],
                      "projects": [], "skills": {}})
    calls = _mock_sequence(monkeypatch, bad, bad)      # existing helper: two replies in order
    result = _client(model="pro").tailor("JD", "cv", 4)
    assert result.kind == "invalid"
    assert len(calls) == 2
    assert "schema validation failed after re-ask" in result.error


def test_tailor_recovers_when_the_reask_returns_valid_json(monkeypatch):
    _mock_sequence(monkeypatch, "not json at all", TAILOR_JSON)
    result = _client(model="pro").tailor("JD", "cv", 4)
    assert result.ok and result.data.projects[0].id == "proj3"


def test_tailor_missing_sections_default_to_empty(monkeypatch):
    _mock_ok(monkeypatch, json.dumps({"experience": [{"id": "exp1", "bullets": []}]}), model="pro")
    result = _client(model="pro").tailor("JD", "cv", 4)
    assert result.ok and result.data.projects == [] and result.data.skills == {}


def test_tailor_401_is_unavailable(monkeypatch):
    _mock_status(monkeypatch, 401)                     # existing helper
    result = _client(model="pro").tailor("JD", "cv", 4)
    assert result.kind == "unavailable"
```

Also add the two re-ask tests deferred from Plan 3's review:

```python
def test_reask_second_call_failing_is_reported_as_that_failure(monkeypatch):
    _mock_then_status(monkeypatch, "not json", 500)    # first ok-but-unparseable, then HTTP 500
    result = _client().score("JD", "cv")
    assert result.kind == "transient" and "500" in result.error


def test_429_without_retry_after_has_no_retry_after(monkeypatch):
    _mock_status(monkeypatch, 429, headers={})
    result = _client().score("JD", "cv")
    assert result.kind == "transient" and result.retry_after is None
```

If the helpers named above do not exist in `tests/test_llm.py`, write them in the style already used there — do not invent a new mocking approach.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_llm.py -q`
Expected: FAIL — `cannot import name 'TailorResponse'`.

- [ ] **Step 3: Implement the prompt**

Append to `src/prompts.py`:

```python
TAILOR_SYSTEM = """\
You choose which of a candidate's existing CV items belong on a one-page resume for one job posting.

You do not write, rewrite, rephrase, summarise or invent anything. You only return IDs that appear
verbatim in the CV you are given. Any text you produce instead of an ID is a failed reply.

How to choose:
- Pick the experience entries and project entries whose evidence best matches this posting, most
  relevant first. The first ones you list are the ones that make the page.
- Inside each entry, list only the bullet IDs worth keeping, most relevant first.
- A bullet ID must be listed under the entry it belongs to. Bullet IDs start with their entry's ID.
- Skills: keep only the skills this posting cares about, under the same group names the CV uses.
  Do not add a skill the CV does not list.
- Education and the summary are always on the resume. Do not select them.

Reply with ONLY a JSON object, no prose, no code fences:
{
  "experience": [{"id": "<entry id>", "bullets": ["<bullet id>", ...]}, ...],
  "projects":   [{"id": "<entry id>", "bullets": ["<bullet id>", ...]}, ...],
  "skills": {"<group name>": ["<skill>", ...], ...}
}
Every value in "bullets" is a string ID. Empty lists are allowed.
"""


def tailor_user_message(description: str, cv_id_text: str, max_bullets: int) -> str:
    return (
        f"JOB POSTING:\n{description}\n\n---\n\nCANDIDATE CV (IDs in brackets):\n{cv_id_text}\n\n"
        f"Select at most {max_bullets} bullets per entry. Return the JSON object now."
    )
```

- [ ] **Step 4: Implement the client**

In `src/llm.py`, add the models next to `ScoreResponse`:

```python
class TailorEntry(BaseModel):
    """Strict: a bullet must be a string ID. An object or a rewritten sentence is a failed reply,
    not something to coerce — rendering unvalidated prose is exactly what this design forbids."""
    model_config = ConfigDict(strict=True, extra="ignore")

    id: str
    bullets: list[str] = Field(default_factory=list)


class TailorResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    experience: list[TailorEntry] = Field(default_factory=list)
    projects: list[TailorEntry] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
```

Widen the result type: `data: ScoreResponse | TailorResponse | None`.

Generalise the parse helper so both steps share the re-ask machinery. Replace `_parse` with a model-parameterised version and route `score()` through it:

```python
    @staticmethod
    def _parse(content: str, model_cls) -> tuple[BaseModel | None, str | None]:
        try:
            return model_cls.model_validate(json.loads(_strip_fence(content))), None
        except (ValueError, ValidationError) as e:  # json.JSONDecodeError is a ValueError
            return None, str(e)[:300]

    def _ask(self, messages: list[dict], model_cls) -> LLMResult:
        """One call, one re-ask on a schema failure. Shared by score and tailor."""
        started = time.monotonic()
        kind, content, error, retry_after, model, usage = self._chat(messages)
        if kind == "ok":
            parsed, perr = self._parse(content, model_cls)
            if parsed is None:
                messages = messages + [{"role": "assistant", "content": content},
                                       {"role": "user", "content": reask_message(perr)}]
                kind, content, error, retry_after, model2, usage2 = self._chat(messages)
                model, usage = model2 or model, _sum_usage(usage, usage2)
                if kind == "ok":
                    parsed, perr = self._parse(content, model_cls)
                    if parsed is None:
                        kind, error = "invalid", f"schema validation failed after re-ask: {perr}"
            if kind == "ok":
                return LLMResult("ok", parsed, None, None, model, usage, self._ms(started))
        return LLMResult(kind, None, error, retry_after, model, usage, self._ms(started))

    def score(self, description: str, cv_text: str) -> LLMResult:
        return self._ask([
            {"role": "system", "content": SCORE_SYSTEM},
            {"role": "user", "content": score_user_message(description, cv_text)},
        ], ScoreResponse)

    def tailor(self, description: str, cv_id_text: str, max_bullets: int) -> LLMResult:
        return self._ask([
            {"role": "system", "content": TAILOR_SYSTEM},
            {"role": "user", "content": tailor_user_message(description, cv_id_text, max_bullets)},
        ], TailorResponse)
```

Import `TAILOR_SYSTEM` and `tailor_user_message` from `prompts`.

**This refactor must not change `score()`'s behaviour.** The whole existing `tests/test_llm.py` is the check.

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/test_llm.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS, including every pre-existing score test.

- [ ] **Step 6: Commit**

```bash
git add src/prompts.py src/llm.py tests/test_llm.py
git commit -m "feat(llm): tailor prompt, strict TailorResponse, shared re-ask path"
```

---

### Task 4: Renderer

**Files:**
- Create: `src/render.py`, `src/templates/resume.html`, `src/templates/resume.css`
- Modify: `requirements.txt`, `Dockerfile`, `.github/workflows/docker-publish.yml`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `MasterCV` and the validated selection dict from Task 2.
- Produces:
  - `render.RenderResult(path: str, pages: int)` — a frozen dataclass whose `overflow` is a property, `pages > 1`
  - `render.render_pdf(cv: MasterCV, selection: dict, out_path: str) -> RenderResult`
  - `render.output_path(output_dir: str, user_id: str, job_id: int, company: str) -> str`

- [ ] **Step 1: Add the dependencies**

`requirements.txt` gains:

```
Jinja2==3.1.6
weasyprint==70.0
```

Install locally: `pip install --break-system-packages --user Jinja2==3.1.6 weasyprint==70.0`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_render.py`:

```python
from pathlib import Path

import pytest

from cv import load_cv
from render import output_path, render_pdf

FIXTURE = str(Path(__file__).parent / "fixtures" / "cv_sample.yaml")


@pytest.fixture
def cv():
    return load_cv(FIXTURE)


def _selection(cv, bullets=2):
    return {
        "experience": [{"id": e.id, "bullets": [b.id for b in e.bullets][:bullets]} for e in cv.experience[:2]],
        "projects": [{"id": p.id, "bullets": [b.id for b in p.bullets][:bullets]} for p in cv.projects[:2]],
        "skills": {k: v for k, v in cv.skills.items()},
    }


def test_renders_a_pdf_file(cv, tmp_path):
    out = str(tmp_path / "r.pdf")
    result = render_pdf(cv, _selection(cv), out)
    assert result.path == out
    assert Path(out).read_bytes().startswith(b"%PDF")
    assert result.pages >= 1


def test_a_short_selection_fits_one_page(cv, tmp_path):
    result = render_pdf(cv, _selection(cv, bullets=1), str(tmp_path / "r.pdf"))
    assert result.pages == 1 and result.overflow is False


def test_overflow_is_reported_not_trimmed(cv, tmp_path):
    # An absurd selection: every entry, every bullet, repeated until it cannot fit.
    fat = {
        "experience": [{"id": e.id, "bullets": [b.id for b in e.bullets]} for e in cv.experience] * 6,
        "projects": [{"id": p.id, "bullets": [b.id for b in p.bullets]} for p in cv.projects] * 6,
        "skills": cv.skills,
    }
    result = render_pdf(cv, fat, str(tmp_path / "r.pdf"))
    assert result.pages > 1 and result.overflow is True
    assert Path(result.path).exists()          # the PDF is kept, not discarded


def test_only_selected_bullets_are_rendered(cv, tmp_path):
    from render import build_context
    entry = cv.experience[0]
    ctx = build_context(cv, {"experience": [{"id": entry.id, "bullets": [entry.bullets[0].id]}],
                             "projects": [], "skills": {}})
    texts = [b for block in ctx["experience"] for b in block["bullets"]]
    assert texts == [entry.bullets[0].text]


def test_education_and_summary_always_come_from_the_master(cv, tmp_path):
    from render import build_context
    ctx = build_context(cv, {"experience": [], "projects": [], "skills": {}})
    assert [e.id for e in ctx["education"]] == [e.id for e in cv.education]
    assert ctx["summary"] == cv.summary


def test_selection_order_is_the_render_order(cv, tmp_path):
    from render import build_context
    reversed_ids = [e.id for e in cv.experience][::-1]
    ctx = build_context(cv, {"experience": [{"id": i, "bullets": []} for i in reversed_ids],
                             "projects": [], "skills": {}})
    assert [block["id"] for block in ctx["experience"]] == reversed_ids


def test_html_is_escaped(cv, tmp_path):
    cv.experience[0].bullets[0].text = "Built <script>alert(1)</script> pipelines"
    from render import build_html
    html = build_html(cv, {"experience": [{"id": cv.experience[0].id,
                                           "bullets": [cv.experience[0].bullets[0].id]}],
                           "projects": [], "skills": {}})
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_project_links_are_rendered(cv, tmp_path):
    from render import build_html
    project = next(p for p in cv.projects if p.link)
    html = build_html(cv, {"experience": [], "skills": {},
                           "projects": [{"id": project.id, "bullets": [project.bullets[0].id]}]})
    assert project.link in html
    if project.demo:
        assert project.demo in html


def test_output_path_is_per_user_and_slugged(tmp_path):
    p = output_path(str(tmp_path), "ron", 42, "Goldman Sachs & Co.")
    assert p.endswith("/ron/42-goldman-sachs-co.pdf")


def test_render_creates_missing_directories(cv, tmp_path):
    out = output_path(str(tmp_path / "output"), "ron", 7, "Stripe")
    render_pdf(cv, _selection(cv), out)
    assert Path(out).exists()
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python3 -m pytest tests/test_render.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'render'`.

- [ ] **Step 4: Write the template**

`src/templates/resume.html` — Jinja2 autoescaping is **on** (`autoescape=True` in Task 5's environment setup below); no `|safe` anywhere.

```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{{ name }}</title></head>
<body>
  <header>
    <h1>{{ name }}</h1>
    <p class="contact">{{ contact_line }}</p>
  </header>

  {% if summary %}<section><p class="summary">{{ summary }}</p></section>{% endif %}

  {% if education %}
  <section>
    <h2>Education</h2>
    {% for e in education %}
    <div class="entry">
      <div class="row"><span class="left">{{ e.school }}</span><span class="right">{{ e.dates }}</span></div>
      <div class="row"><span class="sub">{{ e.degree }}</span></div>
      {% if e.details %}<ul>{% for d in e.details %}<li>{{ d }}</li>{% endfor %}</ul>{% endif %}
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if experience %}
  <section>
    <h2>Experience</h2>
    {% for x in experience %}
    <div class="entry">
      <div class="row"><span class="left">{{ x.company }}</span><span class="right">{{ x.dates }}</span></div>
      <div class="row"><span class="sub">{{ x.title }}</span>{% if x.location %}<span class="right">{{ x.location }}</span>{% endif %}</div>
      <ul>{% for b in x.bullets %}<li>{{ b }}</li>{% endfor %}</ul>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if projects %}
  <section>
    <h2>Projects</h2>
    {% for p in projects %}
    <div class="entry">
      <div class="row">
        <span class="left">{{ p.name }}</span>
        {% if p.tech %}<span class="tech">{{ p.tech | join(', ') }}</span>{% endif %}
        {% if p.dates %}<span class="right">{{ p.dates }}</span>{% endif %}
      </div>
      {% if p.link or p.demo %}
      <div class="row"><span class="links">
        {% if p.link %}<a href="{{ p.link }}">{{ p.link }}</a>{% endif %}
        {% if p.demo %}{% if p.link %} · {% endif %}<a href="{{ p.demo }}">{{ p.demo }}</a>{% endif %}
      </span></div>
      {% endif %}
      <ul>{% for b in p.bullets %}<li>{{ b }}</li>{% endfor %}</ul>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if skills %}
  <section>
    <h2>Skills</h2>
    {% for group, items in skills.items() %}
    <p class="skills"><span class="left">{{ group | title }}:</span> {{ items | join(', ') }}</p>
    {% endfor %}
  </section>
  {% endif %}
</body>
</html>
```

`src/templates/resume.css` — sized so a realistic selection lands on one page:

```css
@page { size: letter; margin: 0.5in; }

body { font-family: "DejaVu Sans", sans-serif; font-size: 9.5pt; line-height: 1.25; color: #111; }
h1 { font-size: 17pt; margin: 0; letter-spacing: 0.5pt; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: 0.6pt;
     border-bottom: 0.7pt solid #444; margin: 9pt 0 4pt; padding-bottom: 1pt; }
header { margin-bottom: 6pt; }
.contact { font-size: 8.5pt; color: #444; margin: 2pt 0 0; }
.summary { margin: 0 0 2pt; }
.entry { margin-bottom: 5pt; }
.row { display: flex; justify-content: space-between; }
.left { font-weight: bold; }
.sub { font-style: italic; }
.right { color: #444; font-size: 8.5pt; white-space: nowrap; }
.tech { font-size: 8.5pt; color: #444; }
.links { font-size: 8pt; }
.links a { color: #1a4f8a; text-decoration: none; }
ul { margin: 1pt 0 0; padding-left: 12pt; }
li { margin-bottom: 1pt; }
.skills { margin: 1pt 0; }
```

- [ ] **Step 5: Implement `src/render.py`**

```python
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import CSS, HTML

from cv import MasterCV

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),   # CV text is data; never markup
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class RenderResult:
    path: str
    pages: int

    @property
    def overflow(self) -> bool:
        return self.pages > 1


def output_path(output_dir: str, user_id: str, job_id: int, company: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", company or "").strip("-").lower()[:40] or "job"
    return str(Path(output_dir) / user_id / f"{job_id}-{slug}.pdf")


def _contact_line(cv: MasterCV) -> str:
    c = cv.contact
    parts = [c.email, c.phone, c.location, c.linkedin, c.github]
    return " · ".join(p for p in parts if p)


def _blocks(entries, chosen, extra):
    """Entries in the selection's order, carrying only the selected bullets' text."""
    by_id = {e.id: e for e in entries}
    out = []
    for item in chosen:
        entry = by_id.get(item["id"])
        if entry is None:      # validate_selection guarantees this, but the snapshot is user data
            continue
        texts = {b.id: b.text for b in entry.bullets}
        block = {"id": entry.id, "bullets": [texts[b] for b in item["bullets"] if b in texts]}
        block.update(extra(entry))
        out.append(block)
    return out


def build_context(cv: MasterCV, selection: dict) -> dict:
    return {
        "name": cv.name,
        "contact_line": _contact_line(cv),
        "summary": cv.summary,
        "education": cv.education,                       # always the master's, in full
        "experience": _blocks(cv.experience, selection.get("experience", []),
                              lambda e: {"company": e.company, "title": e.title,
                                         "dates": e.dates, "location": e.location}),
        "projects": _blocks(cv.projects, selection.get("projects", []),
                            # link and demo are the whole point of a project entry on a resume.
                            lambda p: {"name": p.name, "tech": p.tech, "dates": p.dates,
                                       "link": p.link, "demo": p.demo}),
        "skills": selection.get("skills", {}),
    }


def build_html(cv: MasterCV, selection: dict) -> str:
    return _env.get_template("resume.html").render(**build_context(cv, selection))


def render_pdf(cv: MasterCV, selection: dict, out_path: str) -> RenderResult:
    """Snapshot + validated selection → one-page-target PDF. Overflow is reported, never trimmed."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    document = HTML(string=build_html(cv, selection)).render(
        stylesheets=[CSS(filename=str(TEMPLATE_DIR / "resume.css"))]
    )
    document.write_pdf(out_path)
    return RenderResult(out_path, len(document.pages))
```

- [ ] **Step 6: Run the tests**

Run: `python3 -m pytest tests/test_render.py -q`
Expected: PASS. If `test_a_short_selection_fits_one_page` fails, adjust the CSS sizes — not the test.

- [ ] **Step 7: Container and CI**

`Dockerfile` — WeasyPrint needs pango/cairo at runtime:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# WeasyPrint renders through pango/cairo and needs a font that covers the CV's characters.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
        libgdk-pixbuf-2.0-0 libffi8 shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV PYTHONPATH=/app/src

CMD ["python", "src/main.py"]
```

In `.github/workflows/docker-publish.yml`, replace the import smoke test with one that actually renders, so a missing system library fails CI rather than the server:

```yaml
      - name: Smoke-test the image renders a PDF
        run: |
          docker run --rm internship-tracker:ci python -c "
          import main, render
          from cv import MasterCV
          cv = MasterCV.model_validate({
              'name': 'Smoke Test',
              'contact': {'email': 's@example.com'},
              'education': [{'id': 'edu1', 'school': 'U', 'degree': 'BS', 'dates': '2024'}],
              'experience': [{'id': 'exp1', 'company': 'C', 'title': 'T', 'dates': 'D',
                              'bullets': [{'id': 'exp1.b1', 'text': 'Did a thing'}]}],
          })
          r = render.render_pdf(cv, {'experience': [{'id': 'exp1', 'bullets': ['exp1.b1']}],
                                     'projects': [], 'skills': {}}, '/tmp/smoke.pdf')
          assert open('/tmp/smoke.pdf','rb').read(4) == b'%PDF', 'not a PDF'
          assert r.pages == 1, r.pages
          print('render smoke ok')
          "
```

Also confirm the `docker` job still builds: `docker build -t internship-tracker:local .` locally if Docker is available; otherwise rely on CI.

- [ ] **Step 8: Run the full suite and commit**

```bash
python3 -m pytest tests/ -q
git add src/render.py src/templates requirements.txt Dockerfile .github/workflows/docker-publish.yml tests/test_render.py
git commit -m "feat(render): Jinja2 + WeasyPrint one-page resume, page-count overflow flag, CI render smoke test"
```

---

### Task 5: Discord message variants and attachment robustness

**Files:**
- Modify: `src/discord_client.py`
- Test: `tests/test_discord_client.py`

**Interfaces:**
- Produces: `format_match(..., matched: bool, overflow: bool = False, resume_missing: bool = False)`; `cap_content(header, lists, tail="", body="")`; `DeliveryResult.kind` gains `"attachment"`; `send_message` no longer raises on an unreadable PDF.

**Why:** Plan 2 left `send_message`'s PDF branch raising `OSError` on a missing file — harmless while nothing set `pdf_path`, a crash loop the moment Task 7 does. `cap_content`'s `tail` parameter, unused until now, is what keeps the notice lines from being truncated away.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_discord_client.py`:

```python
OVERFLOW_NOTE = "📄 Resume ran over one page — trim before sending"
NO_RESUME_NOTE = "⚠️ Couldn't generate resume — apply with your master CV."


def test_match_message_has_no_notes_by_default():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why", [], [], matched=True)
    assert OVERFLOW_NOTE not in msg and NO_RESUME_NOTE not in msg


def test_overflow_note_appended():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why", [], [], matched=True, overflow=True)
    assert msg.rstrip().endswith(OVERFLOW_NOTE)


def test_missing_resume_note_appended():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why", [], [], matched=True, resume_missing=True)
    assert NO_RESUME_NOTE in msg


def test_notes_survive_an_oversized_gap_list():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "why",
                       ["a very long requirement " * 20] * 30, [], matched=True,
                       overflow=True, resume_missing=True)
    assert len(msg) <= 2000
    assert OVERFLOW_NOTE in msg and NO_RESUME_NOTE in msg


def test_notes_survive_an_oversized_reasoning():
    # Nothing bounds reasoning: the prompt asks for under 400 characters, the schema does not.
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "r" * 2100, ["gap"], ["unknown"],
                       matched=True, overflow=True, resume_missing=True)
    assert len(msg) <= 2000
    assert msg.startswith("🎯 82% — **Stripe** — SWE")
    assert OVERFLOW_NOTE in msg and NO_RESUME_NOTE in msg


def test_lists_are_dropped_before_the_reasoning_is():
    # Spec order: gaps and unknowns are truncated first, the reasoning second.
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "r" * 1900, ["a confirmed gap"], [],
                       matched=True)
    assert len(msg) <= 2000
    assert "r" * 1000 in msg
    assert "a confirmed gap" not in msg


def test_a_short_message_is_untouched():
    msg = format_match("Stripe", "SWE", "SF", "https://x", 82, "short why", ["g"], ["u"], matched=True)
    assert msg == ("🎯 82% — **Stripe** — SWE\n📍 SF\n🔗 https://x\n"
                   "✅ Why: short why\n⚠️ Gaps: g\n❓ Not on CV: u")


def test_send_message_with_a_missing_pdf_reports_an_attachment_failure(tmp_path):
    result = send_message("https://d/x", "hi", str(tmp_path / "gone.pdf"))
    assert result.kind == "attachment" and "gone.pdf" in result.error


def test_send_message_with_an_unreadable_pdf_reports_an_attachment_failure(tmp_path):
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(b"%PDF stub")
    pdf.chmod(0o000)
    try:
        result = send_message("https://d/x", "hi", str(pdf))
    finally:
        pdf.chmod(0o644)
    assert result.kind == "attachment"      # exists() is true; opening it is what fails


def test_attachment_failure_makes_no_request(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("discord_client.requests.post", lambda *a, **k: calls.append(1))
    send_message("https://d/x", "hi", str(tmp_path / "gone.pdf"))
    assert calls == []


def test_send_message_attaches_the_pdf(monkeypatch, tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7 stub")
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return _resp(200, {"id": "1"})          # existing helper in this file

    monkeypatch.setattr("discord_client.requests.post", fake_post)
    assert send_message("https://d/x", "hi", str(pdf)).ok
    assert seen["params"] == {"wait": "true"}
    assert "files[0]" in seen["files"]
    assert json.loads(seen["data"]["payload_json"])["content"] == "hi"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_discord_client.py -q`
Expected: FAIL — `format_match() got an unexpected keyword argument 'overflow'`, and `FileNotFoundError` from the missing-PDF test.

- [ ] **Step 3: Implement**

In `src/discord_client.py`:

```python
OVERFLOW_NOTE = "📄 Resume ran over one page — trim before sending"
NO_RESUME_NOTE = "⚠️ Couldn't generate resume — apply with your master CV."
```

and widen the kind union in `DeliveryResult`'s comment:

```python
    kind: str  # "ok" | "transient" | "gone" | "invalid" | "attachment"
```

Rewrite `cap_content` so the notes cannot be truncated away. Today the reasoning lives inside
`header`; nothing bounds `ScoreResponse.reasoning`, so a 2,100-character one drives `remaining`
negative, and the final `out[:MAX_CONTENT - 1]` cuts the tail — both notices — off the end. The
reasoning becomes a separate, truncatable `body`:

```python
def cap_content(header: str, lists: list[str], tail: str = "", body: str = "") -> str:
    """Join header + body + list lines + tail under MAX_CONTENT.

    Priority when it does not fit: the header and the tail always survive. The tail carries the
    resume notices, and the difference between "apply with this PDF" and "apply with your master
    CV" must not be what gets dropped. List lines go first, then the body (the reasoning).
    """
    tail_cost = len(tail) + 1 if tail else 0
    if len(header) + tail_cost > MAX_CONTENT:
        # Pathological: even the header does not fit. It gives way, never the tail.
        keep = MAX_CONTENT - tail_cost - 1
        head = header[:keep] + _ELLIPSIS if keep > 0 else ""
        return "\n".join(p for p in (head, tail) if p)

    remaining = MAX_CONTENT - len(header) - tail_cost
    kept_body = ""
    if body and remaining > 2:
        kept_body = body if len(body) + 1 <= remaining else body[: remaining - 2] + _ELLIPSIS
        remaining -= len(kept_body) + 1

    kept_lists = []
    for line in lists:
        if remaining <= 2:
            break
        if len(line) + 1 > remaining:
            line = line[: remaining - 2] + _ELLIPSIS
        kept_lists.append(line)
        remaining -= len(line) + 1

    return "\n".join(p for p in (header, kept_body, *kept_lists, tail) if p)
```

Extend `format_match` to feed it:

```python
def format_match(
    company: str, role: str, location: str, url: str,
    score: int, reasoning: str, missing_confirmed: list[str], missing_unknown: list[str],
    matched: bool, overflow: bool = False, resume_missing: bool = False,
) -> str:
    icon = "🎯" if matched else "📉"
    header = f"{icon} {score}% — **{company}** — {role}\n📍 {location}\n🔗 {url}"
    body = f"✅ Why: {reasoning}" if reasoning else ""
    lists = []
    if missing_confirmed:
        lists.append("⚠️ Gaps: " + ", ".join(missing_confirmed))
    if missing_unknown:
        lists.append("❓ Not on CV: " + ", ".join(missing_unknown))

    notes = []
    if resume_missing:
        notes.append(NO_RESUME_NOTE)
    if overflow:
        notes.append(OVERFLOW_NOTE)
    return cap_content(header, lists, tail="\n".join(notes), body=body)
```

`format_link_only` does not change. Check the existing `cap_content` tests in
`tests/test_discord_client.py` — the ones asserting that a long gaps list is truncated must still
pass; the reasoning moving out of `header` is what makes them assert something different from before,
so read each one and update the expectation deliberately rather than loosening the assertion.

Make the attachment branch of `send_message` total:

```python
    try:
        if pdf_path:
            try:
                fh = open(pdf_path, "rb")
            except OSError as e:
                # A distinct kind, not "invalid": the message is fine, only the file is not, and
                # `invalid` is retried forever. The worker drops the attachment and sends the rest.
                return DeliveryResult("attachment", None, f"cannot read {pdf_path}: {e}")
            with fh:
                resp = requests.post(...)   # unchanged
        else:
            resp = requests.post(...)       # unchanged
    except requests.RequestException as e:
        return DeliveryResult("transient", None, str(e))
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_discord_client.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discord_client.py tests/test_discord_client.py
git commit -m "feat(discord): resume notes in match messages, unreadable PDF is a delivery error not a crash"
```

---

### Task 6: Worker — the tailor stage

**Files:**
- Modify: `src/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `cv.cv_to_id_text`, `cv.validate_selection` (Task 2); `LLMClient.tailor` (Task 3); `db.STAGE_TAILOR`, `Evaluation.resume_error` (Task 1).
- Produces: `worker.TAILOR_BUDGET = 3`; `Worker(..., tailor=None, output_dir=None, max_bullets=4)`; `Worker.tailor(session, ev)`; `Worker._next_tailorable(session)`; pause keys `llm` and `llm:<model>`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker.py`:

```python
from db import STAGE_RENDER, STAGE_SCORE, STAGE_TAILOR
from worker import TAILOR_BUDGET

# Two entries per section, so the too-few-entries fallback does not quietly replace what the
# fake returned. CV_SNAPSHOT is load_cv(tests/fixtures/cv_tailor.yaml).model_dump() (Task 2).
SELECTION = {
    "experience": [{"id": "exp1", "bullets": ["exp1.b1"]}, {"id": "exp2", "bullets": ["exp2.b1"]}],
    "projects": [{"id": "proj1", "bullets": ["proj1.b1"]}, {"id": "proj2", "bullets": ["proj2.b1"]}],
    "skills": {},
}


class FakeTailor:
    """Stands in for an LLMClient built with LLM_TAILOR_MODEL."""
    model = "pro"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def tailor(self, description, cv_id_text, max_bullets):
        self.calls.append((description, cv_id_text, max_bullets))
        return self.results.pop(0) if self.results else _tailor_ok()


def _tailor_ok(data=None):
    from llm import TailorResponse
    return LLMResult("ok", TailorResponse.model_validate(data or SELECTION), None, None, "pro", {}, 5)


def _seed_tailorable(session, user_id="ron", **kw):
    ev = _seed(session, user_id, stage=STAGE_TAILOR, score=82, outcome="matched",
               cv_snapshot=CV_SNAPSHOT, **kw)
    ev.job.description = "JOB DESCRIPTION TEXT"
    session.commit()
    return _resolve(session, ev)


def test_tailor_stores_the_validated_selection_and_moves_to_render(session_factory, session, clock, tmp_path):
    ev = _seed_tailorable(session)
    w = _worker_t(session_factory, FakeTailor(), clock, tmp_path)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.stage == STAGE_RENDER
    assert ev.tailored["experience"][0]["bullets"] == ["exp1.b1"]
    assert ev.attempts == 0            # the counter resets for the next stage
    assert ev.outcome == "matched"     # never overwritten


def test_tailor_is_sent_the_snapshot_not_the_live_cv(session_factory, session, clock, tmp_path):
    # The CV on disk is edited mid-evaluation; the prompt must still describe what was scored.
    snapshot = json.loads(json.dumps(CV_SNAPSHOT))
    snapshot["experience"][0]["bullets"][0]["text"] = "SNAPSHOT BULLET"
    live = json.loads(json.dumps(CV_SNAPSHOT))
    live["experience"][0]["bullets"][0]["text"] = "LIVE BULLET"
    ev = _seed_tailorable(session)
    ev.cv_snapshot = snapshot
    session.commit()
    tailor = FakeTailor()
    _worker_t(session_factory, tailor, clock, tmp_path, cvs={"ron": live}).run_once()
    description, cv_id_text, max_bullets = tailor.calls[0]
    assert "SNAPSHOT BULLET" in cv_id_text and "LIVE BULLET" not in cv_id_text
    assert description == "JOB DESCRIPTION TEXT"
    assert max_bullets == 4


def test_invalid_ids_are_dropped_before_storage(session_factory, session, clock, tmp_path):
    ev = _seed_tailorable(session)
    junk = {"experience": [{"id": "does-not-exist", "bullets": ["nope"]}], "projects": [], "skills": {}}
    _worker_t(session_factory, FakeTailor(_tailor_ok(junk)), clock, tmp_path).run_once()
    session.refresh(ev)
    ids = [e["id"] for e in ev.tailored["experience"]]
    assert "does-not-exist" not in ids and ids                # fell back to the master's order


def test_giving_up_clears_a_previous_runs_pdf(session_factory, session, clock, tmp_path):
    # Re-scoring an already-delivered row leaves the old PDF on the evaluation.
    ev = _seed_tailorable(session, attempts=TAILOR_BUDGET - 1,
                          pdf_path="/data/output/ron/1-stripe.pdf", page_overflow=True)
    w = _worker_t(session_factory, FakeTailor(LLMResult("invalid", None, "bad", None, "pro", None, 1)),
                  clock, tmp_path)
    w.run_once()
    session.refresh(ev)
    assert ev.pdf_path is None and ev.page_overflow is False
    assert "Couldn't generate resume" in message_for(ev)


def test_tailor_budget_exhausted_delivers_the_score_without_a_pdf(session_factory, session, clock, tmp_path):
    ev = _seed_tailorable(session, attempts=TAILOR_BUDGET - 1)
    w = _worker_t(session_factory, FakeTailor(LLMResult("invalid", None, "bad json", None, "pro", None, 3)),
                  clock, tmp_path)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER and ev.outcome == "matched"
    assert ev.pdf_path is None and "bad json" in ev.resume_error
    assert ev.attempts == 0


def test_tailor_transient_pauses_the_endpoint_for_both_stages(session_factory, session, clock, tmp_path):
    _seed_tailorable(session)
    w = _worker_t(session_factory, FakeTailor(LLMResult("transient", None, "429", 60.0, "pro", None, 2)),
                  clock, tmp_path)
    w.run_once()
    assert w.is_paused("llm") is True


def test_tailor_unavailable_pauses_only_its_own_model(session_factory, session, clock, tmp_path):
    _seed_tailorable(session)
    w = _worker_t(session_factory, FakeTailor(LLMResult("unavailable", None, "HTTP 404: no such model",
                                                        None, None, None, 2)), clock, tmp_path)
    w.run_once()
    assert w.is_paused("llm:pro") is True
    assert w.is_paused("llm") is False          # scoring keeps running on the flash model


def test_unavailable_tailor_gives_the_lease_back(session_factory, session, clock, tmp_path):
    ev = _seed_tailorable(session)
    w = _worker_t(session_factory, FakeTailor(LLMResult("unavailable", None, "401", None, None, None, 1)),
                  clock, tmp_path)
    w.run_once()
    session.refresh(ev)
    assert ev.attempts == 0 and ev.stage == STAGE_TAILOR


def test_over_budget_on_restart_makes_no_call(session_factory, session, clock, tmp_path):
    ev = _seed_tailorable(session, attempts=TAILOR_BUDGET)
    tailor = FakeTailor()
    _worker_t(session_factory, tailor, clock, tmp_path).run_once()
    session.refresh(ev)
    assert tailor.calls == [] and ev.stage == STAGE_DELIVER and "budget" in ev.resume_error


def test_attempt_is_leased_before_the_call(session_factory, session, clock, tmp_path):
    ev = _seed_tailorable(session)

    def boom(description, cv_id_text, max_bullets):
        raise RuntimeError("killed mid-call")

    tailor = FakeTailor()
    tailor.tailor = boom
    _worker_t(session_factory, tailor, clock, tmp_path).run_once()
    session.refresh(ev)
    assert ev.attempts == 1          # the attempt was committed before the crash-prone call


def test_matched_goes_to_tailor_when_tailoring_is_configured(session_factory, session, clock, tmp_path):
    ev = _seed_scoreable(session, "ron")          # existing Plan 3 helper
    w = _worker_st(session_factory, FakeLLM(_llm_ok()), FakeTailor(), clock, tmp_path)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_TAILOR


def test_matched_goes_straight_to_deliver_when_tailoring_is_not_configured(session_factory, session, clock):
    ev = _seed_scoreable(session, "ron")
    _worker_s(session_factory, FakeLLM(_llm_ok()), clock, FakeSender()).run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER              # Plan 3 behaviour is unchanged


def test_tailor_skipped_while_the_users_webhook_is_paused(session_factory, session, clock, tmp_path):
    _seed_tailorable(session)
    tailor = FakeTailor()
    w = _worker_t(session_factory, tailor, clock, tmp_path)
    w._pause("discord:ron", None, "gone")
    assert w.run_once() is False and tailor.calls == []    # no tokens spent on an undeliverable row
```

Write `_worker_t` / `_worker_st` next to the existing `_worker_s` helper, passing `tailor=`, `output_dir=str(tmp_path)` and `cvs={"ron": CV_SNAPSHOT, "cousin": CV_SNAPSHOT}`. Define `CV_SNAPSHOT` once at module level:

```python
CV_SNAPSHOT = load_cv(str(Path(__file__).parent / "fixtures" / "cv_tailor.yaml")).model_dump()
```

`SELECTION`'s ids must exist in that fixture (`exp1`/`exp1.b1`, `exp2`/`exp2.b1`, `proj1`, `proj2`) — Task 2 creates it with `exp1`–`exp3` and `proj1`–`proj4`, so they do. The Plan 1–3 tests keep using `cv_sample.yaml`; do not repoint them.

`json` is needed at the top of this file for the snapshot-vs-live test's deep copies.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_worker.py -q`
Expected: FAIL — `Worker.__init__() got an unexpected keyword argument 'tailor'`.

- [ ] **Step 3: Implement**

In `src/worker.py`:

```python
TAILOR_BUDGET = 3
```

Constructor gains `tailor=None, output_dir: str | None = None, max_bullets: int = 4`, stored as `self._tailor`, `self._output_dir`, `self._max_bullets`, plus:

```python
    @property
    def _tailoring_enabled(self) -> bool:
        return self._tailor is not None and self._output_dir is not None
```

Split the pause check used by both LLM stages:

```python
    @staticmethod
    def _model_name(client) -> str:
        """The alias this client was configured with — the only thing pause keys are built from."""
        return getattr(client, "model", "?")

    def _llm_paused(self, client) -> bool:
        # "llm" is the endpoint (a 429/5xx holds both stages); "llm:<alias>" is one bad model or key.
        return self.is_paused("llm") or self.is_paused(f"llm:{self._model_name(client)}")
```

`_next_scoreable` uses `self._llm_paused(self._llm)` in place of `self.is_paused("llm")`. In `score()`, both the `unavailable` branch **and** the invalid-streak breaker pause `f"llm:{self._model_name(self._llm)}"` instead of `"llm"` — both mean "this alias or key is wrong", which is per-model; the `transient` branch still pauses `"llm"`, which is the endpoint. Replace `_llm_model_name()` with `_model_name(self._llm)` throughout and delete the old helper.

**Three Plan 3 tests assert the old key and must be updated** — this is a deliberate behaviour change, not a regression:
- `test_score_unavailable_pauses_llm_without_consuming_attempt` (`tests/test_worker.py:843`)
- `test_score_unavailable_resume_is_measured_from_after_the_call` (`tests/test_worker.py:795`)
- the invalid-streak breaker assertion at `tests/test_worker.py:747`

Each `w.paused["llm"]` in those three becomes `w.paused["llm:flash"]`, and `FakeLLM` gains `model = "flash"` so the key is a real alias rather than `"?"`. The **transient** assertions (`tests/test_worker.py:686`, `:711`, `:787`) keep `paused["llm"]` unchanged — verify that by reading each one, not by assuming. Add one new test proving the split:

```python
def test_unavailable_score_does_not_pause_the_tailor_model(session_factory, session, clock, tmp_path):
    _seed_scoreable(session, "ron")
    _seed_tailorable(session, "cousin")
    w = _worker_st(session_factory, FakeLLM(LLM_DOWN), FakeTailor(), clock, tmp_path)
    w.run_once()
    assert w.is_paused("llm:flash") is True and w.is_paused("llm:pro") is False
    assert w.run_once() is True          # the tailor row still moves
```

In `score()`'s success path, replace the matched branch:

```python
            if data.score >= user.threshold:
                ev.outcome = "matched"
                ev.stage = STAGE_TAILOR if self._tailoring_enabled else STAGE_DELIVER
                ev.attempts = 0            # the budget belongs to the stage, not the row
```

Add the selector and the stage:

```python
    def _next_tailorable(self, session: Session) -> Evaluation | None:
        if self._tailor is None or self._llm_paused(self._tailor):
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_TAILOR, Evaluation.next_attempt_at <= now)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    def tailor(self, session: Session, ev: Evaluation) -> None:
        now = self._now()
        if ev.attempts >= TAILOR_BUDGET:
            # Crashed attempts can leave the row at the budget with no selection: no further call.
            self._give_up_resume(ev, now, f"tailor budget exhausted after {ev.attempts} attempts")
            return
        try:
            cv = MasterCV.model_validate(ev.cv_snapshot)
        except ValidationError as e:
            self._give_up_resume(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:200]}")
            return

        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()        # lease before the slow call, exactly as score and fetch do

        try:
            result = self._tailor.tailor((ev.job.description or "")[:DESCRIPTION_CAP],
                                         cv_to_id_text(cv), self._max_bullets)
        except Exception as e:  # noqa: BLE001 — a client bug is a failed attempt, not a dead worker
            log.exception("Tailor client raised for evaluation %d", ev.id)
            result = LLMResult("transient", None, f"{type(e).__name__}: {e}", None, None, None, 0)
        after = self._now()

        log.info("tailor ev=%d user=%s model=%s outcome=%s ms=%d%s",
                 ev.id, ev.user_id, result.model or getattr(self._tailor, "model", "?"),
                 result.kind, result.ms, f" error={result.error}" if result.error else "")

        if result.ok:
            selection, warnings = validate_selection(cv, result.data.model_dump(), self._max_bullets)
            for w in warnings:
                log.warning("tailor ev=%d: %s", ev.id, w)
            ev.tailored = selection
            ev.tailor_model = result.model
            ev.stage, ev.attempts, ev.next_attempt_at = STAGE_RENDER, 0, after
            return

        if result.kind == "unavailable":
            ev.attempts -= 1                      # not this row's fault: hand the lease back
            resume = after + timedelta(seconds=LLM_PAUSE_SECONDS)
            ev.next_attempt_at = resume
            # The CONFIGURED alias, never result.model. On a re-ask whose first call succeeded and
            # whose second returned 401, LLMResult carries the backend's own model name — pausing
            # that would write a key `_llm_paused` never reads, and the cooldown would do nothing.
            self._pause(f"llm:{self._model_name(self._tailor)}", resume, result.error or "unavailable")
            return

        ev.last_error = result.error
        delay = max(result.retry_after if result.retry_after is not None else backoff(ev.attempts), 1)
        if result.kind == "transient":
            self._pause("llm", after + timedelta(seconds=delay), result.error or result.kind)
        if ev.attempts >= TAILOR_BUDGET:
            self._give_up_resume(ev, after, result.error or result.kind)
            return
        ev.next_attempt_at = after + timedelta(seconds=delay)

    def _give_up_resume(self, ev: Evaluation, when: datetime, why: str) -> None:
        """No PDF for this row. The score message still goes out; `outcome` stays as scored."""
        ev.resume_error = why
        ev.last_error = why
        # A re-scored row can still be carrying the PREVIOUS run's PDF. Attaching it here would
        # ship an old resume under a new score, and pdf_path being set would also suppress the
        # "couldn't generate resume" notice. Clear it: this row has no resume.
        ev.pdf_path, ev.page_overflow = None, False
        ev.stage, ev.attempts, ev.next_attempt_at = STAGE_DELIVER, 0, when
        log.warning("Evaluation %d: no tailored resume (%s)", ev.id, why)
```

Add `tailor_model: Mapped[str | None] = mapped_column(String, nullable=True)` to `Evaluation` in `db.py` (same rationale as `score_model`: knowing which model produced a selection is what makes the ops table readable).

Imports to add in `worker.py`: `STAGE_TAILOR`, `STAGE_RENDER` from `db`; `cv_to_id_text`, `validate_selection` from `cv`.

Wire the stage into `run_once()` (the render slot arrives in Task 7 — until then, tailor goes last):

```python
            ev = self._next_tailorable(session)
            if ev is not None:
                self.tailor(session, ev)
                session.commit()
                return True
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_worker.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS, with every Plan 3 worker test unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/worker.py src/db.py tests/test_worker.py
git commit -m "feat(worker): tailor stage with per-stage budget, snapshot-only inputs, split LLM pause keys"
```

---

### Task 7: Worker — the render stage and PDF delivery

**Files:**
- Modify: `src/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `render.render_pdf`, `render.output_path` (Task 4); `discord_client.format_match`'s new arguments (Task 5).
- Produces: `worker.RENDER_BUDGET = 2`; `Worker.render(session, ev)`; `Worker._next_renderable(session)`; `run_once()` order `deliver → fetch → render → score → tailor`; `message_for` rendering the notes.

- [ ] **Step 1: Write the failing tests**

```python
from worker import RENDER_BUDGET


class FakeRenderer:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cv, selection, out_path):
        self.calls.append((cv, selection, out_path))
        r = self.results.pop(0) if self.results else 1
        if isinstance(r, Exception):
            raise r
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"%PDF-1.7 stub")
        from render import RenderResult
        return RenderResult(out_path, r)


def _seed_renderable(session, user_id="ron", **kw):
    ev = _seed(session, user_id, stage=STAGE_RENDER, score=82, outcome="matched",
               cv_snapshot=CV_SNAPSHOT, tailored=SELECTION, **kw)
    return _resolve(session, ev)


def test_render_writes_the_pdf_and_moves_to_deliver(session_factory, session, clock, tmp_path):
    ev = _seed_renderable(session)
    renderer = FakeRenderer(1)
    w = _worker_r(session_factory, renderer, clock, tmp_path)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER and ev.attempts == 0
    assert ev.pdf_path.endswith("/ron/%d-stripe.pdf" % ev.job_id)
    assert Path(ev.pdf_path).read_bytes().startswith(b"%PDF")
    assert ev.page_overflow is False and ev.resume_error is None


def test_two_page_render_sets_overflow_and_keeps_the_pdf(session_factory, session, clock, tmp_path):
    ev = _seed_renderable(session)
    _worker_r(session_factory, FakeRenderer(2), clock, tmp_path).run_once()
    session.refresh(ev)
    assert ev.page_overflow is True and ev.stage == STAGE_DELIVER and Path(ev.pdf_path).exists()


def test_render_uses_the_snapshot_and_the_stored_selection(session_factory, session, clock, tmp_path):
    ev = _seed_renderable(session)
    renderer = FakeRenderer(1)
    _worker_r(session_factory, renderer, clock, tmp_path,
              cvs={"ron": {**CV_SNAPSHOT, "name": "Live Person"}}).run_once()
    rendered_cv, selection, _ = renderer.calls[0]
    assert rendered_cv.name == CV_SNAPSHOT["name"] != "Live Person"
    assert selection == SELECTION            # no second LLM call after a crash


def test_render_failure_retries_then_gives_up_with_the_score_message(session_factory, session, clock, tmp_path):
    ev = _seed_renderable(session)
    w = _worker_r(session_factory, FakeRenderer(RuntimeError("pango exploded"),
                                                RuntimeError("pango exploded")), clock, tmp_path)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_RENDER and ev.attempts == 1
    clock.advance(BACKOFF_SECONDS[1])
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER and ev.pdf_path is None
    assert "pango exploded" in ev.resume_error and ev.outcome == "matched"
    assert ev.attempts == 0


def test_render_over_budget_on_restart_makes_no_call(session_factory, session, clock, tmp_path):
    ev = _seed_renderable(session, attempts=RENDER_BUDGET)
    renderer = FakeRenderer(1)
    _worker_r(session_factory, renderer, clock, tmp_path).run_once()
    session.refresh(ev)
    assert renderer.calls == [] and ev.stage == STAGE_DELIVER and "budget" in ev.resume_error


def test_render_leases_the_attempt_before_the_call(session_factory, session, clock, tmp_path):
    ev = _seed_renderable(session)
    _worker_r(session_factory, FakeRenderer(RuntimeError("boom")), clock, tmp_path).run_once()
    session.refresh(ev)
    assert ev.attempts == 1


def test_delivery_attaches_the_pdf_and_closes(session_factory, session, clock, tmp_path):
    pdf = tmp_path / "ron" / "1-stripe.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF stub")
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched", pdf_path=str(pdf))
    _resolve(session, ev)
    sender = FakeSender(OK)
    _worker(session_factory, sender, clock).run_once()
    session.refresh(ev)
    assert sender.calls[0][2] == str(pdf) and ev.stage == STAGE_CLOSED


def test_delivery_retry_reuses_the_same_pdf(session_factory, session, clock, tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF stub")
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched", pdf_path=str(pdf))
    _resolve(session, ev)
    sender = FakeSender(DeliveryResult("transient", None, "503"), OK)
    w = _worker(session_factory, sender, clock)
    w.run_once()
    clock.advance(3600)
    w.run_once()
    session.refresh(ev)
    assert [c[2] for c in sender.calls] == [str(pdf), str(pdf)]
    assert ev.stage == STAGE_CLOSED and Path(pdf).exists()


def test_an_unreadable_pdf_degrades_to_the_score_message(session_factory, session, clock, tmp_path):
    # exists() is true and open() fails: the path Task 7's precheck cannot see.
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(b"%PDF stub")
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched",
               reasoning="why", pdf_path=str(pdf))
    _resolve(session, ev)
    sender = FakeSender(DeliveryResult("attachment", None, f"cannot read {pdf}: denied"), OK)
    w = _worker(session_factory, sender, clock)
    w.run_once()
    session.refresh(ev)
    assert ev.pdf_path is None and ev.delivery_attempts == 0    # nothing was sent, nothing counted
    assert ev.stage == STAGE_DELIVER
    w.run_once()
    session.refresh(ev)
    assert sender.calls[1][2] is None and "Couldn't generate resume" in sender.calls[1][1]
    assert ev.stage == STAGE_CLOSED


def test_a_vanished_pdf_degrades_to_the_score_message(session_factory, session, clock, tmp_path):
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched",
               reasoning="why", pdf_path=str(tmp_path / "gone.pdf"))
    _resolve(session, ev)
    sender = FakeSender(OK)
    _worker(session_factory, sender, clock).run_once()
    session.refresh(ev)
    assert sender.calls[0][2] is None
    assert "Couldn't generate resume" in sender.calls[0][1]
    assert ev.stage == STAGE_CLOSED


def test_message_for_a_match_without_a_resume_carries_the_note(session_factory, session):
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched",
               reasoning="why", resume_error="tailor budget exhausted")
    assert "Couldn't generate resume" in message_for(ev)
    assert message_for(ev).startswith("🎯 82%")


def test_message_for_an_overflowing_resume_says_so(session_factory, session):
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched",
               reasoning="why", pdf_path="/x/r.pdf", page_overflow=True)
    assert "ran over one page" in message_for(ev)


def test_below_threshold_message_has_no_resume_note(session_factory, session):
    ev = _seed(session, "ron", stage=STAGE_DELIVER, score=40, outcome="below_threshold", reasoning="why")
    msg = message_for(ev)
    assert "Couldn't generate resume" not in msg and msg.startswith("📉 40%")


def test_deliver_runs_before_render(session_factory, session, clock, tmp_path):
    # A ready message and a ready render, both due now: the message goes first.
    delivered = _seed(session, "ron", stage=STAGE_DELIVER, score=80, outcome="matched")
    _resolve(session, delivered)
    to_render = _seed_renderable(session, "cousin")
    sender, renderer = FakeSender(OK), FakeRenderer(1)
    w = _worker_r(session_factory, renderer, clock, tmp_path, sender=sender)
    w.run_once()
    assert len(sender.calls) == 1 and renderer.calls == []
    w.run_once()
    assert len(renderer.calls) == 1
    session.refresh(delivered); session.refresh(to_render)
    assert delivered.stage == STAGE_CLOSED and to_render.stage == STAGE_DELIVER


def test_render_runs_before_score(session_factory, session, clock, tmp_path):
    # Local CPU work drains before the paid call.
    _seed_scoreable(session, "ron")
    _seed_renderable(session, "cousin")
    llm, renderer = FakeLLM(_llm_ok()), FakeRenderer(1)
    w = _worker_st(session_factory, llm, FakeTailor(), clock, tmp_path, render=renderer)
    w.run_once()
    assert len(renderer.calls) == 1 and len(llm.calls) == 0


def test_score_runs_before_tailor(session_factory, session, clock, tmp_path):
    # The cheap model's queue drains before the expensive one's.
    _seed_scoreable(session, "ron")
    _seed_tailorable(session, "cousin")
    llm, tailor = FakeLLM(_llm_ok()), FakeTailor()
    w = _worker_st(session_factory, llm, tailor, clock, tmp_path)
    w.run_once()
    assert len(llm.calls) == 1 and tailor.calls == []
```

The worker helpers this task's tests use, written beside `_worker_s`:

```python
def _worker_t(session_factory, tailor, clock, tmp_path, cvs=None, users=(RON, COUSIN), sender=None):
    return Worker(session_factory, list(users), cvs=cvs or {u.id: CV_SNAPSHOT for u in users},
                  tailor=tailor, output_dir=str(tmp_path), now=clock,
                  send=sender or FakeSender())


def _worker_r(session_factory, renderer, clock, tmp_path, cvs=None, users=(RON, COUSIN), sender=None):
    return Worker(session_factory, list(users), cvs=cvs or {u.id: CV_SNAPSHOT for u in users},
                  output_dir=str(tmp_path), render=renderer, now=clock, send=sender or FakeSender())


def _worker_st(session_factory, llm, tailor, clock, tmp_path, render=None, users=(RON, COUSIN)):
    return Worker(session_factory, list(users), cvs={u.id: CV_SNAPSHOT for u in users},
                  llm=llm, tailor=tailor, output_dir=str(tmp_path),
                  render=render or FakeRenderer(1), now=clock, send=FakeSender())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_worker.py -q`
Expected: FAIL — `cannot import name 'RENDER_BUDGET'`.

- [ ] **Step 3: Implement**

In `src/worker.py`:

```python
RENDER_BUDGET = 2
```

Constructor gains `render: Callable[..., "RenderResult"] = render_pdf` stored as `self._render` (injectable for tests, same pattern as `send` and `fetch`).

```python
    def _next_renderable(self, session: Session) -> Evaluation | None:
        if self._output_dir is None:
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_RENDER, Evaluation.next_attempt_at <= now)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    def render(self, session: Session, ev: Evaluation) -> None:
        now = self._now()
        if ev.attempts >= RENDER_BUDGET:
            self._give_up_resume(ev, now, f"render budget exhausted after {ev.attempts} attempts")
            return
        try:
            cv = MasterCV.model_validate(ev.cv_snapshot)
        except ValidationError as e:
            self._give_up_resume(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:200]}")
            return
        if not ev.tailored:
            self._give_up_resume(ev, now, "no tailored selection to render")
            return

        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()        # WeasyPrint can hang or be OOM-killed; the attempt must be durable

        out = output_path(self._output_dir, ev.user_id, ev.job_id, ev.job.company)
        try:
            result = self._render(cv, ev.tailored, out)
        except Exception as e:  # noqa: BLE001 — a bad glyph or a full disk is a failed attempt
            log.exception("Render failed for evaluation %d", ev.id)
            after = self._now()
            if ev.attempts >= RENDER_BUDGET:
                self._give_up_resume(ev, after, f"{type(e).__name__}: {e}")
            else:
                ev.last_error = f"{type(e).__name__}: {e}"
                ev.next_attempt_at = after + timedelta(seconds=backoff(ev.attempts))
            return

        after = self._now()
        ev.pdf_path, ev.page_overflow = result.path, result.overflow
        ev.stage, ev.attempts, ev.next_attempt_at = STAGE_DELIVER, 0, after
        log.info("render ev=%d user=%s pages=%d overflow=%s path=%s",
                 ev.id, ev.user_id, result.pages, result.overflow, result.path)
```

`run_once()` becomes, in order: `_next_deliverable` → `_next_fetchable` → `_next_renderable` → `_next_scoreable` → `_next_tailorable`. Add a one-line comment saying why: ready messages, then cheap network, then local CPU, then the cheap LLM call, then the expensive one.

`message_for` learns the notes:

```python
def message_for(ev: Evaluation) -> str:
    job = ev.job
    if ev.score is not None and ev.outcome in ("matched", "below_threshold"):
        return format_match(
            job.company, job.role, job.location, job.url, ev.score, ev.reasoning or "",
            ev.missing_confirmed or [], ev.missing_unknown or [],
            matched=ev.outcome == "matched",
            overflow=bool(ev.page_overflow and ev.pdf_path),
            # Only a match promises a resume; a below-threshold notice never had one.
            resume_missing=ev.outcome == "matched" and ev.pdf_path is None and ev.resume_error is not None,
        )
    return format_link_only(job.company, job.role, job.location, job.url, note=_LINK_ONLY_NOTES.get(ev.outcome))
```

`deliver()` needs two changes. First, the attachment branch, added immediately after the existing
`result.kind == "gone"` branch and before the generic failure handling — `deliver()` has no give-up,
so anything the generic path handles is retried forever, and an unreadable file fails identically
every time:

```python
        if result.kind == "attachment":
            # No exists() check can cover permissions or a race with a cleanup. The message itself
            # is fine: drop the attachment and send it on the next pass rather than looping on a
            # file that will never open. No delivery attempt is counted — nothing was sent.
            log.warning("Evaluation %d: %s; delivering without the resume", ev.id, result.error)
            ev.resume_error = ev.resume_error or result.error
            ev.pdf_path, ev.page_overflow = None, False
            ev.next_attempt_at = self._now()
            return
```

Second, the cheap precheck, at the top of `deliver()` before building the message:

```python
        if ev.pdf_path and not Path(ev.pdf_path).exists():
            # The volume was wiped or the file was cleaned up: send what we still have.
            log.warning("Evaluation %d: resume %s is gone; delivering without it", ev.id, ev.pdf_path)
            ev.resume_error = ev.resume_error or "resume file missing at delivery"
            ev.pdf_path = None
```

Imports: `from pathlib import Path`, `from render import output_path, render_pdf`.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_worker.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/worker.py tests/test_worker.py
git commit -m "feat(worker): render stage, PDF delivery, overflow and missing-resume message variants"
```

---

### Task 8: Wiring, end-to-end test, docs

**Files:**
- Modify: `src/main.py`, `tests/test_main.py`, `tests/test_worker.py`, `docs/ops.md`, `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md`, `.env.example` (create if absent)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a running pipeline — `main.build` constructs both LLM clients and the output directory.

- [ ] **Step 1: Write the failing end-to-end test**

`tests/test_main.py` has no shared setup helpers today — `test_poll_fetch_score_deliver_end_to_end` (`tests/test_main.py:112`) writes its data dir and runs its poll inline, off the `_readme` fixture. Factor that setup out into `_poll_one_posting(session_factory, users, monkeypatch)` and `_write_users_and_cv(tmp_path)`, point the existing tests at them, and write the new ones against them. Reuse `FakeLLM`, `FakeTailor`, `FakeSender`, `FakeRenderer`, `CV_SNAPSHOT` and `_llm_ok` from `tests/test_worker.py` by importing them, rather than redefining them here.

Then add the full-pipeline tests:

```python
def _pipeline(tmp_path, session_factory, users, cvs):
    """Everything external faked; the real cv, render, worker and discord formatting code runs."""
    from render import render_pdf
    llm, tailor, sender = FakeLLM(_llm_ok()), FakeTailor(), FakeSender(OK)
    worker = Worker(session_factory, users, cvs=cvs, llm=llm, tailor=tailor,
                    output_dir=str(tmp_path / "output"), max_bullets=4,
                    send=sender, render=render_pdf)
    return worker, llm, tailor, sender


def test_poll_to_pdf_delivery(tmp_path, session_factory, monkeypatch):
    """One posting, all the way: poll → fetch → score → tailor → render → deliver → closed."""
    users = [RON]
    cvs = {"ron": CV_SNAPSHOT}
    _poll_one_posting(session_factory, users, monkeypatch)   # the existing test_main poll helper
    with session_factory() as session:
        job = session.query(Job).one()
        job.description, job.fetch_status = "We need a Python engineer.", FETCH_OK
        session.commit()

    worker, llm, tailor, sender = _pipeline(tmp_path, session_factory, users, cvs)
    for _ in range(4):          # score, tailor, render, deliver
        assert worker.run_once() is True
    assert worker.run_once() is False

    with session_factory() as session:
        ev = session.query(Evaluation).one()
        assert ev.stage == STAGE_CLOSED and ev.outcome == "matched" and ev.resume_error is None
        assert ev.tailored["experience"] and ev.cv_snapshot == CV_SNAPSHOT
        assert Path(ev.pdf_path).read_bytes().startswith(b"%PDF")
        content, pdf = sender.calls[0][1], sender.calls[0][2]
        assert content.startswith("🎯") and pdf == ev.pdf_path
        assert "Couldn't generate resume" not in content


def test_restart_between_every_stage_resumes_without_repeating(tmp_path, session_factory, monkeypatch):
    """A fresh Worker for each iteration, sharing the fakes: no stage runs twice."""
    users = [RON]
    _poll_one_posting(session_factory, users, monkeypatch)
    with session_factory() as session:
        job = session.query(Job).one()
        job.description, job.fetch_status = "We need a Python engineer.", FETCH_OK
        session.commit()

    from render import render_pdf
    llm, tailor, sender = FakeLLM(_llm_ok(), _llm_ok()), FakeTailor(), FakeSender(OK, OK)
    renders = []

    def counting_render(cv, selection, out_path):
        renders.append(out_path)
        return render_pdf(cv, selection, out_path)

    stages = []
    for _ in range(4):
        # A brand-new Worker each time: only the database carries progress forward.
        w = Worker(session_factory, users, cvs={"ron": CV_SNAPSHOT}, llm=llm, tailor=tailor,
                   output_dir=str(tmp_path / "output"), max_bullets=4,
                   send=sender, render=counting_render)
        assert w.run_once() is True
        with session_factory() as session:
            stages.append(session.query(Evaluation).one().stage)

    assert stages == [STAGE_TAILOR, STAGE_RENDER, STAGE_DELIVER, STAGE_CLOSED]
    assert len(llm.calls) == 1 and len(tailor.calls) == 1 and len(renders) == 1 and len(sender.calls) == 1


def test_build_constructs_both_clients_with_their_own_models(tmp_path, monkeypatch):
    _write_users_and_cv(tmp_path)        # the existing test_main data-dir helper
    settings = load_settings({
        "DATA_DIR": str(tmp_path), "LLM_BASE_URL": "http://x/v1",
        "LLM_SCORE_MODEL": "flash", "LLM_TAILOR_MODEL": "pro", "MAX_BULLETS_PER_ENTRY": "3",
    })
    users = load_users(str(tmp_path / "users.yaml"))
    _, worker = build(settings, users)
    assert worker._llm.model == "flash"
    assert worker._tailor.model == "pro"
    assert worker._max_bullets == 3
    assert worker._output_dir == str(tmp_path / "output")
```

Also fix the cwd-dependent fixture paths carried over from Plan 3: in `tests/test_worker.py` and `tests/test_main.py`, replace every `load_cv("tests/fixtures/...")` with a path built from `Path(__file__).parent`.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_main.py -q`
Expected: FAIL.

- [ ] **Step 3: Wire it up**

Add the startup drain to `src/db.py`, beside `import_legacy_state`:

```python
def drain_resume_stages(session: Session) -> int:
    """Move rows queued for tailoring or rendering to delivery.

    Called only when this process has no tailor client or no output directory. Those rows were
    queued by a build that did, and nothing in this process will ever pick them up; without this
    they sit at their stage forever while the user waits for a notification that was already paid for.
    """
    rows = session.query(Evaluation).filter(Evaluation.stage.in_((STAGE_TAILOR, STAGE_RENDER))).all()
    for ev in rows:
        ev.resume_error = ev.resume_error or "tailoring not configured in this process"
        ev.stage, ev.attempts = STAGE_DELIVER, 0
        ev.pdf_path, ev.page_overflow = None, False
        ev.next_attempt_at = utcnow()
    return len(rows)
```

with its test in `tests/test_db.py`:

```python
def test_drain_resume_stages_moves_queued_rows_to_deliver(session_factory, session):
    a = _eval_at(session, STAGE_TAILOR)          # small helper: an Evaluation at the given stage
    b = _eval_at(session, STAGE_RENDER)
    c = _eval_at(session, STAGE_SCORE)
    assert drain_resume_stages(session) == 2
    session.commit()
    assert a.stage == b.stage == STAGE_DELIVER and c.stage == STAGE_SCORE
    assert "not configured" in a.resume_error and a.pdf_path is None
```

Then in `src/main.py`'s `build`, after the score client:

```python
    tailor = LLMClient(settings.llm_base_url, settings.llm_api_key,
                       settings.llm_tailor_model, settings.llm_timeout)
    output_dir = os.path.join(settings.data_dir, "output")
    log.info("Scoring with %s, tailoring with %s at %s",
             settings.llm_score_model, settings.llm_tailor_model, settings.llm_base_url)
    return session_factory, Worker(session_factory, users, cvs=cvs, llm=llm, tailor=tailor,
                                   output_dir=output_dir, max_bullets=settings.max_bullets_per_entry)
```

and, in the `with session_factory() as session:` block that already runs `ensure_feeds` and
`import_legacy_state`, drain when this process cannot tailor:

```python
        if tailor is None or output_dir is None:
            drained = drain_resume_stages(session)
            if drained:
                log.warning("Tailoring not configured: %d queued row(s) will be delivered "
                            "with the score only", drained)
```

Note this requires `tailor` and `output_dir` to be computed before that block — move the two lines
above it rather than adding a second session.

Keep `build(..., llm=None, cvs=None)`'s injectability and add `tailor=None` the same way, so tests can pass fakes.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Document**

`.env.example` (create if it does not exist) gains:

```
LLM_TAILOR_MODEL=deepseek-v4-pro
MAX_BULLETS_PER_ENTRY=4
```

In `docs/ops.md`, add a **Deploying Plan 4** section:

- Server prep: add `LLM_TAILOR_MODEL=deepseek-v4-pro` and `MAX_BULLETS_PER_ENTRY=4` to `~/deployed-projects/internship-tracker/.env`; the container creates `data/output/<user>/` itself. Confirm the alias exists: `curl -s -H "Authorization: Bearer $LLM_API_KEY" $LLM_BASE_URL/models | python3 -c "import json,sys; print([m['id'] for m in json.load(sys.stdin)['data']])"`.
- Expected first-boot lines: `Schema upgraded: added evaluations.resume_error, evaluations.tailor_model` and `Scoring with deepseek-v4-flash, tailoring with deepseek-v4-pro at ...`.
- Watch: `docker compose logs -f internship-tracker | grep -E "tailor ev=|render ev=|Delivered|ERROR"`.
- Measurement queries:

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

- A line under the existing calibration note: the tailor prompt is separate from `SCORE_SYSTEM`, so calibration-week rubric changes do not touch it.
- Manual re-render of one row (after fixing a template) — the stored selection is reused, no LLM call:
```sql
UPDATE evaluations SET stage='render', attempts=0, pdf_path=NULL, page_overflow=0,
       resume_error=NULL, next_attempt_at=datetime('now') WHERE id = <id>;
```
- **Re-scoring a row (after a CV or rubric change) must clear every downstream artifact.** A bare
  `stage='score'` leaves the previous run's selection and PDF attached, and if tailoring then fails
  the row ships the old resume under the new score:
```sql
UPDATE evaluations SET stage='score', outcome=NULL, attempts=0,
       cv_snapshot=NULL, tailored=NULL, pdf_path=NULL, page_overflow=0, resume_error=NULL,
       score=NULL, reasoning=NULL, missing_confirmed=NULL, missing_unknown=NULL,
       next_attempt_at=datetime('now') WHERE id = <id>;
```
  Clearing `cv_snapshot` is what makes the re-score read the edited CV; leaving it would re-score
  against the old one. Update the Plan 3 manual-retry snippet in this file the same way.

In the spec, record the two amendments under "Data model": (1) `tailor_failed`/`render_failed` are not `outcome` values — `outcome` stays `matched` and the reason goes in the new `resume_error` column, because `outcome` is write-once and `matched` is written at scoring time; (2) `evaluations.attempts` counts attempts at the current stage and resets on every transition.

- [ ] **Step 6: Commit**

```bash
git add src/main.py tests/ docs/ .env.example
git commit -m "feat(main): tailor client and output dir; e2e pipeline test; Plan 4 runbook and spec amendments"
```

---

## Carried-over items closed by this plan

- `send_message`'s PDF branch raising `OSError` → Task 5.
- `cap_content`'s unused `tail` parameter → Task 5 (the notice lines).
- `extra="forbid"` test for `cv.py` → Task 2.
- Re-ask second-call-fails and 429-without-`Retry-After` tests → Task 3.
- cwd-dependent `load_cv("tests/fixtures/...")` paths → Task 8.
- **`MasterCV` gains no required field in this plan** — in-flight `cv_snapshot`s must keep validating.

## Review round — changes made after the first draft

Six defects and two smaller gaps found reviewing the draft against the current code. All were
accepted; each is folded into the task that owns it, not bolted on at the end.

| # | Defect | Where it is now fixed |
|---|---|---|
| 1 | An unreadable (not merely missing) PDF made `send_message` return `invalid`, which `deliver()` retries forever — there is no delivery give-up by design. | Task 5 returns a distinct `"attachment"` kind; Task 7's `deliver()` drops the attachment and sends the score message. |
| 2 | `_give_up_resume` left `pdf_path` set, so a re-scored row could attach the **previous** run's PDF under a new score — and a set `pdf_path` also suppressed the "couldn't generate resume" notice. | Task 7 clears `pdf_path`/`page_overflow`; Task 8's runbook clears every downstream artifact when re-scoring. |
| 3 | The tailor cooldown keyed on `result.model`. Through a re-ask whose first call succeeded and whose second returned 401, that is the backend's model name, not the configured alias — so the worker paused a key `_llm_paused` never reads. | Task 6 builds every pause key from the configured alias; response model names stay in the logs. |
| 4 | "Never parked at a stage nothing can run" was not true for rows already queued at `tailor`/`render` when a process has no tailor client. | Task 8 adds `db.drain_resume_stages`, called from `build`. `LLM_TAILOR_MODEL` stays required — no disable flag was added. |
| 5 | `cap_content` truncates the whole message last, so a long reasoning (nothing bounds `ScoreResponse.reasoning`) erased both notices — the difference between "apply with this PDF" and "apply with your master CV". | Task 5 rewrites `cap_content` with the reasoning as a truncatable `body`; header and tail always survive. |
| 6 | The filtering tests selected one entry each, which the too-few-entries fallback replaces — so they asserted against their own rule. `cv_sample.yaml` has one experience and one project, which is what manufactured the collision. | Task 2 adds `tests/fixtures/cv_tailor.yaml` (3 experience, 4 projects) and those tests now select two entries; Task 6's `SELECTION` likewise. |
| 7 | The renderer dropped each project's `link` and `demo`. | Task 4 renders both. |
| 8 | `MAX_BULLETS_PER_ENTRY=0` disabled the per-entry stopping condition while still emptying the fallback slice. | Task 1 rejects anything below 1 at startup. |

Two consequences worth stating plainly, because they are behaviour changes rather than fixes:

- **Three Plan 3 tests change.** The score stage's `unavailable` branch and its invalid-streak
  breaker now pause `llm:<alias>` rather than `llm`. Task 6 names the three tests and the three
  transient ones that must *not* change.
- **`cap_content`'s output shifts for long messages.** The reasoning is no longer inside the
  protected header. Task 5 says to read each existing `cap_content` test and update the
  expectation deliberately.

## Still open after this plan

- `posting_usable` suppression: the flag exists and is honoured; whether non-posting pages are frequent enough to act on is a calibration-week observation.
- Watch `docs/ops.md`'s per-host table for Workday `/details/<title>_<id>` links and Greenhouse `job_app?token=` embeds, which still take the page path.
- Bullet *rephrasing* stays out of scope (spec: v2, delivered as a labelled draft).
