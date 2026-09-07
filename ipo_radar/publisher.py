"""
The email digest.

Email is not a web page. It is rendered by clients written to no common
standard, several of which strip stylesheets, ignore flexbox and grid, collapse
margins unpredictably, and cut a message off entirely past about 100 KB. So this
module deliberately writes email the old way:

  * layout is TABLES, never divs with flexbox or grid;
  * every style is inline on the element it applies to, because Gmail strips a
    <style> block in some contexts and forwards it in others;
  * no external images, no web fonts, no JavaScript;
  * widths are percentages, so it reflows on a phone;
  * every piece of text that came from a document or a model is HTML-escaped,
    so an ampersand or an angle bracket in a company name cannot break the
    layout or, worse, inject markup;
  * a plain-text version is always sent alongside, which is both a courtesy and
    the thing that keeps the message out of spam folders.

The digest is written to disk on every run whether or not it is sent, so the
formatting can be checked in a browser without spending an email.
"""

import html
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import call as calls, storage

OUT = os.path.join(storage.DATA, "email")

# Gmail stops rendering at roughly 102 KB and hides the rest behind a "View
# entire message" link — which means the part you most need could be the part
# it hides. So the digest budgets itself: every IPO appears in the summary
# table at the top, and the detailed write-ups are added in order of how much
# they matter until the budget is used up. Measured 7 Sep 2026: 16 IPOs with
# full detail came to 173 KB, well past the limit.
GMAIL_CLIP_BYTES = 102_000
BUDGET = 88_000            # leaves room for headers and the plain-text part

INK = "#1c1b19"
MUTED = "#6b6862"
LINE = "#e3e0da"
GOOD = "#1f7a4d"
WARN = "#a86400"
BAD = "#a32d22"
# Each analyst gets its own colour and keeps it everywhere — in the table
# header, in its panel border, in its section heading. Two opinions that look
# alike are easy to blur together; these cannot be.
GROQ = "#1a6b8f"          # teal
GROQ_BG = "#eef6fa"
GEMINI = "#6b3fa0"        # violet
GEMINI_BG = "#f4f0fa"
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
        "Arial,sans-serif")


def e(value):
    """Everything that reaches the email goes through here. No exceptions."""
    return html.escape("" if value is None else str(value), quote=True)


def _n(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, list):
        return "  ·  ".join(_n(v) for v in value)
    return str(value)


# --------------------------------------------------------------- building

def _cell(content, bold=False, align="left", color=INK, width=None):
    style = (f"padding:7px 10px;border-bottom:1px solid {LINE};"
             f"font-family:{FONT};font-size:14px;line-height:1.5;"
             f"color:{color};text-align:{align};"
             f"{'font-weight:600;' if bold else ''}"
             f"{f'width:{width};' if width else ''}")
    return f'<td style="{style}">{content}</td>'


def _head(text, align="left"):
    style = (f"padding:7px 10px;border-bottom:2px solid {LINE};"
             f"font-family:{FONT};font-size:11px;letter-spacing:.06em;"
             f"text-transform:uppercase;color:{MUTED};text-align:{align};"
             f"font-weight:600;")
    return f'<th style="{style}">{e(text)}</th>'


def _table(rows, widths=None):
    return ('<table role="presentation" cellpadding="0" cellspacing="0" '
            'border="0" width="100%" style="width:100%;border-collapse:collapse;'
            'margin:10px 0 4px;">' + "".join(rows) + "</table>")


def _para(text, color=INK, size=14, top=10):
    return (f'<p style="margin:{top}px 0 0;font-family:{FONT};font-size:{size}px;'
            f'line-height:1.6;color:{color};">{text}</p>')


def _bullets(items, color=INK):
    if not items:
        return ""
    lines = "".join(
        f'<li style="margin:0 0 6px;font-family:{FONT};font-size:14px;'
        f'line-height:1.55;color:{color};">{item}</li>' for item in items)
    return f'<ul style="margin:8px 0 0;padding-left:20px;">{lines}</ul>'


