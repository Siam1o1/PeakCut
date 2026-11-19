"""The live card's AI decisions: the words judge says what happened and why it said no, and the clipper can clip
that stretch anyway or dismiss it. The AI is a stand-in, so nothing outside this machine is touched."""
import contextlib, os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-decisions-"); os.environ["DATABASE_URL"] = ""
os.environ["PEAKCUT_FETCH_MODEL"] = "0"
sys.path.insert(0, ".")
from studio import brain, creators, live
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)
def ws(n, gap=0.5):
    return [{"w": f"word{i}" + ("." if i % 8 == 7 else ""), "s": i * gap, "e": i * gap + 0.4} for i in range(n)]
@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name); setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)

# ---- the words judge's answer
asked = {}
def fake(answer):
    def ask(prompt, schema=None, purpose="", **_):
        asked.update(prompt=prompt, schema=schema)
        return answer, "m", 5
    return ask
clip = {"start": 4, "end": 40, "score": 90, "hook": "WOW", "title": "t", "description": "d", "hashtags": [], "reason": "r"}
with patched(brain, "ask_ai", fake({"summary": "He argues about the car. See http://x.com", "why": "Just chatter, no payoff.",
                                    "best_start": 10, "best_end": 40, "clips": []})):
    notes = {}
    got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60, notes=notes)
check("no clips: none returned", got[0], [])
check("the summary is kept and cleaned of links", notes["summary"], "He argues about the car. See")
check("the reason is kept", notes["why"], "Just chatter, no payoff.")
check("the stretch it would clip is kept", notes["best"] and 7 <= notes["best"][0] <= 10.5 and 39 <= notes["best"][1] <= 41, True)
check("the live judge asks for the object answer", asked["schema"] is brain.LIVE_SCHEMA and "summary" in asked["prompt"]
      and "best_start" in asked["prompt"], True)
mk = {"hook": 8, "share": 6, "engagement": 7, "emotion": 15}
with patched(brain, "ask_ai", fake({"summary": "s", "why": "w", "best_start": 10, "best_end": 40, "best_marks": mk, "clips": []})):
    notes = {}
    brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60, notes=notes, min_score=75)
check("the four marks are kept, each held to 10", notes["marks"], {"hook": 8, "share": 6, "engagement": 7, "emotion": 10})
check("...and the viral rating is a quarter each, out of 100", notes["score"], round((8 + 6 + 7 + 10) * 2.5))
check("the rules ask for hook, share, engagement and emotion, with the creator's bar", all(k in asked["prompt"] for k in
      ("best_marks", "hook:", "share:", "engagement:", "emotion:", "average above 7.5")) and
      set(brain.LIVE_SCHEMA["properties"]["best_marks"]["properties"]) == {"hook", "share", "engagement", "emotion"}, True)
with patched(brain, "ask_ai", fake({"summary": "s", "why": "w", "best_start": 10, "best_end": 40,
                                    "best_marks": {"hook": 5, "share": "lots", "engagement": 1, "emotion": 1}, "clips": []})):
    notes = {}
    brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60, notes=notes)
check("a mark that is not a number: no rating at all, never a guess", (notes["marks"], notes["score"]), (None, None))
with patched(brain, "ask_ai", fake({"summary": "s", "why": "w", "best_start": 500, "best_end": 560, "clips": [dict(clip)]})):
    notes = {}
    got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60, notes=notes)
check("clips inside the object are still read", len(got[0]), 1)
check("a stretch outside what the judge was shown is dropped", notes["best"], None)
with patched(brain, "ask_ai", fake([dict(clip)])):
    notes = {}
    got = brain.judge_transcript(ws(140), 0, 70, min_len=15, max_len=60, notes=notes)
check("an old-style list answer still works, with no notes", (len(got[0]), notes), (1, {}))
with patched(brain, "ask_ai", fake({"summary": "s", "why": "w"})):
    check("an object with no clips at all means none", brain.judge_transcript(ws(140), 0, 70, notes={})[0], [])

