import tempfile

from src.migration import STATE_VERSION, migrate_state
from src.state import read_known_urls, read_last_sha, read_state_version, write_known_urls, write_last_sha

# README as it existed at the saved SHA: one job whose identity lives in ?gh_jid=,
# plus a ↳ continuation row — both were mis-keyed or dropped by the old parser.
HISTORICAL_README = """\
## 💻 Software Engineering Internship Roles
<table><tbody>
<tr>
<td><strong>Stripe</strong></td><td>SWE Intern</td><td>SF</td>
<td><div align="center"><a href="https://stripe.com/jobs/search?gh_jid=111&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td>
</tr>
<tr>
<td>↳</td><td>Backend Intern</td><td>NY</td>
<td><div align="center"><a href="https://stripe.com/jobs/search?gh_jid=112&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td>
</tr>
</tbody></table>
"""


def _legacy_state(tmpdir):
    # What the pre-fix tracker wrote: query-stripped key, continuation row never recorded.
    write_last_sha(tmpdir, "oldsha")
    write_known_urls(tmpdir, {"https://stripe.com/jobs/search"})


def test_migrate_rebuilds_known_urls_from_saved_sha():
    with tempfile.TemporaryDirectory() as tmpdir:
        _legacy_state(tmpdir)
        fetched = []

        def fetch(sha):
            fetched.append(sha)
            return HISTORICAL_README

        assert migrate_state(tmpdir, fetch) is True
        assert fetched == ["oldsha"]
        assert read_known_urls(tmpdir) == {
            "https://stripe.com/jobs/search?gh_jid=111",
            "https://stripe.com/jobs/search?gh_jid=112",
        }
        assert read_last_sha(tmpdir) == "oldsha"
        assert read_state_version(tmpdir) == STATE_VERSION


def test_migrate_leaves_state_intact_when_fetch_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        _legacy_state(tmpdir)
        assert migrate_state(tmpdir, lambda sha: None) is False
        assert read_known_urls(tmpdir) == {"https://stripe.com/jobs/search"}
        assert read_last_sha(tmpdir) == "oldsha"
        assert read_state_version(tmpdir) == 0


def test_migrate_leaves_state_intact_when_fetch_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        _legacy_state(tmpdir)

        def fetch(sha):
            raise ConnectionError("boom")

        assert migrate_state(tmpdir, fetch) is False
        assert read_state_version(tmpdir) == 0


def test_migrate_fresh_install_just_stamps_version():
    with tempfile.TemporaryDirectory() as tmpdir:
        called = []
        assert migrate_state(tmpdir, lambda sha: called.append(sha)) is True
        assert called == []
        assert read_state_version(tmpdir) == STATE_VERSION
        assert read_last_sha(tmpdir) is None


def test_migrate_is_noop_when_already_current():
    with tempfile.TemporaryDirectory() as tmpdir:
        _legacy_state(tmpdir)
        from src.state import write_state_version
        write_state_version(tmpdir, STATE_VERSION)
        called = []
        assert migrate_state(tmpdir, lambda sha: called.append(sha)) is True
        assert called == []
        assert read_known_urls(tmpdir) == {"https://stripe.com/jobs/search"}