def _callout(title, body, colour):
    return (f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="width:100%;margin:12px 0;">'
            f'<tr><td style="border-left:4px solid {colour};padding:10px 14px;'
            f'background:#faf9f7;font-family:{FONT};font-size:14px;'
            f'line-height:1.55;color:{INK};">'
            f'<b>{title}</b><br>{body}</td></tr></table>')



def _verdict_banner(call):
    """
    The call, at the top, before anything else.

    Two boxes side by side — listing and long term — because they are different
    questions and blending them into one number would hide the case where an
    issue is worth applying to and not worth keeping.
    """
    def box(title, body):
        return (f'<td width="50%" style="width:50%;padding:0 6px;vertical-align:top;">'
                f'<table role="presentation" width="100%" cellpadding="0" '
                f'cellspacing="0" border="0" style="width:100%;">'
                f'<tr><td style="background:{body["colour"]};padding:12px 14px;'
                f'border-radius:8px 8px 0 0;font-family:{FONT};color:#ffffff;">'
                f'<div style="font-size:11px;letter-spacing:.09em;'
                f'text-transform:uppercase;opacity:.85;">{e(title)}</div>'
                f'<div style="font-size:19px;font-weight:700;line-height:1.25;'
                f'padding-top:2px;">{e(body["call"])}</div></td></tr>'
                f'<tr><td style="border:1px solid {LINE};border-top:none;'
                f'border-radius:0 0 8px 8px;padding:9px 14px;font-family:{FONT};'
                f'font-size:12.5px;line-height:1.5;color:{MUTED};">'
                f'score {e(_n(body.get("score")))} · confidence '
                f'{e(body.get("confidence"))}<br>{e(body.get("why"))}'
                f'</td></tr></table></td>')

    return ('<table role="presentation" width="100%" cellpadding="0" '
            'cellspacing="0" border="0" style="width:100%;margin:14px 0 4px;">'
            '<tr>'
            + box("If you are deciding whether to apply", call["listing"])
            + box("If you intend to hold for years", call["longterm"])
            + '</tr></table>')


# ------------------------------------------------------- the two-model table

def _model_column(result):
    """One model's headline numbers, or why there are none."""
    if not result:
        return None
    if result.get("skipped"):
        return {"state": "skipped", "why": result["skipped"]}
    if not result.get("ok"):
        return {"state": "failed", "why": result.get("why")}
    answer = result.get("answer") or {}
    checked = answer.get("citation_check") or {}
    return {
        "state": "ok",
        "model": result.get("model"),
        "listing": (answer.get("listing_view") or {}).get("score"),
        "longterm": (answer.get("longterm_view") or {}).get("score"),
        "governance": (answer.get("governance") or {}).get("score"),
        "confidence": answer.get("confidence"),
        "citations": (f"{checked.get('citations_verified', 0)} of "
                      f"{checked.get('claims_checked', 0)} verified"
                      + (f", {checked['citations_not_matching_what_we_sent']} "
                         f"not in what we sent"
                         if checked.get("citations_not_matching_what_we_sent")
                         else "")),
        "answer": answer,
    }


def _comparison_table(groq, gemini):
    """
    The two models side by side.

    Presented as two columns rather than one blended number on purpose: an
    average of two disagreeing readers describes neither of them, and the
    disagreement is the most useful thing on the page.
    """
    def coloured_head(text, colour, background):
        return (f'<th style="padding:8px 10px;border-bottom:2px solid {colour};'
                f'background:{background};font-family:{FONT};font-size:12px;'
                f'letter-spacing:.06em;text-transform:uppercase;color:{colour};'
                f'text-align:center;font-weight:700;">{e(text)}</th>')

    rows = [f"<tr>{_head('')}"
            f"{coloured_head('Groq', GROQ, GROQ_BG)}"
            f"{coloured_head('Gemini', GEMINI, GEMINI_BG)}</tr>"]

    def line(label, key, formatter=_n):
        left = groq.get(key) if groq and groq["state"] == "ok" else None
        right = gemini.get(key) if gemini and gemini["state"] == "ok" else None
        def tinted(value, colour, background):
            return (f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};'
                    f'background:{background};font-family:{FONT};font-size:14px;'
                    f'color:{colour};text-align:center;font-weight:600;">'
                    f'{e(formatter(value))}</td>')

        return (f"<tr>{_cell(e(label), width='38%')}"
                f"{tinted(left, GROQ, GROQ_BG)}"
                f"{tinted(right, GEMINI, GEMINI_BG)}</tr>")

    rows.append(line("Model used", "model"))
    rows.append(line("Listing view (0–100)", "listing"))
    rows.append(line("Long-term view (0–100)", "longterm"))
    rows.append(line("Governance (0–100)", "governance"))
    rows.append(line("Its own confidence", "confidence"))
    rows.append(line("Citations checked", "citations"))
    return _table(rows)


