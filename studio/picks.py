"""Live Transcriber: the moments a clipper can pick from a live stream (points), the stretch of stream kept around
each one so it can still be cut after the buffer has moved on (holds), and the clips cut from them (cuts)."""
import math
import time
from pathlib import Path

from . import jobs, media, safe

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
        notes.append(f"only {_s(t - hold_from)} seconds were kept before this moment, so the clip starts there")
    end = start + length
    if end > hold_to:
        end = hold_to
        if end - start < LEN_MIN:
            start = max(hold_from, end - LEN_MIN)
        notes.append(f"only {_s(hold_to - t)} seconds were kept after this moment, "
                     f"so the clip is {_s(end - start)} seconds long")
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
    for p in waiting(s):
        if s.ended or reach >= p["t"] + HOLD_AFTER + 1:
            p["hold"]["status"] = "saving"
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
    out.mkdir(exist_ok=True)
    lst, ts, mp4 = out / f"{p['id']}.txt", out / f"{p['id']}.ts", out / f"{p['id']}.mp4"
    lst.write_text("".join(f"file '{g[0].as_posix()}'\n" for g in segs), encoding="utf-8")
    try:
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
