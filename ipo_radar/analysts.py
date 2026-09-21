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
    "groq": ["llama", "qwen", "kimi", "gpt-oss", "instant", "versatile"],
    # NVIDIA hosts many models behind one OpenAI-compatible endpoint, so we
    # name the one we want rather than letting a preference word choose. It can
    # be changed without touching code by setting NVIDIA_MODEL.
    "nvidia": ["gemma", "llama", "qwen", "nemotron"],
}

# First choice and fallback, in that order. Kimi K3 is a far stronger reader;
# Gemma is smaller and quicker, and steps in if K3 times out or the free
# endpoint is busy. Either can be overridden with a secret of the same name.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "moonshotai/kimi-k3")
NVIDIA_FALLBACK = os.environ.get("NVIDIA_FALLBACK", "google/gemma-4-31b-it")

# ONE SWITCH for how hard and how long Kimi works: ANALYST_MODE.
#
#   test     — quick. Light reasoning, short time limits. For getting the whole
#              pipeline working end to end without waiting hours per run.
#   quality  — deepest reasoning and generous time. The goal once "test" has
#              run cleanly (decided 21 Sep 2026: quality over speed, no
#              real-time requirement).
#
# Switch it in GitHub → Settings → Secrets and variables → Actions → the
# "Variables" tab → ANALYST_MODE = quality. No code change needed. Each
# individual setting below can still be overridden on its own if ever needed.
MODES = {
    #            effort   retry    per answer  silence  whole run
    "test":    ("low",   "",       180,        90,      45),
    "quality": ("max",   "high",   900,        300,     240),
}
ANALYST_MODE = (os.environ.get("ANALYST_MODE") or "test").strip().lower()
if ANALYST_MODE not in MODES:
    ANALYST_MODE = "test"
_effort, _retry, _answer, _silence, _run = MODES[ANALYST_MODE]

# How hard Kimi thinks before answering: low / high / max.
NVIDIA_REASONING_EFFORT = os.environ.get("NVIDIA_REASONING_EFFORT") or _effort

# If that setting spends its whole allowance thinking and never reaches an
# answer, ask Kimi once more at this lighter setting before giving up on it.
# Empty means no second try (in test mode "low" is already the lightest).
NVIDIA_RETRY_EFFORT = os.environ.get("NVIDIA_RETRY_EFFORT", _retry)

# How long one answer may take, start to finish, in seconds. Past this the
# fallback model takes over.
NVIDIA_MAX_SECONDS = int(os.environ.get("NVIDIA_MAX_SECONDS") or _answer)

# How long the stream may go completely silent before we call it dead.
NVIDIA_SILENCE_SECONDS = int(os.environ.get("NVIDIA_SILENCE_SECONDS") or _silence)

# How long, in minutes, the whole AI step may keep starting new IPOs.
ANALYST_BUDGET_MINUTES = int(os.environ.get("ANALYST_BUDGET_MINUTES") or _run)

