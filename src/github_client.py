import requests

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"


def _headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_latest_sha(repo: str, branch: str, token: str | None = None) -> str:
    url = f"{GITHUB_API}/repos/{repo}/commits"
    resp = requests.get(url, params={"sha": branch, "per_page": 1}, headers=_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json()[0]["sha"]


def get_readme_content(repo: str, sha: str, token: str | None = None) -> str | None:
    url = f"{GITHUB_RAW}/{repo}/{sha}/README.md"
    resp = requests.get(url, headers=_headers(token), timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text
