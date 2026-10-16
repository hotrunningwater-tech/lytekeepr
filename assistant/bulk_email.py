"""Chat-driven funnel email: one draft written once, sent one by one to everyone in a funnel stage.

Same safety pattern as the single email draft: the AI tools only STAGE a batch (funnel stage + subject + body template) in a local
file. Three steps, each needing the user:
  1. "template": the one email is shown in the chat and the user edits it, then approves it;
  2. "review":   every person's copy is shown in a popup and each can be edited on its own, then the user approves the send;
  3. sending:    one email at a time with a pause between (/api/email/bulk-pending/send in server.py).

The template greets with {{first_name}}: each person gets their own first name; a contact with no real name gets just "Hi,".
Sending runs in a background thread, one email at a time with a pause between, and reports progress to the popup.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crm import db as crm_db
from profiles.store import profile_component_dir

TTL_MINUTES = 24 * 60
MAX_RECIPIENTS = 100            # one batch; a person's Gmail account has daily sending limits
PAUSE_SECONDS = 10              # between two emails (the popup can choose another)
MIN_PAUSE, MAX_PAUSE = 5, 120
NAME_TOKEN = re.compile(r"[ \t]*\{\{first_name\}\}")
# What the AI may write for "the recipient's name". It is turned into {{first_name}} BEFORE the user's own name is filled in
# (the sign-off code treats a plain {{name}} as the sender's name).
_AI_NAME_FORMS = re.compile(r"\{\{\s*(?:first[_ ]?)?name\s*\}\}|\[\s*(?:recipient'?s?\s*)?(?:first\s+)?name\s*\]|\[\s*insert\s+name\s*\]", re.IGNORECASE)

_lock = threading.Lock()


def _pending_path() -> Path:
    return profile_component_dir("assistant-state") / "pending-bulk-email.json"


def _job_path() -> Path:
    return profile_component_dir("assistant-state") / "bulk-email-job.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


# ------------------------------------------------------------------ the people in a funnel
def _real_first_name(name: str, company: str, email: str) -> str:
    """The first name of a contact, or '' when the contact has no real name (an empty name, or one that is only the company, an email or a link)."""
    n = (name or "").strip()
    if not n or n.lower() in ("no name", "n/a", "-"):
        return ""
    if "@" in n or "/" in n or n.lower().startswith(("name:", "http")):
        return ""
    if company and n.lower() == company.strip().lower():
        return ""
    if email and n.lower() == email.split("@")[0].lower():
        return ""
    if "," in n:                                   # "Cruz, Ana" -> Ana
        n = n.split(",", 1)[1].strip() or n
    words = [w.strip(".") for w in n.split() if w.strip(".")]
    while len(words) > 1 and words[0].lower() in ("dr", "mr", "mrs", "ms", "mx", "prof", "sir"):
        words.pop(0)                               # "Dr. Smith John" -> the title is not a first name
    first = words[0] if words else ""
    if first.lower() in ("dr", "mr", "mrs", "ms", "mx", "prof", "sir"):
        return ""
    return first[:1].upper() + first[1:] if first else ""


def _stage_people(conn, funnel_id: int, stage_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT p.person_id, p.name, co.company_name,
                  (SELECT ca.channel_identifier FROM channel_accounts ca
                    WHERE ca.person_id = p.person_id AND ca.channel = 'email' ORDER BY ca.account_id LIMIT 1) AS email,
                  (SELECT MAX(m.timestamp) FROM messages m WHERE m.person_id = p.person_id AND m.channel = 'email' AND m.direction = 'outgoing') AS last_emailed
           FROM people p LEFT JOIN companies co ON co.company_id = p.company_id
           WHERE p.funnel_id = ? AND p.funnel_stage_id = ? ORDER BY p.name""", (funnel_id, stage_id)).fetchall()
    return [dict(r) for r in rows]


def funnel_overview(funnel_name: str = "") -> dict:
    """Every funnel (or the one matching the name) with how many people sit in each stage and how many of them have an email address."""
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        funnels = crm_db.list_funnels(conn)
        typed = (funnel_name or "").strip().lower()
        if typed:
            funnels = [f for f in funnels if typed in f["name"].lower()]
        if not funnels:
            return {"status": "not_found", "message": f"No funnel matches '{funnel_name}'." if typed else "There are no funnels yet."}
        out = []
        for f in funnels:
            stages = []
            for st in crm_db.list_funnel_stages(conn, f["funnel_id"]):
                people = _stage_people(conn, f["funnel_id"], st["stage_id"])
                stages.append({"stage": st["name"], "contacts": len(people), "with_email": sum(1 for p in people if p["email"])})
            out.append({"funnel": f["name"], "stages": stages})
        return {"status": "found", "funnels": out}
    finally:
        conn.close()


# ------------------------------------------------------------------ staging
def render(template: str, first_name: str) -> str:
    """The template for one person: {{first_name}} becomes their first name; with no name the greeting loses it ("Hi {{first_name}}," -> "Hi,")."""
    return NAME_TOKEN.sub(lambda m: (" " + first_name) if first_name else "", template or "")


