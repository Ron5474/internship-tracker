import logging
from collections.abc import Callable

from parser import parse_sections, url_key
from state import read_last_sha, read_state_version, write_known_urls, write_state_version

log = logging.getLogger(__name__)

# Bump when the meaning of known_urls.json changes. Version 2: url_key keeps
# job-identifying query params and the parser no longer drops ↳ rows, so keys
# written by version 1 no longer match and must be rebuilt.
STATE_VERSION = 2


def migrate_state(data_dir: str, fetch_readme: Callable[[str], str | None]) -> bool:
    """Bring on-disk state up to STATE_VERSION. Returns False if it must be retried.

    Rebuilds known_urls.json from the README at the saved SHA so that jobs which
    were already present (but mis-keyed or dropped by the old parser) stay silent,
    while anything added after that SHA is still picked up by the next poll.
    """
    if read_state_version(data_dir) >= STATE_VERSION:
        return True

    last_sha = read_last_sha(data_dir)
    if last_sha is None:
        write_state_version(data_dir, STATE_VERSION)
        return True

    try:
        readme = fetch_readme(last_sha)
    except Exception as e:
        log.error("State migration: failed to fetch README at %s: %s", last_sha[:7], e)
        return False
    if readme is None:
        log.error("State migration: README at %s not found, will retry", last_sha[:7])
        return False

    sections = parse_sections(readme)
    all_urls = {url_key(r["url"]) for rows in sections.values() for r in rows}
    write_known_urls(data_dir, all_urls)
    write_state_version(data_dir, STATE_VERSION)
    log.info("State migration: rebuilt %d known URLs from SHA %s", len(all_urls), last_sha[:7])
    return True
