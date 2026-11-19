"""Deleting things for good: one clip, a creator with everything under it, or a whole account.

Nothing here can be undone, so every caller asks the person first. Work in progress is never pulled out from under
itself: a video still being made, or a stream still being recorded, has to finish or be stopped first."""
import os
import shutil
import time
import zipfile
from pathlib import Path

from . import auth, config, creators, ctx, db, jobs, live, usage


class Busy(Exception):
    """Something is still being worked on."""


def _busy(job) -> bool:
    s = live.SESSIONS.get(job["id"])                  # a recording that has wrapped up no longer counts
    if job["status"] in ("queued", "running", "live") or bool(s and not s.finished):
        return True
    # a re-make, an Edit render or a Live Transcriber cut keeps the video "done" while ffmpeg writes into its folder
    if any(c.get("status") in ("queued", "rendering") for c in job.get("clips", [])):
        return True
    if any(c.get("status") == "making" for c in job.get("cuts", [])):
        return True
    return any((p.get("hold") or {}).get("status") in ("waiting", "saving") for p in job.get("points", []))


# ---------------------------------------------------------------- clips and videos
def delete_clip(jid: str, cid: str) -> bool:
    """Remove one clip and its files. The video and its other clips stay."""
    job = jobs.get(jid)
    if not job:
        return False
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        return False
    if clip.get("status") in ("queued", "rendering"):
        raise Busy("This clip is still being made. Wait for it to finish first.")
    job["clips"] = [c for c in job["clips"] if c["id"] != cid]
    jobs.save(job)
    d = Path(job["dir"])
    for name in (clip.get("file"), clip.get("cover")):
        if name:
            (d / "clips" / Path(str(name)).name).unlink(missing_ok=True)
    for stem in {cid, Path(str(clip.get("file") or cid)).stem}:     # the captions and the notes kept beside the clip
        for ext in ("ass", "json"):
            (d / "clips" / f"{stem}.{ext}").unlink(missing_ok=True)
    src = clip.get("src")
    if src:                                        # the piece of video fetched or recorded just for this clip
        p = Path(src)
        try:
            mine = p.resolve().parent in ((d / "clips").resolve(), d.resolve()) and p.name.startswith(f"{cid}.src")
        except OSError:
            mine = False
        if mine:                                   # never the whole video's own file
            p.unlink(missing_ok=True)
    ctx.bump()
    return True


def delete_many(job_ids: list, clip_ids: list) -> dict:
    """Delete several videos and clips in one go. What is still being worked on is left alone and counted."""
    out = {"videos": 0, "clips": 0, "skipped": 0}
    whole = set()
    for jid in dict.fromkeys(job_ids):
        job = jobs.get(jid)
        if not job:
            continue
        if _busy(job):
            out["skipped"] += 1
            continue
        if jobs.delete(jid):
            whole.add(jid)
            out["videos"] += 1
    for jid, cid in dict.fromkeys(clip_ids):
        if jid in whole:
            continue
        try:
            out["clips"] += bool(delete_clip(jid, cid))
        except Busy:
            out["skipped"] += 1
    return out


def zip_clips(clip_ids: list, limit_gb: float = 4.0) -> Path:
    """One .zip of finished clips, for downloading many at once. Clips are already compressed, so they are only
    stored, not squeezed again: making the file is as fast as copying."""
    files, total, seen = [], 0, set()
    for jid, cid in dict.fromkeys(clip_ids):
        job = jobs.get(jid)
        clip = next((c for c in (job or {}).get("clips", []) if c["id"] == cid and c.get("status") == "done"), None)
        if not clip or not clip.get("file"):
            continue
        f = Path(job["dir"]) / "clips" / Path(str(clip["file"])).name
        if not f.is_file():
            continue
        total += f.stat().st_size
        if total > limit_gb * 1e9:
            raise ValueError(f"That is more than {limit_gb:g} GB of clips. Download them in smaller groups.")
        base = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in (clip.get("title") or clip.get("hook") or cid))[:60].strip() or cid
        name, n = f"{base}.mp4", 2
        while name.lower() in seen:
            name, n = f"{base} ({n}).mp4", n + 1
        seen.add(name.lower())
        files.append((f, name))
    if not files:
        raise ValueError("None of those clips are ready to download.")
    tmp = config.user_dir(ctx.uid()) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.glob("clips-*.zip"):                       # leftovers from downloads that were abandoned
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    out = tmp / f"clips-{int(time.time() * 1000)}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for f, name in files:
            z.write(f, name)
    return out


