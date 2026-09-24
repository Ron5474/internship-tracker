from pathlib import Path

import pytest

from cv import load_cv
from render import (UNDERFILL_BELOW, attachment_name, build_html, output_path,
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
