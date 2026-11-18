"""Live Transcriber: the moment the AI points at, the maths of a cut, keeping the stream around a moment, cutting a
clip from it, and sending a cut to the editor. Runs against the code directly with its own empty data folder; the AI
is a stand-in and the stream is made with ffmpeg, so nothing outside this machine is touched."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-picks-"); os.environ["DATABASE_URL"] = ""
os.environ["PEAKCUT_FETCH_MODEL"] = "0"
sys.path.insert(0, ".")
from studio import brain
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)
def ws(n, gap=0.5):
    """n words, a sentence every 8 words, one word every `gap` seconds."""
    return [{"w": f"word{i}" + ("." if i % 8 == 7 else ""), "s": i * gap, "e": i * gap + 0.4} for i in range(n)]

# ---- the AI's peak
answer = {}
brain.ask_ai = lambda prompt, schema=None, purpose="": ([dict(answer)], "m", 5)
base = {"start": 4, "end": 40, "score": 90, "hook": "WOW", "title": "t", "description": "d", "hashtags": [], "reason": "r"}
answer.update(base, peak=30)
got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60)[0]
check("the AI's peak is kept when it lies inside the clip", got[0]["peak"] if got else None, 30.0)
answer.clear(); answer.update(base, peak=500)
got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60)[0]
check("a peak outside the clip is dropped", got[0]["peak"] if got else "no clip", None)
answer.clear(); answer.update(base)
got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60)[0]
check("no peak given: None", got[0]["peak"] if got else "no clip", None)
answer.clear(); answer.update(base, peak="soon")
got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60)[0]
check("a peak that is not a number is dropped", got[0]["peak"] if got else "no clip", None)
check("the rules ask the AI for the peak", "peak:" in brain.RULES and "peak" in brain.SCHEMA["items"]["properties"])

# ---- the maths
from studio import picks
check("with a peak, the point is the peak", picks.point_time({"start": 10, "end": 50, "peak": 31.5}), 31.5)
check("without one, 20 s after the start", picks.point_time({"start": 10, "end": 50, "peak": None}), 30.0)
check("...but never past the end", picks.point_time({"start": 10, "end": 18}), 18.0)
check("a plain cut", picks.plan_cut(100, 40, 160, 20, 45), (80.0, 125.0, ""))
check("lead-in above 60 is held at 60", picks.plan_cut(100, 40, 160, 90, 45), (40.0, 85.0, ""))
check("length below 5 is raised to 5", picks.plan_cut(100, 40, 160, 20, 1)[:2], (80.0, 85.0))
s, e, note = picks.plan_cut(100, 70, 160, 40, 45)
check("not enough kept before: starts where the kept part starts, and says so",
      (s, e, "30 seconds were kept before" in note), (70.0, 115.0, True))
s, e, note = picks.plan_cut(100, 40, 138, 20, 60)
check("not enough kept after: shortened, and says how long it is",
      (s, e, note), (80.0, 138.0, "Only 38 seconds were kept after this moment, so the clip is 58 seconds long."))
s, e, note = picks.plan_cut(100, 99, 101, 0, 30)
check("a very short kept part still gives the whole of it", (s, e), (99.0, 101.0))
try:
    picks.plan_cut(100, 40, 160, float("nan"), 45); said = ""
except ValueError as x:
    said = str(x)
check("nonsense numbers are refused in plain words", said, "Those numbers aren't valid.")

# ---- points and holds, on a pretend stream
from pathlib import Path
from studio import creators, ctx, jobs, live
from tests.fakestream import make_buffer
ctx.set_uid("u_picks1")
jobs.submit_live = lambda fn: fn()                     # the live worker is not running in tests: do it here
cr = creators.create({"name": "Pick Tester"})
job = jobs.create({"type": "live", "url": "https://www.twitch.tv/x", "name": "x live"}, {}, enqueue=False,
                  creator=cr["id"], kind="live")
job.update(mode="pick", points=[], cuts=[])
s = live.Session(job, creators.get(cr["id"]))
s._align = lambda src, guess, dur: guess               # no speech model in tests: trust the piece clock
make_buffer(s.buf, seconds=150)
s.part = 1
s._scan_segments()
s.words = [{"w": f"w{i}", "s": float(i), "e": i + 0.5} for i in range(150)]
check("a pick-mode session knows it", s.pick, True)
s._add_clip({"start": 60, "end": 90, "peak": 75, "score": 88, "reason": "Big laugh", "from": "ai"})
check("in pick mode the AI's moment becomes a point, not a clip", (len(job["points"]), len(job["clips"])), (1, 0))
p = job["points"][0]
check("the point is at the peak, waiting for its stretch of stream", (p["id"], p["t"], p["hold"]["status"], p["from"]), ("pt001", 75.0, "waiting", "ai"))
check("the point carries the words around it", "w75" in p["text"] and "w60" in p["text"])
check("the same moment found again is not listed twice", s._add_clip({"start": 62, "end": 92, "peak": 79, "score": 70, "reason": "again"}), None)
full = list(s.segments)
s.segments = [g for g in full if g[2] <= 120]          # the stream has only reached 2:00
picks.flush_holds(s)
check("nothing is saved until the stream is a minute past the moment", p["hold"]["status"], "waiting")
s.segments = full                                      # now it has reached 2:30
picks.flush_holds(s)
h = p["hold"]
check("once it is, the hold is saved", (h["status"], h["error"]), ("ready", None))
check("...about a minute either side of the moment", h["from"] <= 15 and h["to"] >= 135, True)
check("...as a file the page can play", (Path(job["dir"]) / h["file"]).suffix == ".mp4" and (Path(job["dir"]) / h["file"]).is_file(), True)
check("the buffer pieces are left alone for the buffer's own clean-up", any(s.buf.glob("*.ts")), True)
m = s.mark(20.0)
check("a moment marked by hand is a point too", (m["from"], m["reason"], m["t"], m["score"]), ("you", "Marked by you", 20.0, 0))
picks.flush_holds(s)
check("...and a moment near the start keeps what there is", (m["hold"]["status"], m["hold"]["from"]), ("ready", 0.0))
for f in s.buf.glob("*.ts"):
    f.unlink()
s.ended = True
late = s.mark(110.0)
picks.flush_holds(s)
check("when the pieces are gone the point says so", (late["hold"]["status"], late["hold"]["error"]),
      ("error", "That part of the stream is no longer in the buffer."))
brain.ask_ai = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("The AI is not set up on this server."))
s._ai_check((110.0, 160.0), "Jump: loudness +80%")
jp = job["points"][-1]
check("with no AI, a loud moment is still listed", (jp["from"], jp["t"], jp["reason"]), ("jump", 140.0, "Jump: loudness +80%"))
check("a normal (not pick) recording still makes clips", live.Session({**job, "mode": None, "id": "x"}, creators.get(cr["id"])).pick, False)


# ---- fix round: how moments are labelled, "must clip" in pick mode, a recording that ends, marking too late
good = {"start": 30, "end": 60, "peak": 40, "score": 88, "reason": "Funny", "hook": "h", "title": "t", "description": "d", "hashtags": []}
brain.judge_transcript = lambda *a, **k: ([dict(good)], 5)
brain.judge_signals = lambda *a, **k: ([{"start": 0, "end": 400, "score": 80, "reason": "loud"}], 5)
s2 = live.Session(job, creators.get(cr["id"]))
s2.words, s2.t_done, s2.settings["min_gap_min"] = s.words, 150.0, 0
s2._ai_check((30.0, 60.0), "Jump: loudness +80%")
check("an AI point found after a jump is labelled as a jump", job["points"][-1]["from"], "jump")
good.update(start=90, end=120, peak=95)
s2._ai_check()
check("an AI point found on the schedule is labelled as the AI's", job["points"][-1]["from"], "ai")
n = len(job["points"])
s2.settings["must_clip_min"] = 1
s2.started_at -= 3600                                  # recording for an hour; the last point was made just now
good.update(start=0, end=20, peak=5, score=50)         # below the bar: only a forced check would take it
s2._ai_check()
check("a point made just now stops 'must clip' from forcing another", len(job["points"]), n)
s2.settings["must_clip_min"] = 0
live.resolve = lambda url: (_ for _ in ()).throw(RuntimeError("boom"))
s3 = live.Session(job, creators.get(cr["id"]))
try:
    s3._ingest()
except RuntimeError:
    pass
check("however recording stops, the session knows it has ended", s3.ended, True)
s2.analysis_done = True
try:
    s2.mark(5.0); said = ""
except ValueError as x:
    said = str(x)
check("marking a moment after the recording ended is refused in plain words", said, "This recording has already ended.")
# ---- cuts
from studio import media
cut = picks.make_cut(job, "pt001", 20, 45)
check("a cut starts 20 s before the moment and is as long as asked", (cut["start"], cut["end"], cut["note"]), (55.0, 100.0, ""))
check("...and is made", (cut["status"], cut["file"]), ("ready", "cuts/cut001.mp4"))
meta = media.probe(Path(job["dir"]) / cut["file"])
check("...and really is 45 seconds long", abs(meta["duration"] - 45) < 0.5, True)
c2 = picks.make_cut(job, "pt001", 20, 120)
check("asking for more than was kept: shortened, and said", (c2["end"] == h["to"], "kept after" in c2["note"]), (True, True))
try:
    picks.make_cut(job, late["id"], 20, 30); said = ""
except ValueError as x:
    said = str(x)
check("a moment whose stream was lost cannot be cut, and says why", said, "That part of the stream is no longer in the buffer.")
try:
    picks.make_cut(job, "pt999", 20, 30); said = ""
except LookupError:
    said = "missing"
check("an unknown moment is refused", said, "missing")
picks.delete_cut(job, c2["id"])
check("deleting a cut removes it and its file", (any(c["id"] == c2["id"] for c in job["cuts"]), (Path(job["dir"]) / c2["file"]).exists()), (False, False))
hold_file = Path(job["dir"]) / p["hold"]["file"]
picks.delete_point(job, "pt001")
check("deleting a moment removes its kept stream and its cuts", (hold_file.exists(), any(c["point"] == "pt001" for c in job["cuts"])), (False, False))
job["cuts"].append({"id": "cut050", "point": m["id"], "status": "making", "file": None, "error": None})
jobs.save(job)
picks.after_restart()
check("a cut interrupted by a restart says to make it again", job["cuts"][-1]["status"], "error")
# (later tasks add more sections here, above this line)
print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
