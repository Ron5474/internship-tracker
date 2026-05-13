import logging
import os
import time

from dotenv import load_dotenv

from discord_client import format_message, send_notification
from github_client import get_latest_sha, get_readme_content
from parser import find_new_rows, parse_sections, url_key
from state import read_known_urls, read_last_sha, write_known_urls, write_last_sha

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

REPO = "SimplifyJobs/Summer2026-Internships"
BRANCH = "dev"
DATA_DIR = os.getenv("DATA_DIR", "/data")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
FILTER_SECTIONS = [
    s.strip().lower()
    for s in os.getenv("FILTER_SECTIONS", "software engineering,product management,data science").split(",")
]


def poll() -> None:
    current_sha = get_latest_sha(REPO, BRANCH, GITHUB_TOKEN)
    last_sha = read_last_sha(DATA_DIR)
    known_urls = {url_key(u) for u in read_known_urls(DATA_DIR)}

    if last_sha is None or not known_urls:
        readme = get_readme_content(REPO, current_sha, GITHUB_TOKEN)
        if readme:
            sections = parse_sections(readme)
            all_urls = {url_key(r["url"]) for rows in sections.values() for r in rows}
            write_known_urls(DATA_DIR, all_urls)
        write_last_sha(DATA_DIR, current_sha)
        log.info("Initialized state — recording SHA %s, no notifications sent", current_sha[:7])
        return

    if current_sha == last_sha:
        log.debug("No new commits")
        return

    log.info("New commits: %s → %s", last_sha[:7], current_sha[:7])
    readme = get_readme_content(REPO, current_sha, GITHUB_TOKEN)

    if readme is None:
        write_last_sha(DATA_DIR, current_sha)
        return

    sections = parse_sections(readme)
    new_rows = find_new_rows(sections, known_urls, FILTER_SECTIONS)
    log.info("New postings in target sections: %d", len(new_rows))

    for posting in new_rows:
        message = format_message(posting)
        ok = send_notification(DISCORD_WEBHOOK_URL, message)
        if ok:
            log.info("Notified: %s — %s", posting["company"], posting["role"])
        else:
            log.error("Failed to notify for %s — %s", posting["company"], posting["role"])

    all_urls = {url_key(r["url"]) for rows in sections.values() for r in rows}
    write_known_urls(DATA_DIR, all_urls)
    write_last_sha(DATA_DIR, current_sha)


def main() -> None:
    log.info("Internship tracker started (interval: %ds, sections: %s)", POLL_INTERVAL, FILTER_SECTIONS)
    while True:
        try:
            poll()
        except Exception as e:
            log.error("Poll error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
