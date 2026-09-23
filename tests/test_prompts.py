"""Guards on the load-bearing parts of the scoring prompt.

These assert on prompt text, which is unusual, but the discipline gate is not decoration:
without it the model scored "Records and Information Management" at 82 and "Research Business
Survey Intern" at 62 against a software CV, because on their stated requirements those really
are matches. A careless edit that drops the gate produces no test failure anywhere else —
the damage only shows up days later as noise in Discord.
"""

from prompts import SCORE_SYSTEM, reask_message, score_user_message


def test_score_prompt_asks_for_the_discipline_decision_first():
    head = SCORE_SYSTEM[: SCORE_SYSTEM.index("Rubric")]
    assert "FIRST decide whether this is a technology role" in head
    # The gate has to come before the fit scoring, or the model scores first and gates never.
    assert head.index("technology role") < head.index("DEMONSTRATED fit")


def test_score_prompt_caps_non_technology_roles():
    assert "at most 40" in SCORE_SYSTEM
    assert "Transferable skills do not raise that score" in SCORE_SYSTEM
    # The ceiling must outrank the band table, which would otherwise score these in the 80s.
    assert "this ceiling wins over everything else" in SCORE_SYSTEM


def test_score_prompt_names_the_disciplines_that_misfired():
    # Each of these produced a false match against this CV before the gate existed.
    for discipline in ("records and information management", "survey", "governance", "coordination"):
        assert discipline in SCORE_SYSTEM.lower(), discipline


def test_score_prompt_keeps_the_json_contract():
    for key in ("score", "reasoning", "missing_confirmed", "missing_unknown", "posting_usable"):
        assert f'"{key}"' in SCORE_SYSTEM


def test_score_user_message_carries_both_inputs_and_closes_with_the_instruction():
    msg = score_user_message("JD TEXT", "CV TEXT")
    assert "JD TEXT" in msg and "CV TEXT" in msg
    assert msg.rstrip().endswith("Return the JSON object now.")


def test_reask_names_the_problem():
    assert "bad key" in reask_message("bad key")