def _model_detail(name, column, colour=None, background=None):
    """
    The full reasoning from one model, inside a panel in that model's colour.

    Each analyst's own words stay visually enclosed, so you always know which
    of the two you are reading without checking the heading.
    """
    if not column:
        return ""
    colour = colour or MUTED
    background = background or "#ffffff"
    if column["state"] != "ok":
        return _para(f"<b style=\'color:{colour};\'>{e(name)}</b> — "
                     f"{e(column['state'])}: {e(column.get('why'))}", color=MUTED)

    answer = column["answer"]
    parts = [f'<p style="margin:0;font-family:{FONT};font-size:12px;'
             f'letter-spacing:.07em;text-transform:uppercase;color:{colour};'
             f'font-weight:700;">{e(name)} — in its own words '
             f'<span style="font-weight:400;text-transform:none;letter-spacing:0;'
             f'color:{MUTED};">({e(column.get("model") or "")})</span></p>']

    for label, key in (("On listing", "listing_view"),
                       ("On the long term", "longterm_view"),
                       ("On governance", "governance")):
        body = answer.get(key) or {}
        if body.get("reasoning"):
            parts.append(_para(f"<b>{e(label)} ({_n(body.get('score'))}).</b> "
                               f"{e(body['reasoning'])}"))

    risks = []
    for item in answer.get("key_risks") or []:
        mark = (f'<span style="color:{GOOD};">{e(item.get("citation"))}</span>'
                if item.get("citation_verified")
                else f'<span style="color:{BAD};">{e(item.get("citation"))} '
                     f'(could not be verified)</span>')
        risks.append(f"<b>{e(item.get('risk'))}</b> — "
                     f"{e(item.get('why_it_matters'))} {mark}")
    if risks:
        parts.append(_para("<b>Key risks it identified</b>"))
        parts.append(_bullets(risks))

    answers = []
    for item in answer.get("answers") or []:
        mark = (f'<span style="color:{GOOD};">{e(item.get("citation"))}</span>'
                if item.get("citation_verified")
                else f'<span style="color:{BAD};">{e(item.get("citation"))}</span>')
        answers.append(f"<i>{e(item.get('question'))}</i><br>"
                       f"{e(item.get('answer'))} {mark}")
    if answers:
        parts.append(_para("<b>Answers to the questions our own scoring raised</b>"))
        parts.append(_bullets(answers))

    if answer.get("what_would_change_my_mind"):
        parts.append(_para(f"<i>What would change its mind:</i> "
                           f"{e(answer['what_would_change_my_mind'])}",
                           color=MUTED, size=13))

    return ('<table role="presentation" width="100%" cellpadding="0" '
            'cellspacing="0" border="0" style="width:100%;margin:16px 0 0;">'
            f'<tr><td style="border-left:4px solid {colour};background:{background};'
            f'padding:14px 16px;border-radius:0 8px 8px 0;">'
            + "".join(parts) + '</td></tr></table>')


# ------------------------------------------------------------- one company

