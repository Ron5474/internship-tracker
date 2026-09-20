from src.parser import find_new_rows, parse_sections, url_key

_SWE_TABLE = """\
<table>
<thead>
<tr>
<th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Date</th>
</tr>
</thead>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Stripe">Stripe</a></strong></td>
<td>Software Engineer Intern</td>
<td>San Francisco, CA</td>
<td><div align="center"><a href="https://stripe.com/jobs/123"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
<tr>
<td>↳</td>
<td>Backend Engineer Intern</td>
<td>New York, NY</td>
<td><div align="center"><a href="https://stripe.com/jobs/124"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Google">Google</a></strong></td>
<td>🔒 SWE Intern</td>
<td>Mountain View, CA</td>
<td><div align="center"><a href="https://careers.google.com/closed"><img alt="Apply"></a></div></td>
<td>3mo</td>
</tr>
<tr>
<td>↳</td>
<td>Site Reliability Intern</td>
<td>Sunnyvale, CA</td>
<td><div align="center"><a href="https://careers.google.com/sre"><img alt="Apply"></a></div></td>
<td>1d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Ciena">Ciena</a></strong></td>
<td>ASIC Verification Intern</td>
<td>Ottawa, ON, Canada</td>
<td>🔒</td>
<td>3d</td>
</tr>
<tr>
<td>↳</td>
<td>Firmware Intern</td>
<td>Ottawa, ON, Canada</td>
<td><div align="center"><a href="https://ciena.com/jobs/fw"><img alt="Apply"></a></div></td>
<td>2d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Meta">Meta</a></strong></td>
<td>SWE Intern</td>
<td>Menlo Park, CA<br>Remote</td>
<td><div align="center"><a href="https://metacareers.com/apply"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
</tbody>
</table>"""

_PM_TABLE = """\
<table>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/PMCorp">PM Corp</a></strong></td>
<td>Product Manager Intern</td>
<td>New York, NY</td>
<td><div align="center"><a href="https://pmcorp.com/apply"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
</tbody>
</table>"""

_DS_TABLE = """\
<table>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/DeepMind">DeepMind</a></strong></td>
<td>ML Research Intern</td>
<td>London, UK</td>
<td><div align="center"><a href="https://deepmind.com/apply/xyz"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
</tbody>
</table>"""

_FINANCE_TABLE = """\
<table>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/FinCo">FinCo</a></strong></td>
<td>Quant Research Intern</td>
<td>Chicago, IL</td>
<td><div align="center"><a href="https://finco.com/apply"><img alt="Apply"></a></div></td>
<td>0d</td>
</tr>
</tbody>
</table>"""

SAMPLE_README = "\n".join([
    "# Summer 2026 Internships",
    "Some intro text.",
    "",
    "## 💻 Software Engineering Internship Roles",
    _SWE_TABLE,
    "",
    "## 📊 Product Management Internship Roles",
    _PM_TABLE,
    "",
    "## 🤖 Data Science, AI & Machine Learning Internship Roles",
    _DS_TABLE,
    "",
    "## 💰 Quantitative Finance Internship Roles",
    _FINANCE_TABLE,
])

_TARGET = ["software engineering", "product management", "data science"]


def test_parse_sections_returns_four_sections():
    sections = parse_sections(SAMPLE_README)
    assert len(sections) == 4


def test_parse_sections_swe_key_present():
    sections = parse_sections(SAMPLE_README)
    assert any("software engineering" in k for k in sections)


def test_parse_sections_swe_skips_closed_rows():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    urls = [r["url"] for r in sections[swe_key]]
    assert "https://careers.google.com/closed" not in urls


def test_parse_sections_skips_rows_with_locked_application_cell():
    # The New-Grad feed marks closed postings with 🔒 in the Application column, not the role
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    assert all(r["url"] for r in sections[swe_key])
    assert "ASIC Verification Intern" not in [r["role"] for r in sections[swe_key]]


def test_continuation_row_inherits_company_across_locked_application_cell():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    fw = next(r for r in sections[swe_key] if r["url"] == "https://ciena.com/jobs/fw")
    assert fw["company"] == "Ciena"


def test_continuation_row_inherits_previous_company():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    backend = next(r for r in sections[swe_key] if r["url"] == "https://stripe.com/jobs/124")
    assert backend["company"] == "Stripe"
    assert backend["role"] == "Backend Engineer Intern"


def test_continuation_row_inherits_company_across_closed_row():
    # Google's first row is closed and filtered out, but its ↳ row is still open
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    sre = next(r for r in sections[swe_key] if r["url"] == "https://careers.google.com/sre")
    assert sre["company"] == "Google"


