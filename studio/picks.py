"""Live Transcriber: the moments a clipper can pick from a live stream (points), the stretch of stream kept around
each one so it can still be cut after the buffer has moved on (holds), and the clips cut from them (cuts)."""
import json
import math
import shutil
import time
from pathlib import Path

from . import brain, creators, jobs, media, render, safe, usage

HOLD_BEFORE, HOLD_AFTER = 60.0, 60.0    # seconds of stream kept either side of a moment
LEAD_DEFAULT, LEAD_MAX = 20.0, 60.0     # "seconds before": how far before the moment a clip starts
LEN_MIN, LEN_MAX = 5.0, 120.0           # how long a clip can be


def point_time(c: dict) -> float:
    """Where the moment itself is: the AI's peak, else 20 seconds after its start (so the default lead-in gives back
    the AI's own start), never past its end."""
    if c.get("peak") is not None:
        return float(c["peak"])
    return min(float(c["start"]) + LEAD_DEFAULT, float(c["end"]))


def _s(x):
    return str(round(x))


def _secs(x):
    n = round(x)
    return f"{n} second" if n == 1 else f"{n} seconds"


def plan_cut(t, hold_from, hold_to, lead, length):
    """Start and end of a clip `lead` seconds before the moment `t` and `length` long, kept inside what was saved
    (hold_from..hold_to), and one sentence saying what had to change ("" when nothing did)."""
    try:
        lead, length = float(lead), float(length)
    except (TypeError, ValueError):
        raise ValueError("Those numbers aren't valid.")
    if not (math.isfinite(lead) and math.isfinite(length)):
        raise ValueError("Those numbers aren't valid.")
    lead = max(0.0, min(LEAD_MAX, lead))
    length = max(LEN_MIN, min(LEN_MAX, length))
    start, notes = t - lead, []
    if start < hold_from:
        start = hold_from
        n = round(t - hold_from)
        notes.append(f"only {_secs(n)} {'was' if n == 1 else 'were'} kept before this moment, so the clip starts there")
    end = start + length
    if end > hold_to:
        end = hold_to
        if end - start < LEN_MIN:
            start = max(hold_from, end - LEN_MIN)
        n = round(hold_to - t)
        notes.append(f"only {_secs(n)} {'was' if n == 1 else 'were'} kept after this moment, "
                     f"so the clip is {_secs(end - start)} long")
    note = "; ".join(notes)
    return round(start, 2), round(end, 2), (note[:1].upper() + note[1:] + ".") if note else ""


def _next_id(job, key, prefix):
    job[key] = job.get(key, 0) + 1
    return f"{prefix}{job[key]:03d}"


def add_point(s, c):
    """A moment worth clipping, from the AI, a jump, a trigger word or the clipper. Its stretch of stream is saved once
    the stream has gone a minute past it. Returns the point, or None when the same moment is already listed."""
    job, t = s.job, point_time(c)
    with s.lock:
        if any(abs(p["t"] - t) < 10 for p in job["points"]):
            return None
        text = " ".join(w["w"] for w in s.words if t - 15 <= w["s"] <= t + 10)
        p = {"id": _next_id(job, "next_point", "pt"), "t": round(t, 2), "start": round(float(c["start"]), 2),
             "end": round(float(c["end"]), 2), "score": int(c.get("score") or 0),
             "reason": str(c.get("reason") or "")[:300], "text": text[:600], "from": c.get("from") or "ai",
             "made_at": time.time(),
             "hold": {"status": "waiting", "file": None, "from": None, "to": None, "error": None}}
        job["points"].append(p)
    jobs.save(job)
    return p


def waiting(s):
    return [p for p in s.job.get("points", []) if p["hold"]["status"] == "waiting"]


def flush_holds(s):
    """Points whose stretch of stream is now fully recorded (or the stream has ended): save their holds. Called on
    every pass of the session loop, so the render worker never sits waiting for the stream."""
    with s.lock:
        reach = (s.segments[-1][2] + s.clock_fix) if s.segments else 0.0
    due = [p for p in waiting(s) if s.ended or reach >= p["t"] + HOLD_AFTER + 1]
    for p in due:
        p["hold"]["status"] = "saving"
    if due:
        jobs.save(s.job)
    for p in due:
        jobs.submit_live(lambda p=p: save_hold(s, p))