# ---------------------------------------------------------------- creators
def _rehome(job, uid) -> None:
    """Move a video or recording out of a creator's folder into its owner's own folder, keeping every file."""
    old = Path(job["dir"])
    new = config.user_dir(uid) / "jobs" / old.name
    real = jobs._jobs.get(job["id"], job)             # the registered job itself, so the page and later renders see the move
    o, n = str(old), str(new)
    fix = lambda p: (n + p[len(o):]) if isinstance(p, str) and p.startswith(o) else p
    with jobs._lock:                                  # the move and the new paths change together
        if old.resolve() != new.resolve():            # already in the owner's own folder: nothing to move
            if new.exists():
                raise Busy("A folder with the same name already exists. Nothing was moved.")
            new.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(old, new)                  # a plain rename: never a slow copy that could be cut half-way
            except OSError:
                raise Busy("A file of this creator's is in use. Try again in a moment. "
                           "Videos moved so far are under All videos.")
        real["dir"] = n
        if real.get("source_path"):
            real["source_path"] = fix(real["source_path"])
        if isinstance(real.get("source"), dict) and real["source"].get("path"):
            real["source"]["path"] = fix(real["source"]["path"])
        for c in real.get("clips", []):
            if c.get("src"):
                c["src"] = fix(c["src"])
        real["creator"] = None
    jobs.save(real)


def delete_creator(cid: str, with_clips: bool = False) -> dict:
    """Remove a creator: their settings, posting rules, activity log and what PeakCut learned about them. Their
    videos and recordings are kept (moved to "No creator") unless with_clips is set. A recording of theirs is
    stopped first. Posts still waiting for a kept clip are cancelled, so nothing is posted under the old settings."""
    c = creators.get(cid)
    if not c:
        return {"ok": False}
    uid = ctx.uid()

    def live_session():
        return next((x for x in live.SESSIONS.values()
                     if x.job.get("creator") == cid and x.job.get("user") == uid and not x.finished), None)

    s = live_session()
    # a refusal for some other video must change nothing: the recording is left running and auto-record stays as it was
    if any(_busy(j) for j in jobs.list_jobs() if j.get("creator") == cid and not (s and j["id"] == s.job["id"])):
        raise Busy("One of this creator's videos is still being made. Wait for it to finish first.")
    if c.get("auto_watch"):
        creators.update(cid, {"auto_watch": False})      # the watcher must not start a new recording meanwhile
    if s:
        if s.user_stopped:                                # already asked to stop, and still wrapping up
            raise Busy("The recording is stopping. Try again in a moment.")
        if not s.ended:
            s.stop("Stopped because the creator was deleted")
        for _ in range(40):                               # up to 20 s for the recording to wrap up
            if s.finished:
                break
            time.sleep(0.5)
        else:
            raise Busy("The recording is stopping. Try again in a moment.")
    mine = [j for j in jobs.list_jobs() if j.get("creator") == cid]
    if any(_busy(j) for j in mine):
        raise Busy("One of this creator's videos is still being made. Wait for it to finish first.")
    kept = 0
    for j in mine:
        if with_clips:
            jobs.delete(j["id"])
        else:
            _rehome(j, uid)
            kept += 1
    if live_session():                                    # a recording began while the videos were being moved
        raise Busy("The creator went live while it was being deleted. Try again in a moment.")
    from . import posting
    with posting._lock:                                   # kept clips must not be posted later with the old settings
        touched = []
        for p in posting._posts:
            if p.get("user") == uid and p.get("creator") == cid:
                if p.get("status") == "scheduled":
                    p["status"] = "cancelled"
                    p["error"] = "Cancelled because the creator was deleted."
                p["creator"] = None
                touched.append(p)
        posting._save()
    for p in touched:
        posting._mirror(p)
    with creators._lock:
        creators._mine().pop(cid, None)
    shutil.rmtree(creators.root(cid), ignore_errors=True)
    ctx.bump()
    return {"ok": True, "videos": len(mine), "kept": kept}


# ---------------------------------------------------------------- the whole account
def delete_account(uid: str) -> dict:
    """Remove an account and everything it owns: recordings, videos, clips, creators, connected accounts and
    history. Sign-in is removed too."""
    u = auth.get_user(uid)
    if not u:
        return {"ok": False}
    if u.is_admin:
        raise Busy("Admin accounts can't be deleted. Make it a normal account first.")
    with ctx.as_user(uid):
        for s in list(live.SESSIONS.values()):
            if s.job.get("user") == uid and not s.finished:
                raise Busy("A stream is still being recorded. Stop the recording first.")
        mine = jobs.list_jobs()
        if any(_busy(j) for j in mine):
            raise Busy("A video is still being made. Wait for it to finish, then delete the account.")
        for j in mine:
            jobs.delete(j["id"])
    with db.engine.begin() as c:
        for t in reversed(db.meta.sorted_tables):
            if t.name != "users" and "user_id" in t.c:
                c.execute(t.delete().where(t.c.user_id == uid))
        c.execute(db.users.delete().where(db.users.c.id == uid))
    auth.forget(uid)
    creators._mem.pop(uid, None)
    try:
        from . import accounts
        accounts._settings_mem.pop(uid, None)
    except Exception:  # noqa: BLE001
        pass
    from . import posting
    posting.forget(uid)                            # scheduled posts must not outlive the account that owns them
    usage.forget(uid)
    shutil.rmtree(config.user_dir(uid), ignore_errors=True)
    return {"ok": True}
