from unittest.mock import Mock, patch

import requests

from fetcher import (
    MAX_PAGE_CHARS,
    MIN_DESCRIPTION_CHARS,
    FetchResult,
    classify_status,
    fetch_description,
    fetch_via_api,
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


def test_html_to_text_none_is_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_html_to_text_separates_table_cells():
    assert html_to_text(
        "<table><tr><th>Level</th><th>Pay</th></tr><tr><td>L3</td><td>$100k</td></tr></table>"
    ) == "Level\nPay\nL3\n$100k"


# --- has_requirements --------------------------------------------------------

def test_has_requirements_true_on_heading_lines():
    for text in (
        "About us\n\nQualifications\n- Python",
        "Intro\nWhat you'll need:\n2 years",
        "Basic Qualifications\n...",
        "Minimum Qualifications\nBS",
        "  Requirements\n...",           # leading spaces
    ):
        assert has_requirements(text), text


def test_has_requirements_false_on_boilerplate():
    for text in (
        "If you have a disability and need an accommodation, contact us.",
        "Do you have what it takes?",
        "We pay above minimum wage.",
        "We are a fun company. Apply now.",
        "the requirements are listed elsewhere",   # mid-line, not a heading
    ):
        assert not has_requirements(text), text


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


def test_match_lever_accepts_uppercase_uuid():
    assert match_ats("https://jobs.lever.co/acme/718B3135-D15D-4CBC-9541-1CBB8A6F5EC5") == (
        "lever", {"company": "acme", "uuid": "718B3135-D15D-4CBC-9541-1CBB8A6F5EC5"})


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


# --- ATS API handlers ---------------------------------------------------------

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


def test_lever_handler_tolerates_null_list_content():
    body = {
        "descriptionPlain": LONG,
        "lists": [{"text": "Perks", "content": None}],
        "additionalPlain": None,
    }
    with patch("fetcher.requests.get", return_value=_resp(200, body)):
        r = fetch_via_api("lever", {"company": "x", "uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12})
    assert r.ok and "Perks" in r.text


def test_workday_handler_null_description_is_permanent_too_short():
    body = {"jobPostingInfo": {"jobDescription": None}}
    with patch("fetcher.requests.get", return_value=_resp(200, body)):
        r = fetch_via_api("workday", {"tenant": "t", "wd": "wd1", "site": "s", "path": "p"})
    assert r.kind == "permanent" and "too short" in r.error


# --- fetch_description ---------------------------------------------------------

PAGE_HTML = "<html><body><main><h1>Software Engineer</h1><h2>Qualifications</h2><p>" + LONG + "</p></main></body></html>"


def _page(status=200, text=PAGE_HTML, url="https://careers.example.com/job/1", headers=None):
    r = _resp(status, None, text)
    r.url = url
    r.headers = {"Content-Type": "text/html; charset=utf-8"} if headers is None else headers
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


def test_fetch_description_rejects_non_html_content_type():
    with patch("fetcher.requests.get", return_value=_page(headers={"Content-Type": "application/pdf"})), \
         patch("fetcher.trafilatura.extract") as extract:
        r = fetch_description("https://careers.example.com/job/1.pdf")
    assert r.kind == "permanent" and r.strategy == "none"
    assert r.error == "not HTML (application/pdf)"
    extract.assert_not_called()


def test_fetch_description_missing_content_type_is_treated_as_html():
    with patch("fetcher.requests.get", return_value=_page(headers={})), \
         patch("fetcher.trafilatura.extract", return_value="Qualifications\n" + LONG):
        r = fetch_description("https://careers.example.com/job/1")
    assert r.ok


def test_fetch_description_caps_page_size():
    with patch("fetcher.requests.get", return_value=_page(text="x" * (MAX_PAGE_CHARS + 100))), \
         patch("fetcher.trafilatura.extract", return_value="Qualifications\n" + LONG) as extract:
        fetch_description("https://careers.example.com/job/1")
    assert len(extract.call_args.args[0]) == MAX_PAGE_CHARS