# Things that are not general chat models, whatever else their name says.
NOT_CHAT = ("embedding", "aqa", "vision", "tts", "audio", "whisper", "guard",
            "image", "imagen", "veo", "gemma", "learnlm",
            # "compound" models are agentic systems that can SEARCH THE WEB.
            # That breaks the rule this whole project rests on: both models must
            # see the same evidence and nothing else, or their disagreement
            # means nothing and their claims cannot be checked against a page.
            # Run #32 picked groq/compound-mini and it promptly asserted a cash
            # flow figure that contradicts the document.
            "compound")

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
    if bundle.get("observations"):
        lines.append("\nNOTED WHILE READING THE STATEMENTS")
        for item in bundle["observations"]:
            lines.append(f"  - {item.get('from')}: {item.get('says')}")
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
        parts.append("  Answer from the figures above as well as the extracts "
                     "below — the figures are evidence too. Only say a question "
                     "is unanswerable if neither contains what you need.")
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
        elif family == "nvidia":
            resp = session.get("https://integrate.api.nvidia.com/v1/models",
                               headers={"Authorization": f"Bearer {key}"},
                               timeout=30)
            if resp.status_code == 200:
                names = [m.get("id") for m in resp.json().get("data", [])
                         if m.get("id")]
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
    """
    Discovered models, best first.

    For NVIDIA we PIN the model rather than guessing: their catalogue holds
    hundreds, most of them unsuited to reading a prospectus, and the one we want
    was chosen deliberately. It still goes through discovery so that if it is
    ever withdrawn we fall back to something rather than failing outright.
    """
    if family == "nvidia":
        # Only the two models we chose, in order. NVIDIA's catalogue holds
        # hundreds and most are unsuited to this; guessing among them is how
        # we once ended up analysing IPOs with an Arabic-language model.
        return [m for m in (NVIDIA_MODEL, NVIDIA_FALLBACK) if m]
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
    """
    Groq, sticking with ONE model wherever it can.

    Measured 21 Sep 2026: one run used three different Groq models across
    different IPOs. That quietly breaks the comparison — "Groq's view" must mean
    the same reader every time, or its track record means nothing. The cause was
    the free tier's rate limit: when a model answered HTTP 429 ("too many
    requests, slow down") we jumped to the next model instead of waiting.

    Now a 429 means wait — for as long as Groq asks, up to a minute — and try the
    SAME model again, up to three times. Only a model that is genuinely broken
    or gone moves us on to the next.
    """
    import time
    session = plain_session()
    last = "no usable model was offered by the provider"
    for model in models_to_try("groq", key):
        for attempt in range(1, 4):
            try:
                resp = session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    timeout=TIMEOUT,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0.2,
                          "messages": [{"role": "user", "content": prompt}]})
            except Exception as exc:
                last = f"{model}: {type(exc).__name__}: {exc}"
                break
            if resp.status_code == 429 and attempt < 3:
                try:
                    wait = float(resp.headers.get("retry-after") or 20)
                except ValueError:
                    wait = 20
                time.sleep(min(max(wait, 5), 60))
                continue
            if resp.status_code != 200:
                last = f"{model}: HTTP {resp.status_code}: {resp.text[:200]}"
                break
            body = resp.json()
            try:
                return body["choices"][0]["message"]["content"], model
            except (KeyError, IndexError):
                last = f"{model}: unexpected shape: {json.dumps(body)[:200]}"
                break
    raise FetchError(f"Groq: {last}")


def _nvidia_payload(model: str, prompt: str, effort: str = None) -> dict:
    """
    The request body, which differs by model family.

    Kimi K3 always reasons and takes a `reasoning_effort` setting; the sample
    runs it at temperature 1, which is what Moonshot recommends for its
    reasoning models, with a fixed seed so the same brief gives the same answer
    on a re-run. Gemma instead takes `enable_thinking`, which we switch off.
    """
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True}
    if "kimi" in model.lower():
        body.update({"max_tokens": 16384,          # reasoning spends tokens too
                     "temperature": 1,
                     "seed": 0,
                     "reasoning_effort": effort or NVIDIA_REASONING_EFFORT})
    else:
        body.update({"max_tokens": 4096,
                     "temperature": 0.2,
                     "top_p": 0.95,
                     "chat_template_kwargs": {"enable_thinking": False}})
    return body


THINKING = re.compile(r"<think>.*?</think>", re.S | re.I)


def _read_stream(resp) -> tuple:
    """
    Reassemble a streamed answer, keeping the conclusion and discarding the
    working.

    The server sends the reply a few words at a time, each as a line starting
    `data: {...}`. A reasoning model sends two kinds of text: its thinking, in a
    field called `reasoning_content`, and its answer, in `content`. We keep only
    the answer — the thinking is often longer than the answer and would sit in
    front of the JSON we have to read. Any thinking that arrives wrapped in
    <think> tags inside the answer itself is stripped as well.
    """
    import time
    started = time.monotonic()
    answer, thought = [], 0
    for raw in resp.iter_lines():
        if time.monotonic() - started > NVIDIA_MAX_SECONDS:
            raise FetchError(f"still answering after {NVIDIA_MAX_SECONDS}s")
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                answer.append(delta["content"])
            if delta.get("reasoning_content") or delta.get("reasoning"):
                thought += len(delta.get("reasoning_content")
                               or delta.get("reasoning") or "")
    text = THINKING.sub("", "".join(answer)).strip()
    return text, thought


# What happened on the last NVIDIA call, in full, for the run log.
LAST_NVIDIA_NOTES = []


