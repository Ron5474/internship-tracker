import copy
from pathlib import Path

import pytest

from cv import load_cv
from render import (FILL_TARGET, RenderResult, UNDERFILL_BELOW, fit_to_page, attachment_name, build_html, output_path,
                    render_pdf)

# cv_tailor.yaml (Task 2), not cv_sample.yaml: these tests slice two experience entries and two
# projects, need a project carrying a `link` and a `demo`, and need enough content that the
# overflow case genuinely runs past one page. cv_sample.yaml has one entry per section.
FIXTURE = str(Path(__file__).parent / "fixtures" / "cv_tailor.yaml")


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
    # Education is rendered as rows, not model objects, but every entry still survives
    # in the master's order no matter what the selection said.
    from render import build_context
    ctx = build_context(cv, {"experience": [], "projects": [], "skills": {}})
    assert [e["school"] for e in ctx["education"]] == [e.school for e in cv.education]
    assert ctx["summary"] == cv.summary


def test_education_row_carries_the_degree_and_its_details(cv):
    from render import build_context
    entry = next(e for e in cv.education if e.details)
    row = next(r for r in build_context(cv, {})["education"] if r["school"] == entry.school)
    assert row["degree"].startswith(entry.degree)
    for detail in entry.details:
        assert detail in row["degree"]
    assert row["dates"] == entry.dates


def test_contact_renders_profiles_by_name_not_url(cv):
    html = build_html(cv, {"experience": [], "projects": [], "skills": {}})
    assert f'<a href="{cv.contact.linkedin}">LinkedIn</a>' in html
    assert f'<a href="{cv.contact.github}">GitHub</a>' in html
    assert cv.contact.email in html                      # plain text, not a link
    assert f'<a href="{cv.contact.email}"' not in html


def test_skills_render_as_one_list_without_group_names(cv):
    html = build_html(cv, {"experience": [], "projects": [],
                           "skills": {k: v for k, v in cv.skills.items()}})
    for group in cv.skills:
        assert f"{group}:" not in html                    # no "languages:" label on the page
    for skill in cv.skills["languages"]:
        assert skill in html


def test_flat_skills_keeps_order_and_drops_duplicates():
    from render import _flat_skills
    assert _flat_skills({"a": ["Python", "Go"], "b": ["Go", "Rust"]}) == ["Python", "Go", "Rust"]


def test_project_tech_is_not_rendered(cv):
    # The master resume this layout copies keeps tech detail inside the bullets; a separate
    # tech line is what pushed the page over. Dropping it is deliberate, so pin it.
    project = next(p for p in cv.projects if p.tech)
    html = build_html(cv, {"experience": [], "skills": {},
                           "projects": [{"id": project.id, "bullets": [project.bullets[0].id]}]})
    assert project.name in html
    assert ", ".join(project.tech) not in html


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


def test_fill_rises_with_content(cv, tmp_path):
    # An absolute number would only measure how big this fixture happens to be; the fixture
    # CV is small enough that even "everything" does not fill a page.
    thin = render_pdf(cv, {"experience": [], "projects": [], "skills": {}}, str(tmp_path / "a.pdf"))
    full = render_pdf(cv, {
        "experience": [{"id": e.id, "bullets": [b.id for b in e.bullets]} for e in cv.experience],
        "projects": [{"id": p.id, "bullets": [b.id for b in p.bullets]} for p in cv.projects],
        "skills": cv.skills,
    }, str(tmp_path / "b.pdf"))
    assert full.fill > thin.fill


def test_underfilled_reads_the_threshold_and_the_page_count():
    from render import RenderResult
    assert RenderResult("p", 1, UNDERFILL_BELOW - 0.01).underfilled is True
    assert RenderResult("p", 1, UNDERFILL_BELOW).underfilled is False
    assert RenderResult("p", 2, 0.10).underfilled is False       # two pages is never "room to spare"


def test_a_nearly_empty_resume_is_reported_as_underfilled(cv, tmp_path):
    thin = {"experience": [], "projects": [], "skills": {}}
    result = render_pdf(cv, thin, str(tmp_path / "r.pdf"))
    assert result.pages == 1
    assert result.fill < UNDERFILL_BELOW
    assert result.underfilled is True


