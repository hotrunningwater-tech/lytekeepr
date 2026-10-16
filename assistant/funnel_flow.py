"""The funnel-email conversation, driven by code so it never depends on the AI choosing to ask the right thing.

Step 1  the user mentions a funnel and an email      -> list the funnels, ask which one
Step 2  they name a funnel                           -> list its stages with counts, ask which stage
Step 3  they name a stage                            -> say how many will get it, ask what it should say
Step 4  they give the wording (or "write it")        -> the AI calls draft_funnel_email (it is told exactly which funnel and stage)
The state lives in a small file for 30 minutes. Everything before step 4 is a plain code answer; no AI call is made.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from profiles.store import profile_component_dir

TTL_SECONDS = 30 * 60
_FUNNEL_WORDS = ("funnel", "funnels", "pipeline", "pipelines")
_ACTION_WORDS = ("email", "emails", "mail", "message", "messages", "msg", "write", "send", "draft", "compose", "prepare", "outreach")


def _has_word(text: str, targets: tuple, cutoff: float = 0.78) -> bool:
    """True when some word of the text is one of the target words, typos allowed ("funnle", "mesage", "emial")."""
    from difflib import get_close_matches
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in targets or (len(w) >= 4 and get_close_matches(w, targets, n=1, cutoff=cutoff)):
            return True
    return False


_STOP = {"the", "a", "an", "of", "my", "in", "to", "for", "stage", "funnel", "and", "is", "it", "this", "that", "one", "please", "pls"}


def _path() -> Path:
    return profile_component_dir("assistant-state") / "funnel-email-intent.json"


def load() -> dict | None:
    try:
        p = _path()
        data = json.loads(p.read_text()) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None
    if not data or time.time() - data.get("at", 0) > TTL_SECONDS:
        return None
    return data


def _save(state: dict) -> None:
    state["at"] = time.time()
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))


def clear() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP}


def _match(text: str, names: list[str]) -> str | None:
    """The one name the text refers to (all its words appear, or the text is that name with a typo); None when none or several fit."""
    low = (text or "").lower()
    typed = _words(text)
    hits = []
    for n in names:
        nw = _words(n)
        if nw and nw <= typed:
            hits.append(n)
    if not hits and typed:                          # a part of the name: "nexy" for "Nexy Pilot" (only when exactly one name fits)
        part = [n for n in names if typed <= _words(n)]
        if len(part) == 1:
            hits = part
    if not hits:
        from difflib import get_close_matches
        close = get_close_matches(low.strip(" .!?"), [n.lower() for n in names], n=2, cutoff=0.8)
        hits = [n for n in names if n.lower() in close]
    if len(hits) > 1:                              # "new lead" and "new lead 2": prefer the longest name that fits
        hits.sort(key=lambda n: -len(n))
        if len(_words(hits[0])) > len(_words(hits[1])):
            hits = hits[:1]
    return hits[0] if len(hits) == 1 else None


def _funnels() -> list[dict]:
    from assistant import bulk_email
    o = bulk_email.funnel_overview()
    return o["funnels"] if o.get("status") == "found" else []


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {'people' if word == 'person' else word + 's'}"


def _ask_funnel(funnels: list[dict]) -> str:
    lines = [f"- {f['funnel']} ({_plural(sum(s['with_email'] for s in f['stages']), 'person')} with an email address)" for f in funnels]
    return "Which funnel?\n\n" + "\n".join(lines)


def _stage_lines(f: dict) -> str:
    """Only stages that have someone to email; empty ones are not listed."""
    return "\n".join(f"- {s['stage']}: {_plural(s['contacts'], 'contact')} ({s['with_email']} with an email address)" for s in f["stages"] if s["with_email"])


def _ask_stage(f: dict) -> str:
    lines = _stage_lines(f)
    if not lines:
        clear()
        return f"Nobody in {f['funnel']} has an email address yet, so there is nobody to email."
    return f"{f['funnel']}. Which stage do you want to email?\n\n" + lines


def _emailable(funnels: list[dict]) -> list[dict]:
    """Only funnels with someone to email count as options; the rest are not offered."""
    return [f for f in funnels if any(st["with_email"] for st in f["stages"])]


def _chosen(f: dict, st: dict, lead: str = "") -> str:
    return (f"{lead}{f['funnel']} / {st['stage']}: {_plural(st['with_email'], 'person')} will get it ({st['contacts'] - st['with_email']} skipped, no email address).\n\n"
            "What should the email say? Tell me the idea, or say \"write it\" and I'll write a short one. Each person gets their own copy with their first name.")


def route(history: list[dict]) -> str | None:
    """A ready code answer for the latest user message, or None when the AI should handle it (including step 4).
    A question with only one possible answer is not asked: the single funnel, or the single stage with people, is taken."""
    if not history or history[-1].get("role") != "user":
        return None
    text = history[-1].get("content") or ""
    state = load()
    wants = bool(_has_word(text, _FUNNEL_WORDS) and _has_word(text, _ACTION_WORDS) and "linkedin" not in text.lower())
    all_funnels = _funnels()
    funnels = _emailable(all_funnels)
    if not funnels:
        if wants:
            return "You have no funnels yet. Create one in the CRM tab first." if not all_funnels else "Nobody in any of your funnels has an email address yet, so there is nobody to email."
        return None
    if wants:                                       # a new request always starts over; an old choice is never reused
        state = {"funnel": None, "stage": None}
        _save(state)
    if not state:
        return None
    if state.get("stage"):
        return None                                 # step 4 belongs to the AI (see context_message)
    lead = ""
    if not state.get("funnel"):
        name = _match(text, [f["funnel"] for f in funnels])
        if not name and len(funnels) == 1:
            name = funnels[0]["funnel"]
            lead = f"Only {name} has people to email. "
        if not name:
            return _ask_funnel(funnels) if wants or len(text.split()) <= 6 else None
        state["funnel"] = name
        _save(state)
    f = next((x for x in funnels if x["funnel"] == state["funnel"]), None)
    if not f:
        clear()
        return _ask_funnel(funnels)
    options = [st for st in f["stages"] if st["with_email"]]
    stage = _match(text, [st["stage"] for st in f["stages"]]) if not wants or len(text.split()) > 2 else None
    if not stage and len(options) == 1:
        stage = options[0]["stage"]
        lead = lead + "Only one stage has people. "
    if not stage:
        return _ask_stage(f)
    st = next(x for x in f["stages"] if x["stage"] == stage)
    if not st["with_email"]:
        return f"Nobody in {f['funnel']} / {stage} has an email address ({_plural(st['contacts'], 'contact')} in the stage). Pick another stage:\n\n" + _stage_lines(f)
    state["stage"] = stage
    _save(state)
    return _chosen(f, st, lead)


def context_message() -> dict | None:
    """For step 4: tells the AI exactly what to do with the user's next message."""
    state = load()
    if not state or not state.get("stage"):
        return None
    return {"role": "system", "content": (
        f"The user is writing ONE email for everyone with an email address in funnel \"{state['funnel']}\", stage \"{state['stage']}\" (already chosen). "
        "Their next message is the wording or 'write it'. Do not ask any clarifying question (not about group email, CC, threads or recipients: each person gets "
        "their own copy). Call draft_funnel_email NOW with funnel_name, stage_name, a subject, and a body that starts with 'Hi {{first_name}},'. If they gave no "
        "wording, write a short, friendly one from what you know of them. Then tell them, in one or two short sentences, that the email is shown above to adjust and approve, that they will then see every person's copy, and that nothing is sent until they approve the send.")}
