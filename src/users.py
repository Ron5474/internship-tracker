from pathlib import Path
import hashlib

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from config import FEEDS


def webhook_key(url: str) -> str:
    """A stable id for a destination that is safe to store.

    The webhook URL is a credential — anyone holding it can post to the channel — so the
    database records a hash of it. Truncated to 16 hex characters, which is far beyond
    collision range for the handful of destinations one deployment has.
    """
    return hashlib.sha256(url.encode()).hexdigest()[:16]


class User(BaseModel):
    # A typo'd or misspelled key (e.g. `discord_webhook_privat:`) must fail loudly at load time
    # rather than being silently dropped by Pydantic's default extra="ignore" — which would leave
    # discord_webhook_private unset and route CV-derived content to the public feed.
    model_config = ConfigDict(extra="forbid")

    id: str
    cv: str
    discord_webhook: str
    feeds: list[str] = Field(min_length=1)
    sections: list[str] = Field(min_length=1)
    threshold: int = 60
    notify_below_threshold: bool = False
    # min_length=1 so `discord_webhook_private: ""` fails validation instead of passing as a
    # falsy-but-present string that `results_webhook` would then silently route to the public URL.
    discord_webhook_private: str | None = Field(default=None, min_length=1)
    # Public channel per feed, e.g. internships in one and full-time roles in another. A feed
    # named here is announced there; anything unnamed falls back to `discord_webhook`.
    discord_webhook_feeds: dict[str, str] = Field(default_factory=dict)

    @field_validator("discord_webhook_feeds")
    @classmethod
    def _known_feed_channels(cls, mapping: dict[str, str]) -> dict[str, str]:
        unknown = [f for f in mapping if f not in FEEDS]
        if unknown:
            raise ValueError(f"unknown feed(s) in discord_webhook_feeds: {', '.join(unknown)}")
        if any(not url for url in mapping.values()):
            raise ValueError("discord_webhook_feeds URLs must not be empty")
        return mapping

    @model_validator(mode="after")
    def _destinations_are_distinct(self) -> "User":
        # A channel receiving both a 🆕 posting and the 🎯 scored message with the resume
        # attached defeats the split, so no public destination may be the results channel.
        if self.discord_webhook_private is not None and self.discord_webhook_private == self.discord_webhook:
            raise ValueError("discord_webhook_private must differ from discord_webhook")
        clashing = [f for f, url in self.discord_webhook_feeds.items() if url == self.results_webhook]
        if clashing:
            raise ValueError(
                f"discord_webhook_feeds[{clashing[0]!r}] must differ from the results webhook")
        return self

    @field_validator("feeds")
    @classmethod
    def _known_feeds(cls, feeds: list[str]) -> list[str]:
        unknown = [f for f in feeds if f not in FEEDS]
        if unknown:
            raise ValueError(f"unknown feed(s): {', '.join(unknown)}")
        return feeds

    @field_validator("sections")
    @classmethod
    def _lowercase(cls, sections: list[str]) -> list[str]:
        return [s.strip().lower() for s in sections]

    def wants(self, feed_name: str, section: str) -> bool:
        """Same matching rule the old FILTER_SECTIONS used: substring on the normalized heading."""
        return feed_name in self.feeds and any(s in section for s in self.sections)

    @property
    def results_webhook(self) -> str:
        """Scores, gaps and resumes. Private when one is configured."""
        return self.discord_webhook_private or self.discord_webhook

    def feed_webhook_for(self, feed_name: str) -> str | None:
        """Where postings from this feed are announced publicly, if anywhere.

        A channel named for the feed wins — that naming is itself the opt-in, and it cannot
        duplicate anything, because results go elsewhere by definition. Otherwise the shared
        webhook serves as the feed, but only once a private one exists: with a single webhook
        it already receives everything, so announcing as well would deliver each posting twice.
        """
        return self.discord_webhook_feeds.get(feed_name) or (
            self.discord_webhook if self.discord_webhook_private else None)

    def feed_webhooks(self) -> dict[str, str]:
        """Every public destination this user announces to, keyed by feed."""
        return {feed: url for feed in self.feeds if (url := self.feed_webhook_for(feed))}


def load_users(path: str) -> list[User]:
    raw = yaml.safe_load(Path(path).read_text()) or []
    if not raw:
        raise ValueError(f"{path}: no users defined")
    try:
        users = [User.model_validate(item) for item in raw]
    except ValidationError as e:
        raise ValueError(f"{path}: {e}") from e
    ids = [u.id for u in users]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate user id(s): {', '.join(sorted(dupes))}")
    return users
