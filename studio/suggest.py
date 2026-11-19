"""The AI's suggestions on a recording: stretches the words judge looked at but did not clip on its own (or a jump
it never asked about), kept so the clipper can clip them later, even after the stream has ended, or delete them.

Each suggestion keeps its own short piece of the stream (a few seconds either side of the stretch), copied out of
the rolling buffer without re-encoding, because the buffer is deleted when the recording ends. Clipping one turns
that piece into an ordinary clip of the recording; deleting one deletes the piece too. Live Transcriber
recordings have their own moments (studio/picks.py) and do not use this.

Clip makes a draft: nothing is rendered until the clipper has set it up in the editor and pressed Make clip.
Nothing is ever deleted for the clipper: a suggestion stays until they clip it or delete it."""
import re
import threading
import time
from pathlib import Path

from . import brain, config, jobs, media, safe, usage

PAD = 5.0           # seconds kept either side of the stretch, so a clip can start or end a little wider
POST_KEYS = ("hook", "title", "description", "hashtags", "highlight_words", "category", "rating")
_lock = threading.RLock()


def _list(job):
    return job.setdefault("suggestions", [])


def get(job, sid):
    return next((g for g in _list(job) if g["id"] == sid), None)


def _drop_file(job, g):
    f = (g.get("hold") or {}).get("file")
    if f:
        (Path(job["dir"]) / f).unlink(missing_ok=True)


def add(s, v, clip=None):
    """A decision on the live card that offers a stretch: keep it as a suggestion. `clip` is the AI's own moment
    when it gave one: its hook, title and post text become the draft's. Returns its id."""
    job = s.job
    with _lock:
        n = int(job.get("next_sug") or 1)
        job["next_sug"] = n + 1
        g = {"id": f"sg{n:03d}", "t": v.get("t"), "start": v["start"], "end": v["end"], "peak": v.get("peak"),
             "summary": str(v.get("summary") or "")[:300], "why": str(v.get("why") or "")[:300],
             "result": str(v.get("result") or "")[:120], "words": v.get("transcript"), "made_at": time.time(),
             "score": int((clip or {}).get("score") or 0),
             "hold": {"status": "waiting", "file": None, "from": None, "to": None, "error": None}}
        for k in POST_KEYS:
            if clip and clip.get(k):
                g[k] = clip[k]
        _list(job).append(g)
    jobs.save(job)
    return g["id"]


def waiting(job):
    return [g for g in _list(job) if g["hold"]["status"] in ("waiting", "saving")]


def flush(s):
    """Suggestions whose stretch is fully recorded now (or the stream has ended): save their pieces."""
    with s.lock:
        reach = s.segments[-1][2] if s.segments else 0.0
    due = [g for g in _list(s.job) if g["hold"]["status"] == "waiting" and (s.ended or reach >= g["end"] + PAD + 1)]
    for g in due:
        g["hold"]["status"] = "saving"
    if due:
        jobs.save(s.job)
    for g in due:
        jobs.submit_live(lambda g=g: save_hold(s, g))


def save_hold(s, g):
    """Copy the stretch, with PAD either side, out of the rolling buffer into suggest/<id>.mp4 (no re-encoding; mp4
    so the editor can play it), and note where it starts and ends on the transcript clock."""
    h = g["hold"]
    if get(s.job, g["id"]) is None:          # deleted while it waited
        return
    a, b = g["start"] - PAD, g["end"] + PAD
    with s.lock:
        segs = [x for x in s.segments if x[2] > a and x[1] < b and x[0].exists()]
    if not segs or segs[0][1] > g["start"] + 2:
        h.update(status="error", error="That part of the stream is no longer in the buffer.")
        jobs.save(s.job)
        return
    out = s.dir / "suggest"
    lst, ts, mp4 = out / f"{g['id']}.txt", out / f"{g['id']}.ts", out / f"{g['id']}.mp4"
    try:
        out.mkdir(exist_ok=True)
        lst.write_text("".join(f"file '{x[0].as_posix()}'\n" for x in segs), encoding="utf-8")
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(ts)])
        dur = segs[-1][2] - segs[0][1]
        offset = s._align(ts, segs[0][1] + s.clock_fix, dur)
        media.run(["ffmpeg", "-y", "-v", "error", "-i", str(ts), "-c", "copy", "-movflags", "+faststart", str(mp4)])
    except Exception as e:  # noqa: BLE001
        mp4.unlink(missing_ok=True)
        h.update(status="error", error=safe.friendly(e, 300))
        jobs.save(s.job)
        return
    finally:
        lst.unlink(missing_ok=True)
        ts.unlink(missing_ok=True)
    if get(s.job, g["id"]) is None:          # deleted while it was being saved
        mp4.unlink(missing_ok=True)
        return
    h.update(status="ready", file=f"suggest/{g['id']}.mp4", **{"from": round(offset, 2), "to": round(offset + dur, 2)},
             error=None)
    jobs.save(s.job)


