import requests

GITHUB_API = "https://api.github.com"


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


def get_readme_patch(repo: str, base_sha: str, head_sha: str, token: str | None = None) -> str | None:
    url = f"{GITHUB_API}/repos/{repo}/compare/{base_sha}...{head_sha}"
    resp = requests.get(url, headers=_headers(token), timeout=10)
    resp.raise_for_status()
    for f in resp.json().get("files", []):
        if f["filename"] == "README.md":
            return f.get("patch")
    return None
