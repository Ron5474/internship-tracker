SCORE_SYSTEM = """\
You evaluate how well a candidate's CV demonstrates fit for one job posting.

Score the DEMONSTRATED fit only: what the CV shows versus what the posting asks for.
A requirement the CV does not address either way (work authorization, graduation year,
security clearance, a technology never mentioned) goes in "missing_unknown" and does not
lower the score — the candidate judges those, you cannot.

Rubric (use the whole range; be consistent across postings):
- 90–100: demonstrates every stated hard requirement and most preferred ones
- 70–89: demonstrates the hard requirements; some preferred ones not shown
- 50–69: one hard requirement confirmed missing, otherwise a fit
- below 50: multiple hard requirements confirmed missing, or the role is a different discipline

"Hard requirements" are what the posting says is required / must-have / minimum.
"Confirmed missing" means the CV shows the candidate lacks it (e.g. the posting requires 5+
years of professional experience and the CV shows internships only), not merely that the CV
is silent on it.

If the text is not a job posting (an error page, login wall, cookie banner, navigation only,
or a list of unrelated jobs), set `posting_usable` to false, `score` to 0 and leave the lists empty.

Reply with ONLY a JSON object, no prose, no code fences:
{
  "score": <integer 0-100>,
  "reasoning": "<two or three sentences on the strongest evidence for and against>",
  "missing_confirmed": ["<requirement the CV clearly does not meet>", ...],
  "missing_unknown": ["<requirement the CV does not mention either way>", ...],
  "posting_usable": <true|false>
}
Keep each list item under 12 words. Empty lists are fine.
"""

def score_user_message(description: str, cv_text: str) -> str:
    return f"JOB POSTING:\n{description}\n\n---\n\nCANDIDATE CV:\n{cv_text}"


def reask_message(problem: str) -> str:
    return (f"That reply was not a valid JSON object matching the schema ({problem}). "
            "Reply again with ONLY the JSON object — no prose, no code fences.")