def save_hold(s, p):
    """Copy the minute either side of the moment out of the rolling buffer into points/<id>.mp4, without re-encoding,
    and note where it starts and ends on the transcript clock."""
    h, fix = p["hold"], s.clock_fix
    a, b = p["t"] - HOLD_BEFORE, p["t"] + HOLD_AFTER
    with s.lock:
        segs = [g for g in s.segments if g[2] + fix > a and g[1] + fix < b and g[0].exists()]
    if not segs or segs[0][1] + fix > p["t"] - 5:
        h.update(status="error", error="That part of the stream is no longer in the buffer.")
        jobs.save(s.job)
        return
    out = s.dir / "points"
    lst, ts, mp4 = out / f"{p['id']}.txt", out / f"{p['id']}.ts", out / f"{p['id']}.mp4"
    try:
        out.mkdir(exist_ok=True)
        lst.write_text("".join(f"file '{g[0].as_posix()}'\n" for g in segs), encoding="utf-8")
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(ts)])
        dur = segs[-1][2] - segs[0][1]
        offset = s._align(ts, segs[0][1] + fix, dur)
        s.clock_fix = offset - segs[0][1]
        media.run(["ffmpeg", "-y", "-v", "error", "-i", str(ts), "-c", "copy", "-movflags", "+faststart", str(mp4)])
    except Exception as e:  # noqa: BLE001
        h.update(status="error", error=safe.friendly(e, 300))
        jobs.save(s.job)
        return
    finally:
        lst.unlink(missing_ok=True)
        ts.unlink(missing_ok=True)
    h.update({"status": "ready", "file": f"points/{p['id']}.mp4", "from": round(offset, 2),
              "to": round(offset + dur, 2), "error": None})
    jobs.save(s.job)


def point_of(job, pid):
    p = next((p for p in job.get("points", []) if p["id"] == pid), None)
    if not p:
        raise LookupError("No such moment")
    return p


def cut_of(job, cut_id):
    c = next((c for c in job.get("cuts", []) if c["id"] == cut_id), None)
    if not c:
        raise LookupError("No such clip")
    return c


def make_cut(job, pid, lead, length):
    """A clip `lead` seconds before the moment and `length` long, cut exactly from the kept stretch. Made on the live
    render worker; the cut is listed at once as "making"."""
    p = point_of(job, pid)
    h = p["hold"]
    if h["status"] in ("waiting", "saving"):
        raise ValueError("This moment's video is still being saved. Try again in a few seconds.")
    if h["status"] != "ready":
        raise ValueError(h.get("error") or "This moment's video could not be kept.")
    start, end, note = plan_cut(p["t"], h["from"], h["to"], lead, length)
    with jobs._lock:                            # two quick clicks must not get the same id
        cut = {"id": _next_id(job, "next_cut", "cut"), "point": pid, "lead": round(p["t"] - start, 2),
               "length": round(end - start, 2), "start": start, "end": end, "note": note, "status": "making",
               "file": None, "error": None, "made_at": time.time()}
        job.setdefault("cuts", []).append(cut)
    jobs.save(job)
    jobs.submit_live(lambda: _encode_cut(job, p, cut))
    return cut


def _encode_cut(job, p, cut):
    d = Path(job["dir"])
    (d / "cuts").mkdir(exist_ok=True)
    try:
        media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cut['start'] - p['hold']['from']:.2f}",
                   "-i", str(d / p["hold"]["file"]), "-t", f"{cut['end'] - cut['start']:.2f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(d / "cuts" / f"{cut['id']}.mp4")])
        cut.update(status="ready", file=f"cuts/{cut['id']}.mp4", error=None)
    except Exception as e:  # noqa: BLE001
        cut.update(status="error", error=safe.friendly(e, 300))
    jobs.save(job)


def _unlink(job, rel):
    if rel:
        (Path(job["dir"]) / rel).unlink(missing_ok=True)


def delete_cut(job, cut_id):
    c = cut_of(job, cut_id)
    if c["status"] == "making":
        raise ValueError("This clip is still being made. Try again in a moment.")
    _unlink(job, c.get("file"))
    job["cuts"].remove(c)
    jobs.save(job)


def delete_point(job, pid):
    p = point_of(job, pid)
    if p["hold"]["status"] == "saving" or any(c["point"] == pid and c["status"] == "making" for c in job.get("cuts", [])):
        raise ValueError("This moment is still being saved. Try again in a moment.")
    for c in [c for c in job.get("cuts", []) if c["point"] == pid]:
        _unlink(job, c.get("file"))
        job["cuts"].remove(c)
    _unlink(job, p["hold"].get("file"))
    job["points"].remove(p)
    jobs.save(job)


