import requests


def format_message(posting: dict) -> str:
    lines = [
        f"🆕 **{posting['company']}** — {posting['role']}",
        f"📍 {posting['location']}",
    ]
    if posting.get("url"):
        lines.append(f"🔗 {posting['url']}")
    return "\n".join(lines)


def send_notification(webhook_url: str, message: str) -> bool:
    resp = requests.post(webhook_url, json={"content": message}, timeout=10)
    return resp.status_code in (200, 204)
