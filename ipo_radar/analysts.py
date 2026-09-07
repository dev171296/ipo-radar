"""
The two AI analysts.

What they are for
-----------------
Not arithmetic. The scorer already works out growth, margins, cash conversion,
debt and the multiple being asked, and it does so the same way every time. Ask a
language model to do that and you have turned a fact into an opinion.

These models do the part arithmetic cannot reach: reading 88 pages of litigation
and saying whether any of it matters, reading the promoter chapter and judging
the governance, and answering the specific questions our own flags raised —
"is the peer set fairly chosen?", "where did the cash go?".

Four rules, each guarding against a way this normally goes wrong
---------------------------------------------------------------
1. **No internet.** The models see the evidence bundle and passages we chose,
   and nothing else. Both models see EXACTLY the same brief, which is what makes
   their disagreement meaningful rather than an artefact of different inputs.

2. **No anchoring.** They are not shown our scores, our flags-as-conclusions, or
   each other's answers. A model shown "our fundamentals score is 80.8" returns
   something close to 80.8 — not because it agrees, but because that is what
   language models do with a number in front of them.

3. **Every claim carries a page.** Passages arrive labelled `[chapter, page N]`
   and the model is required to cite. A claim with no citation is treated as an
   opinion, not a finding, and is marked as such.

4. **Disagreement is a result.** Where the two differ by more than 15 points, we
   report the gap rather than averaging it away. Two models converging is not
   evidence of truth; two models diverging IS evidence of uncertainty.
"""

import hashlib
import json
import os
import re
from datetime import date

from . import retrieval, storage
from .http import FetchError, plain_session

ANALYSIS = os.path.join(storage.DATA, "analysis")

# Free tiers, both. The first name that answers is used, so a model being
# retired does not stop the run.
# Model names are NOT hardcoded, because hardcoding them failed: measured
# 7 Sep 2026, every name in the previous version had been retired and both
# providers answered HTTP 404 "this model is no longer available". A list
# written today will rot the same way.
#
# So we ASK each provider what it currently offers, and choose from that. The
# lists below are only a preference order — words we look for in the names that
# come back, best first. An unknown future model whose name contains "flash"
# will be picked up without anyone touching this file.
PREFERENCES = {
    "gemini": ["flash-lite", "flash", "pro"],
    "groq": ["instant", "versatile", "llama"],
}

# Things that are not general chat models, whatever else their name says.
NOT_CHAT = ("embedding", "aqa", "vision", "tts", "audio", "whisper", "guard",
            "image", "imagen", "veo", "gemma", "learnlm")

_discovered = {}

TIMEOUT = 90



# --------------------------------------------------------------- the budget

# Groq's free tier is generous and this project is its only user, so Groq is the
# standing analyst — it looks at everything. The Gemini key is shared with other
# projects, so it is spent like a scarce resource: only where a second opinion
# actually changes a decision, and never more than a few times a run.
# Both models read everything, by design: the whole point of running two is to
# see where they disagree, and a second opinion you only sometimes ask for
# cannot tell you that. The rationing machinery below is kept but switched OFF
# — set GEMINI_BUDGET_MODE=1 to bring it back if the shared quota ever bites.
BUDGET_MODE = os.environ.get("GEMINI_BUDGET_MODE") == "1"
GEMINI_CALLS_PER_RUN = int(os.environ.get("GEMINI_CALLS_PER_RUN", "99"))
DECISION_WINDOW_DAYS = 3          # how close to closing counts as "deciding now"
SECOND_OPINION_SCORE = 65         # a score high enough to be acted on


def _days_to_close(bundle):
    close = ((bundle.get("offer") or {}).get("close_date") or {}).get("value")
    if not close:
        return None
    try:
        closing = date.fromisoformat(close)
    except ValueError:
        return None
    return (closing - date.today()).days