def after_restart():
    """Cuts that were being made when PeakCut stopped: marked so the clipper can make them again."""
    for job in jobs.all_jobs():
        no_video = not any((Path(job["dir"]) / "buffer").glob("*.ts"))
        for p in job.get("points", []):
            if no_video and p["hold"]["status"] in ("waiting", "saving"):
                p["hold"].update(status="error", error="PeakCut was closed before this moment's video was saved.")
                jobs.save(job)
        stuck = [c for c in job.get("cuts", []) if c.get("status") == "making"]
        for c in stuck:
            c.update(status="error", error="PeakCut was closed while this clip was being made. Make it again.")
        if stuck:
            jobs.save(job)


def _words_of(job):
    """The live transcript: from the running session, else from the words saved next to the recording."""
    from . import live
    s = live.SESSIONS.get(job["id"])
    if s:
        with s.lock:
            return list(s.words)
    try:
        return json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def edit_cut(job, cut_id, options):
    """Send a cut to the editor: it becomes a normal video of the same creator with one clip covering the whole cut,
    which is then made (captions, layout, hook) like any other clip and can be re-made and posted from there."""
    cut = cut_of(job, cut_id)
    if cut["status"] == "error":
        raise ValueError(cut.get("error") or "This clip could not be made. Make it again.")
    if cut["status"] != "ready":
        raise ValueError("This clip is still being made. Try again in a moment.")
    p = point_of(job, cut["point"])
    cr = (creators.get(job["creator"]) if job.get("creator") else None) or {}
    st, ly = options.get("style"), options.get("layout")
    style = st if isinstance(st, str) and st in render.STYLES else cr.get("style", "bounce")
    layout = ly if isinstance(ly, str) and ly in render.LAYOUTS else cr.get("layout", "blur")
    res = int(options["res"]) if str(options.get("res")) in ("720", "1080") else cr.get("res", 1080)
    hook = str(options.get("hook") or "").strip()[:60]
    boxes = render.clean_split(options.get("split"))
    words = [dict(w, s=round(w["s"] - cut["start"], 2), e=round(w["e"] - cut["start"], 2))
             for w in _words_of(job) if cut["start"] <= w["s"] <= cut["end"]]
    m, s = divmod(int(p["t"]), 60)
    new = jobs.create({"type": "cut", "path": "", "name": f"{job.get('title') or 'Live stream'} at {m}:{s:02d}"},
                      {"style": style, "layout": layout, "res": res}, enqueue=False, creator=job.get("creator"))
    dest = Path(new["dir"]) / "source.mp4"
    try:
        shutil.copyfile(Path(job["dir"]) / cut["file"], dest)
        meta = media.probe(dest)
    except Exception:  # noqa: BLE001
        jobs.delete(new["id"])
        raise ValueError("This clip couldn't be opened for editing. Make it again.")
    if job.get("creator") and str(options.get("res")) in ("720", "1080"):
        creators.remember_res(job["creator"], res)       # the person's choice, used for this creator from now on
    (Path(new["dir"]) / "words.json").write_text(json.dumps(words), encoding="utf-8")
    clip = {"id": "clip001", "start": 0.0, "end": round(meta["duration"], 2), "score": p["score"],
            "reason": p["reason"], "hook": hook, "title": new["source"]["name"], "description": "", "hashtags": [], "highlight_words": [],
            "category": "other", "style": style, "layout": layout, "res": res, "status": "queued", "version": 0,
            "made_at": time.time()}
    if boxes:
        clip["split"] = boxes
    new["source"]["path"] = str(dest)
    new.update(source_path=str(dest), status="done", stage="Made from a live moment", title=new["source"]["name"],
               uploader=job.get("uploader", ""), duration=meta["duration"], width=meta["width"],
               height=meta["height"], clips=[clip], from_live={"job": job["id"], "cut": cut_id})
    jobs.save(new)
    cut["edited"] = new["id"]
    jobs.save(job)
    title, up, text = job.get("title") or "", job.get("uploader") or "", " ".join(w["w"] for w in words)

    def make():
        usage.set_context(new.get("creator"), title or "Live stream")
        d = brain.describe(text, title, up)
        for k in ("title", "description", "hashtags", "highlight_words", "category"):
            if d.get(k) or k != "title":        # keep the job's name when the AI gives no title
                clip[k] = d.get(k, clip[k])
        if not hook:
            clip["hook"] = d.get("hook", "")
        if clip["layout"] == "gameplay":     # the webcam in the corner: found in the picture, as for any video
            face = jobs._facecam(new)
            if face:
                clip["facecam"] = face
        jobs.render_one(new, clip, words)

    jobs.submit(make)
    return new
