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
