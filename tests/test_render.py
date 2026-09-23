from pathlib import Path

import pytest

from cv import load_cv
from render import output_path, render_pdf

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
