from pathlib import Path

import pytest
import yaml

from cv import MAX_EXPERIENCE_ENTRIES, MAX_PROJECT_ENTRIES, MIN_ENTRIES, MasterCV, all_ids, cv_to_id_text, cv_to_text, load_cv, validate_selection

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


def test_directory_path_raises_value_error_with_path(tmp_path):
    with pytest.raises(ValueError, match=str(tmp_path)):
        load_cv(str(tmp_path))


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


# A CV with room to choose from: the 1-experience/1-project `cv_sample.yaml` cannot distinguish
# "the validator filtered correctly" from "the too-few-entries fallback replaced the selection".
# A NEW NAME: `FIXTURE` already exists at tests/test_cv.py:8 and nine tests read it, including
# test_all_ids_covers_entries_and_bullets, which asserts cv_sample.yaml's exact id set. Rebinding
# it would break them. Leave line 8 alone.
TAILOR_FIXTURE = str(Path(__file__).parent / "fixtures" / "cv_tailor.yaml")


def _cv():
    return load_cv(TAILOR_FIXTURE)


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
    # Two valid entries, so what survives is the filtering and not the fallback.
    cv = _cv()
    a, b = cv.experience[0], cv.experience[1]
    sel, warnings = validate_selection(cv, {
        "experience": [{"id": "nope", "bullets": []},
                       {"id": a.id, "bullets": [a.bullets[0].id]},
                       {"id": b.id, "bullets": [b.bullets[0].id]}],
        "projects": [], "skills": {},
    })
    assert [e["id"] for e in sel["experience"]] == [a.id, b.id]
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
    assert sel["skills"][group][0] == real          # the model's pick leads its group
    assert "COBOL-on-Mars" not in sel["skills"][group]
    assert "invented_group" not in sel["skills"]
    assert any("COBOL-on-Mars" in w for w in warnings)
    assert any("invented_group" in w for w in warnings)


def test_an_empty_section_is_topped_up_from_the_master():
    cv = _cv()
    sel, warnings = validate_selection(cv, {"experience": [], "projects": [], "skills": {}}, max_bullets=2)
    # Topped up to the floor, not to the cap: padding is a safety net, not a target.
    assert [e["id"] for e in sel["experience"]] == [e.id for e in cv.experience][:MIN_ENTRIES]
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


def test_topping_up_keeps_the_models_own_pick_and_its_bullets():
    # The defect this replaces: a candidate with two jobs, whose model correctly picked the one
    # relevant job, had that choice thrown away and the whole section rebuilt from the master.
    cv = _cv()
    chosen = cv.experience[1]                       # deliberately NOT the master's first
    bullet = chosen.bullets[-1].id                  # deliberately NOT the master's first bullet
    sel, warnings = validate_selection(
        cv, {"experience": [{"id": chosen.id, "bullets": [bullet]}], "projects": [], "skills": {}},
        max_bullets=2)

    assert sel["experience"][0]["id"] == chosen.id          # the model still leads
    assert sel["experience"][0]["bullets"] == [bullet]      # with the bullet it chose
    assert len(sel["experience"]) == MIN_ENTRIES            # padded only to the floor
    assert chosen.id not in [e["id"] for e in sel["experience"][1:]]   # no duplicate
    assert any("topped up" in w for w in warnings)


def test_topping_up_never_exceeds_the_entry_cap():
    cv = _cv()
    sel, _ = validate_selection(cv, {"experience": [], "projects": [], "skills": {}})
    assert len(sel["projects"]) <= MAX_PROJECT_ENTRIES


def test_skills_below_the_floor_are_topped_up_with_the_models_picks_first():
    cv = _cv()
    group = list(cv.skills)[-1]
    pick = cv.skills[group][-1]
    sel, warnings = validate_selection(cv, {"experience": [], "projects": [],
                                            "skills": {group: [pick]}})
    assert sel["skills"][group][0] == pick
    # The fixture has fewer skills than the floor, so everything it has should now be present.
    assert sum(len(v) for v in sel["skills"].values()) == sum(len(v) for v in cv.skills.values())
    assert any("topped up" in w for w in warnings)


def test_top_up_skills_stops_at_the_floor():
    from cv import MIN_SKILLS, _top_up_skills
    master = {"languages": [f"L{i}" for i in range(40)]}
    out = _top_up_skills({"languages": ["L39"]}, master)
    assert len(out["languages"]) == MIN_SKILLS
    assert out["languages"][0] == "L39"                      # the pick still leads
    assert "L39" not in out["languages"][1:]                 # and is not duplicated


def test_top_up_skills_leaves_a_full_selection_alone():
    from cv import MIN_SKILLS, _top_up_skills
    chosen = {"languages": [f"L{i}" for i in range(MIN_SKILLS + 5)]}
    master = {"languages": [f"L{i}" for i in range(60)]}
    assert _top_up_skills(chosen, master) == chosen


def test_selection_is_json_round_trippable():
    import json
    cv = _cv()
    sel, _ = validate_selection(cv, {"experience": [], "projects": [], "skills": {}})
    assert json.loads(json.dumps(sel)) == sel


def test_extra_key_in_cv_yaml_is_rejected():
    # Guards `extra="forbid"` on the CV schema — carried over from Plan 3's review.
    raw = yaml.safe_load(Path(TAILOR_FIXTURE).read_text())
    raw["favourite_colour"] = "blue"
    with pytest.raises(Exception):
        MasterCV.model_validate(raw)
