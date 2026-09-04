"""
Finding the parts of a prospectus that matter.

Indian prospectuses follow a structure SEBI mandates, so the big headings are
near-identical from one company to the next. That is why we can locate sections
by name rather than needing to search the text semantically — the answer's
address is known, so no clever retrieval is required. (Retrieval earns its
place later, inside the long prose sections where the answer could be anywhere.)

Every section records the page it started on, so anything quoted from it can
be traced back.
"""

import re

# Heading -> the wordings we have seen for it. Longest match wins.
SECTION_PATTERNS = {
    "summary": [
        "summary of the offer document",
        "summary of offer document",
    ],
    "risk_factors": [
        "risk factors",
    ],
    "business": [
        "our business",
        "business overview",
    ],
    "objects": [
        "objects of the offer",
        "objects of the issue",
    ],
    "basis_for_price": [           # this is where the peer comparison lives
        "basis for the offer price",
        "basis for offer price",
        "basis for issue price",
    ],
    "capital_structure": [
        "capital structure",
    ],
    "promoters": [
        "our promoters and promoter group",
        "our promoter and promoter group",
    ],
    "financials": [
        "restated financial statements",
        "financial information",
        "financial statements",
    ],
    "related_party": [
        "related party transactions",
    ],
    "litigation": [
        "outstanding litigation and material developments",
        "outstanding litigations and material developments",
        "outstanding litigation",
    ],
    "management_discussion": [
        "management's discussion and analysis",
        "managements discussion and analysis",
    ],
}


def _headingish(line: str) -> bool:
    """
    Does this line look like a heading rather than a mention in a sentence?

    Headings in these documents are short and usually shouting in capitals.
    Without this test, the phrase 'risk factors' inside a paragraph on page 3
    would be mistaken for the start of the risk factors section.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    capital_share = sum(1 for c in letters if c.isupper()) / len(letters)
    return capital_share > 0.7


def find_headings(pages: list) -> list:
    """
    Locate every section heading. Returns [{section, page, line}] in page order.

    A heading that appears in the contents list at the front will also match, so
    we keep them all and let the caller take the LAST occurrence — the real
    section is always further into the document than its table-of-contents entry.
    """
    hits = []
    for page_number, text in enumerate(pages, start=1):
        for line in (text or "").splitlines():
            if not _headingish(line):
                continue
            cleaned = re.sub(r"[^a-z\s]", " ", line.lower())
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            for name, wordings in SECTION_PATTERNS.items():
                if any(cleaned.startswith(w) or cleaned == w for w in wordings):
                    hits.append({"section": name, "page": page_number,
                                 "line": line.strip()})
                    break
    return hits


def split(pages: list) -> dict:
    """
    Cut the document into its named sections.

    Returns {section_name: {start_page, end_page, pages, text, chars}}.
    Sections we cannot find are simply absent — we report what is missing
    rather than inventing a guess at where it was.
    """
    hits = find_headings(pages)
    if not hits:
        return {}

    # Take the last occurrence of each heading: earlier ones are contents entries.
    starts = {}
    for hit in hits:
        starts[hit["section"]] = hit["page"]

    ordered = sorted(starts.items(), key=lambda kv: kv[1])
    result = {}

    for index, (name, start) in enumerate(ordered):
        end = ordered[index + 1][1] - 1 if index + 1 < len(ordered) else len(pages)
        end = max(end, start)
        body = "\n".join(pages[start - 1:end])
        result[name] = {
            "start_page": start,
            "end_page": end,
            "pages": end - start + 1,
            "chars": len(body),
            "text": body,
        }
    return result


def summarise(found: dict) -> str:
    """A one-line report of what we got, for the run log."""
    if not found:
        return "no sections identified"
    parts = [f"{name}(p{v['start_page']}-{v['end_page']})"
             for name, v in sorted(found.items(), key=lambda kv: kv[1]["start_page"])]
    return " ".join(parts)


def describe(pages: list, max_lines: int = 40) -> dict:
    """
    What does this document actually look like?

    Used when section-finding comes up empty. Rather than guessing again at
    what the headings might be called, this reports the lines that LOOK like
    headings, so the real structure can be read off a run log and the patterns
    written from fact.
    """
    # Sample from EVERY page, not just the first. The last attempt showed
    # eighteen lines that all came from page one, which told us nothing about
    # how the rest of the document is arranged.
    per_page = max(2, max_lines // max(1, len(pages)))
    candidates = []
    for page_number, text in enumerate(pages, start=1):
        found_here = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if 3 < len(stripped) <= 90 and _headingish(stripped):
                found_here.append(f"p{page_number}: {stripped}")
        candidates.extend(found_here[:per_page])

    first_page = (pages[0] if pages else "") or ""
    return {
        "total_pages": len(pages),
        "total_chars": sum(len(p or "") for p in pages),
        "heading_candidates": candidates[:max_lines],
        "heading_count": len(candidates),
        "first_page_start": first_page[:400].replace("\n", " | "),
    }