def _words(job):
    from . import live
    s = live.SESSIONS.get(job["id"])
    if s:
        with s.lock:
            return list(s.words)
    try:
        import json
        return json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _next_clip_id(job):
    """The next clipNNN of this recording. A running session numbers its own clips: it is moved on too."""
    from . import live
    nums = [int(m.group(1)) for c in job["clips"] for m in [re.fullmatch(r"clip(\d+)", str(c.get("id", "")))] if m]
    n = max(nums, default=0) + 1
    s = live.SESSIONS.get(job["id"])
    if s:
        n = max(n, s.n + 1)
        s.n = n
    return f"clip{n:03d}"


def clip(job, sid):
    """Turn the suggestion into a draft clip: its kept piece becomes the clip's own source and the suggestion leaves
    the list. Nothing is rendered: the clipper sets the clip up in the editor and presses Make clip. When the AI gave
    no hook for this stretch it is asked for one (a short wait, never a failure). Returns the clip."""
    from . import creators
    with _lock:
        g = get(job, sid)
        if not g:
            raise KeyError(sid)
        h = g["hold"]
        if h["status"] in ("waiting", "saving"):
            raise ValueError("This part of the stream is still being saved. Try again in a few seconds.")
        if h["status"] != "ready" or not (Path(job["dir"]) / h["file"]).is_file():
            raise ValueError(h.get("error") or "This suggestion's video could not be kept.")
        cid = _next_clip_id(job)
        d = Path(job["dir"]) / "clips"
        d.mkdir(exist_ok=True)
        src = d / f"{cid}.src.mp4"
        (Path(job["dir"]) / h["file"]).replace(src)
        _list(job).remove(g)
        c = (creators.get(job["creator"]) or {}) if job.get("creator") else {}
        res = c.get("res") if c.get("res") in (720, 1080) else 1080
        opts = job.get("options") or {}
        if "width" not in job:
            meta = media.probe(src)
            job.update(width=meta["width"], height=meta["height"])
        new = {"id": cid, "start": round(max(g["start"], h["from"]), 2), "end": round(min(g["end"], h["to"]), 2),
               "src": str(src), "src_offset": h["from"], "src_end": h["to"], "preview": src.name,
               "score": g.get("score") or 0, "category": g.get("category") or "other",
               "hook": g.get("hook") or "", "title": g.get("title") or job.get("title") or "",
               "description": g.get("description") or g["summary"], "hashtags": g.get("hashtags") or [],
               "highlight_words": g.get("highlight_words") or [],
               "reason": "From the AI's suggestions" + (": " + g["summary"] if g.get("summary") else ""),
               "style": c.get("style") or opts.get("style") or "bounce", "layout": c.get("layout") or opts.get("layout") or "blur",
               "res": res, "captions": opts.get("captions", True), "status": "draft", "version": 0,
               "made_at": time.time(), "from": "you", "sug": g["id"]}
        if g.get("rating"):
            new["rating"] = g["rating"]
        job["clips"].append(new)
    jobs.save(job)
    if not new["hook"]:
        _describe(job, new)
    return new


def _describe(job, c):
    """A hook and post text for a draft whose stretch the AI never wrote one for. Never fails the draft."""
    words = _words(job)
    text = " ".join(w["w"] for w in words if c["start"] <= w["s"] <= c["end"])
    try:
        usage.set_context(job.get("creator"), job.get("title") or "Live stream")
        d = brain.describe(text, job.get("title", ""), job.get("uploader", ""), timeout=min(20, config.LIVE_AI_TIMEOUT))
        for k in ("hook", "title", "hashtags", "highlight_words", "category"):
            if d.get(k):
                c[k] = d[k]
        jobs.save(job)
    except Exception:  # noqa: BLE001 - the clipper writes the hook in the editor instead
        pass


def delete(job, sid):
    with _lock:
        g = get(job, sid)
        if not g:
            raise KeyError(sid)
        _list(job).remove(g)
        _drop_file(job, g)
    jobs.save(job)


def after_restart():
    """Suggestions whose piece was being saved when PeakCut stopped: if the stream pieces are gone, say so."""
    for job in jobs.all_jobs():
        no_video = not any((Path(job["dir"]) / "buffer").glob("*.ts"))
        changed = False
        for g in job.get("suggestions", []):
            if no_video and g["hold"]["status"] in ("waiting", "saving"):
                g["hold"].update(status="error", error="PeakCut was closed before this part of the stream was saved.")
                changed = True
        if changed:
            jobs.save(job)
