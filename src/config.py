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
    llm_tailor_model: str
    max_bullets_per_entry: int
    llm_timeout: int


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required (set it in .env)")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """A cap of zero is not "no cap": it would disable validate_selection's stopping condition
    while still emptying the fallback slice. Refuse it at startup, not at the first tailor call."""
    value = int(env.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1 (got {value})")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    return Settings(
        data_dir=env.get("DATA_DIR", "/data"),
        poll_interval=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        github_token=env.get("GITHUB_TOKEN") or None,
        llm_base_url=_required(env, "LLM_BASE_URL").rstrip("/"),
        llm_api_key=env.get("LLM_API_KEY") or None,
        llm_score_model=_required(env, "LLM_SCORE_MODEL"),
        llm_tailor_model=_required(env, "LLM_TAILOR_MODEL"),
        max_bullets_per_entry=_positive_int(env, "MAX_BULLETS_PER_ENTRY", 4),
        llm_timeout=int(env.get("LLM_TIMEOUT_SECONDS", "120")),
    )
