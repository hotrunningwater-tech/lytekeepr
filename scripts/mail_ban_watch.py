#!/usr/bin/env python3
"""Hourly (root cron on the server): a new ban on the mail server puts a task on LyteKeepr's Home.

The dovecot and custom jails ban mail apps (IMAP logins): those always make a task. The postfix jail mostly bans internet
bots (well over a hundred a week), so a postfix ban makes a task only when that address has logged in successfully before
(one of our own devices). One task per ban; an address that is unbanned and banned again makes a new one.
"""

import json
import re
import subprocess
from datetime import date
from pathlib import Path

CONTAINER = "mailserver"
JAILS = ("dovecot", "custom", "postfix")
STATE = Path("/opt/mailserver/ban-watch/notified.json")
APP_DIR = "/opt/lytekeepr-calendar/app"
APP_PYTHON = "/opt/lytekeepr-calendar/venv/bin/python"


def sh(*cmd) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def banned(jail: str) -> list:
    out = sh("docker", "exec", CONTAINER, "fail2ban-client", "status", jail)
    m = re.search(r"Banned IP list:\s*(.*)", out)
    return m.group(1).split() if m else []


def mail_log() -> str:
    return sh("docker", "exec", CONTAINER, "sh", "-c", "zcat -f /var/log/mail/mail.log*")


def ever_logged_in(ip: str, log: str) -> bool:
    return bool(re.search(rf"Login: user=<[^>]*>.*rip={re.escape(ip)},", log)
                or re.search(rf"client=\S*\[{re.escape(ip)}\], sasl_method=", log))


def failed_users(ip: str, log: str) -> dict:
    users = {}
    for m in re.finditer(rf"auth failed, (\d+) attempts?.*?user=<([^>]*)>.*?rip={re.escape(ip)},", log):
        users[m.group(2) or "(no name)"] = users.get(m.group(2) or "(no name)", 0) + int(m.group(1))
    for m in re.finditer(rf"\[{re.escape(ip)}\]: SASL \S+ authentication failed.*?(?:sasl_username=(\S+))?$", log, re.M):
        users[m.group(1) or "(no name)"] = users.get(m.group(1) or "(no name)", 0) + 1
    return users


def where(ip: str) -> str:
    if ip.startswith("192.197.121."):
        return " (McGill Wi-Fi)"
    return ""


def add_task(title: str, notes: str) -> None:
    code = ("import json,sys; from lk_calendar import store; d=json.loads(sys.argv[1]); "
            "store.create_event({**d, 'project': store.default_project('nexy')})")
    data = {"title": title, "notes": notes, "kind": "task", "start_date": date.today().isoformat(), "untimed": 1}
    r = subprocess.run(["sudo", "-u", "lkcal", "--preserve-env=PATH", APP_PYTHON, "-c", code, json.dumps(data)],
                       cwd=APP_DIR, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-500:])


def main() -> None:
    notified = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    now = {f"{jail} {ip}" for jail in JAILS for ip in banned(jail)}
    new = sorted(now - notified)
    log = mail_log() if new else ""
    done = set()
    for key in new:
        jail, ip = key.split(" ", 1)
        if jail == "postfix" and not ever_logged_in(ip, log):
            done.add(key)                                       # an internet bot: no task
            continue
        users = failed_users(ip, log)
        tried = ", ".join(f"'{u}' {n}x" for u, n in sorted(users.items(), key=lambda x: -x[1])) or "unknown"
        add_task(f"Mail server blocked {ip}{where(ip)}: failed logins as {tried}",
                 f"Blocked for 1 week by the {jail} rule after repeated failed logins.\n"
                 f"Logins tried: {tried}.\nIf this is one of your devices, ask Claude Code to unban {ip}.")
        done.add(key)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(sorted((notified | done) & now)))


if __name__ == "__main__":
    main()