def _quant_table(score):
    """Our own scoring, with the coverage figure given equal billing."""
    fundamentals = (score or {}).get("fundamentals") or {}
    demand = (score or {}).get("demand") or {}

    def row(label, block):
        shown = "not scored" if block.get("score") is None else str(block["score"])
        covered = f"{block.get('coverage_pct', 0)}%"
        return (f"<tr>{_cell(e(label), width='50%')}"
                f"{_cell(e(shown), bold=True, align='center')}"
                f"{_cell(e(covered), align='center', color=MUTED)}</tr>")

    rows = [f"<tr>{_head('Our own scoring (no AI)')}{_head('Score', 'center')}"
            f"{_head('How much could be assessed', 'center')}</tr>",
            row("Fundamentals — the business and the price", fundamentals),
            row("Demand — what the market is doing", demand)]
    return _table(rows)


def company_block(ipo, score, bundle, groq_result, gemini_result, comparison,
                  call=None):
    groq = _model_column(groq_result)
    gemini = _model_column(gemini_result)

    dates = (ipo.get("dates") or {})
    facts = [f"{e(ipo.get('type', ''))} issue",
             f"open {e(dates.get('open', '?'))} to {e(dates.get('close', '?'))}"]
    if ipo.get("price_band"):
        facts.append(e(ipo["price_band"]))
    subscription = ((bundle or {}).get("demand") or {}).get("subscription_times")
    if subscription:
        facts.append(f"{_n(subscription.get('value'))}× subscribed")

    parts = [
        f'<h2 style="margin:34px 0 2px;font-family:{FONT};font-size:20px;'
        f'line-height:1.3;color:{INK};">{e(ipo.get("name"))}</h2>',
        _para(" &nbsp;·&nbsp; ".join(facts), color=MUTED, size=13, top=2),
    ]
    # The call comes first. Everything below it is the working.
    if call:
        parts.append(_verdict_banner(call))
    parts.append(_quant_table(score))

    # Hard stops and contradictions come first — they change what everything
    # else is worth.
    for veto in ((score or {}).get("vetoes") or {}).get("triggered", []):
        parts.append(_callout("Hard stop: " + e(veto.get("veto")),
                              e(_n(veto.get("detail"))), BAD))
    for clash in (bundle or {}).get("conflicts", []):
        parts.append(_callout("Sources contradict each other: "
                              + e(clash.get("what")),
                              e(clash.get("means")), BAD))

    flags = []
    for flag in (score or {}).get("flags", []):
        line = f"<b>{e(flag.get('flag'))}</b> — {e(flag.get('detail'))}"
        if flag.get("for_ai"):
            line += (f'<br><span style="color:{MUTED};">put to both models: '
                     f'{e(flag["for_ai"])}</span>')
        flags.append(line)
    if flags:
        parts.append(_para("<b>What our scoring flagged</b>"))
        parts.append(_bullets(flags))

    parts.append(_para("<b>The two analysts, side by side</b>"))
    parts.append(_comparison_table(groq, gemini))

    for gap in (comparison or {}).get("disagreements", []):
        scores = ", ".join(f"{e(k)} {v}" for k, v in (gap.get("scores") or {}).items())
        parts.append(_callout(
            f"The models disagree on {e(gap.get('on', '').replace('_', ' '))} "
            f"— {e(gap.get('gap'))} points apart",
            f"{scores}. {e(gap.get('means'))}", WARN))

    parts.append(_model_detail("Groq", groq, GROQ, GROQ_BG))
    parts.append(_model_detail("Gemini", gemini, GEMINI, GEMINI_BG))

    gaps = [f"{e(item['what'])} — {e(item['why'])}"
            for item in (bundle or {}).get("missing", [])[:6]]
    if gaps:
        parts.append(_para("<b>What we could not establish</b> "
                           "(absent is not the same as bad)", size=13))
        parts.append(_bullets(gaps, color=MUTED))

    return ("".join(parts)
            + f'<hr style="border:none;border-top:1px solid {LINE};margin:26px 0 0;">')



