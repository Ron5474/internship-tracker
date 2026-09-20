from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from config import FEEDS


class User(BaseModel):
    id: str
    cv: str
    discord_webhook: str
    feeds: list[str] = Field(min_length=1)
    sections: list[str] = Field(min_length=1)
    threshold: int = 60

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
