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