def _one_nvidia_call(session, key, model, prompt, effort=None) -> tuple:
    """One attempt. Returns (text, thought_chars) or raises."""
    resp = session.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        timeout=(30, NVIDIA_SILENCE_SECONDS),
        stream=True,
        headers={"Authorization": f"Bearer {key}",
                 "Accept": "text/event-stream",
                 "Content-Type": "application/json"},
        json=_nvidia_payload(model, prompt, effort))
    try:
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                raise FetchError(
                    f"HTTP {resp.status_code} — NVIDIA rejected the key. Check "
                    f"that NVIDIA_API_KEY in GitHub Secrets is the NEW key, "
                    f"complete, and still active at build.nvidia.com")
            raise FetchError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return _read_stream(resp)
    finally:
        resp.close()


def ask_nvidia(prompt: str, key: str) -> tuple:
    """
    NVIDIA's model catalogue, over its OpenAI-compatible endpoint.

    The ladder, in order, and why each step exists:

      1. Kimi K3 at maximum reasoning, with up to 15 minutes. The best answer
         we can get, and quality is the stated priority.
      2. Kimi K3 again at "high", ONLY if step 1 thought for so long that it ran
         out of room before writing an answer. Same strong model, a little less
         deliberation — better than giving up on it.
      3. Gemma, if Kimi timed out, errored, or the free endpoint was busy.

    Streamed, as in NVIDIA's own sample, because a thinking model can work for
    minutes before its first word of answer; a connection held open silently
    for that long tends to be cut by something in between.
    """
    session = plain_session()
    notes, short = [], []
    for model in models_to_try("nvidia", key):
        efforts = ([None, NVIDIA_RETRY_EFFORT]
                   if "kimi" in model.lower() and NVIDIA_RETRY_EFFORT else [None])
        for effort in efforts:
            label = f"{model}" + (f" at {effort}" if effort else "")
            try:
                text, thought = _one_nvidia_call(session, key, model, prompt, effort)
            except Exception as exc:
                kind = ("timed out" if "Timeout" in type(exc).__name__
                        or "still answering" in str(exc) else "failed")
                notes.append(f"{label}: {type(exc).__name__} {str(exc)[:140]}")
                short.append(f"{model.split('/')[-1]} {kind}")
                break                       # a timeout or error: go to fallback
            if text:
                LAST_NVIDIA_NOTES[:] = notes
                # The label that appears in the email and on the dashboard.
                # Kept short, but honest about anything that went differently:
                # "kimi-k3 (retried at high)" or "gemma-4-31b-it (fallback:
                # kimi-k3 timed out)". The full detail goes to the run log.
                if effort:
                    return text, f"{model} (retried at {effort})"
                if short:
                    return text, f"{model} (fallback: {'; '.join(short)})"
                return text, model
            notes.append(f"{label}: reasoned for {thought:,} characters and "
                         f"ran out of room before answering")
            short.append(f"{model.split('/')[-1]} ran out of room")
    LAST_NVIDIA_NOTES[:] = notes
    raise FetchError("NVIDIA: " + " | ".join(notes)[:600])


ASKERS = {"gemini": ask_gemini, "groq": ask_groq, "nvidia": ask_nvidia}
KEY_NAMES = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
             "nvidia": "NVIDIA_API_KEY"}

# The two analysts in use. Gemini's code is kept and still works — swapping
# back is a one-word change — but the second seat now belongs to the model
# NVIDIA hosts, which is a different family from Groq's and so more likely to
# disagree in a useful way.
ANALYSTS = ("groq", "nvidia")


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

# Bump this whenever the prompt, the model choice or the checking rules change.
# The fingerprint below decides whether an IPO is re-asked, and it used to cover
# only the EVIDENCE — so improving the analyst left every old answer in place,
# because the evidence had not moved. Including this version means a better
# analyst re-reads everything once, then goes quiet again.
ANALYST_VERSION = "2026-09-21-kimi-k3-no-web-peer-names"


def fingerprint(bundle: dict) -> str:
    """
    A short signature of what we know, ignoring when we knew it.

    The analyst is only re-run when this changes. Four runs a day against
    unchanged evidence would burn the free tiers re-answering the same question,
    and would fill the record with identical opinions.
    """
    material = {key: value for key, value in bundle.items()
                if key not in ("built_at",)}
    material["_analyst_version"] = ANALYST_VERSION
    # The mode is part of what the answer IS: a "test" answer is not a
    # "quality" answer. Without this, switching ANALYST_MODE to quality would
    # change nothing for IPOs already analysed in test mode, because their
    # evidence had not moved. With it, the switch re-reads everything once.
    material["_analyst_mode"] = ANALYST_MODE
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


