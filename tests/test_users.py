import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from users import User, load_users, webhook_key

VALID = """\
- id: ron
  cv: /data/cvs/ron.yaml
  discord_webhook: https://discord.com/api/webhooks/1/a
  feeds: [internships, new-grad]
  sections: [Software Engineering, data science]
  threshold: 60
- id: cousin
  cv: /data/cvs/cousin.yaml
  discord_webhook: https://discord.com/api/webhooks/2/b
  feeds: [new-grad]
  sections: [software engineering]
"""


def _write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "users.yaml"
    p.write_text(text)
    return str(p)


def test_load_users_parses_both_users():
    users = load_users(_write(VALID))
    assert [u.id for u in users] == ["ron", "cousin"]


def test_sections_are_lowercased():
    users = load_users(_write(VALID))
    assert users[0].sections == ["software engineering", "data science"]


def test_threshold_defaults_to_60():
    users = load_users(_write(VALID))
    assert users[1].threshold == 60


def test_wants_matches_feed_and_section_substring():
    ron = load_users(_write(VALID))[0]
    assert ron.wants("internships", "software engineering internship roles")
    assert ron.wants("new-grad", "data science, ai & machine learning new grad roles")


def test_wants_rejects_unsubscribed_feed():
    cousin = load_users(_write(VALID))[1]
    assert not cousin.wants("internships", "software engineering internship roles")


def test_wants_rejects_unsubscribed_section():
    cousin = load_users(_write(VALID))[1]
    assert not cousin.wants("new-grad", "hardware engineering new grad roles")


def test_unknown_feed_name_rejected():
    bad = VALID.replace("feeds: [new-grad]", "feeds: [phd-positions]")
    with pytest.raises(ValueError, match="phd-positions"):
        load_users(_write(bad))


def test_duplicate_user_id_rejected():
    bad = VALID.replace("id: cousin", "id: ron")
    with pytest.raises(ValueError, match="duplicate"):
        load_users(_write(bad))


def test_missing_webhook_rejected():
    bad = VALID.replace("  discord_webhook: https://discord.com/api/webhooks/2/b\n", "")
    with pytest.raises(ValueError):
        load_users(_write(bad))


def test_empty_file_rejected():
    with pytest.raises(ValueError, match="no users"):
        load_users(_write(""))


def test_notify_below_threshold_defaults_false():
    assert load_users(_write(VALID))[0].notify_below_threshold is False


def test_notify_below_threshold_parsed():
    text = VALID.replace("  threshold: 60\n", "  threshold: 60\n  notify_below_threshold: true\n")
    assert load_users(_write(text))[0].notify_below_threshold is True


def _user(**kw):
    base = dict(id="ron", cv="/x", discord_webhook="https://d/shared",
                feeds=["internships"], sections=["software"])
    return User(**{**base, **kw})


def test_without_a_private_webhook_everything_goes_to_the_one_destination():
    u = _user()
    assert u.results_webhook == "https://d/shared"
    # No feed webhook: a single-destination user would otherwise get every posting twice.
    assert u.feed_webhook_for("internships") is None


def test_with_a_private_webhook_the_shared_one_becomes_the_feed():
    u = _user(discord_webhook_private="https://d/private")
    assert u.results_webhook == "https://d/private"
    assert u.feed_webhook_for("internships") == "https://d/shared"


def test_webhook_key_is_stable_and_not_the_url():
    key = webhook_key("https://discord.com/api/webhooks/123/secret-token")
    assert key == webhook_key("https://discord.com/api/webhooks/123/secret-token")
    assert "secret-token" not in key and "discord" not in key
    assert len(key) == 16


def test_webhook_key_separates_different_destinations():
    assert webhook_key("https://d/a") != webhook_key("https://d/b")


def test_unknown_key_is_rejected_not_silently_dropped():
    # A typo'd `discord_webhook_privat:` must fail loudly, not be ignored by
    # Pydantic's default extra="ignore" and leave results routed to the public webhook.
    bad = VALID.replace("  threshold: 60\n", "  threshold: 60\n  discord_webhook_privat: https://d/typo\n")
    with pytest.raises(ValueError):
        load_users(_write(bad))