def _overview(companies):
    """
    Every call on one screen, before any of the detail.

    This is the part you read on a phone at a traffic light. Nothing here is a
    number without a word beside it, because "72" tells you nothing on its own.
    """
    rows = [f"<tr>{_head('Company')}{_head('Closes', 'center')}"
            f"{_head('If applying', 'center')}{_head('If holding', 'center')}</tr>"]

    for entry in companies:
        call = entry.get("call") or {}
        listing = call.get("listing") or {}
        longterm = call.get("longterm") or {}

        def pill(body):
            if not body:
                return _cell("—", align="center")
            return (f'<td style="padding:8px 10px;border-bottom:1px solid {LINE};'
                    f'text-align:center;font-family:{FONT};">'
                    f'<span style="display:inline-block;background:'
                    f'{body.get("colour", MUTED)};color:#ffffff;border-radius:999px;'
                    f'padding:3px 11px;font-size:12px;font-weight:700;'
                    f'white-space:nowrap;">{e(body.get("call", "—"))}</span>'
                    f'<div style="font-size:11px;color:{MUTED};padding-top:3px;">'
                    f'{e(_n(body.get("score")))}</div></td>')

        closes = (entry["ipo"].get("dates") or {}).get("close") or "—"
        name = e(entry["ipo"].get("name", ""))
        if (call.get("hard_stops") or []):
            name += (f'<br><span style="color:{BAD};font-size:12px;">hard stop: '
                     f'{e(call["hard_stops"][0])}</span>')
        if call.get("models_disagree"):
            name += (f'<br><span style="color:{WARN};font-size:12px;">'
                     f'the two models disagree</span>')

        rows.append(f"<tr>{_cell(name, width='46%')}"
                    f"{_cell(e(closes), align='center', color=MUTED)}"
                    f"{pill(listing)}{pill(longterm)}</tr>")
    return _table(rows)



def _priority(entry):
    """
    Which write-ups earn the space, best first.

    A decision you have to make in the next few days beats one you do not, and
    an IPO we can actually say something about beats one we cannot. Sorting on
    this rather than alphabetically means the truncation, when it happens,
    removes the least useful thing rather than everything after "P".
    """
    ipo = entry.get("ipo") or {}
    call = entry.get("call") or {}
    score = ((entry.get("score") or {}).get("fundamentals") or {}).get("score")

    has_call = 0 if (call.get("listing") or {}).get("call") not in (
        None, "NOT ENOUGH INFORMATION") else 1
    has_long_call = 0 if (call.get("longterm") or {}).get("call") not in (
        None, "NOT ENOUGH INFORMATION") else 1
    closing = (ipo.get("dates") or {}).get("close") or "9999-99-99"
    urgent = 0 if closing <= _in_days(5) else 1
    flagged = 0 if (call.get("hard_stops") or call.get("models_disagree")) else 1

    return (has_call, urgent, has_long_call, flagged, -(score or 0),
            ipo.get("name", ""))


def _in_days(days):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


# ------------------------------------------------------------ the message