def test_overflow_is_never_also_underfilled(cv, tmp_path):
    fat = {
        "experience": [{"id": e.id, "bullets": [b.id for b in e.bullets]} for e in cv.experience] * 6,
        "projects": [{"id": p.id, "bullets": [b.id for b in p.bullets]} for p in cv.projects] * 6,
        "skills": cv.skills,
    }
    result = render_pdf(cv, fat, str(tmp_path / "r.pdf"))
    assert result.overflow is True
    assert result.underfilled is False          # more than one page is never "room to spare"


def test_fill_falls_back_to_full_rather_than_raising():
    # It reads WeasyPrint's private box tree; a surprise there must not fail a render, and a
    # wrong "looks sparse" note is worse than no note.
    from render import _fill

    class Unhelpful:
        @property
        def _page_box(self):
            raise AttributeError("moved in a new version")

    assert _fill(Unhelpful()) == 1.0


def test_attachment_name_reads_like_something_you_would_send():
    assert attachment_name("Ronak Patel", "RTX", "Software Engineer Intern") == \
        "Ronak_Patel_Resume_RTX_Software_Engineer_Intern.pdf"


def test_attachment_name_survives_punctuation_and_length():
    name = attachment_name("Ronak Patel", "Goldman Sachs & Co.", "SWE Intern - Summer 2027")
    assert name == "Ronak_Patel_Resume_Goldman_Sachs_Co_SWE_Intern_Summer_2027.pdf"
    long_name = attachment_name("A B", "C" * 200, "D" * 200)
    assert len(long_name) <= 124 and long_name.endswith(".pdf")


# --- fit_to_page ----------------------------------------------------------------------------

def _sized_render(weights=None, capacity=10.0):
    """Stands in for WeasyPrint. Each bullet occupies `weight / capacity` of a page (default
    weight 1), so a test controls exactly what fits. Records every selection it renders."""
    weights = weights or {}

    def render(cv, selection, out_path):
        used = sum(weights.get(b, 1.0) for s in ("experience", "projects")
                   for item in selection.get(s) or [] for b in item["bullets"])
        render.calls.append(copy.deepcopy(selection))
        fill = used / capacity
        return RenderResult(out_path, 2 if fill > 1 else 1, min(fill, 1.0))

    render.calls = []
    return render


def _bullets(sel, section):
    return [b for item in sel.get(section) or [] for b in item["bullets"]]


def _all_selected(cv, n_projects=None):
    projects = cv.projects if n_projects is None else cv.projects[:n_projects]
    return {
        "experience": [{"id": e.id, "bullets": [b.id for b in e.bullets]} for e in cv.experience],
        "projects": [{"id": p.id, "bullets": [b.id for b in p.bullets]} for p in projects],
        "skills": {},
    }


def test_an_overflowing_resume_is_trimmed_to_one_page(cv, tmp_path):
    sel = _all_selected(cv)
    total = len(_bullets(sel, "experience")) + len(_bullets(sel, "projects"))
    render = _sized_render(capacity=total - 3)
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=render)
    assert result.pages == 1
    kept = len(_bullets(result.selection, "experience")) + len(_bullets(result.selection, "projects"))
    assert kept == total - 3


def test_trimming_takes_projects_before_experience(cv, tmp_path):
    sel = _all_selected(cv)
    experience_before = _bullets(sel, "experience")
    total = len(experience_before) + len(_bullets(sel, "projects"))
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=total - 2))
    assert _bullets(result.selection, "experience") == experience_before


def test_trimming_works_from_the_last_entry_up(cv, tmp_path):
    # The model's order is its relevance ranking, so the last project is the first to give.
    sel = _all_selected(cv, n_projects=2)
    first, last = sel["projects"][0], sel["projects"][1]
    total = len(_bullets(sel, "experience")) + len(_bullets(sel, "projects"))
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=total - 1))
    assert result.selection["projects"][0] == first                      # untouched
    assert result.selection["projects"][1]["bullets"] == last["bullets"][:-1]


def test_an_entry_down_to_one_bullet_is_dropped_whole(cv, tmp_path):
    sel = _all_selected(cv, n_projects=2)
    sel["projects"][1]["bullets"] = sel["projects"][1]["bullets"][:1]
    dropped = sel["projects"][1]["id"]
    total = len(_bullets(sel, "experience")) + len(_bullets(sel, "projects"))
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=total - 1))
    assert dropped not in [p["id"] for p in result.selection["projects"]]


