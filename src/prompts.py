SCORE_SYSTEM = """\
You evaluate how well a candidate's CV demonstrates fit for one job posting.

FIRST decide whether this is a technology role. The candidate wants software engineering,
data, AI/ML, and cloud/infrastructure work — roles whose day-to-day output is code, models,
data pipelines, or the systems that run them. Analysing data with SQL, Python or notebooks
counts. So does an analytics or research role whose work is genuinely technical.

These are NOT technology roles, no matter how analytical they sound and no matter how well
the CV matches what they ask for: records and information management, market or survey
research, policy, compliance or governance work, business, marketing or operations analytics
that does not involve building or querying anything, program and project coordination, and
general business internships. A vague rotational posting ("College Intern", "Summer Intern")
is a technology role only if the posting itself describes technical work.

If it is not a technology role, set `score` to at most 40, name the discipline it actually
belongs to in the reasoning, and stop there. Transferable skills do not raise that score:
the candidate is not looking for this job, so a strong match against its requirements is
still a bad recommendation.

Otherwise, score the DEMONSTRATED fit: what the CV shows versus what the posting asks for.
A requirement the CV does not address either way (work authorization, graduation year,
security clearance, a technology never mentioned) goes in "missing_unknown" and does not
lower the score — the candidate judges those, you cannot.

Rubric (use the whole range; be consistent across postings):
- 90–100: demonstrates every stated hard requirement and most preferred ones
- 70–89: demonstrates the hard requirements; some preferred ones not shown
- 50–69: one hard requirement confirmed missing, otherwise a fit
- below 50: multiple hard requirements confirmed missing
- at most 40: not a technology role (see above) — this ceiling wins over everything else

"Hard requirements" are what the posting says is required / must-have / minimum.
"Confirmed missing" means the CV shows the candidate lacks it (e.g. the posting requires 5+
years of professional experience and the CV shows internships only), not merely that the CV
is silent on it.

Location: if the posting is on-site/hybrid in a place the candidate's stated location does not
match, list it under `missing_unknown` (relocation is the candidate's call), never under
`missing_confirmed`; remote postings have no location requirement.

If the posting lists no hard requirements at all, score on the preferred/desired ones and say
so in the reasoning.

If the text is not a job posting (an error page, login wall, cookie banner, navigation only,
or a list of unrelated jobs), set `posting_usable` to false, `score` to 0 and leave the lists empty.

Reply with ONLY a JSON object, no prose, no code fences:
{
  "score": <integer 0-100>,
  "reasoning": "<two or three sentences, under 400 characters, on the strongest evidence for and against>",
  "missing_confirmed": ["<requirement the CV clearly does not meet>", ...],
  "missing_unknown": ["<requirement the CV does not mention either way>", ...],
  "posting_usable": <true|false>
}
Keep each list item under 12 words. Empty lists are fine.
"""


def score_user_message(description: str, cv_text: str) -> str:
    return f"JOB POSTING:\n{description}\n\n---\n\nCANDIDATE CV:\n{cv_text}\n\nReturn the JSON object now."


def reask_message(problem: str) -> str:
    return (f"That reply was not a valid JSON object matching the schema ({problem}). "
            "Reply again with ONLY the JSON object — no prose, no code fences.")


TAILOR_SYSTEM = """\
You choose which of a candidate's existing CV items belong on a one-page resume for one job posting.

You do not write, rewrite, rephrase, summarise or invent anything. You only return IDs that appear
verbatim in the CV you are given. Any text you produce instead of an ID is a failed reply.

How to choose:
- Pick the experience entries and project entries whose evidence best matches this posting, most
  relevant first. The first ones you list are the ones that make the page.
- Inside each entry, list only the bullet IDs worth keeping, most relevant first.
- A bullet ID must be listed under the entry it belongs to. Bullet IDs start with their entry's ID.
- Skills: keep every skill that is plausibly relevant, under the same group names the CV uses,
  most relevant first. Aim for 15-25. They cost about one line per ten, and a four-item skills
  list reads as a mistake rather than as focus. Do not add a skill the CV does not list.
- Include every experience entry unless it is clearly irrelevant to this posting. A resume
  showing one job looks thin, and the candidate may only have two.
- Education and the summary are always on the resume. Do not select them.

Reply with ONLY a JSON object, no prose, no code fences:
{
  "experience": [{"id": "<entry id>", "bullets": ["<bullet id>", ...]}, ...],
  "projects":   [{"id": "<entry id>", "bullets": ["<bullet id>", ...]}, ...],
  "skills": {"<group name>": ["<skill>", ...], ...}
}
Every value in "bullets" is a string ID. Empty lists are allowed.
"""


def tailor_user_message(description: str, cv_id_text: str, max_bullets: int) -> str:
    return (
        f"JOB POSTING:\n{description}\n\n---\n\nCANDIDATE CV (IDs in brackets):\n{cv_id_text}\n\n"
        f"Select at most {max_bullets} bullets per entry. Return the JSON object now."
    )
