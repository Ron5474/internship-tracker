# Plan 5 — Split Destinations: a public feed and a private results channel

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Postings keep flowing to the shared Discord server as a plain link-only feed, while scores, gaps and tailored resume PDFs go to a private channel only the owner reads.

**Architecture:** A new `feed_posts` table records "this job was announced to this channel", keyed on `(job_id, webhook_key)` so N users watching one shared channel produce one post, not N. The poller creates those rows alongside evaluations; a new worker slot drains them. Evaluations are untouched: their delivery simply routes to a per-user private webhook instead of the shared one.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (SQLite), Pydantic 2, requests, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md` — this plan amends its "Delivery" and "Users" sections; Task 5 records the amendment.

## Why this shape

The obvious implementation — a second delivery on each evaluation — is wrong, and was rejected during design. Evaluations are per-user, so two users watching the same shared channel would each post the same job to it. The feed is not a fact about a user's evaluation; it is a fact about *a job having been announced to a channel*, and keying it that way makes the deduplication structural rather than a matter of configuration discipline.

It also keeps Plans 1–4 intact: no new columns on `evaluations`, no change to the stage machine, and the rule that a row closes when Discord confirms its message stays exactly as it was.

## Global Constraints

- **Opt-in.** A user without `discord_webhook_private` behaves exactly as today: everything to the one webhook, and **no feed posts at all** — otherwise they would receive every posting twice.
- **Never backfill.** Feed posts are created only for jobs the poller inserts, and only when that feed is not seeding. Creating them for existing rows would blast thousands of historical postings into the shared channel.
- **No webhook URL in the database.** `feed_posts.webhook_key` is a hash. The URL is a secret and lives only in `users.yaml`.
- `stage` is always *the next action to run*; `outcome` is written once and never overwritten. Neither changes in this plan.
- No notification is lost: feed delivery has no give-up, mirroring `deliver()`. A `gone` webhook pauses that destination until restart.
- An attempt is leased — incremented and committed — before any slow call; deadlines come from the post-call clock.
- Tests: pytest, all externals mocked, in-memory SQLite, no network.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Routing, in one table

| | destination | contents |
|---|---|---|
| Feed | `discord_webhook` (only when private is set) | `🆕 Company — Role / 📍 / 🔗` for every posting matching a user's filters |
| Results | `discord_webhook_private`, falling back to `discord_webhook` | 🎯 score, reasoning, gaps, attached PDF; the `couldn't read the description` / `couldn't score` fallbacks; and 📉 below-threshold when `notify_below_threshold` is set |

Below-threshold notices go to the **private** channel, not the feed. They carry a score, and the calibration workflow depends on seeing them.

## File Structure

| File | Responsibility |
|---|---|
| `src/users.py` (modify) | `discord_webhook_private`, `feed_webhook`, `results_webhook`, `webhook_key()` |
| `src/db.py` (modify) | `FeedPost` model |
| `src/poller.py` (modify) | create feed posts for newly inserted jobs |
| `src/worker.py` (modify) | feed slot, feed send, results routing |
| `src/main.py` (modify) | report split destinations at startup |
| `data/users.example.yaml`, `docs/ops.md`, the spec (modify) | configuration and runbook |

---

### Task 1: User configuration and webhook identity

**Files:**
- Modify: `src/users.py`
- Test: `tests/test_users.py`

**Interfaces:**
- Produces: `User.discord_webhook_private: str | None`; `User.results_webhook -> str`; `User.feed_webhook -> str | None`; `users.webhook_key(url: str) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
from users import User, webhook_key

def _user(**kw):
    base = dict(id="ron", cv="/x", discord_webhook="https://d/shared",
                feeds=["internships"], sections=["software"])
    return User(**{**base, **kw})


def test_without_a_private_webhook_everything_goes_to_the_one_destination():
    u = _user()
    assert u.results_webhook == "https://d/shared"
    # No feed webhook: a single-destination user would otherwise get every posting twice.
    assert u.feed_webhook is None