def worth_a_second_opinion(bundle: dict, score: dict) -> tuple:
    """
    Is this one worth spending a Gemini call on?

    Yes when the answer could change what you do:
      * the issue is open or closes within a few days — you must decide now;
      * the quant score is high enough that you might act on it, and a second
        reader disagreeing would matter;
      * our own reading raised a hard stop or a contradiction, which is exactly
        the sort of thing worth a second pair of eyes.
    Otherwise Groq's answer stands on its own and the quota is left alone.
    """
    days = _days_to_close(bundle)
    if days is not None and 0 <= days <= DECISION_WINDOW_DAYS:
        return True, f"closes in {days} day(s) — you have to decide now"
    if bundle.get("status") in ("open", "current"):
        return True, "the issue is open"

    fundamentals = ((score or {}).get("fundamentals") or {}).get("score")
    if fundamentals and fundamentals >= SECOND_OPINION_SCORE:
        return True, f"scores {fundamentals} — high enough to act on"
    if ((score or {}).get("vetoes") or {}).get("triggered"):
        return True, "a hard stop was triggered"
    if bundle.get("conflicts"):
        return True, "two sources contradict each other"
    return False, ("not close to a decision and not scoring highly — "
                   "Groq's reading stands, Gemini quota saved")


def record_spend(model_family: str, model: str, ipo_id: str):
    """Append-only note of every paid-quota call, so usage is never a mystery."""
    os.makedirs(ANALYSIS, exist_ok=True)
    line = {"at": storage.now(), "family": model_family, "model": model,
            "ipo_id": ipo_id}
    with open(os.path.join(ANALYSIS, "usage.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def spent_today(model_family: str) -> int:
    path = os.path.join(ANALYSIS, "usage.jsonl")
    if not os.path.exists(path):
        return 0
    today = date.today().isoformat()
    count = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("family") == model_family and \
                    str(entry.get("at", "")).startswith(today):
                count += 1
    return count


# ------------------------------------------------------------- the brief

INSTRUCTIONS = """You are an equity analyst assessing an Indian IPO.

You are given verified figures and extracts from the company's own filed
prospectus. Work ONLY from these. You have no other information, and you must
not use anything you may recall about this company from elsewhere.

Rules:
- Every factual claim must cite a passage, in the form [chapter, page N], using
  only the citations given to you. If you cannot support a claim with a passage,
  do not make it.
- Do not restate the figures back to me. I have them. Tell me what they MEAN.
- Be specific about what would have to be true for your view to be wrong.
- Do not be agreeable. If the evidence is thin, say the evidence is thin. A
  confident answer built on nothing is worse than "I cannot tell from this".
- Indian IPOs are frequently priced for the seller, not the buyer. Treat the
  prospectus as the seller's own document, because it is.

Answer as strict JSON, no markdown fence, with exactly these keys:
{
  "listing_view": {"score": <0-100>, "reasoning": "<2-3 sentences>"},
  "longterm_view": {"score": <0-100>, "reasoning": "<2-3 sentences>"},
  "governance": {"score": <0-100>, "reasoning": "<1-2 sentences>"},
  "key_risks": [{"risk": "<short>", "why_it_matters": "<1 sentence>",
                 "citation": "<[chapter, page N]>"}],
  "answers": [{"question": "<the question asked>", "answer": "<your answer>",
               "citation": "<[chapter, page N] or 'not answerable from what I was given'>"}],
  "confidence": "low" | "medium" | "high",
  "what_would_change_my_mind": "<1-2 sentences>"
}

listing_view is about the first days of trading: demand, pricing, the mood.
longterm_view is about the business over years, and should ignore listing pop.
Score 50 means "no view either way". Use the full range; do not cluster at 70."""


def _facts_for_prompt(bundle: dict) -> str:
    """
    The numbers, with no scores and no verdicts attached.

    Deliberately excludes our own scoring, our flags' conclusions, and any
    earlier AI answer. The model reaches its own view before it is ever told
    what anyone else thought.
    """
    lines = [f"COMPANY: {bundle.get('name')} ({bundle.get('type')} IPO, "
             f"status: {bundle.get('status')})"]

    def dump(title, section):
        if not section:
            return
        lines.append(f"\n{title}")
        for name, body in sorted(section.items()):
            if not isinstance(body, dict):
                continue
            value = body.get("value")
            note = body.get("note")
            line = f"  {name}: {value}"
            if note:
                line += f"   ({note})"
            lines.append(line)

    dump("THE OFFER", bundle.get("offer"))
    dump("FINANCIALS (newest year first, ₹ million)", bundle.get("financials"))
    dump("DERIVED", bundle.get("derived"))
    dump("VALUATION", bundle.get("valuation"))
    dump("CASH", bundle.get("cash"))
    dump("BALANCE SHEET", bundle.get("balance_sheet"))
    dump("DEMAND", bundle.get("demand"))

    if bundle.get("conflicts"):
        lines.append("\nCONTRADICTIONS BETWEEN SOURCES (do not resolve these "
                     "silently; say which you would trust and why)")
        for item in bundle["conflicts"]:
            lines.append(f"  - {item.get('what')}: {item.get('means')}")

    if bundle.get("missing"):
        lines.append("\nWHAT WE COULD NOT ESTABLISH (absence of a figure is not "
                     "evidence either way)")
        for item in bundle["missing"][:10]:
            lines.append(f"  - {item['what']}: {item['why']}")

    return "\n".join(lines)


def questions_from(score: dict) -> list:
    """
    The questions our own scorer wants answered.

    Each flag the scorer raises carries a `for_ai` field — the specific thing a
    reader should check. We pass the QUESTIONS but not the flags themselves, so
    the model investigates rather than agrees.
    """
    out = []
    for flag in (score or {}).get("flags", []):
        question = flag.get("for_ai")
        if question:
            out.append(question)
    for veto in ((score or {}).get("vetoes") or {}).get("triggered", []):
        out.append(f"Our reading triggered a hard stop: {veto['veto']}. "
                   f"Is there an explanation in the document?")
    return out


def build_prompt(bundle: dict, sections: dict, score: dict = None) -> tuple:
    """Returns (prompt_text, citations_offered)."""
    passages = retrieval.brief(sections or {})
    questions = questions_from(score)

    parts = [INSTRUCTIONS, "\n=== VERIFIED FIGURES ===", _facts_for_prompt(bundle)]
    if questions:
        parts.append("\n=== QUESTIONS YOU MUST ANSWER ===")
        parts.extend(f"  {n}. {q}" for n, q in enumerate(questions, 1))
    parts.append("\n=== EXTRACTS FROM THE PROSPECTUS (your only source) ===")
    parts.append(retrieval.as_text(passages))

    return "\n".join(parts), passages



def available_models(family: str, key: str) -> list:
    """
    What this provider will actually serve us today, best first.

    Asked once per run and remembered, so we do not spend a request per IPO
    finding out something that cannot change mid-run.
    """
    if family in _discovered:
        return _discovered[family]

    session = plain_session()
    names = []
    try:
        if family == "gemini":
            resp = session.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key}, timeout=30)
            if resp.status_code == 200:
                for model in resp.json().get("models", []):
                    if "generateContent" not in (
                            model.get("supportedGenerationMethods") or []):
                        continue
                    names.append(model["name"].split("/")[-1])
        else:
            resp = session.get("https://api.groq.com/openai/v1/models",
                               headers={"Authorization": f"Bearer {key}"},
                               timeout=30)
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", [])
                         if m.get("id")]
    except Exception:
        names = []

    usable = [n for n in names
              if not any(word in n.lower() for word in NOT_CHAT)]

    def rank(name):
        lowered = name.lower()
        for position, word in enumerate(PREFERENCES.get(family, [])):
            if word in lowered:
                # Within the same preference, a longer name is usually the
                # newer, more specific one — but a plain name beats a dated
                # preview build, so previews sink.
                return (position, "preview" in lowered or "exp" in lowered,
                        -len(lowered))
        return (99, True, 0)

    usable.sort(key=rank)
    _discovered[family] = usable
    return usable


