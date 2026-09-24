"""The render libraries must not drown out the pipeline's own log lines.

Rendering one resume emitted roughly a hundred INFO lines of per-glyph subsetting detail
into the deployed logs — enough that `docker compose logs --tail 50` showed no pipeline
activity at all, only font internals.
"""

import logging

import main  # noqa: F401 — importing it is what configures logging


def test_render_libraries_are_quiet_at_info():
    for name in ("weasyprint", "weasyprint.progress", "fontTools", "fontTools.subset"):
        logger = logging.getLogger(name)
        assert not logger.isEnabledFor(logging.INFO), f"{name} still logs at INFO"


def test_render_libraries_still_report_warnings():
    # Silencing the chatter must not hide a genuine rendering problem.
    for name in ("weasyprint", "fontTools"):
        assert logging.getLogger(name).isEnabledFor(logging.WARNING), name


def test_the_pipelines_own_loggers_are_left_alone():
    # They must inherit the root level rather than carry one of their own — otherwise
    # quietening the render libraries would quietly change the pipeline's verbosity too.
    # (Asserting an effective level here would only measure pytest's root handler.)
    for name in ("main", "worker", "poller"):
        assert logging.getLogger(name).level == logging.NOTSET, name