def build(companies, dashboard_url=None) -> tuple:
    """Returns (subject, html, plain_text)."""
    when = storage.now()[:16].replace("T", " ")
    live = [c for c in companies if c["ipo"].get("status") != "closed"]
    # The subject carries the calls themselves. "IPO Radar — 5 tracked" tells
    # you nothing from a phone's lock screen; "Kanohar: Apply" does.
    headline = []
    for entry in companies:
        decision = entry.get("call") or {}
        for view, prefix in (("listing", ""), ("longterm", "hold ")):
            body = (decision.get(view) or {}).get("call")
            if body and body != "NOT ENOUGH INFORMATION":
                short = (entry["ipo"].get("symbol")
                         or entry["ipo"].get("name", "").split()[0])
                headline.append(f"{short}: {prefix}{body.title()}")
                break
    subject = ("IPO Radar — " + "; ".join(headline[:3])
               if headline else f"IPO Radar — {len(companies)} tracked")

    overview = _overview(companies)

    # Detail in order of usefulness, until the budget runs out.
    ordered = sorted(companies, key=_priority)
    blocks, used, shown = [], len(overview), 0
    for entry in ordered:
        piece = company_block(entry["ipo"], entry.get("score"), entry.get("bundle"),
                              (entry.get("ai") or {}).get("groq"),
                              (entry.get("ai") or {}).get("gemini"),
                              (entry.get("ai") or {}).get("comparison"),
                              entry.get("call"))
        if used + len(piece) > BUDGET and shown:
            break
        blocks.append(piece)
        used += len(piece)
        shown += 1

    left_out = len(companies) - shown
    if left_out > 0:
        blocks.append(_para(
            f"<b>{left_out} more IPO{'s' if left_out > 1 else ''}</b> "
            f"{'are' if left_out > 1 else 'is'} in the table above but not "
            f"written up here — this email would be cut off by Gmail past about "
            f"100 KB, and a digest that hides its own ending is worse than a "
            f"shorter one. The full detail for every IPO is always on the "
            f"dashboard.", color=MUTED, size=13))
    blocks = "".join(blocks)

    body = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f7f7f5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="width:100%;background:#f7f7f5;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
       style="width:640px;max-width:100%;background:#ffffff;border:1px solid {LINE};
              border-radius:10px;">
<tr><td style="padding:26px 26px 30px;">
  <h1 style="margin:0;font-family:{FONT};font-size:22px;color:{INK};">IPO Radar</h1>
  {_para(f"{len(companies)} IPOs tracked · {len(live)} still open or upcoming · "
         f"generated {e(when)} UTC", color=MUTED, size=13, top=4)}
  {_para("<b>Read the coverage before the score.</b> Each score is worked out only "
         "over the parts that could be assessed from the filed documents. Anything "
         "missing is reported as missing, never counted as zero. The two AI readings "
         "are shown separately and never averaged — where they disagree, that "
         "disagreement is the finding.", color=MUTED, size=13)}
  {overview}
  {blocks}
  {_para(("Dashboard: <a href='" + e(dashboard_url) + "' style='color:#2f5fd0;'>"
          + e(dashboard_url) + "</a>") if dashboard_url else "", size=13)}
  {_para("Every number here comes from a document this system fetched and parsed "
         "itself, and can be traced to a page. This is not investment advice.",
         color=MUTED, size=12)}