def test_with_a_private_webhook_the_shared_one_becomes_the_feed():
    u = _user(discord_webhook_private="https://d/private")
    assert u.results_webhook == "https://d/private"
    assert u.feed_webhook == "https://d/shared"


def test_webhook_key_is_stable_and_not_the_url():
    key = webhook_key("https://discord.com/api/webhooks/123/secret-token")
    assert key == webhook_key("https://discord.com/api/webhooks/123/secret-token")
    assert "secret-token" not in key and "discord" not in key
    assert len(key) == 16


def test_webhook_key_separates_different_destinations():
    assert webhook_key("https://d/a") != webhook_key("https://d/b")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_users.py -q`
Expected: FAIL — `cannot import name 'webhook_key' from 'users'`.

- [ ] **Step 3: Implement**

```python
import hashlib


def webhook_key(url: str) -> str:
    """A stable id for a destination that is safe to store.

    The webhook URL is a credential — anyone holding it can post to the channel — so the
    database records a hash of it. Truncated to 16 hex characters, which is far beyond
    collision range for the handful of destinations one deployment has.
    """
    return hashlib.sha256(url.encode()).hexdigest()[:16]
```

and on `User`, after `notify_below_threshold`:

```python
    discord_webhook_private: str | None = None

    @property
    def results_webhook(self) -> str:
        """Scores, gaps and resumes. Private when one is configured."""
        return self.discord_webhook_private or self.discord_webhook

    @property
    def feed_webhook(self) -> str | None:
        """The link-only public feed, and only when the destinations are actually split:
        with one webhook it already receives everything, so a feed post would duplicate it."""
        return self.discord_webhook if self.discord_webhook_private else None
```

- [ ] **Step 4: Run the tests, then the suite**

Run: `python3 -m pytest tests/test_users.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/users.py tests/test_users.py
git commit -m "feat(users): optional private webhook, with the shared one becoming the feed"
```

---

### Task 2: The `feed_posts` table

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.FeedPost` with `id, job_id, webhook_key, sent_at, attempts, error, next_attempt_at`, unique on `(job_id, webhook_key)`, and a `job` relationship.

- [ ] **Step 1: Write the failing tests**

```python
def test_feed_post_is_unique_per_job_and_destination(session_factory, session):
    # The whole point: two users watching one shared channel must produce ONE post.
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    session.add_all([feed, job, FeedPost(job=job, webhook_key="abc")])
    session.commit()
    session.add(FeedPost(job=job, webhook_key="abc"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_two_destinations_each_get_their_own_row(session_factory, session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    session.add_all([feed, job, FeedPost(job=job, webhook_key="aaa"), FeedPost(job=job, webhook_key="bbb")])
    session.commit()
    assert session.query(FeedPost).count() == 2


def test_feed_post_defaults_are_unsent(session_factory, session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    post = FeedPost(job=job, webhook_key="abc")
    session.add_all([feed, job, post])
    session.commit()
    assert post.sent_at is None and post.attempts == 0 and post.error is None
    assert post.next_attempt_at is not None


def test_ensure_columns_is_still_a_noop_on_a_current_database(tmp_path):
    engine = make_engine(str(tmp_path / "new.db"))
    init_db(engine)
    assert ensure_columns(engine) == []
```