def test_the_last_experience_entry_is_never_dropped(cv, tmp_path):
    # Nothing can fit: trimming stops with the last job still on the page and reports overflow.
    sel = {"experience": [{"id": cv.experience[0].id, "bullets": [cv.experience[0].bullets[0].id]}],
           "projects": [], "skills": {}}
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=0.5))
    assert result.overflow is True
    assert [e["id"] for e in result.selection["experience"]] == [cv.experience[0].id]


def test_an_underfilled_resume_gets_the_candidates_own_bullets_back(cv, tmp_path):
    entry = cv.experience[0]
    sel = {"experience": [{"id": entry.id, "bullets": [entry.bullets[0].id]}],
           "projects": [], "skills": {}}
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=3))
    assert result.fill >= FILL_TARGET
    added = _bullets(result.selection, "experience")[1:]
    # Deepen what is shown before adding anything new.
    assert added == [b.id for b in entry.bullets[1:3]]


def test_filling_adds_new_entries_only_after_deepening(cv, tmp_path):
    entry = cv.experience[0]
    sel = {"experience": [{"id": entry.id, "bullets": [b.id for b in entry.bullets]}],
           "projects": [], "skills": {}}
    capacity = len(entry.bullets) + 1
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=capacity))
    shown = [e["id"] for e in result.selection["experience"]]
    assert shown[0] == entry.id and len(shown) == 2       # one new entry, after the first was full


def test_a_candidate_that_would_spill_over_is_skipped_for_a_shorter_one(cv, tmp_path):
    entry = cv.experience[0]
    long_bullet, short_bullet = entry.bullets[1].id, entry.bullets[2].id
    sel = {"experience": [{"id": entry.id, "bullets": [entry.bullets[0].id]}],
           "projects": [], "skills": {}}
    render = _sized_render(weights={long_bullet: 5.0}, capacity=2.5)
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=render)
    bullets = _bullets(result.selection, "experience")
    assert long_bullet not in bullets and short_bullet in bullets
    assert result.pages == 1


def test_the_file_on_disk_holds_the_returned_selection(cv, tmp_path):
    # When the last attempt was a rejected trial, the accepted selection is rendered again.
    entry = cv.experience[0]
    sel = {"experience": [{"id": entry.id, "bullets": [entry.bullets[0].id]}],
           "projects": [], "skills": {}}
    weights = {b.id: 5.0 for b in entry.bullets[1:]}
    weights.update({b.id: 5.0 for e in cv.experience[1:] for b in e.bullets})
    weights.update({b.id: 5.0 for p in cv.projects for b in p.bullets})
    render = _sized_render(weights=weights, capacity=2.0)
    result = fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=render)
    assert render.calls[-1] == result.selection


def test_fitting_stops_once_the_target_is_reached(cv, tmp_path):
    sel = _all_selected(cv)
    total = len(_bullets(sel, "experience")) + len(_bullets(sel, "projects"))
    render = _sized_render(capacity=total / 0.95)          # already 95% full
    fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=render)
    assert len(render.calls) == 1


def test_fitting_is_bounded(cv, tmp_path):
    from render import MAX_FIT_RENDERS
    sel = {"experience": [{"id": cv.experience[0].id, "bullets": [cv.experience[0].bullets[0].id]}],
           "projects": [], "skills": {}}
    render = _sized_render(capacity=1000)                  # nothing will ever fill it
    fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=render)
    assert len(render.calls) <= MAX_FIT_RENDERS + 1


def test_fitting_does_not_mutate_the_callers_selection(cv, tmp_path):
    sel = _all_selected(cv)
    snapshot = copy.deepcopy(sel)
    total = len(_bullets(sel, "experience")) + len(_bullets(sel, "projects"))
    fit_to_page(cv, sel, str(tmp_path / "r.pdf"), render=_sized_render(capacity=total - 4))
    assert sel == snapshot


def test_fitting_with_the_real_renderer_produces_one_page(cv, tmp_path):
    fat = {
        "experience": [{"id": e.id, "bullets": [b.id for b in e.bullets]} for e in cv.experience] * 4,
        "projects": [{"id": p.id, "bullets": [b.id for b in p.bullets]} for p in cv.projects] * 4,
        "skills": cv.skills,
    }
    result = fit_to_page(cv, fat, str(tmp_path / "r.pdf"))
    assert result.pages == 1
    assert Path(result.path).read_bytes().startswith(b"%PDF")