def test_empty_private_webhook_is_rejected():
    # `discord_webhook_private: ""` must not validate as a falsy-but-present value that
    # silently falls back to the public webhook via `results_webhook`.
    with pytest.raises(ValueError):
        _user(discord_webhook_private="")


def test_private_webhook_same_as_public_is_rejected():
    # Configured this way, one channel would get both the feed post and the full scored message.
    with pytest.raises(ValueError):
        _user(discord_webhook="https://d/shared", discord_webhook_private="https://d/shared")


# --- per-feed public channels -------------------------------------------------------------

INTERN_HOOK = "https://d/internships"
FULLTIME_HOOK = "https://d/fulltime"


def _split_user(**kw):
    base = dict(id="ron", cv="/x", discord_webhook="https://d/shared",
                discord_webhook_private="https://d/private",
                feeds=["internships", "new-grad"], sections=["software"])
    return User(**{**base, **kw})


def test_a_feed_with_its_own_channel_uses_it():
    u = _split_user(discord_webhook_feeds={"internships": INTERN_HOOK, "new-grad": FULLTIME_HOOK})
    assert u.feed_webhook_for("internships") == INTERN_HOOK
    assert u.feed_webhook_for("new-grad") == FULLTIME_HOOK


def test_a_feed_without_its_own_channel_falls_back_to_the_shared_one():
    # Partial configuration is legitimate: route full-time somewhere new, leave the rest alone.
    u = _split_user(discord_webhook_feeds={"new-grad": FULLTIME_HOOK})
    assert u.feed_webhook_for("new-grad") == FULLTIME_HOOK
    assert u.feed_webhook_for("internships") == "https://d/shared"


def test_per_feed_channels_work_without_a_private_webhook():
    # Naming a public channel for a feed is itself the opt-in; it cannot double-post, because
    # results go to discord_webhook and postings go somewhere else entirely.
    u = User(id="ron", cv="/x", discord_webhook="https://d/shared",
             discord_webhook_feeds={"internships": INTERN_HOOK},
             feeds=["internships", "new-grad"], sections=["software"])
    assert u.feed_webhook_for("internships") == INTERN_HOOK
    assert u.feed_webhook_for("new-grad") is None        # no private webhook, no override
    assert u.results_webhook == "https://d/shared"


def test_no_feed_channels_and_no_private_webhook_announces_nothing():
    u = User(id="ron", cv="/x", discord_webhook="https://d/shared",
             feeds=["internships"], sections=["software"])
    assert u.feed_webhook_for("internships") is None
    assert u.feed_webhooks() == {}


def test_feed_webhooks_lists_every_destination_by_feed():
    u = _split_user(discord_webhook_feeds={"internships": INTERN_HOOK})
    assert u.feed_webhooks() == {"internships": INTERN_HOOK, "new-grad": "https://d/shared"}


def test_feed_webhooks_covers_only_subscribed_feeds():
    u = _split_user(feeds=["internships"],
                    discord_webhook_feeds={"internships": INTERN_HOOK, "new-grad": FULLTIME_HOOK})
    assert list(u.feed_webhooks()) == ["internships"]


def test_an_unknown_feed_name_is_rejected():
    with pytest.raises(ValidationError):
        _split_user(discord_webhook_feeds={"podcasts": INTERN_HOOK})


def test_a_feed_channel_may_not_be_the_results_channel():
    # Otherwise that channel gets the 🆕 posting and the 🎯 scored message with the resume.
    with pytest.raises(ValidationError):
        _split_user(discord_webhook_feeds={"internships": "https://d/private"})
    with pytest.raises(ValidationError):
        User(id="ron", cv="/x", discord_webhook="https://d/shared",
             discord_webhook_feeds={"internships": "https://d/shared"},
             feeds=["internships"], sections=["software"])


def test_an_empty_feed_channel_url_is_rejected():
    with pytest.raises(ValidationError):
        _split_user(discord_webhook_feeds={"internships": ""})
