"""
Choosing what the AI is allowed to read.

The problem
-----------
A prospectus runs 500 to 600 pages. Kanohar's litigation chapter alone is 88.
No free AI tier will take that, and even if one would, burying the important
sentence in 300,000 words makes it less likely to be found, not more.

So we choose. For each question we want answered, we score every paragraph in
the relevant chapters and keep the best few — with the page number attached, so
every sentence the model later quotes can be traced back to a page you can open.

Why keyword scoring rather than embeddings
------------------------------------------
Because we already know where the answer lives. "Is there serious litigation?"
is answered in the litigation chapter, by paragraphs containing words like
"criminal", "penalty" and "SEBI". That is a search with a known address, and a
list of words does it as well as a neural model would — with no API key, no
quota, and no downloads. Embeddings earn their place when the question is open
("what could go wrong with this business?"), and that is where we will add them.

A paragraph scores on three things: how many of the question's words it carries,
whether it carries the rarer ones, and whether it looks like substance rather
than boilerplate. Documents like these are full of sentences that mention
"criminal proceedings" only to say there are none.
"""

import re

# Questions we always ask, with the words that mark a relevant paragraph.
# Weight 3 = strongly indicative, 1 = supporting.
TOPICS = {
    "litigation": {
        "chapters": ["litigation"],
        "terms": {"criminal": 3, "sebi": 3, "penalty": 3, "fraud": 3,
                  "prosecution": 3, "conviction": 3, "show cause": 3,
                  "tax demand": 2, "arbitration": 2, "material": 2,
                  "aggregating to": 2, "pending": 1, "notice": 1,
                  "filed against": 3, "proceedings against": 3,
                  "first information report": 3, "complaint against": 2,
                  "order dated": 2, "appeal": 1,
                  "against our promoter": 3, "against our company": 2},
    },
    "governance": {
        "chapters": ["promoters", "capital_structure", "related_party"],
        "terms": {"related party": 3, "promoter group": 2, "pledged": 3,
                  "conflict of interest": 3, "remuneration": 2,
                  "loans to": 2, "guarantee": 2, "resignation": 2,
                  "independent director": 1, "shareholding": 1,
                  "transactions with": 2},
    },
    "business_quality": {
        "chapters": ["business"],
        "terms": {"customer concentration": 3, "top ten customers": 3,
                  "single customer": 3, "order book": 2, "capacity utilisation": 2,
                  "dependent on": 2, "competition": 2, "raw material": 2,
                  "long-term contract": 2, "revenue from operations": 1},
    },
    "risks": {
        "chapters": ["risk_factors", "business"],
        "terms": {"we may not": 2, "adversely affect": 2, "our inability": 2,
                  "we have incurred": 3, "negative cash flow": 3,
                  "we do not own": 2, "no long-term": 2, "depend on": 2},
    },
}

# Sentences that exist only to say nothing is wrong. Keeping them wastes the
# budget and, worse, gives a model reassuring text to latch onto.
BOILERPLATE = re.compile(
    r"there (?:are|is) no (?:outstanding|pending|material)|"
    r"not applicable|nil\b|no such (?:cases|proceedings|litigation)|"
    r"has been considered .{0,20}material|for the purpose of this (?:section|chapter)|"
    r"in accordance with the sebi icdr regulations",
    re.I)


def paragraphs(text: str, start_page=None, end_page=None):
    """
    Split a chapter into paragraphs, keeping a page estimate for each.

    Prospectus text carries the page number on its own line, so we watch for a
    bare number and use it to keep count. Where that fails we fall back to the
    chapter's first page, which is honest: an approximate citation you can find
    beats a precise one that is wrong.
    """
    page = start_page
    out = []
    # Walk the text line by line so a page number printed on its own line is
    # seen even when no blank line separates it from the paragraph beneath.
    blocks, current = [], []
    for line in (text or "").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        alone = re.fullmatch(r"(\d{1,4})", stripped)
        if alone:
            candidate = int(alone.group(1))
            inside = (start_page is None or candidate >= start_page - 2) and \
                     (end_page is None or candidate <= end_page + 2)
            if inside:
                if current:
                    blocks.append((" ".join(current), page))
                    current = []
                page = candidate
                continue
        if not stripped:
            if current:
                blocks.append((" ".join(current), page))
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append((" ".join(current), page))

    for block, block_page in blocks:
        block = " ".join(block.split())
        if not block:
            continue
        # A bare number on its own line is the printed page number. Anything
        # else that looks like a number is a year, an amount or a clause number
        # — an earlier version read "2025" as page 2,025, and a citation that
        # points nowhere is worse than no citation at all.
        out.append({"text": block, "page": block_page})
    return out


def score(paragraph: str, terms: dict) -> float:
    """How much this paragraph looks like an answer to the question."""
    lowered = paragraph.lower()
    points = sum(weight for term, weight in terms.items() if term in lowered)
    if not points:
        return 0.0
    if BOILERPLATE.search(lowered):
        points *= 0.3
    if len(paragraph) < 120:
        points *= 0.5                     # a heading or a stub, not an answer
    if re.search(r"[\d,]{4,}", paragraph):
        points += 1                       # carries an actual amount
    return points


def gather(sections: dict, topic: str, budget_chars=6000, most=8) -> list:
    """
    The best passages for one topic, newest scoring first, within a budget.

    The budget is a hard limit on how much text leaves this machine. Free AI
    tiers are the constraint, but so is honesty: a shorter, better-chosen brief
    produces answers we can check.
    """
    settings = TOPICS.get(topic)
    if not settings:
        return []

    candidates = []
    for name in settings["chapters"]:
        body = (sections or {}).get(name)
        if not body or not body.get("text"):
            continue
        for item in paragraphs(body["text"], body.get("start_page"),
                                   body.get("end_page")):
            value = score(item["text"], settings["terms"])
            if value > 0:
                candidates.append({"chapter": name, "page": item["page"],
                                   "score": round(value, 1),
                                   "text": item["text"][:1200]})

    candidates.sort(key=lambda item: item["score"], reverse=True)

    chosen, used = [], 0
    for item in candidates:
        if len(chosen) >= most or used + len(item["text"]) > budget_chars:
            continue
        chosen.append(item)
        used += len(item["text"])
    return chosen


def brief(sections: dict, topics=None, budget_chars=14000) -> dict:
    """
    Everything the analyst is allowed to read, and where each piece came from.

    Split evenly across topics so one long litigation chapter cannot crowd out
    the governance question entirely.
    """
    topics = topics or list(TOPICS)
    share = max(2000, budget_chars // max(1, len(topics)))
    out = {}
    for topic in topics:
        passages = gather(sections, topic, budget_chars=share)
        if passages:
            out[topic] = passages
    return out


def as_text(passages: dict) -> str:
    """The brief as the model will see it, with citations attached."""
    lines = []
    for topic, items in passages.items():
        lines.append(f"\n### {topic.replace('_', ' ').upper()}")
        for item in items:
            lines.append(f"[{item['chapter']}, page {item['page']}] {item['text']}")
    return "\n".join(lines)
