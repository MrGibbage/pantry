"""Receipt photo uploads, home network only.

Photos land in the grocery repo's receipts/inbox (mounted at /inbox), where `/receipt` picks
them up exactly like Taildropped ones. Receipt photos show the card's last 4 digits and approval
codes, and the grocery project's rule is that they never leave the house. So uploads are refused
on any request that came through Cloudflare: only the LAN path (phone -> Caddy on OPNsense ->
docker-server) is allowed.

Photos shared into Pantry from another app arrive as a plain form POST (no X-Pantry header), so
they're parked in /data/pending first and only moved to the inbox when someone taps "Add" on
the confirm page, which is a normal X-Pantry API call. A cross-site form can't do that step.
"""
import os
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

INBOX = Path(os.environ.get("PANTRY_RECEIPT_INBOX", "/inbox"))
# Pending shares live inside the inbox (hidden) rather than in /data: /srv is in the off-site
# restic sweep, and receipt photos must never leave the house. The inbox is backed up only by
# the Proxmox VM backup, like the rest of the photos.
PENDING = INBOX / ".pending"
MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
MAX_REQUEST = 64 * 1024 * 1024   # uploads spool to the /tmp tmpfs, which counts against memory
PENDING_TTL = 24 * 3600


class UploadError(Exception):
    pass


def enabled():
    return INBOX.is_dir() and os.access(INBOX, os.W_OK)


def _kind(head):
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def waiting():
    """Photos in the inbox that /receipt hasn't processed yet."""
    if not INBOX.is_dir():
        return 0
    return sum(1 for p in INBOX.iterdir() if p.is_file() and not p.name.startswith("."))


async def _save(upload, dest_dir, stamp, n):
    head = await upload.read(16)
    ext = _kind(head)
    if not ext:
        raise UploadError(f"{upload.filename or 'A file'} isn't a JPEG or PNG photo.")
    name = f"pantry-{stamp}-{n:02d}-{secrets.token_hex(3)}.{ext}"
    tmp = dest_dir / f".{name}.part"
    size = len(head)
    try:
        with open(tmp, "wb") as f:
            f.write(head)
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise UploadError(f"{upload.filename or 'A photo'} is over 20 MB.")
                f.write(chunk)
        tmp.rename(dest_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    return name


async def save_all(uploads, dest_dir):
    uploads = [u for u in uploads if getattr(u, "filename", None) is not None]
    if not uploads:
        raise UploadError("No photos came through.")
    if len(uploads) > MAX_FILES:
        raise UploadError(f"At most {MAX_FILES} photos at a time.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = []
    try:
        for n, u in enumerate(uploads, 1):
            saved.append(await _save(u, dest_dir, stamp, n))
    except UploadError:
        for s in saved:
            (dest_dir / s).unlink(missing_ok=True)
        raise
    return saved


async def to_inbox(uploads):
    if not enabled():
        raise UploadError("The receipt inbox isn't available.")
    return await save_all(uploads, INBOX)


async def to_pending(uploads):
    purge_pending()
    token = secrets.token_urlsafe(12)
    names = await save_all(uploads, PENDING / token)
    return token, names


def pending(token):
    d = _pending_dir(token)
    return sorted(p.name for p in d.iterdir()) if d and d.is_dir() else []


def pending_file(token, name):
    d = _pending_dir(token)
    if not d or "/" in name or name.startswith("."):
        return None
    p = d / name
    return p if p.is_file() else None


def confirm(token):
    d = _pending_dir(token)
    if not d or not d.is_dir():
        raise UploadError("Those photos have expired. Share them again.")
    if not enabled():
        raise UploadError("The receipt inbox isn't available.")
    names = []
    for p in sorted(d.iterdir()):
        shutil.move(str(p), INBOX / p.name)
        names.append(p.name)
    d.rmdir()
    return names


def discard(token):
    d = _pending_dir(token)
    if d and d.is_dir():
        shutil.rmtree(d)


def _pending_dir(token):
    if not token or not token.replace("-", "").replace("_", "").isalnum() or len(token) > 40:
        return None
    return PENDING / token


def purge_pending():
    if not PENDING.is_dir():
        return
    cutoff = time.time() - PENDING_TTL
    for d in PENDING.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