def models_to_try(family: str, key: str) -> list:
    """Discovered models first; if discovery itself failed, we have nothing."""
    found = available_models(family, key)
    return found[:4]


# ------------------------------------------------------------ the models

def ask_gemini(prompt: str, key: str) -> tuple:
    session = plain_session()
    last = "no usable model was offered by the provider"
    for model in models_to_try("gemini", key):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        try:
            resp = session.post(
                url, timeout=TIMEOUT,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.2,
                                           "maxOutputTokens": 2048}})
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue
        body = resp.json()
        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            last = f"unexpected shape: {json.dumps(body)[:200]}"
            continue
        return text, model
    raise FetchError(f"Gemini: {last}")


def ask_groq(prompt: str, key: str) -> tuple:
    session = plain_session()
    last = "no usable model was offered by the provider"
    for model in models_to_try("groq", key):
        try:
            resp = session.post(
                "https://api.groq.com/openai/v1/chat/completions", timeout=TIMEOUT,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.2,
                      "messages": [{"role": "user", "content": prompt}]})
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue
        body = resp.json()
        try:
            return body["choices"][0]["message"]["content"], model
        except (KeyError, IndexError):
            last = f"unexpected shape: {json.dumps(body)[:200]}"
    raise FetchError(f"Groq: {last}")


