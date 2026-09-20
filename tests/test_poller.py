import pytest

from config import FeedSpec
from db import Evaluation, Feed, Job, ensure_feeds, STAGE_DELIVER
from poller import poll_feed
from users import User

SPEC = FeedSpec("internships", "a/b", "dev")

README_V1 = """\
## 💻 Software Engineering Internship Roles
<table><tbody>
<tr><td><strong>Stripe</strong></td><td>SWE Intern</td><td>SF</td>
<td><div align="center"><a href="https://stripe.com/j?gh_jid=1&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>

## 💰 Quantitative Finance Internship Roles
<table><tbody>
<tr><td><strong>Jane</strong></td><td>Quant Intern</td><td>NY</td>
<td><div align="center"><a href="https://jane.com/q"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>
"""

README_V2 = README_V1.replace(
    "</tbody></table>\n\n## 💰",
    """<tr><td>↳</td><td>Backend Intern</td><td>NY</td>
<td><div align="center"><a href="https://stripe.com/j?gh_jid=2&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>

## 💰""",
)


def _user(uid, feeds, sections):
    return User(id=uid, cv="/x", discord_webhook="https://d/1", feeds=feeds, sections=sections)


RON = _user("ron", ["internships", "new-grad"], ["software engineering"])
COUSIN = _user("cousin", ["new-grad"], ["software engineering"])


def _github(sha, readme):
    return (lambda repo, branch: sha), (lambda repo, s: readme)


@pytest.fixture
def db(session):
    ensure_feeds(session, [SPEC, FeedSpec("new-grad", "a/c", "dev")])
    session.commit()
    return session


def test_first_poll_seeds_jobs_without_evaluations(db):
    sha, readme = _github("s1", README_V1)
    result = poll_feed(db, SPEC, [RON], sha, readme)
    assert result.sha_changed and result.jobs_added == 2 and result.evaluations_added == 0
    assert db.query(Evaluation).count() == 0
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"


def test_unchanged_sha_is_noop(db):
    sha, readme = _github("s1", README_V1)
    poll_feed(db, SPEC, [RON], sha, readme)
    calls = []
    result = poll_feed(db, SPEC, [RON], sha, lambda r, s: calls.append(s) or README_V1)
    assert not result.sha_changed
    assert calls == []


def test_new_row_creates_job_and_evaluation_for_matching_user(db):
    poll_feed(db, SPEC, [RON, COUSIN], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [RON, COUSIN], *_github("s2", README_V2))
    assert result.jobs_added == 1
    assert result.evaluations_added == 1
    ev = db.query(Evaluation).one()
    assert ev.user_id == "ron"           # cousin is not subscribed to internships
    assert ev.stage == STAGE_DELIVER
    assert ev.job.url_key == "https://stripe.com/j?gh_jid=2"
    assert ev.job.company == "Stripe"    # ↳ row inherited the company
    assert ev.job.section == "software engineering internship roles"


def test_job_in_unsubscribed_section_gets_no_evaluation(db):
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1.replace("https://jane.com/q", "https://old.com/q")))
    # jane.com/q appears as a *new* quant row in s2
    result = poll_feed(db, SPEC, [RON], *_github("s2", README_V1))
    assert result.jobs_added == 1
    assert result.evaluations_added == 0


def test_readme_missing_leaves_sha_and_jobs_untouched(db):
    # raw.githubusercontent.com can lag the commits API; a missing README must not
    # advance last_sha, or the next poll would treat the whole feed as new.
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    jobs_before = db.query(Job).count()
    result = poll_feed(db, SPEC, [RON], (lambda r, b: "s2"), (lambda r, s: None))
    assert result.sha_changed is True
    assert result.jobs_added == 0
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"
    assert db.query(Job).count() == jobs_before == 2


def test_readme_missing_then_present_seeds_without_evaluations(db):
    poll_feed(db, SPEC, [RON], (lambda r, b: "s1"), (lambda r, s: None))
    result = poll_feed(db, SPEC, [RON], *_github("s2", README_V1))
    assert result.jobs_added > 0
    assert result.evaluations_added == 0
    assert db.query(Evaluation).count() == 0


def test_feed_with_sha_but_no_jobs_is_seeded(db):
    feed = db.query(Feed).filter_by(name="internships").one()
    feed.last_sha = "old"
    db.commit()
    result = poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    assert result.jobs_added == 2
    assert result.evaluations_added == 0
    assert db.query(Evaluation).count() == 0


def test_same_url_in_two_feeds_yields_two_jobs(db):
    other = FeedSpec("new-grad", "a/c", "dev")
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    poll_feed(db, other, [RON], *_github("n1", README_V1))
    assert db.query(Job).filter_by(url_key="https://stripe.com/j?gh_jid=1").count() == 2


def test_failure_mid_poll_writes_nothing(db, monkeypatch):
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    import poller as poller_mod

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(poller_mod, "_evaluations_for", boom)
    with pytest.raises(RuntimeError):
        poll_feed(db, SPEC, [RON], *_github("s2", README_V2))
    db.rollback()
    assert db.query(Job).count() == 2
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"
