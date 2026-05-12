from src.parser import parse_new_rows, filter_by_keywords

SAMPLE_PATCH = """\
@@ -45,6 +45,9 @@
 | Company | Role | Location | Application | Date |
 | ------- | ---- | -------- | ----------- | ---- |
+| **[Stripe](https://stripe.com)** | Software Engineer Intern | San Francisco, CA | <a href="https://simplify.jobs/p/abc">Apply</a> | May 1 |
+| **[DeepMind](https://deepmind.com)** | ML Research Intern | London, UK | <a href="https://simplify.jobs/p/xyz">Apply</a> | May 2 |
+| **[McKinsey](https://mckinsey.com)** | Business Analyst Intern | New York, NY | <a href="https://simplify.jobs/p/mck">Apply</a> | May 3 |
 | **[Old Corp](https://old.com)** | Old Role | Old Location | <a href="https://simplify.jobs/p/old">Apply</a> | Apr 1 |
"""


def test_parse_new_rows_returns_three_rows():
    rows = parse_new_rows(SAMPLE_PATCH)
    assert len(rows) == 3


def test_parse_new_rows_extracts_company():
    rows = parse_new_rows(SAMPLE_PATCH)
    assert rows[0]["company"] == "Stripe"


def test_parse_new_rows_extracts_role():
    rows = parse_new_rows(SAMPLE_PATCH)
    assert rows[0]["role"] == "Software Engineer Intern"


def test_parse_new_rows_extracts_location():
    rows = parse_new_rows(SAMPLE_PATCH)
    assert rows[0]["location"] == "San Francisco, CA"


def test_parse_new_rows_extracts_url():
    rows = parse_new_rows(SAMPLE_PATCH)
    assert rows[0]["url"] == "https://simplify.jobs/p/abc"


def test_parse_new_rows_ignores_unchanged_lines():
    patch = " | **[Old Corp](url)** | Old Role | Old Location | <a href='url'>Apply</a> | Apr 1 |"
    rows = parse_new_rows(patch)
    assert rows == []


def test_parse_new_rows_ignores_separator_row():
    patch = "+| ------- | ---- | -------- | ----------- | ---- |"
    rows = parse_new_rows(patch)
    assert rows == []


def test_filter_by_keywords_matches_software_engineer():
    rows = [{"company": "Stripe", "role": "Software Engineer Intern", "location": "SF", "url": ""}]
    result = filter_by_keywords(rows, ["software engineer", "swe"])
    assert result == rows


def test_filter_by_keywords_matches_ml():
    rows = [{"company": "DeepMind", "role": "ML Research Intern", "location": "London", "url": ""}]
    result = filter_by_keywords(rows, ["software engineer", "swe", "ai", "machine learning", "ml"])
    assert result == rows


def test_filter_by_keywords_excludes_non_matching():
    rows = [{"company": "McKinsey", "role": "Business Analyst Intern", "location": "NY", "url": ""}]
    result = filter_by_keywords(rows, ["software engineer", "swe", "ai", "machine learning", "ml"])
    assert result == []


def test_filter_by_keywords_is_case_insensitive():
    rows = [{"company": "OpenAI", "role": "AI Safety Intern", "location": "SF", "url": ""}]
    result = filter_by_keywords(rows, ["ai"])
    assert result == rows


def test_filter_by_keywords_empty_rows_returns_empty():
    result = filter_by_keywords([], ["swe"])
    assert result == []


def test_parse_new_rows_skips_closed_positions():
    patch = '+| **[Stripe](https://stripe.com)** | 🔒 Software Engineer Intern | SF | <a href="https://simplify.jobs/p/abc">Apply</a> | May 1 |'
    rows = parse_new_rows(patch)
    assert rows == []


def test_parse_new_rows_skips_continuation_rows():
    patch = '+| ↳ | Software Engineer Intern | New York, NY | <a href="https://simplify.jobs/p/abc">Apply</a> | May 1 |'
    rows = parse_new_rows(patch)
    assert rows == []