# ---- a live session
def session(**settings):
    d = tempfile.mkdtemp(prefix="pc-decisions-live-")
    st = dict(creators.DEFAULTS, live_per_hour=4, min_score=80, ai_every_min=3); st.update(settings)
    s = live.Session({"title": "t", "uploader": "u", "dir": d, "creator": "", "ai": {}, "clips": [], "points": [],
                      "source": {"url": "", "name": "t"}}, st)
    s.words = [{"w": "word", "s": float(i), "e": i + 0.5} for i in range(120)]; s.t_done = 120.0
    return s

def judge(cands, summary="Talks about cars.", why="Just chatter.", best=(20.0, 50.0), score=None, marks=None):
    def j(*a, notes=None, **k):
        if notes is not None:
            notes.update(summary=summary, why=why, best=best, score=score, marks=marks)
        return [dict(c) for c in cands], 7
    return j

se = session()
with patched(live.brain, "judge_transcript", judge([])):
    se._ai_check()
v = se.verdicts[-1]
check("nothing clip-worthy: the result stays the same", v["result"], "Words judge: nothing clip-worthy")
check("...with the judge's summary and reason", (v["summary"], v["why"]), ("Talks about cars.", "Just chatter."))
check("...and the stretch to clip anyway", (v["start"], v["end"]), (20.0, 50.0))
check("each decision has its own number", isinstance(v["id"], int) and v["id"] >= 1, True)
check("no moment and no rating from the judge: not rated", v.get("viral"), None)
ser = session()
M4 = {"hook": 4, "share": 3, "engagement": 3, "emotion": 3.6}
with patched(live.brain, "judge_transcript", judge([], score=34, marks=M4)):
    ser._ai_check()
check("the judge's rating of the stretch is the decision's viral rating, with its marks",
      (ser.verdicts[-1]["viral"], ser.verdicts[-1]["marks"]), (34, M4))

weak = dict(clip, start=30.0, end=60.0, score=62, peak=45.0, description="A weak joke.")
se2 = session()
with patched(live.brain, "judge_transcript", judge([weak], summary="")):
    se2._ai_check()
v2 = se2.verdicts[-1]
check("a moment below the bar: its score and the bar are said", v2["why"].startswith("Best moment scored 62, 80 needed."), True)
check("...and its own stretch is offered, not the fallback", (v2["start"], v2["end"], v2["peak"]), (30.0, 60.0, 45.0))
check("...with its description as the summary when the judge gave none", v2["summary"], "A weak joke.")
check("...and with no marks from the judge, its score is the viral rating", (v2["viral"], v2["marks"]), (62, None))
sm = session()
with patched(live.brain, "judge_transcript", judge([weak], summary="", score=41, marks=M4)):
    sm._ai_check()
check("with the judge's four marks, they rate the moment", (sm.verdicts[-1]["viral"], sm.verdicts[-1]["marks"]), (41, M4))

# clip it anyway
out = se2.decide(v2["id"], True)
check("Clip: the stretch is queued like a hand clip", (se2.manual[-1]["start"], se2.manual[-1]["end"], se2.manual[-1]["from"]),
      (30.0, 60.0, "you"))
check("...the peak is kept for the moment list", se2.manual[-1]["t"], 50.0)
check("...and the decision shows it was clipped", (out["state"], se2.verdicts[-1]["state"]), ("clipped", "clipped"))
try:
    se2.decide(v2["id"], True); twice = None
except ValueError as e:
    twice = str(e)
check("a decision cannot be answered twice", twice, "This decision was answered already.")
check("...and nothing more is queued", len(se2.manual), 1)

se3 = session()
with patched(live.brain, "judge_transcript", judge([])):
    se3._ai_check()