def test_no_company_leaks_into_next_section():
    # A ↳ row at the top of a section has nothing to inherit from and must be dropped
    readme = "\n".join([
        "## 💻 Software Engineering Internship Roles",
        _SWE_TABLE,
        "",
        "## 📊 Product Management Internship Roles",
        _PM_TABLE.replace("PM Corp", "↳").replace('<strong><a href="https://simplify.jobs/c/PMCorp">↳</a></strong>', "↳"),
    ])
    sections = parse_sections(readme)
    pm_key = next(k for k in sections if "product management" in k)
    assert sections[pm_key] == []


def test_parse_sections_swe_row_company():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    stripe = next(r for r in sections[swe_key] if r["company"] == "Stripe")
    assert stripe["company"] == "Stripe"


def test_parse_sections_swe_row_role():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    stripe = next(r for r in sections[swe_key] if r["company"] == "Stripe")
    assert stripe["role"] == "Software Engineer Intern"


def test_parse_sections_swe_row_location():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    stripe = next(r for r in sections[swe_key] if r["company"] == "Stripe")
    assert stripe["location"] == "San Francisco, CA"


def test_parse_sections_swe_row_url():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    stripe = next(r for r in sections[swe_key] if r["company"] == "Stripe")
    assert stripe["url"] == "https://stripe.com/jobs/123"


def test_parse_sections_multi_location_br():
    sections = parse_sections(SAMPLE_README)
    swe_key = next(k for k in sections if "software engineering" in k)
    meta = next(r for r in sections[swe_key] if r["company"] == "Meta")
    assert meta["location"] == "Menlo Park, CA, Remote"


def test_parse_sections_ds_section_has_deepmind():
    sections = parse_sections(SAMPLE_README)
    ds_key = next(k for k in sections if "data science" in k)
    assert sections[ds_key][0]["company"] == "DeepMind"


def test_parse_sections_finance_section_present():
    sections = parse_sections(SAMPLE_README)
    assert any("finance" in k for k in sections)


def test_find_new_rows_returns_all_when_known_urls_empty():
    sections = parse_sections(SAMPLE_README)
    rows = find_new_rows(sections, set(), _TARGET)
    companies = {r["company"] for r in rows}
    assert "Stripe" in companies
    assert "PM Corp" in companies
    assert "DeepMind" in companies


def test_find_new_rows_excludes_known_urls():
    sections = parse_sections(SAMPLE_README)
    rows = find_new_rows(sections, {"https://stripe.com/jobs/123"}, _TARGET)
    urls = {r["url"] for r in rows}
    assert "https://stripe.com/jobs/123" not in urls
    assert "https://stripe.com/jobs/124" in urls
    assert "https://pmcorp.com/apply" in urls


def test_find_new_rows_excludes_non_target_sections():
    sections = parse_sections(SAMPLE_README)
    rows = find_new_rows(sections, set(), _TARGET)
    companies = {r["company"] for r in rows}
    assert "FinCo" not in companies


def test_find_new_rows_empty_sections_returns_empty():
    assert find_new_rows({}, set(), _TARGET) == []


def test_find_new_rows_all_known_returns_empty():
    sections = parse_sections(SAMPLE_README)
    all_urls = {url_key(r["url"]) for rows in sections.values() for r in rows}
    rows = find_new_rows(sections, all_urls, _TARGET)
    assert rows == []


def test_find_new_rows_ignores_utm_param_changes():
    # Simulate SimplifyJobs adding utm_source to an existing URL — should not re-notify
    sections = parse_sections(SAMPLE_README)
    # Store the bare URL (no query string) as if initialized before utm params were added
    known = {"https://stripe.com/jobs/123"}
    rows = find_new_rows(sections, known, _TARGET)
    urls = {r["url"] for r in rows}
    assert "https://stripe.com/jobs/123" not in urls


def test_url_key_strips_tracking_params_and_fragment():
    assert url_key("https://example.com/job?utm_source=Simplify&ref=x") == "https://example.com/job"
    assert url_key("https://example.com/job#section") == "https://example.com/job"
    assert url_key("https://example.com/job") == "https://example.com/job"


def test_url_key_keeps_job_identifying_params():
    # Greenhouse-embedded boards identify the posting only via gh_jid
    assert url_key("https://stripe.com/jobs/search?gh_jid=8212508&utm_source=Simplify&ref=Simplify") \
        == "https://stripe.com/jobs/search?gh_jid=8212508"
    assert url_key("https://stripe.com/jobs/search?gh_jid=1") != url_key("https://stripe.com/jobs/search?gh_jid=2")


def test_url_key_param_order_is_stable():
    assert url_key("https://x.com/j?b=2&a=1") == url_key("https://x.com/j?a=1&b=2")