Import `IntegrityError` from `sqlalchemy.exc` and `FeedPost` from `db`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_db.py -q`
Expected: FAIL — `cannot import name 'FeedPost' from 'db'`.

- [ ] **Step 3: Implement**

```python
class FeedPost(Base):
    """One announcement of one job to one destination channel.

    Keyed on the destination rather than on a user, so several users sharing a channel
    produce a single post. `webhook_key` is a hash, never the URL itself.
    """

    __tablename__ = "feed_posts"
    __table_args__ = (UniqueConstraint("job_id", "webhook_key", name="uq_feed_post_job_webhook"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    webhook_key: Mapped[str] = mapped_column(String)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[Job] = relationship()
```

Add `feed_posts` to `Job` as a relationship only if a test needs it; the plan does not.

- [ ] **Step 4: Run the tests, then the suite**

Run: `python3 -m pytest tests/test_db.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS. `init_db`'s `create_all` creates the new table on existing databases; nothing needs a migration.

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): feed_posts, one announcement per job per destination"
```

---

### Task 3: The poller creates feed posts

**Files:**
- Modify: `src/poller.py`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `users.webhook_key`, `User.feed_webhook` (Task 1); `db.FeedPost` (Task 2).
- Produces: `PollResult` gains `feed_posts_added: int`.

- [ ] **Step 1: Write the failing tests**

```python
from db import FeedPost
from users import webhook_key

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
```

`db`, `SPEC`, `RON`, `README_V1`, `README_V2` and `_github` already exist in `tests/test_poller.py`; use them rather than a new harness. Note that the first `poll_feed` in most of these seeds the feed, and the second is the one under test.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_poller.py -q`
Expected: FAIL — `PollResult` has no `feed_posts_added`.

- [ ] **Step 3: Implement**

`PollResult` gains `feed_posts_added: int`, and inside the not-seeding branch:

```python
            if not seeding:
                evals = _evaluations_for(job, spec.name, section, users)
                session.add_all(evals)
                evals_added += len(evals)
                posts = _feed_posts_for(job, spec.name, section, users)
                session.add_all(posts)
                posts_added += len(posts)
```

```python
def _feed_posts_for(job: Job, feed_name: str, section: str, users: list[User]) -> list[FeedPost]:
    """One row per distinct destination, not per user.

    Users sharing a channel share its key, so the set collapses them. Sorted so a poll
    writes rows in a deterministic order.
    """
    keys = {webhook_key(u.feed_webhook) for u in users
            if u.feed_webhook and u.wants(feed_name, section)}
    return [FeedPost(job=job, webhook_key=key) for key in sorted(keys)]
```

Include the count in the existing log line.

- [ ] **Step 4: Run the tests, then the suite**

Run: `python3 -m pytest tests/test_poller.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS. Existing `PollResult` assertions that unpack positionally must be updated for the new field.

- [ ] **Step 5: Commit**

```bash
git add src/poller.py tests/test_poller.py
git commit -m "feat(poller): announce new postings to each distinct feed destination once"
```

---

### Task 4: The worker's feed slot, and routing results privately

**Files:**
- Modify: `src/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `db.FeedPost`, `User.feed_webhook`, `User.results_webhook`, `users.webhook_key`.
- Produces: `worker.FEED_BUDGET = 10`; `Worker._next_feed_post`; `Worker.post_feed(session, post)`; pause key `feed:<webhook_key>`; `run_once()` order `deliver → feed → fetch → render → score → tailor`.

- [ ] **Step 1: Write the failing tests**

```python
from db import FeedPost
from users import webhook_key
from worker import FEED_BUDGET

SHARED = "https://d/shared"
RON_SPLIT = User(id="ron", cv="/x", discord_webhook=SHARED,
                 discord_webhook_private="https://d/private",
                 feeds=["internships"], sections=["software"])


def _seed_feed_post(session, key=None):
    ev = _seed(session, "ron", stage=STAGE_CLOSED)       # a job exists; the evaluation is irrelevant
    post = FeedPost(job=ev.job, webhook_key=key or webhook_key(SHARED), next_attempt_at=T0)
    session.add(post)
    session.commit()
    return post


def test_a_feed_post_sends_the_link_only_message_to_the_shared_channel(session_factory, session, clock):
    post = _seed_feed_post(session)
    sender = FakeSender(OK)
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    assert w.run_once() is True
    webhook, content, pdf, filename = sender.calls[0]
    assert webhook == SHARED                     # the public feed, not the private channel
    assert content.startswith("🆕")
    assert pdf is None                           # never a resume in the shared channel
    assert "%" not in content                    # and never a score
    session.refresh(post)
    assert post.sent_at is not None


def test_a_sent_feed_post_is_never_sent_again(session_factory, session, clock):
    _seed_feed_post(session)
    sender = FakeSender(OK)
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    assert w.run_once() is True
    assert w.run_once() is False
    assert len(sender.calls) == 1


def test_a_feed_post_retries_with_backoff(session_factory, session, clock):
    post = _seed_feed_post(session)
    sender = FakeSender(DeliveryResult("transient", None, "503"), OK)
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    w.run_once()
    session.refresh(post)
    assert post.sent_at is None and post.attempts == 1 and "503" in post.error
    assert post.next_attempt_at == T0 + timedelta(seconds=BACKOFF_SECONDS[0])
    clock.advance(BACKOFF_SECONDS[0])
    w.run_once()
    session.refresh(post)
    assert post.sent_at is not None


def test_a_gone_feed_webhook_pauses_that_destination_until_restart(session_factory, session, clock):
    key = webhook_key(SHARED)
    _seed_feed_post(session)
    sender = FakeSender(DeliveryResult("gone", None, "webhook returned 404"))
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    w.run_once()
    assert w.is_paused(f"feed:{key}") is True
    clock.advance(86400)
    assert w.run_once() is False                 # still paused a day later


def test_a_feed_post_for_an_unknown_destination_is_skipped(session_factory, session, clock):
    # Its user left users.yaml. Nothing can send it; it must not block the loop either.
    _seed_feed_post(session, key="deadbeefdeadbeef")
    sender = FakeSender()
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    assert w.run_once() is False
    assert sender.calls == []


def test_the_feed_never_blocks_on_the_private_channel(session_factory, session, clock):
    # A dead private webhook must not stop the public feed: they are different destinations.
    post = _seed_feed_post(session)
    sender = FakeSender(OK)
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    w._pause("discord:ron", None, "private webhook gone")
    assert w.run_once() is True
    session.refresh(post)
    assert post.sent_at is not None


def test_results_go_to_the_private_webhook(session_factory, session, clock):
    ev = _resolve(session, _seed(session, "ron", stage=STAGE_DELIVER, score=82,
                                 outcome="matched", reasoning="why"))
    sender = FakeSender(OK)
    _worker(session_factory, sender, clock, users=(RON_SPLIT,)).run_once()
    assert sender.calls[0][0] == "https://d/private"
    assert sender.calls[0][1].startswith("🎯")


def test_results_stay_on_the_single_webhook_when_no_private_one_is_set(session_factory, session, clock):
    # Plan 1-4 behaviour, unchanged for a user who has not opted in.
    ev = _resolve(session, _seed(session, "ron", stage=STAGE_DELIVER, score=82,
                                 outcome="matched", reasoning="why"))
    sender = FakeSender(OK)
    _worker(session_factory, sender, clock).run_once()          # RON has no private webhook
    assert sender.calls[0][0] == RON.discord_webhook


def test_a_ready_result_is_delivered_before_a_feed_post(session_factory, session, clock):
    _seed_feed_post(session)
    ev = _resolve(session, _seed(session, "ron", stage=STAGE_DELIVER, score=82, outcome="matched"))
    sender = FakeSender(OK, OK)
    w = _worker(session_factory, sender, clock, users=(RON_SPLIT,))
    w.run_once()
    assert sender.calls[0][1].startswith("🎯")


def test_feed_over_budget_logs_but_keeps_trying(session_factory, session, clock):
    # A feed post is a notification: like delivery, it has no give-up.
    post = _seed_feed_post(session)
    post.attempts = FEED_BUDGET + 1
    session.commit()
    sender = FakeSender(OK)
    _worker(session_factory, sender, clock, users=(RON_SPLIT,)).run_once()
    session.refresh(post)
    assert post.sent_at is not None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_worker.py -q`
Expected: FAIL — `cannot import name 'FEED_BUDGET'`.

- [ ] **Step 3: Implement**

```python
FEED_BUDGET = 10
```

In `__init__`, build the destination map from the users the worker was given:

```python
        # key -> URL, so a feed_posts row can be resolved without storing the secret.
        self._feed_webhooks = {webhook_key(u.feed_webhook): u.feed_webhook
                               for u in users if u.feed_webhook}
```

```python
    def _next_feed_post(self, session: Session) -> FeedPost | None:
        if not self._feed_webhooks:
            return None
        now = self._now()
        candidates = (
            session.query(FeedPost)
            .filter(FeedPost.sent_at.is_(None),
                    FeedPost.next_attempt_at <= now,
                    FeedPost.webhook_key.in_(self._feed_webhooks))
            .order_by(FeedPost.next_attempt_at, FeedPost.id)
            .all()
        )
        for post in candidates:
            if not self.is_paused(f"feed:{post.webhook_key}"):
                return post
        return None

    def post_feed(self, session: Session, post: FeedPost) -> None:
        """Announce one job to one channel: the link, nothing about the candidate."""
        webhook = self._feed_webhooks[post.webhook_key]
        job = post.job
        content = format_link_only(job.company, job.role, job.location, job.url)
        try:
            result = self._send(webhook, content)
        except Exception as e:  # noqa: BLE001 — a sender bug is a failed attempt, not a dead worker
            log.exception("Sender raised for feed post %d", post.id)
            result = DeliveryResult("transient", None, f"{type(e).__name__}: {e}")

        if result.ok:
            post.sent_at = self._now()
            post.error = None
            log.info("feed post=%d job=%d: %s — %s", post.id, job.id, job.company, job.role)
            return

        if result.kind == "gone":
            self._pause(f"feed:{post.webhook_key}", None, result.error or "feed webhook gone")
            return

        post.attempts += 1
        post.error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(post.attempts)
        post.next_attempt_at = self._now() + timedelta(seconds=delay)
        level = logging.ERROR if post.attempts > FEED_BUDGET else logging.WARNING
        log.log(level, "Feed post %d failed (attempt %d, %s): %s; retry in %ss",
                post.id, post.attempts, result.kind, result.error, delay)
        if result.kind == "transient":
            self._pause(f"feed:{post.webhook_key}", post.next_attempt_at, result.error or result.kind)
```

Wire it into `run_once()` immediately after the deliver slot — a posting should reach the shared channel within seconds, and this costs one HTTP call:

```python
            post = self._next_feed_post(session)
            if post is not None:
                self.post_feed(session, post)
                session.commit()
                return True
```

In `deliver()`, send to the private destination:

```python
            result = self._send(user.results_webhook, message_for(ev), pdf_path,
                                self._attachment_name(ev) if pdf_path else None)
```

Update the `run_once()` ordering comment to name the feed slot and why it sits where it does.

- [ ] **Step 4: Run the tests, then the suite**

Run: `python3 -m pytest tests/test_worker.py -q` then `python3 -m pytest tests/ -q`
Expected: PASS, with every Plan 1–4 worker test unchanged — a user with no private webhook must behave exactly as before.

- [ ] **Step 5: Commit**

```bash
git add src/worker.py tests/test_worker.py
git commit -m "feat(worker): drain feed posts to the public channel, results to the private one"
```

---

### Task 5: Wiring, end-to-end proof, and documentation

**Files:**
- Modify: `src/main.py`, `tests/test_main.py`, `data/users.example.yaml`, `docs/ops.md`, `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md`

- [ ] **Step 1: Write the failing end-to-end test**

In `tests/test_main.py`, using the existing `_poll_one_posting` / `_write_users_and_cv` helpers:

```python
def test_a_posting_reaches_the_feed_and_the_match_reaches_the_private_channel(tmp_path, session_factory, monkeypatch):
    """The whole point of this plan, proved once end to end."""
    users = [RON.model_copy(update={"discord_webhook_private": "https://d/private"})]
    _poll_one_posting(session_factory, users, monkeypatch)
    with session_factory() as session:
        job = session.query(Job).one()
        job.description, job.fetch_status = "We need a Python engineer.", FETCH_OK
        session.commit()

    worker, llm, tailor, sender = _pipeline(tmp_path, session_factory, users, {"ron": CV_SNAPSHOT})
    while worker.run_once():
        pass

    by_webhook = {}
    for webhook, content, pdf, _name in sender.calls:
        by_webhook.setdefault(webhook, []).append((content, pdf))

    feed = by_webhook[users[0].discord_webhook]
    assert len(feed) == 1 and feed[0][0].startswith("🆕") and feed[0][1] is None
    assert "%" not in feed[0][0]                      # no score ever reaches the shared channel

    private = by_webhook["https://d/private"]
    assert len(private) == 1 and private[0][0].startswith("🎯")
    assert private[0][1] is not None                  # the PDF went here
```

- [ ] **Step 2: Run to verify it fails, then wire it up**

`main.build` needs no change — `Worker` already receives `users`. Add a startup line so the deployment says what it will do:

```python
    split = [u.id for u in users if u.feed_webhook]
    if split:
        log.info("Public feed enabled for: %s (results go to their private webhooks)", ", ".join(split))
```

- [ ] **Step 3: Documentation**

`data/users.example.yaml` gains, with the explanation:

```yaml
  # Optional. Set this and the split begins: `discord_webhook` above becomes a public,
  # link-only feed of every matching posting, and scores, gaps and tailored resumes go
  # here instead. Leave it out and everything goes to the one webhook, as before.
  discord_webhook_private: https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
```

`docs/ops.md` gains a **Split destinations** section covering: what lands where; that the split is opt-in per user; that enabling it takes effect on the next poll and **never backfills**, so the shared channel gets no history; that `feed_posts` is keyed on `(job_id, webhook_key)` so several users on one channel produce one post; and the queries:

```sql
-- feed posts waiting to go out
SELECT COUNT(*) FROM feed_posts WHERE sent_at IS NULL;

-- feed posts that keep failing
SELECT id, job_id, attempts, substr(error,1,60) FROM feed_posts
WHERE sent_at IS NULL AND attempts > 0 ORDER BY attempts DESC;

-- re-send one announcement
UPDATE feed_posts SET sent_at=NULL, attempts=0, error=NULL,
       next_attempt_at=CURRENT_TIMESTAMP WHERE id = <id>;
```

Record the spec amendment under "Delivery": a user may have two destinations; the public one receives link-only announcements tracked in `feed_posts` and keyed on the destination rather than the user, while the private one receives everything derived from the candidate's CV. Note that below-threshold notices go to the private channel.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/main.py tests/ data/users.example.yaml docs/
git commit -m "feat(main): report split destinations; end-to-end proof, runbook and spec amendment"
```

---

## Deploying this

1. Create the private Discord channel and its webhook.
2. Add `discord_webhook_private` to your entry in `data/users.yaml`.
3. `docker compose pull && docker compose up -d`. The `feed_posts` table is created automatically.
4. Confirm the startup line names you under "Public feed enabled for".
5. The next posting appears as 🆕 in the shared channel and as 🎯 with its PDF in the private one. Nothing historical is announced.

To turn the split off, remove `discord_webhook_private` and restart: everything returns to the single webhook, and unsent feed posts are simply never selected again.

## Open afterwards

- Rows in `feed_posts` whose destination has left `users.yaml` are skipped forever. They are inert, but a `docs/ops.md` query to find and delete them would be tidy.
- `feed_posts` grows by one row per job per destination and is never pruned. At this scale that is a few thousand rows a year.
