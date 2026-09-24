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

    @model_validator(mode="after")
    def _private_differs_from_public(self) -> "User":
        if self.discord_webhook_private is not None and self.discord_webhook_private == self.discord_webhook:
            # Same channel would then get both the public feed post and the full scored message.
            raise ValueError("discord_webhook_private must differ from discord_webhook")
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

    @property
    def feed_webhook(self) -> str | None:
        """The link-only public feed, and only when the destinations are actually split:
        with one webhook it already receives everything, so a feed post would duplicate it."""
        return self.discord_webhook if self.discord_webhook_private else None


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
