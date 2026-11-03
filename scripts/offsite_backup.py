#!/usr/bin/env python3
"""Nightly copy of all LyteKeepr data to Backblaze, so that losing this server does not lose the data. Runs as root on the server
(see /etc/cron.d/lytekeepr-offsite-backup; a copy of this file lives at /opt/lytekeepr-calendar/offsite_backup.py, the deploy does not
touch it). No extra software: Python and openssl.

    offsite_backup.py              make a copy, encrypt it here, upload, check Backblaze got it exactly, remove copies past KEEP_DAYS
    offsite_backup.py verify       download the newest copy, decrypt it and check every database in it opens and is sound
    offsite_backup.py restore DIR  download and decrypt the newest copy into DIR, for a real restore (docs/BACKUPS.md)

In a copy: every workspace (databases, saved emails, Google connections, recordings), the directory, and the settings file, whose key
is needed to read the Google connections. Not in a copy: workspaces already deleted or closed, so they are erased on schedule. Copies
are encrypted before they leave, with a password only this server and the owner's password manager have: without it a copy
cannot be read, by Backblaze or anyone else. Credentials: /opt/lytekeepr-calendar/backblaze.env (root only).

The outcome goes to a status file the app reads (lk_calendar/backup_watch.py): it tells the owner when a night fails or does not run."""

import base64
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.getenv("LK_OFFSITE_ROOT", "/opt/lytekeepr-calendar"))
CREDENTIALS = ROOT / "backblaze.env"
STATUS = ROOT / "offsite-status.json"
KEEP_DAYS = 14                  # the same as the nightly copies on the server, and what the privacy page promises
PREFIX = "lytekeepr-"
SUFFIX = ".tar.gz.enc"
WEB_TIMEOUT = 600


def settings() -> dict:
    out = {}
    for line in CREDENTIALS.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ---- making a copy
def stage(root: Path, dest: Path) -> dict:
    """Everything worth keeping, in dest. Databases through SQLite's own backup, so it is safe while the app runs. Returns counts."""
    profiles = root / "app" / "profiles"
    counts = {"databases": 0, "files": 0}
    pairs = [(profiles / "data", dest / "profiles" / "data")]
    for tree_src, tree_dest in pairs:
        for path in sorted(tree_src.rglob("*")) if tree_src.exists() else []:
            if not path.is_file() or path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            _copy(path, tree_dest / path.relative_to(tree_src), counts)
    for name in ("directory.db", "registry.json", "active-profile.json"):
        if (profiles / name).exists():
            _copy(profiles / name, dest / "profiles" / name, counts)
    if (root / "lytekeepr.env").exists():
        _copy(root / "lytekeepr.env", dest / "lytekeepr.env", counts)
    if not counts["databases"]:
        raise RuntimeError("no databases found to copy")
    return counts