se3.decide(se3.verdicts[-1]["id"], False)
check("Cancel: nothing is queued and the decision is dismissed", (se3.manual, se3.verdicts[-1]["state"]), ([], "cancelled"))
try:
    se3.decide(999, True); unknown = None
except KeyError:
    unknown = "KeyError"
check("an unknown decision is refused", unknown, "KeyError")

se4 = session()
se4.words = []; se4.t_done = 120.0
se4._ai_check()
try:
    se4.decide(se4.verdicts[-1]["id"], True); nothing = None
except KeyError:
    nothing = "KeyError"
check("a decision with no stretch (nobody talking) cannot be clipped", ("start" in se4.verdicts[-1], nothing), (False, "KeyError"))

se5 = session()
se5.user_stopped = True
with patched(live.brain, "judge_transcript", judge([])):
    se5._ai_check()
try:
    se5.decide(se5.verdicts[-1]["id"], True); ended = None
except ValueError as e:
    ended = str(e)
check("a stopped recording takes no more clips", ended, "This recording has already ended.")

# the reaction judge says no: why, and the stretch
good = dict(clip, start=30.0, end=60.0, score=85, description="He crashes the car.")
se6 = session(decide_mode="both")
with patched(live.brain, "judge_transcript", judge([good], summary="")), \
     patched(live.brain, "judge_signals", lambda *a, **k: ([{"start": 0, "end": 10, "score": 20, "reason": "flat <b>"}], 3)):
    se6._ai_check()
v6 = se6.verdicts[-1]
check("reaction judge said no: the result stays the same", v6["result"], "Reaction judge said no, not clipped")
check("...why says the words score and what the reaction judge saw",
      v6["why"].startswith("Words judge gave it 85, but the loudness and chat showed no big reaction (best 20: flat"), True)
check("...and the moment can be clipped anyway", (v6["start"], v6["end"], v6["summary"]), (30.0, 60.0, "He crashes the car."))

# a clipped one has nothing to answer
se7 = session(decide_mode="either")
made = []
se7._add_clip = lambda c: made.append(c) or c
with patched(live.brain, "judge_transcript", judge([dict(good, score=95)])), \
     patched(live.brain, "judge_signals", lambda *a, **k: ([], 3)):
    se7._ai_check()
v7 = se7.verdicts[-1]
check("a clip the AI made: no Clip or Cancel, but its reason", (v7["result"].startswith("Clipped"), "start" in v7, v7["why"]),
      (True, False, "r (words judge was very sure.)"))

# a jump passed over: what was said there stands in for the summary
se8 = session(sig_baseline=True)
se8.words = [{"w": f"w{i}", "s": float(i), "e": i + 0.5} for i in range(120)]
se8.top.admit(99.0, time.time()); se8.top.admit(98.0, time.time()); se8.top.admit(97.0, time.time())
se8.top.n = 1
se8._ai_check((60.0, 100.0), "Jump: loudness z 4.0", 1.0)
v8 = se8.verdicts[-1]
check("a jump passed over: the reason is said", (v8["result"], v8["why"]),
      ("Passed over: stronger moments this hour", "Stronger jumps were already sent to the AI this hour, so this one was not."))
check("...the words heard there are the summary", v8["summary"].startswith("“… w77 w78") and v8["summary"].endswith("w100”"), True)
check("...and it can be clipped, no longer than the longest clip", (v8["start"], v8["end"]), (60.0, 100.0))

# only the last six decisions are kept, and the card shows them newest first
se9 = session()
with patched(live.brain, "judge_transcript", judge([])):
    for _ in range(8):
        se9._ai_check()
se9._publish()
ids = [v["id"] for v in se9.job["live"]["verdicts"]]
check("six decisions, newest first", ids, [8, 7, 6, 5, 4, 3])
se9.decide(8, False)
check("an answer shows on the card's copy at once", se9.job["live"]["verdicts"][0]["state"], "cancelled")

print("\nFAILED: " + ", ".join(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
