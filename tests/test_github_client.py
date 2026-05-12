from unittest.mock import Mock, patch

from src.github_client import get_latest_sha, get_readme_content


def _make_response(json_data=None, status_code=200, text=""):
    mock = Mock()
    mock.json.return_value = json_data
    mock.status_code = status_code
    mock.text = text
    mock.raise_for_status.return_value = None
    return mock


def test_get_latest_sha_returns_first_commit_sha():
    resp = _make_response(json_data=[{"sha": "abc123"}])
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        sha = get_latest_sha("owner/repo", "main")
        assert sha == "abc123"
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["sha"] == "main"
        assert call_kwargs[1]["params"]["per_page"] == 1


def test_get_latest_sha_sends_auth_header_when_token_provided():
    resp = _make_response(json_data=[{"sha": "def456"}])
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_latest_sha("owner/repo", "main", token="mytoken")
        headers = mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"


def test_get_latest_sha_omits_auth_header_when_no_token():
    resp = _make_response(json_data=[{"sha": "def456"}])
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_latest_sha("owner/repo", "main")
        headers = mock_get.call_args[1]["headers"]
        assert "Authorization" not in headers


def test_get_readme_content_returns_text():
    resp = _make_response(status_code=200, text="# README content")
    with patch("src.github_client.requests.get", return_value=resp):
        result = get_readme_content("owner/repo", "abc123")
        assert result == "# README content"


def test_get_readme_content_returns_none_on_404():
    resp = _make_response(status_code=404)
    with patch("src.github_client.requests.get", return_value=resp):
        result = get_readme_content("owner/repo", "abc123")
        assert result is None


def test_get_readme_content_sends_token_header():
    resp = _make_response(status_code=200, text="content")
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_readme_content("owner/repo", "abc123", token="mytoken")
        headers = mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"


def test_get_readme_content_builds_raw_url():
    resp = _make_response(status_code=200, text="")
    with patch("src.github_client.requests.get", return_value=resp) as mock_get:
        get_readme_content("SimplifyJobs/Summer2026-Internships", "deadbeef")
        url = mock_get.call_args[0][0]
        assert "raw.githubusercontent.com" in url
        assert "SimplifyJobs/Summer2026-Internships" in url
        assert "deadbeef" in url