def _copy(src: Path, target: Path, counts: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".db":
        from_db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        to_db = sqlite3.connect(target)
        from_db.backup(to_db)
        to_db.close()
        from_db.close()
        _check_db(target)
        counts["databases"] += 1
    else:
        shutil.copy2(src, target)
        counts["files"] += 1


def _check_db(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"database failed its check: {path.name}")


def pack(folder: Path, out: Path, password: str) -> None:
    """A compressed archive of folder, encrypted on the way out. The password goes through a private pipe, never the command line."""
    read_end, write_end = os.pipe()
    with os.fdopen(write_end, "w") as w:
        w.write(password)
    try:
        proc = subprocess.Popen(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", f"fd:{read_end}", "-out", str(out)],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(read_end,))
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|gz") as tar:
                tar.add(folder, arcname="lytekeepr")
            proc.stdin.close()
            err = proc.stderr.read()
            if proc.wait() != 0:
                raise RuntimeError(f"encrypting failed: {err[:200]!r}")
        finally:
            _finish(proc)
    finally:
        os.close(read_end)


def unpack(archive: Path, dest: Path, password: str) -> None:
    read_end, write_end = os.pipe()
    with os.fdopen(write_end, "w") as w:
        w.write(password)
    try:
        proc = subprocess.Popen(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", f"fd:{read_end}", "-in", str(archive)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(read_end,))
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:
                tar.extractall(dest, filter="data")
            err = proc.stderr.read()
            if proc.wait() != 0:
                raise RuntimeError(f"decrypting failed: {err[:200]!r}")
        finally:
            _finish(proc)
    finally:
        os.close(read_end)


def _finish(proc) -> None:
    """openssl is never left running, and its pipes are closed, when packing or unpacking stops halfway."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream:
            stream.close()


def check_copy(folder: Path) -> dict:
    """A restored copy is sound: the expected parts are there and every database passes its check."""
    top = folder / "lytekeepr"
    dbs = sorted(top.rglob("*.db"))
    if not dbs or not (top / "profiles" / "directory.db").exists() or not (top / "lytekeepr.env").exists():
        raise RuntimeError("the copy is missing the directory or the settings file")
    for db in dbs:
        _check_db(db)
    return {"databases": len(dbs), "files": sum(1 for p in top.rglob("*") if p.is_file())}


def to_remove(names: list, now: datetime.datetime, keep_days: int = KEEP_DAYS) -> list:
    """Copies past keep_days. The newest always stays, whatever its age, and a name we did not make is never touched."""
    ours = []
    for name in names:
        if name.startswith(PREFIX) and name.endswith(SUFFIX):
            try:
                ours.append((datetime.datetime.strptime(name[len(PREFIX):-len(SUFFIX)], "%Y%m%d-%H%M%S"), name))
            except ValueError:
                pass
    ours.sort()
    return [name for when, name in ours[:-1] if now - when > datetime.timedelta(days=keep_days)]


# ---- Backblaze
class Backblaze:
    def __init__(self, key_id: str, key: str, bucket: str):
        basic = base64.b64encode(f"{key_id}:{key}".encode()).decode()
        info = self._web("https://api.backblazeb2.com/b2api/v3/b2_authorize_account", headers={"Authorization": "Basic " + basic})
        self.token = info["authorizationToken"]
        storage = info["apiInfo"]["storageApi"]
        self.api = storage["apiUrl"]
        self.download = storage["downloadUrl"]
        self.bucket = bucket
        self.bucket_id = self.call("b2_list_buckets", {"accountId": info["accountId"], "bucketName": bucket})["buckets"][0]["bucketId"]

    @staticmethod
    def _web(url, headers=None, body=None, method=None):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=WEB_TIMEOUT) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as exc:          # the reply's code only: nothing secret goes in a log
            raise RuntimeError(f"Backblaze refused ({exc.code}) {url.rsplit('/', 1)[-1]}") from None

    def call(self, name, body):
        return self._web(f"{self.api}/b2api/v3/{name}", {"Authorization": self.token, "Content-Type": "application/json"}, json.dumps(body).encode())

    def upload(self, path: Path) -> dict:
        place = self.call("b2_get_upload_url", {"bucketId": self.bucket_id})
        data = path.read_bytes()
        sha1 = hashlib.sha1(data).hexdigest()
        got = self._web(place["uploadUrl"], {"Authorization": place["authorizationToken"], "X-Bz-File-Name": urllib.parse.quote(path.name), "Content-Type": "application/octet-stream",
                                             "Content-Length": str(len(data)), "X-Bz-Content-Sha1": sha1}, data)
        if got.get("contentSha1") != sha1 or got.get("contentLength") != len(data):
            raise RuntimeError("Backblaze did not get the copy exactly")
        return got

    def files(self) -> list:
        out, start = [], None
        while True:
            body = {"bucketId": self.bucket_id, "maxFileCount": 1000, "prefix": PREFIX}
            if start:
                body["startFileName"] = start
            page = self.call("b2_list_file_names", body)
            out += page["files"]
            start = page.get("nextFileName")
            if not start:
                return out

    def fetch(self, name: str, out: Path) -> None:
        url = f"{self.download}/file/{urllib.parse.quote(self.bucket)}/{urllib.parse.quote(name)}"
        req = urllib.request.Request(url, headers={"Authorization": self.token})
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT) as res, open(out, "wb") as f:
            shutil.copyfileobj(res, f)

    def remove(self, name: str, file_id: str) -> None:
        self.call("b2_delete_file_version", {"fileName": name, "fileId": file_id})


def connect(cfg: dict) -> Backblaze:
    return Backblaze(cfg["B2_KEY_ID"], cfg["B2_APP_KEY"], cfg["B2_BUCKET"])


def save_status(**fields) -> None:
    try:
        status = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        status = {}
    status.update(fields)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    tmp.chmod(0o644)               # no secrets in it; the app reads it
    tmp.replace(STATUS)


def now_text() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def log(text: str) -> None:
    print(f"{datetime.datetime.now():%F %T} {text}", flush=True)


def make_copy() -> None:
    cfg = settings()
    with tempfile.TemporaryDirectory(prefix="lk-offsite-", dir=ROOT) as tmp:
        os.chmod(tmp, 0o700)
        stage_dir, name = Path(tmp) / "stage", f"{PREFIX}{datetime.datetime.now(datetime.timezone.utc):%Y%m%d-%H%M%S}{SUFFIX}"
        counts = stage(ROOT, stage_dir)
        archive = Path(tmp) / name
        pack(stage_dir, archive, cfg["BACKUP_PASSWORD"])
        b2 = connect(cfg)
        got = b2.upload(archive)
        files = b2.files()
        ids = {f["fileName"]: f["fileId"] for f in files}
        gone = to_remove(list(ids), datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
        for old in gone:
            b2.remove(old, ids[old])
    save_status(ok=True, at=now_text(), file=name, bytes=got["contentLength"], error="")
    log(f"OK {name}: {counts['databases']} databases, {counts['files']} files, {got['contentLength']} bytes; {len(gone)} old copies removed")


def newest(b2: Backblaze) -> str:
    names = sorted(f["fileName"] for f in b2.files() if f["fileName"].endswith(SUFFIX))
    if not names:
        raise RuntimeError("no copies on Backblaze")
    return names[-1]


def verify() -> None:
    cfg = settings()
    b2 = connect(cfg)
    name = newest(b2)
    with tempfile.TemporaryDirectory(prefix="lk-offsite-", dir=ROOT) as tmp:
        os.chmod(tmp, 0o700)
        archive = Path(tmp) / name
        b2.fetch(name, archive)
        unpack(archive, Path(tmp), cfg["BACKUP_PASSWORD"])
        result = check_copy(Path(tmp))
    save_status(verified_ok=True, verified_at=now_text(), verified_file=name, verified_error="")
    log(f"VERIFIED {name}: {result['databases']} databases sound, {result['files']} files")


def restore(dest: str) -> None:
    cfg = settings()
    b2 = connect(cfg)
    name = newest(b2)
    target = Path(dest)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    archive = target / name
    b2.fetch(name, archive)
    unpack(archive, target, cfg["BACKUP_PASSWORD"])
    archive.unlink()
    log(f"RESTORED {name} into {target / 'lytekeepr'}: {check_copy(target)}")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "backup"
    try:
        if command == "backup":
            make_copy()
        elif command == "verify":
            verify()
        elif command == "restore" and len(sys.argv) == 3:
            restore(sys.argv[2])
        else:
            print(__doc__)
            return 2
    except Exception as exc:  # noqa: BLE001 - every failure must reach the status file, so the owner hears about it
        text = f"{type(exc).__name__}: {exc}"[:300]
        log(f"FAILED {command}: {text}")
        if command == "backup":
            save_status(ok=False, at=now_text(), error=text)
        elif command == "verify":
            save_status(verified_ok=False, verified_at=now_text(), verified_error=text)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
