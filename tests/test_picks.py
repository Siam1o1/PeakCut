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

# (later tasks add more sections here, above this line)
print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
