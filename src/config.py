from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedSpec:
    name: str
    repo: str
    branch: str


FEEDS: dict[str, FeedSpec] = {
    "internships": FeedSpec("internships", "SimplifyJobs/Summer2026-Internships", "dev"),
    "new-grad": FeedSpec("new-grad", "SimplifyJobs/New-Grad-Positions", "dev"),
}


@dataclass(frozen=True)
class Settings:
    data_dir: str
    poll_interval: int
    github_token: str | None


def load_settings(env: Mapping[str, str]) -> Settings:
    return Settings(
        data_dir=env.get("DATA_DIR", "/data"),
        poll_interval=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        github_token=env.get("GITHUB_TOKEN") or None,
    )
