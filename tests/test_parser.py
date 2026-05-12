from src.parser import parse_new_rows, filter_by_keywords

_TR_STRIPE = """\
+<tr>
+<td><strong><a href="https://simplify.jobs/c/Stripe">Stripe</a></strong></td>
+<td>Software Engineer Intern</td>
+<td>San Francisco, CA</td>
+<td><div align="center"><a href="https://stripe.com/jobs/123?utm_source=Simplify"><img src="https://i.imgur.com/fbjwDvo.png" width="50" alt="Apply"></a> <a href="https://simplify.jobs/p/abc"><img src="https://i.imgur.com/aVnQdox.png" width="26" alt="Simplify"></a></div></td>
+<td>0d</td>
+</tr>"""

_TR_DEEPMIND = """\
+<tr>
+<td><strong><a href="https://simplify.jobs/c/DeepMind">DeepMind</a></strong></td>
+<td>ML Research Intern</td>
+<td>London, UK</td>
+<td><div align="center"><a href="https://deepmind.com/apply/xyz"><img src="https://i.imgur.com/fbjwDvo.png" width="50" alt="Apply"></a></div></td>
+<td>0d</td>
+</tr>"""

_TR_MCKINSEY = """\
+<tr>
+<td><strong><a href="https://simplify.jobs/c/McKinsey">McKinsey</a></strong></td>
+<td>Business Analyst Intern</td>
+<td>New York, NY</td>
+<td><div align="center"><a href="https://mckinsey.com/apply"><img src="https://i.imgur.com/fbjwDvo.png" width="50" alt="Apply"></a></div></td>
+<td>0d</td>
+</tr>"""

_TR_OLD_CONTEXT = """\
 <tr>
 <td><strong><a href="https://simplify.jobs/c/OldCorp">Old Corp</a></strong></td>
 <td>Old Role</td>
 <td>Old Location</td>
 <td><div align="center"><a href="https://old.com/apply"><img src="https://i.imgur.com/fbjwDvo.png" width="50" alt="Apply"></a></div></td>
 <td>1mo</td>
 </tr>"""

SAMPLE_PATCH = "\n".join([
    "@@ -91,6 +91,13 @@",
    " </thead>",
    " <tbody>",
    _TR_OLD_CONTEXT,
    _TR_STRIPE,
    _TR_DEEPMIND,
    _TR_MCKINSEY,
])


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
    assert rows[0]["url"] == "https://stripe.com/jobs/123?utm_source=Simplify"


def test_parse_new_rows_ignores_unchanged_lines():
    rows = parse_new_rows(_TR_OLD_CONTEXT)
    assert rows == []


def test_parse_new_rows_skips_closed_positions():
    patch = "\n".join([
        "+<tr>",
        '+<td><strong><a href="https://simplify.jobs/c/Stripe">Stripe</a></strong></td>',
        "+<td>🔒 Software Engineer Intern</td>",
        "+<td>San Francisco, CA</td>",
        '+<td><div align="center"><a href="https://stripe.com/apply"><img alt="Apply"></a></div></td>',
        "+<td>0d</td>",
        "+</tr>",
    ])
    assert parse_new_rows(patch) == []


def test_parse_new_rows_skips_continuation_rows():
    patch = "\n".join([
        "+<tr>",
        "+<td>↳</td>",
        "+<td>Software Engineer Intern</td>",
        "+<td>New York, NY</td>",
        '+<td><div align="center"><a href="https://co.com/apply"><img alt="Apply"></a></div></td>',
        "+<td>0d</td>",
        "+</tr>",
    ])
    assert parse_new_rows(patch) == []


def test_parse_new_rows_multi_location_br():
    patch = "\n".join([
        "+<tr>",
        '+<td><strong><a href="https://simplify.jobs/c/Google">Google</a></strong></td>',
        "+<td>SWE Intern</td>",
        "+<td>Mountain View, CA<br>Austin, TX</td>",
        '+<td><div align="center"><a href="https://careers.google.com/apply"><img alt="Apply"></a></div></td>',
        "+<td>0d</td>",
        "+</tr>",
    ])
    rows = parse_new_rows(patch)
    assert rows[0]["location"] == "Mountain View, CA, Austin, TX"


def test_filter_by_keywords_matches_software_engineer():
    rows = [{"company": "Stripe", "role": "Software Engineer Intern", "location": "SF", "url": ""}]
    assert filter_by_keywords(rows, ["software engineer", "swe"]) == rows


def test_filter_by_keywords_matches_ml():
    rows = [{"company": "DeepMind", "role": "ML Research Intern", "location": "London", "url": ""}]
    assert filter_by_keywords(rows, ["software engineer", "swe", "ai", "machine learning", "ml"]) == rows


def test_filter_by_keywords_excludes_non_matching():
    rows = [{"company": "McKinsey", "role": "Business Analyst Intern", "location": "NY", "url": ""}]
    assert filter_by_keywords(rows, ["software engineer", "swe", "ai", "machine learning", "ml"]) == []


def test_filter_by_keywords_is_case_insensitive():
    rows = [{"company": "OpenAI", "role": "AI Safety Intern", "location": "SF", "url": ""}]
    assert filter_by_keywords(rows, ["ai"]) == rows


def test_filter_by_keywords_empty_rows_returns_empty():
    assert filter_by_keywords([], ["swe"]) == []