ASKERS = {"gemini": ask_gemini, "groq": ask_groq}
KEY_NAMES = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}


# --------------------------------------------------------- reading the reply

def parse(text: str) -> dict:
    """
    Turn the model's reply into a checked answer, or refuse it.

    Models wrap JSON in explanations and code fences however firmly you ask them
    not to, so we take the outermost braces. What we do NOT do is accept a reply
    that is missing pieces or carries a score outside 0-100: a malformed answer
    is recorded as a failure, never patched up into something usable.
    """
    if not text:
        raise ValueError("empty reply")
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                     flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON in the reply: {cleaned[:120]}")
    answer = json.loads(cleaned[start:end + 1])

    for key in ("listing_view", "longterm_view", "confidence"):
        if key not in answer:
            raise ValueError(f"missing '{key}'")
    for key in ("listing_view", "longterm_view", "governance"):
        view = answer.get(key)
        if isinstance(view, dict) and view.get("score") is not None:
            score = float(view["score"])
            if not 0 <= score <= 100:
                raise ValueError(f"{key} score {score} is outside 0-100")
            view["score"] = score
    return answer


def check_citations(answer: dict, passages: dict) -> dict:
    """
    Which claims are backed by a passage we actually supplied.

    A model can invent a page number as easily as a fact. Every citation is
    checked against the pages we sent; one that does not match is not deleted —
    it is marked, so you can see the model reaching beyond its evidence.
    """
    offered = set()
    for items in (passages or {}).values():
        for item in items:
            offered.add((item["chapter"], str(item["page"])))

    checked, bad = [], 0
    for group in ("key_risks", "answers"):
        for item in answer.get(group) or []:
            citation = str(item.get("citation") or "")
            match = re.search(r"\[?\s*([a-z_]+)\s*,\s*page\s*(\d+)", citation, re.I)
            ok = bool(match and (match.group(1).lower(), match.group(2)) in offered)
            item["citation_verified"] = ok
            if not ok and citation and "not answerable" not in citation.lower():
                bad += 1
            checked.append(ok)

    return {"claims_checked": len(checked),
            "citations_verified": sum(1 for c in checked if c),
            "citations_not_matching_what_we_sent": bad}


# ------------------------------------------------------------------ running

