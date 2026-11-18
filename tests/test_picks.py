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

# (later tasks add more sections here, above this line)
print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
