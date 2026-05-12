from unittest.mock import patch, Mock

from src.github_client import get_latest_sha, get_readme_patch


def _make_response(json_data, status_code=200):
    mock = Mock()
    mock.json.return_value = json_data
    mock.status_code = status_code
    mock.raise_for_status.return_value = None
    return mock


def test_get_latest_sha_returns_first_commit_sha():
    resp = _make_response([{"sha": "abc123"}])
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        sha = get_latest_sha("owner/repo", "main")
        assert sha == "abc123"
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["sha"] == "main"
        assert call_kwargs[1]["params"]["per_page"] == 1


def test_get_latest_sha_sends_auth_header_when_token_provided():
    resp = _make_response([{"sha": "def456"}])
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_latest_sha("owner/repo", "main", token="mytoken")
        headers = mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"


def test_get_latest_sha_omits_auth_header_when_no_token():
    resp = _make_response([{"sha": "def456"}])
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_latest_sha("owner/repo", "main")
        headers = mock_get.call_args[1]["headers"]
        assert "Authorization" not in headers


def test_get_readme_patch_returns_patch_for_readme():
    resp = _make_response({
        "files": [
            {"filename": "README.md", "patch": "+| new row |"},
            {"filename": "other.py", "patch": "+some code"},
        ]
    })
    with patch("src.github_client.requests.get", return_value=resp):
        result = get_readme_patch("owner/repo", "abc", "def")
        assert result == "+| new row |"


def test_get_readme_patch_returns_none_when_readme_not_changed():
    resp = _make_response({"files": [{"filename": "other.py", "patch": "+code"}]})
    with patch("src.github_client.requests.get", return_value=resp):
        result = get_readme_patch("owner/repo", "abc", "def")
        assert result is None


def test_get_readme_patch_returns_none_when_readme_has_no_patch():
    resp = _make_response({"files": [{"filename": "README.md"}]})
    with patch("src.github_client.requests.get", return_value=resp):
        result = get_readme_patch("owner/repo", "abc", "def")
        assert result is None


def test_get_readme_patch_builds_correct_compare_url():
    resp = _make_response({"files": []})
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_readme_patch("SimplifyJobs/Summer2026-Internships", "abc123", "def456")
        url = mock_get.call_args[0][0]
        assert "SimplifyJobs/Summer2026-Internships" in url
        assert "abc123...def456" in url