def fingerprint(bundle: dict) -> str:
    """
    A short signature of what we know, ignoring when we knew it.

    The analyst is only re-run when this changes. Four runs a day against
    unchanged evidence would burn the free tiers re-answering the same question,
    and would fill the record with identical opinions.
    """
    material = {key: value for key, value in bundle.items()
                if key not in ("built_at",)}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def already_done(ipo_id: str, model: str, signature: str) -> bool:
    path = os.path.join(ANALYSIS, ipo_id, f"{model}-latest.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("evidence_fingerprint") == signature
    except (OSError, ValueError):
        return False


def analyse(bundle: dict, sections: dict, score: dict = None,
            which=("groq", "gemini"), gemini_left=None) -> dict:
    """
    Ask the models the same question, separately, and record the answers.

    Both are asked, every time the evidence has changed. Two readings only tell
    you something when they read the same thing — an opinion you solicit
    selectively cannot be compared with one you always take.

    The one thing that is still rationed is repetition: an IPO whose evidence
    has not changed since the last answer is not asked again. That saves quota
    without costing a single comparison.
    """
    prompt, passages = build_prompt(bundle, sections, score)
    signature = fingerprint(bundle)
    results = {}

    for name in which:
        if name == "gemini" and BUDGET_MODE:
            worth, why = worth_a_second_opinion(bundle, score)
            if not worth:
                results[name] = {"ok": True, "skipped": why}
                continue
            if gemini_left is not None and gemini_left() <= 0:
                results[name] = {"ok": True,
                                 "skipped": "this run's Gemini allowance is spent"}
                continue
            results.setdefault("_gemini_reason", why)
        key = os.environ.get(KEY_NAMES[name])
        if not key:
            results[name] = {"ok": False, "why": f"{KEY_NAMES[name]} is not set"}
            continue
        if already_done(bundle["ipo_id"], name, signature):
            results[name] = {"ok": True, "skipped": "evidence unchanged since "
                                                    "the last analysis"}
            continue
        try:
            text, model_used = ASKERS[name](prompt, key)
            record_spend(name, model_used, bundle["ipo_id"])
        except Exception as exc:
            results[name] = {"ok": False, "why": f"{type(exc).__name__}: "
                                                 f"{str(exc)[:160]}"}
            continue
        try:
            answer = parse(text)
        except Exception as exc:
            results[name] = {"ok": False, "why": f"unusable reply — {exc}",
                             "raw": text[:400]}
            continue

        answer["citation_check"] = check_citations(answer, passages)
        results[name] = {
            "ok": True,
            "model": model_used,
            "answered_at": storage.now(),
            "evidence_fingerprint": signature,
            "prompt_chars": len(prompt),
            "passages_supplied": {topic: len(items)
                                  for topic, items in passages.items()},
            "answer": answer,
        }
    return results


def compare(results: dict) -> dict:
    """
    Where the two models disagree — reported, never averaged away.

    Two models agreeing is weak evidence: they are both trained to be
    agreeable. Two models disagreeing is strong evidence that the question is
    genuinely open, and that is worth more to you than a tidy single number.
    """
    scores = {}
    for name, result in results.items():
        if name.startswith("_"):
            continue
        answer = (result or {}).get("answer") or {}
        for view in ("listing_view", "longterm_view", "governance"):
            value = (answer.get(view) or {}).get("score")
            if value is not None:
                scores.setdefault(view, {})[name] = value

    out = {"scores": scores, "disagreements": [], "ai_block": {}}
    for view, by_model in scores.items():
        values = list(by_model.values())
        if len(values) == 2:
            gap = abs(values[0] - values[1])
            out["ai_block"][view] = round(sum(values) / 2, 1)
            if gap > 15:
                out["disagreements"].append({
                    "on": view, "gap": round(gap, 1), "scores": by_model,
                    "means": "the models read the same evidence differently — "
                             "treat this verdict as uncertain, and read both "
                             "reasonings rather than the average"})
        elif values:
            out["ai_block"][view] = values[0]
            out["ai_block"][view + "_note"] = "only one model answered"
    return out


def save(ipo_id: str, results: dict, comparison: dict) -> str:
    folder = os.path.join(ANALYSIS, ipo_id)
    os.makedirs(folder, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", storage.now())[:14]

    for name, result in results.items():
        if name.startswith("_") or not result.get("ok") or result.get("skipped"):
            continue
        for target in (f"{name}-{stamp}.json", f"{name}-latest.json"):
            with open(os.path.join(folder, target), "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=1, ensure_ascii=False)

    path = os.path.join(folder, f"comparison-{stamp}.json")
    for target in (path, os.path.join(folder, "comparison-latest.json")):
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(comparison, handle, indent=1, ensure_ascii=False)
    return path


def summarise(results: dict, comparison: dict) -> str:
    parts = []
    for name, result in results.items():
        if name.startswith("_"):
            continue
        if result.get("skipped"):
            parts.append(f"{name}: unchanged")
        elif result.get("ok"):
            answer = result["answer"]
            parts.append(f"{name} {result.get('model', '')}: "
                         f"listing {(answer.get('listing_view') or {}).get('score')}, "
                         f"long-term {(answer.get('longterm_view') or {}).get('score')}, "
                         f"confidence {answer.get('confidence')}")
        else:
            parts.append(f"{name}: FAILED — {result.get('why')}")
    if comparison.get("disagreements"):
        parts.append(f"{len(comparison['disagreements'])} DISAGREEMENT(S)")
    return " | ".join(parts)
