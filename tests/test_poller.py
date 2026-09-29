import pytest

from config import FeedSpec
from db import Evaluation, Feed, FeedPost, Job, ensure_feeds, STAGE_SCORE
from poller import poll_feed
from users import User, webhook_key

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
    assert ev.stage == STAGE_SCORE
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


def test_failure_mid_poll_writes_no_feed_posts_either(db, monkeypatch):
    # The existing atomicity test above patches _evaluations_for, which runs *before*
    # _feed_posts_for in poll_feed's loop, so it never reaches the feed-post write and proves
    # nothing about it. This exercises that path directly, with a split user so a feed post
    # would actually be produced if the failure didn't roll it back.
    poll_feed(db, SPEC, [FEED_RON], *_github("s1", README_V1))
    import poller as poller_mod

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(poller_mod, "_feed_posts_for", boom)
    with pytest.raises(RuntimeError):
        poll_feed(db, SPEC, [FEED_RON], *_github("s2", README_V2))
    db.rollback()
    assert db.query(FeedPost).count() == 0
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"


SHARED = "https://d/1"        # _user() already points every user at this webhook

FEED_RON = RON.model_copy(update={"discord_webhook_private": "https://d/private"})


def test_a_new_posting_creates_one_feed_post_per_destination(db):
    # Two users, same shared channel: one post, not two.
    sam = FEED_RON.model_copy(update={"id": "sam"})
    poll_feed(db, SPEC, [FEED_RON, sam], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [FEED_RON, sam], *_github("s2", README_V2))
    assert [p.webhook_key for p in db.query(FeedPost).all()] == [webhook_key(SHARED)]
    assert result.feed_posts_added == 1


def test_two_different_channels_each_get_a_post(db):
    elsewhere = FEED_RON.model_copy(update={"id": "sam", "discord_webhook": "https://d/other"})
    poll_feed(db, SPEC, [FEED_RON, elsewhere], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [FEED_RON, elsewhere], *_github("s2", README_V2))
    assert result.feed_posts_added == 2
    assert db.query(FeedPost).count() == 2


def test_a_user_without_a_private_webhook_gets_no_feed_post(db):
    # RON has not opted in: one webhook already receives everything, so a feed post
    # would deliver the same posting twice.
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [RON], *_github("s2", README_V2))
    assert result.jobs_added == 1 and result.feed_posts_added == 0
    assert db.query(FeedPost).count() == 0


def test_seeding_creates_no_feed_posts(db):
    # The first poll inserts a feed's whole history. Announcing that would post thousands
    # of old jobs into the shared channel.
    result = poll_feed(db, SPEC, [FEED_RON], *_github("s1", README_V1))
    assert result.jobs_added == 2 and result.feed_posts_added == 0
    assert db.query(FeedPost).count() == 0


def test_feed_posts_follow_the_section_filter(db):
    # The new row in README_V2 is a software role; this user only watches quant.
    picky = FEED_RON.model_copy(update={"sections": ["quantitative finance"]})
    poll_feed(db, SPEC, [picky], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [picky], *_github("s2", README_V2))
    assert result.jobs_added == 1 and result.feed_posts_added == 0


def test_a_job_already_known_creates_no_second_feed_post(db):
    poll_feed(db, SPEC, [FEED_RON], *_github("s1", README_V1))
    poll_feed(db, SPEC, [FEED_RON], *_github("s2", README_V2))
    before = db.query(FeedPost).count()
    poll_feed(db, SPEC, [FEED_RON], *_github("s3", README_V2))   # same README, new SHA
    assert db.query(FeedPost).count() == before


INTERN_CHANNEL = "https://d/interns"
FULLTIME_CHANNEL = "https://d/fulltime"


def test_each_feed_announces_to_its_own_channel(db):
    """The requirement: internships in one channel, full-time roles in another."""
    new_grad = FeedSpec("new-grad", "a/c", "dev")
    u = FEED_RON.model_copy(update={
        "feeds": ["internships", "new-grad"],
        "discord_webhook_feeds": {"internships": INTERN_CHANNEL, "new-grad": FULLTIME_CHANNEL},
    })
    # Seed both feeds first — a first poll announces nothing — then let each discover a row.
    poll_feed(db, SPEC, [u], *_github("s1", README_V1))
    poll_feed(db, new_grad, [u], *_github("n1", README_V1))
    poll_feed(db, SPEC, [u], *_github("s2", README_V2))
    poll_feed(db, new_grad, [u], *_github("n2", README_V2))

    posts = db.query(FeedPost).all()
    assert len(posts) == 2
    by_key = {p.webhook_key: p.job.feed.name for p in posts}
    assert by_key == {
        webhook_key(INTERN_CHANNEL): "internships",
        webhook_key(FULLTIME_CHANNEL): "new-grad",
    }


def test_a_feed_without_its_own_channel_still_uses_the_shared_one(db):
    # Partial configuration: full-time gets a new home, internships stay where they were.
    u = FEED_RON.model_copy(update={
        "feeds": ["internships", "new-grad"],
        "discord_webhook_feeds": {"new-grad": FULLTIME_CHANNEL},
    })
    poll_feed(db, SPEC, [u], *_github("s1", README_V1))
    poll_feed(db, SPEC, [u], *_github("s2", README_V2))
    assert [p.webhook_key for p in db.query(FeedPost).all()] == [webhook_key(SHARED)]
