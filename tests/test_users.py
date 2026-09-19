import tempfile
from pathlib import Path

import pytest

from users import User, load_users

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