def _build(funnel_id: int, stage_id: int) -> tuple[list[dict], list[dict]]:
    conn = crm_db.get_connection()
    try:
        people = _stage_people(conn, funnel_id, stage_id)
    finally:
        conn.close()
    recipients, skipped = [], []
    for p in people:
        label = p["name"] or p["company_name"] or "Unnamed contact"
        if not p["email"]:
            skipped.append({"person_id": p["person_id"], "name": label, "reason": "no email address"})
            continue
        recipients.append({"person_id": p["person_id"], "name": label, "email": p["email"],
                           "first_name": _real_first_name(p["name"], p["company_name"] or "", p["email"]),
                           "last_emailed": p["last_emailed"] or "", "included": True, "override": None})
    return recipients, skipped


def create_bulk(funnel_name: str, stage_name: str, subject: str, body: str) -> dict:
    """Stage one email for everyone in a funnel stage. Nothing is sent; the user reviews it in a popup and sends it from there."""
    if not (body or "").strip():
        return {"status": "invalid", "message": "The email needs text."}
    if _job_running():
        return {"status": "busy", "message": "A batch is still being sent. Wait for it to finish first."}
    crm_db.init_db()
    conn = crm_db.get_connection()
    try:
        typed = (funnel_name or "").strip().lower()
        funnels = [f for f in crm_db.list_funnels(conn) if typed and typed in f["name"].lower()]
        exact = [f for f in funnels if f["name"].lower() == typed]
        funnels = exact or funnels
        if not funnels:
            return {"status": "not_found", "message": f"No funnel matches '{funnel_name}'."}
        if len(funnels) > 1:
            return {"status": "ambiguous", "message": "More than one funnel matches: " + ", ".join(f["name"] for f in funnels) + ". Ask which one."}
        funnel = funnels[0]
        stages = crm_db.list_funnel_stages(conn, funnel["funnel_id"])
    finally:
        conn.close()
    want = (stage_name or "").strip().lower()
    hit = [s for s in stages if s["name"].lower() == want] or [s for s in stages if want and want in s["name"].lower()]
    if len(hit) != 1:
        return {"status": "not_found" if not hit else "ambiguous",
                "message": f"Stage '{stage_name}' not found in {funnel['name']}. Stages: " + ", ".join(s["name"] for s in stages) + "."}
    stage = hit[0]
    recipients, skipped = _build(funnel["funnel_id"], stage["stage_id"])
    if not recipients:
        return {"status": "empty", "message": f"Nobody in {funnel['name']} / {stage['name']} has an email address" + (f" ({len(skipped)} contacts have none)." if skipped else " (the stage is empty).")}
    if len(recipients) > MAX_RECIPIENTS:
        return {"status": "too_many", "message": f"{len(recipients)} people have an email in that stage; one batch is limited to {MAX_RECIPIENTS}. Ask the user to split the stage first."}
    try:
        from lk_calendar import profile
        subject, body = _AI_NAME_FORMS.sub("{{first_name}}", subject or ""), _AI_NAME_FORMS.sub("{{first_name}}", body)
        subject, body = profile.sign(subject), profile.sign(body)
    except Exception:
        pass
    created = _now()
    payload = {"type": "bulk_email", "id": secrets.token_hex(4), "phase": "template", "funnel_id": funnel["funnel_id"], "funnel_name": funnel["name"],
               "stage_id": stage["stage_id"], "stage_name": stage["name"], "subject": (subject or "").strip(), "body": body.strip(),
               "recipients": recipients, "skipped": skipped, "createdAt": created.isoformat(),
               "expiresAt": (created + timedelta(minutes=TTL_MINUTES)).isoformat()}
    _save(_pending_path(), payload)
    try:
        from assistant import funnel_flow
        funnel_flow.clear()
    except Exception:
        pass
    return {"status": "pending_confirmation", "recipients": len(recipients), "skipped_without_email": len(skipped),
            "message": f"The email is now shown in the chat for the user to adjust and approve; it will go to {len(recipients)} people ({len(skipped)} contacts skipped: no email address). "
                       "After they approve it they see every person's copy and can edit each one, then approve the send. Nothing has been sent and nothing sends until they press the send button."}


def load_pending() -> dict | None:
    payload = _load(_pending_path())
    if not payload:
        return None
    try:
        if datetime.fromisoformat(payload["expiresAt"]) <= _now():
            clear_pending()
            return None
    except (KeyError, ValueError):
        clear_pending()
        return None
    return payload


def clear_pending() -> None:
    try:
        _pending_path().unlink()
    except FileNotFoundError:
        pass


def _out(p: dict, r: dict) -> tuple[str, str]:
    """The subject and text this person will get: their own edited copy if they have one, else the template for them."""
    o = r.get("override")
    if o:
        return o.get("subject", ""), o.get("body", "")
    return render(p["subject"], r["first_name"]), render(p["body"], r["first_name"])


