from unittest.mock import patch, Mock

from src.discord_client import format_message, send_notification


SAMPLE_POSTING = {
    "company": "Stripe",
    "role": "Software Engineer Intern",
    "location": "San Francisco, CA",
    "url": "https://simplify.jobs/p/abc123",
}


def test_format_message_includes_company_bolded():
    msg = format_message(SAMPLE_POSTING)
    assert "**Stripe**" in msg


def test_format_message_includes_role():
    msg = format_message(SAMPLE_POSTING)
    assert "Software Engineer Intern" in msg


def test_format_message_includes_location():
    msg = format_message(SAMPLE_POSTING)
    assert "San Francisco, CA" in msg


def test_format_message_includes_url():
    msg = format_message(SAMPLE_POSTING)
    assert "https://simplify.jobs/p/abc123" in msg


def test_format_message_omits_url_line_when_url_empty():
    posting = {**SAMPLE_POSTING, "url": ""}
    msg = format_message(posting)
    assert "🔗" not in msg


def test_send_notification_returns_true_on_204():
    mock_resp = Mock()
    mock_resp.status_code = 204
    with patch("src.discord_client.requests.post", return_value=mock_resp):
        result = send_notification("https://discord.com/api/webhooks/123/abc", "hello")
        assert result is True


def test_send_notification_returns_true_on_200():
    mock_resp = Mock()
    mock_resp.status_code = 200
    with patch("src.discord_client.requests.post", return_value=mock_resp):
        result = send_notification("https://discord.com/api/webhooks/123/abc", "hello")
        assert result is True


def test_send_notification_returns_false_on_400():
    mock_resp = Mock()
    mock_resp.status_code = 400
    with patch("src.discord_client.requests.post", return_value=mock_resp):
        result = send_notification("https://discord.com/api/webhooks/123/abc", "hello")
        assert result is False


def test_send_notification_posts_to_correct_url():
    mock_resp = Mock()
    mock_resp.status_code = 204
    with patch("src.discord_client.requests.post", return_value=mock_resp) as mock_post:
        send_notification("https://discord.com/api/webhooks/123/abc", "test message")
        url = mock_post.call_args[0][0]
        assert url == "https://discord.com/api/webhooks/123/abc"


def test_send_notification_sends_content_in_json():
    mock_resp = Mock()
    mock_resp.status_code = 204
    with patch("src.discord_client.requests.post", return_value=mock_resp) as mock_post:
        send_notification("https://discord.com/api/webhooks/123/abc", "test message")
        json_body = mock_post.call_args[1]["json"]
        assert json_body["content"] == "test message"
