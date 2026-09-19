from unittest.mock import Mock, patch

import requests

from discord_client import (
    MAX_CONTENT,
    DeliveryResult,
    cap_content,
    format_link_only,
    send_message,
)

WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def _resp(status, body=None, headers=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else {}
    r.text = "" if body is None else str(body)
    return r


# --- format_link_only -------------------------------------------------------

def test_format_link_only_basic():
    msg = format_link_only("Stripe", "SWE Intern", "SF", "https://stripe.com/j?gh_jid=1")
    assert msg == "🆕 **Stripe** — SWE Intern\n📍 SF\n🔗 https://stripe.com/j?gh_jid=1"


def test_format_link_only_with_note():
    msg = format_link_only("Stripe", "SWE Intern", "SF", "https://x", note="couldn't read the description")
    assert msg.startswith("🆕 **Stripe** — SWE Intern (couldn't read the description)")


# --- cap_content -----------------------------------------------------------

def test_cap_content_leaves_short_message_alone():
    assert cap_content("head", ["⚠️ Gaps: a, b"], "tail") == "head\n⚠️ Gaps: a, b\ntail"


def test_cap_content_truncates_lists_before_tail():
    long_list = "⚠️ Gaps: " + ", ".join(f"req{i}" for i in range(600))
    out = cap_content("head", [long_list], "tail")
    assert len(out) <= MAX_CONTENT
    assert out.endswith("tail")
    assert "…" in out


def test_cap_content_truncates_tail_when_lists_already_minimal():
    out = cap_content("head", [], "x" * 5000)
    assert len(out) <= MAX_CONTENT
    assert out.startswith("head\n")


# --- send_message ----------------------------------------------------------

def test_send_uses_wait_true_and_succeeds_on_200_with_body():
    with patch("discord_client.requests.post", return_value=_resp(200, {"id": "1"})) as post:
        result = send_message(WEBHOOK, "hi")
    assert result.ok
    assert post.call_args.kwargs["params"] == {"wait": "true"}
    assert post.call_args.kwargs["json"] == {"content": "hi"}


def test_send_204_is_not_success():
    # wait=true should never yield 204; if it does, Discord did not confirm the message.
    with patch("discord_client.requests.post", return_value=_resp(204)):
        result = send_message(WEBHOOK, "hi")
    assert not result.ok
    assert result.kind == "transient"


def test_send_429_is_transient_with_retry_after():
    with patch("discord_client.requests.post", return_value=_resp(429, {}, {"Retry-After": "7"})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert result.retry_after == 7.0


def test_send_5xx_is_transient():
    with patch("discord_client.requests.post", return_value=_resp(503)):
        assert send_message(WEBHOOK, "hi").kind == "transient"


def test_send_connection_error_is_transient():
    with patch("discord_client.requests.post", side_effect=requests.ConnectionError("down")):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert "down" in result.error


def test_send_404_is_gone():
    with patch("discord_client.requests.post", return_value=_resp(404)):
        assert send_message(WEBHOOK, "hi").kind == "gone"


def test_send_401_is_gone():
    with patch("discord_client.requests.post", return_value=_resp(401)):
        assert send_message(WEBHOOK, "hi").kind == "gone"


def test_send_400_is_invalid():
    with patch("discord_client.requests.post", return_value=_resp(400, {"message": "bad"})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "invalid"
    assert "bad" in result.error


def test_send_with_pdf_uses_multipart(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with patch("discord_client.requests.post", return_value=_resp(200, {"id": "1"})) as post:
        result = send_message(WEBHOOK, "hi", pdf_path=str(pdf))
    assert result.ok
    kwargs = post.call_args.kwargs
    assert "json" not in kwargs
    assert kwargs["data"] == {"payload_json": '{"content": "hi"}'}
    filename, _fh, ctype = kwargs["files"]["files[0]"]
    assert filename == "r.pdf"
    assert ctype == "application/pdf"


def test_delivery_result_ok_property():
    assert DeliveryResult("ok", None, None).ok
    assert not DeliveryResult("transient", None, None).ok
