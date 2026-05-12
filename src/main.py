import logging
import os
import time

from dotenv import load_dotenv

from discord_client import format_message, send_notification
from github_client import get_latest_sha, get_readme_patch
from parser import filter_by_keywords, parse_new_rows
from state import read_last_sha, write_last_sha

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
FILTER_KEYWORDS = [
    k.strip()
    for k in os.getenv("FILTER_KEYWORDS", "software engineer,swe,ai,machine learning,ml").split(",")
]


def poll() -> None:
    current_sha = get_latest_sha(REPO, BRANCH, GITHUB_TOKEN)
    last_sha = read_last_sha(DATA_DIR)

    if last_sha is None:
        log.info("First run — recording SHA %s, no notifications sent", current_sha[:7])
        write_last_sha(DATA_DIR, current_sha)
        return

    if current_sha == last_sha:
        log.debug("No new commits")
        return

    log.info("New commits: %s → %s", last_sha[:7], current_sha[:7])
    patch = get_readme_patch(REPO, last_sha, current_sha, GITHUB_TOKEN)

    if patch is None:
        log.info("README.md unchanged in this commit range")
        write_last_sha(DATA_DIR, current_sha)
        return

    rows = parse_new_rows(patch)
    matching = filter_by_keywords(rows, FILTER_KEYWORDS)
    log.info("New rows: %d total, %d match keywords", len(rows), len(matching))

    for posting in matching:
        message = format_message(posting)
        ok = send_notification(DISCORD_WEBHOOK_URL, message)
        if ok:
            log.info("Notified: %s — %s", posting["company"], posting["role"])
        else:
            log.error("Failed to notify for %s — %s", posting["company"], posting["role"])

    write_last_sha(DATA_DIR, current_sha)


def main() -> None:
    log.info("Internship tracker started (interval: %ds, keywords: %s)", POLL_INTERVAL, FILTER_KEYWORDS)
    while True:
        try:
            poll()
        except Exception as e:
            log.error("Poll error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