</td></tr></table>
</td></tr></table>
</body></html>"""

    if len(body) > GMAIL_CLIP_BYTES:
        # Should not happen now, but if it ever does, say so in the email
        # itself rather than letting Gmail silently swallow the end.
        body = body.replace("</td></tr></table>\n</td></tr></table>",
                            _para("This digest is unusually long and your mail "
                                  "client may cut it short. Open the dashboard "
                                  "for the complete picture.", color=BAD, size=13)
                            + "</td></tr></table>\n</td></tr></table>", 1)
    return subject, body, plain_text(companies, when, dashboard_url)


def plain_text(companies, when, dashboard_url=None) -> str:
    """The text-only version. Sent alongside, always."""
    lines = [f"IPO RADAR — generated {when} UTC", "=" * 60, ""]
    for entry in companies:
        ipo, score = entry["ipo"], entry.get("score") or {}
        fundamentals = score.get("fundamentals") or {}
        demand = score.get("demand") or {}
        call = entry.get("call") or {}
        lines.append(ipo.get("name", "?"))
        if call:
            lines.append(f"  CALL: applying -> {(call.get('listing') or {}).get('call')}"
                         f"   |   holding -> {(call.get('longterm') or {}).get('call')}")
        lines.append(f"  {ipo.get('type')} · {(ipo.get('dates') or {}).get('open')}"
                     f" to {(ipo.get('dates') or {}).get('close')}"
                     f" · {ipo.get('price_band') or 'band not announced'}")
        lines.append(f"  Fundamentals: {fundamentals.get('score', 'not scored')} "
                     f"({fundamentals.get('coverage_pct', 0)}% assessed)   "
                     f"Demand: {demand.get('score', 'not scored')} "
                     f"({demand.get('coverage_pct', 0)}% assessed)")
        for name in ("groq", "gemini"):
            column = _model_column(((entry.get("ai") or {}).get(name)))
            if not column:
                continue
            if column["state"] != "ok":
                lines.append(f"  {name}: {column['state']} — {column.get('why')}")
                continue
            lines.append(f"  {name}: listing {_n(column['listing'])}, "
                         f"long-term {_n(column['longterm'])}, "
                         f"confidence {column['confidence']}")
        for gap in ((entry.get("ai") or {}).get("comparison") or {}).get(
                "disagreements", []):
            lines.append(f"  !! models disagree on {gap['on']} by {gap['gap']} points")
        for flag in score.get("flags", []):
            lines.append(f"  * {flag.get('flag')}: {flag.get('detail')}")
        lines.append("")
    if dashboard_url:
        lines.append(f"Dashboard: {dashboard_url}")
    lines.append("Not investment advice.")
    return "\n".join(lines)


# ------------------------------------------------------------- writing/sending

def save(subject, body, text):
    os.makedirs(OUT, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", storage.now())[:14]
    for name, content in ((f"{stamp}.html", body), ("latest.html", body),
                          ("latest.txt", text)):
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as handle:
            handle.write(content)
    with open(os.path.join(OUT, "latest-subject.txt"), "w",
              encoding="utf-8") as handle:
        handle.write(subject)
    return os.path.join(OUT, "latest.html")


def recipients(raw: str) -> list:
    """Split a comma or semicolon separated list, drop blanks and duplicates."""
    seen, out = set(), []
    for piece in re.split(r"[,;]", raw or ""):
        address = piece.strip()
        if address and "@" in address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def send(subject, body, text) -> dict:
    """
    Send through Gmail.

    Gmail specifics, all of them deliberate:

      * SMTP over SSL on port 465. Port 587 with STARTTLS also works, but 465 is
        encrypted from the first byte rather than upgrading mid-conversation.
      * An **app password**, never the account password. Gmail refuses the real
        password from a program, and an app password can be revoked on its own
        without touching the account.
      * The From address must be the account that authenticated. Gmail rewrites
        or rejects anything else, so we send as GMAIL_USER and do not pretend
        otherwise.
      * Recipients are passed as a list to sendmail, and also written into the
        To: header, so each person can see it went to both of them rather than
        wondering if it was meant for someone else.
      * multipart/alternative with the plain text FIRST and the HTML second —
        that order is the standard, and mail clients read the last part they
        understand. Getting it backwards is why some digests arrive as raw HTML.

    Never raises. A mail server having a bad day must not fail the collector or
    lose the data it just gathered; the digest is on disk either way.
    """
    user = os.environ.get("GMAIL_USER")
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = recipients(os.environ.get("MAIL_TO") or user or "")

    if not user or not password:
        return {"sent": False,
                "why": "GMAIL_USER / GMAIL_APP_PASSWORD not set — digest written "
                       "to data/email/latest.html instead"}
    if not to:
        return {"sent": False, "why": "MAIL_TO has no usable address"}

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"IPO Radar <{user}>"
    message["To"] = ", ".join(to)
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45) as server:
            server.login(user, password)
            refused = server.sendmail(user, to, message.as_string())
        if refused:
            return {"sent": True, "to": to,
                    "warning": f"some addresses were refused: {refused}"}
        return {"sent": True, "to": to}

    except smtplib.SMTPAuthenticationError as exc:
        return {"sent": False,
                "why": "Gmail rejected the login. This is almost always the app "
                       "password: it must come from the SENDING account, be "
                       "entered without spaces, and 2-step verification must be "
                       "on for that account. "
                       f"({exc.smtp_code})"}
    except smtplib.SMTPRecipientsRefused as exc:
        return {"sent": False, "why": f"every recipient was refused: {exc.recipients}"}
    except Exception as exc:
        return {"sent": False, "why": f"{type(exc).__name__}: {str(exc)[:160]}"}