def preview() -> dict | None:
    """The staged batch as the popup shows it: every person's own subject and text, exactly as they will be sent."""
    p = load_pending()
    if not p:
        return None
    recipients = []
    for r in p["recipients"]:
        subject, body = _out(p, r)
        recipients.append({**r, "subject_out": subject, "body_out": body, "edited": bool(r.get("override"))})
    return {**{k: p[k] for k in ("id", "funnel_name", "stage_name", "subject", "body", "skipped")}, "phase": p.get("phase", "template"), "recipients": recipients}


def update(subject: str, body: str, excluded: list, overrides: dict | None = None) -> dict | None:
    """The user edited the template, ticked people off, or edited one person's own copy (overrides: {person_id: {subject, body}})."""
    p = load_pending()
    if not p:
        return None
    if subject is not None:
        p["subject"] = (subject or "").strip()
    if body is not None:
        p["body"] = (body or "").rstrip()
    if excluded is not None:
        gone = {int(i) for i in excluded}
        for r in p["recipients"]:
            r["included"] = r["person_id"] not in gone
    if overrides is not None:
        for r in p["recipients"]:
            o = overrides.get(str(r["person_id"]))
            if o is None:
                continue
            same = (o.get("subject", "").strip() == render(p["subject"], r["first_name"]).strip()
                    and o.get("body", "").strip() == render(p["body"], r["first_name"]).strip())
            r["override"] = None if same else {"subject": o.get("subject", "").strip(), "body": o.get("body", "").rstrip()}
    _save(_pending_path(), p)
    return preview()


def set_phase(phase: str) -> dict | None:
    """'review' = the user approved the one email and now checks every copy; 'template' = back to editing the one email."""
    p = load_pending()
    if not p:
        return None
    p["phase"] = "review" if phase == "review" else "template"
    _save(_pending_path(), p)
    return preview()


# ------------------------------------------------------------------ sending
def _job_running() -> bool:
    job = _load(_job_path())
    if not job or job.get("status") != "running":
        return False
    return time.time() - job.get("heartbeat", 0) < 90        # a job whose server restarted stops beating: it is not running any more


def job_status() -> dict | None:
    job = _load(_job_path())
    if job and job.get("status") == "running" and time.time() - job.get("heartbeat", 0) >= 90:
        job["status"] = "interrupted"
    return job


def start_send(batch_id: str, pause: int | None = None) -> dict:
    """Begin sending the staged batch (only ever called from the popup's send button, after the user approved every copy)."""
    with _lock:
        p = load_pending()
        if not p or p["id"] != batch_id:
            return {"status": "no_pending", "message": "This batch is no longer available."}
        if p.get("phase") != "review":
            return {"status": "not_reviewed", "message": "Approve the email and review every copy first."}
        if _job_running():
            return {"status": "busy", "message": "A batch is already being sent."}
        todo = [r for r in p["recipients"] if r.get("included", True)]
        if not todo:
            return {"status": "empty", "message": "Nobody is ticked."}
        try:
            gap = max(MIN_PAUSE, min(MAX_PAUSE, int(pause))) if pause is not None else PAUSE_SECONDS
        except (TypeError, ValueError):
            gap = PAUSE_SECONDS
        job = {"id": p["id"], "status": "running", "funnel_name": p["funnel_name"], "stage_name": p["stage_name"], "heartbeat": time.time(),
               "total": len(todo), "pause": gap,
               "results": [{"person_id": r["person_id"], "name": r["name"], "email": r["email"], "status": "waiting", "message": ""} for r in todo]}
        _save(_job_path(), job)
        clear_pending()
        from profiles.store import get_active_profile_id
        threading.Thread(target=_run_in, args=(get_active_profile_id(), job, p, todo, gap), name="bulk-email-send", daemon=True).start()
        return {"status": "started", "total": len(todo), "pause": gap}


def _run_in(profile_id: str, *args) -> None:
    """A new thread does not know which workspace it belongs to: it works inside the workspace that started the batch
    (its Gmail, its contacts, its job file)."""
    from profiles.store import use_profile
    with use_profile(profile_id):
        _run(*args)


def _run(job: dict, p: dict, todo: list[dict], gap: int) -> None:
    from assistant import email_send_flow
    for i, r in enumerate(todo):
        entry = job["results"][i]
        subject, body = _out(p, r)
        try:
            res = email_send_flow.send_email(r["person_id"], r["name"], r["email"], subject, body)
            entry["status"], entry["message"] = res["status"], res["message"]
        except Exception as exc:                 # one failure never stops the rest
            entry["status"], entry["message"] = "failed", f"Not sent to {r['name']} — {exc}"
        job["heartbeat"] = time.time()
        _save(_job_path(), job)
        if i < len(todo) - 1:
            for _ in range(gap):                 # the pause, in one-second steps so the job keeps showing it is alive
                time.sleep(1)
                job["heartbeat"] = time.time()
            _save(_job_path(), job)
    job["status"] = "done"
    job["heartbeat"] = time.time()
    _save(_job_path(), job)
