import functools
import logging
import os
import threading
import time

from dotenv import load_dotenv

from config import FEEDS, Settings, load_settings
from cv import load_cv
from db import ensure_columns, ensure_feeds, import_legacy_state, init_db, make_engine, make_session_factory
from github_client import get_latest_sha, get_readme_content
from llm import LLMClient
from migration import migrate_state
from poller import poll_feed
from users import User, load_users
from worker import Worker

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")


def build(settings: Settings, users: list[User], llm=None):
    engine = make_engine(os.path.join(settings.data_dir, "tracker.db"))
    init_db(engine)
    added = ensure_columns(engine)
    if added:
        log.info("Schema upgraded: added %s", ", ".join(added))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        ensure_feeds(session, FEEDS.values())
        seeded = import_legacy_state(session, settings.data_dir)
        session.commit()
    if seeded:
        log.info("Imported %d jobs from legacy known_urls.json", seeded)

    cvs = {u.id: load_cv(u.cv).model_dump() for u in users}   # fails fast naming the bad file
    if llm is None:
        llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_score_model, settings.llm_timeout)
    log.info("Scoring with %s at %s", settings.llm_score_model, settings.llm_base_url)
    return session_factory, Worker(session_factory, users, cvs=cvs, llm=llm)


def poll_all(session_factory, users: list[User], settings: Settings) -> None:
    latest_sha = functools.partial(get_latest_sha, token=settings.github_token)
    readme = functools.partial(get_readme_content, token=settings.github_token)
    for spec in FEEDS.values():
        try:
            with session_factory() as session:
                poll_feed(session, spec, users, latest_sha, readme)
        except Exception:
            log.exception("[%s] poll failed", spec.name)


def _poll_loop(session_factory, users, settings) -> None:
    while True:
        try:
            poll_all(session_factory, users, settings)
        except Exception:
            log.exception("Poll cycle failed")
        time.sleep(settings.poll_interval)


def main() -> None:
    settings = load_settings(os.environ)
    users = load_users(os.path.join(settings.data_dir, "users.yaml"))
    log.info("Tracker starting: %d users, feeds %s, interval %ds",
             len(users), list(FEEDS), settings.poll_interval)

    # Bring pre-pipeline state files to version 2 first (PR #1). Retry until GitHub answers.
    internships = FEEDS["internships"]
    while not migrate_state(settings.data_dir,
                            lambda sha: get_readme_content(internships.repo, sha, settings.github_token)):
        log.error("State migration failed; retrying in 60s")
        time.sleep(60)

    session_factory, worker = build(settings, users)

    threading.Thread(target=_poll_loop, args=(session_factory, users, settings), name="poller", daemon=True).start()
    worker.run_forever()


if __name__ == "__main__":
    main()
