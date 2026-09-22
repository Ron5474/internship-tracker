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
    llm_base_url: str
    llm_api_key: str | None
    llm_score_model: str
    llm_timeout: int


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required (set it in .env)")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    return Settings(
        data_dir=env.get("DATA_DIR", "/data"),
        poll_interval=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        github_token=env.get("GITHUB_TOKEN") or None,
        llm_base_url=_required(env, "LLM_BASE_URL").rstrip("/"),
        llm_api_key=env.get("LLM_API_KEY") or None,
        llm_score_model=_required(env, "LLM_SCORE_MODEL"),
        llm_timeout=int(env.get("LLM_TIMEOUT_SECONDS", "120")),
    )