def clean_key(raw):
    """
    An API key as it should be, whatever happened when it was pasted.

    Copying a key from a web page easily brings along a trailing space or line
    break, surrounding quote marks, or the word "Bearer" from an example.
    Measured 21 Sep 2026: NVIDIA answered HTTP 401 Unauthorized on every call,
    and a pasting slip is the commonest cause. The key itself never contains
    spaces or quotes, so removing them cannot damage a correct one.
    """
    key = (raw or "").strip().strip('"').strip("'").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return "".join(key.split())


def key_problem(name: str, raw: str):
    """What looks wrong with a key, in plain words, without revealing it."""
    key = clean_key(raw)
    if not key:
        return f"{KEY_NAMES[name]} is empty"
    notes = []
    if key != (raw or ""):
        notes.append("had extra spaces, quotes or 'Bearer' around it (removed)")
    if name == "nvidia" and not key.startswith("nvapi-"):
        notes.append("does not start with 'nvapi-' as NVIDIA keys do")
    return f"{KEY_NAMES[name]}: {len(key)} characters" + (
        f", {'; '.join(notes)}" if notes else ", looks well-formed")


def analyse(bundle: dict, sections: dict, score: dict = None,
            which=ANALYSTS, gemini_left=None) -> dict:
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
        key = clean_key(os.environ.get(KEY_NAMES[name]))
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


# A model whose citations mostly fail to match the pages we sent is not a
# second opinion; it is a confident stranger. Below this share of verified
# claims its scores are shown but kept out of the average that feeds the call.
RELIABLE_ENOUGH = 0.5


def reliability(result: dict) -> dict:
    checked = ((result or {}).get("answer") or {}).get("citation_check") or {}
    claims = checked.get("claims_checked") or 0
    verified = checked.get("citations_verified") or 0
    invented = checked.get("citations_not_matching_what_we_sent") or 0
    if claims < 2:
        return {"share": None, "trusted": True,
                "why": "too few claims to judge"}
    share = verified / claims
    return {
        "share": round(share, 2),
        "trusted": share >= RELIABLE_ENOUGH and invented == 0,
        "why": (f"{verified} of {claims} citations matched the pages we sent"
                + (f", and {invented} pointed at pages we never sent"
                   if invented else "")),
    }


def compare(results: dict) -> dict:
    """
    Where the two models disagree — reported, never averaged away.

    Two models agreeing is weak evidence: they are both trained to be
    agreeable. Two models disagreeing is strong evidence that the question is
    genuinely open, and that is worth more to you than a tidy single number.

    A model that failed its citation check is shown in full but excluded from
    the average that feeds the call. Checking a model's sources and then
    letting an unsourced answer count anyway would make the check decoration.
    """
    scores, trust = {}, {}
    for name, result in results.items():
        if name.startswith("_"):
            continue
        trust[name] = reliability(result)
        answer = (result or {}).get("answer") or {}
        for view in ("listing_view", "longterm_view", "governance"):
            value = (answer.get(view) or {}).get("score")
            if value is not None:
                scores.setdefault(view, {})[name] = value

    out = {"scores": scores, "reliability": trust,
           "disagreements": [], "ai_block": {}, "excluded": []}

    usable = {name for name, body in trust.items() if body["trusted"]}
    for name, body in trust.items():
        if name not in usable:
            out["excluded"].append({"model": name, "why": body["why"]})

    for view, by_model in scores.items():
        values = list(by_model.values())
        counted = [v for name, v in by_model.items() if name in usable]

        if counted:
            out["ai_block"][view] = round(sum(counted) / len(counted), 1)
            if len(counted) < len(values):
                out["ai_block"][view + "_note"] = (
                    "one model was left out of this average — its citations "
                    "did not check out")
        if len(values) == 2:
            gap = abs(values[0] - values[1])
            if gap > 15:
                out["disagreements"].append({
                    "on": view, "gap": round(gap, 1), "scores": by_model,
                    "means": "the models read the same evidence differently — "
                             "treat this verdict as uncertain, and read both "
                             "reasonings rather than the average"})
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
    for item in comparison.get("excluded", []):
        parts.append(f"{item['model']} EXCLUDED from the average ({item['why']})")
    if comparison.get("disagreements"):
        parts.append(f"{len(comparison['disagreements'])} DISAGREEMENT(S)")
    return " | ".join(parts)
