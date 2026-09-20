from types import SimpleNamespace

import pytest

import main


class _StopLoop(BaseException):
    """Sentinel used to break out of _poll_loop after the desired number of iterations.

    Subclasses BaseException (not Exception) so it is NOT swallowed by the
    `except Exception` guard inside _poll_loop that this test is verifying.
    """


def test_poll_loop_survives_exception_and_continues(monkeypatch):
    calls = []

    def fake_poll_all(session_factory, users, settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        # Second call: stop the loop cleanly once we've proven it continued.
        raise _StopLoop()

    def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main, "poll_all", fake_poll_all)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)

    settings = SimpleNamespace(poll_interval=0)

    with pytest.raises(_StopLoop):
        main._poll_loop(session_factory=None, users=[], settings=settings)

    assert len(calls) == 2
