"""The AI's suggestions on a recording: kept with their own piece of the stream, so they can still be clipped after
the stream ends, or deleted. The stream is made with ffmpeg and the AI is a stand-in: nothing outside this machine
is touched."""
import os, sys, tempfile
from pathlib import Path
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-suggest-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="", PEAKCUT_FETCH_MODEL="0",
                  PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402
from studio import auth, brain, creators, ctx, db, jobs, live, server, suggest  # noqa: E402
from tests.fakestream import make_buffer  # noqa: E402
db.init()
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

jobs.submit_live = lambda fn: fn()                     # the live worker is not running in tests: do it here
rendered = []
jobs.render_one = lambda job, clip, words: rendered.append((clip["id"], len(words)))
brain.describe = lambda text, *a, **k: {"hook": "BIG HOOK", "title": "A title", "hashtags": ["x"]}

def session(cr, mode=None):
    job = jobs.create({"type": "live", "url": "https://www.twitch.tv/x", "name": "x live"}, {}, enqueue=False,
                      creator=cr["id"], kind="live")
    if mode:
        job.update(mode=mode, points=[], cuts=[])
    s = live.Session(job, creators.get(cr["id"]))
    s._align = lambda src, guess, dur: guess           # no speech model in tests: trust the piece clock
    make_buffer(s.buf, seconds=150)
    s.part = 1
    s._scan_segments()
    s.words = [{"w": f"w{i}", "s": float(i), "e": i + 0.5} for i in range(150)]
    return s

with ctx.as_user(auth.LOCAL_ID):
    cr = creators.create({"name": "Suggest Tester"})
    s = session(cr)
    job = s.job
    s._verdict({"t": 100, "transcript": 62, "start": 60.0, "end": 90.0, "peak": 75.0, "summary": "He crashes the car.",
                "why": "Best moment scored 62, 80 needed."}, "Words judge: nothing clip-worthy")
    v = s.verdicts[-1]
    g = job["suggestions"][0]
    check("a decision with a stretch is kept as a suggestion", (v["sid"], g["id"], g["start"], g["end"], g["summary"], g["words"]),
          ("sg001", "sg001", 60.0, 90.0, "He crashes the car.", 62))
    check("...waiting for its piece of the stream", g["hold"]["status"], "waiting")
    s._verdict({"t": 110}, "Skipped: nobody talking")
    s._verdict({"t": 120, "start": 10.0, "end": 40.0}, "Clipped: both judges agreed")
    check("decisions with nothing to offer, and clips the AI made, are not kept", len(job["suggestions"]), 1)

    full = list(s.segments)
    s.segments = [x for x in full if x[2] <= 90]          # the stream has only reached 1:30
    suggest.flush(s)
    check("nothing is saved until the stream is past the stretch", g["hold"]["status"], "waiting")
    check("...and the session waits for it before it ends", len(suggest.waiting(job)), 1)
    s.segments = full
    suggest.flush(s)
    h = g["hold"]
    f = Path(job["dir"]) / (h["file"] or "x")
    check("once it is, the piece is saved", (h["status"], h["error"], f.is_file()), ("ready", None, True))
    check("...a few seconds either side of the stretch", h["from"] <= 55 and h["to"] >= 95, True)
    check("...and nothing is waiting any more", suggest.waiting(job), [])

    # the stream ends: the buffer goes, the suggestion and its piece stay
    for x in s.buf.glob("*"):
        x.unlink()
    live.SESSIONS.pop(job["id"], None)
    (Path(job["dir"]) / "words.json").write_text(__import__("json").dumps(s.words), encoding="utf-8")
    check("after the stream, the suggestion is still there with its video", (len(job["suggestions"]), f.is_file()), (1, True))
    check("the page sees it, with no server paths", "dir" not in str(jobs.public(job)["suggestions"]) and
          jobs.public(job)["suggestions"][0]["hold"]["file"] == "suggest/sg001.mp4", True)

    n_rendered = len(rendered)
    c = suggest.clip(job, "sg001")
    src = Path(c["src"])
    check("Clip makes a draft of the stretch", (c["id"], c["start"], c["end"], c["status"], c["sug"]), ("clip001", 60.0, 90.0, "draft", "sg001"))
    check("...nothing is rendered until Make clip", len(rendered), n_rendered)
    check("...its piece becomes the clip's own source, playable in the editor", (src.name, src.is_file(), f.exists(), c["preview"]),
          ("clip001.src.mp4", True, False, "clip001.src.mp4"))
    check("...the recording's size is known for the render", (job.get("width"), job.get("height")) != (None, None), True)
    check("...and it gets a hook from the AI, as the stretch had none", (c["hook"], c["title"]), ("BIG HOOK", "A title"))
    check("...and the suggestion leaves the list", job["suggestions"], [])
    try:
        suggest.clip(job, "sg001"); again = None
    except KeyError:
        again = "KeyError"
    check("a suggestion can't be clipped twice", again, "KeyError")

    # delete
    s2 = session(cr)
    s2._verdict({"t": 100, "start": 60.0, "end": 90.0}, "Words judge: nothing clip-worthy")
    suggest.flush(s2)
    g2 = s2.job["suggestions"][0]
    f2 = Path(s2.job["dir"]) / g2["hold"]["file"]
    suggest.delete(s2.job, g2["id"])
    check("Delete removes the suggestion and its video", (s2.job["suggestions"], f2.exists()), ([], False))

    # a piece still being saved
    s3 = session(cr)
    s3._verdict({"t": 100, "start": 60.0, "end": 90.0}, "Words judge: nothing clip-worthy")
    try:
        suggest.clip(s3.job, "sg001"); early = None
    except ValueError as e:
        early = str(e)
    check("a suggestion whose piece is not saved yet says to wait", early, "This part of the stream is still being saved. Try again in a few seconds.")
    check("...and while the session is running, clip numbers don't clash", suggest._next_clip_id(s3.job), "clip001")
    live.SESSIONS[s3.job["id"]] = s3
    s3.n = 4
    check("...a running session's own numbering is followed", (suggest._next_clip_id(s3.job), s3.n), ("clip005", 5))
    live.SESSIONS.pop(s3.job["id"], None)

    # a piece that is gone from the buffer
    for x in s3.buf.glob("*.ts"):
        x.unlink()
    s3.ended = True
    suggest.flush(s3)
    check("when the stream pieces are gone, the suggestion says so", (s3.job["suggestions"][0]["hold"]["status"],
          s3.job["suggestions"][0]["hold"]["error"]), ("error", "That part of the stream is no longer in the buffer."))

    # nothing is deleted for the clipper: every suggestion stays until they clip or delete it
    s4 = session(cr)
    for i in range(25):
        s4._verdict({"t": i, "start": 60.0, "end": 90.0}, "Words judge: nothing clip-worthy")
    ids = [x["id"] for x in s4.job["suggestions"]]
    check("every suggestion is kept, none dropped", (len(ids), ids[0], ids[-1]), (25, "sg001", "sg025"))

    # the AI's own moment: its hook and post text go with the suggestion, and no second AI call is made
    s8 = session(cr)
    asked = []
    real_describe = brain.describe
    brain.describe = lambda *a, **k: asked.append(1) or {"hook": "OTHER"}
    s8._verdict({"t": 100, "transcript": 62, "start": 60.0, "end": 90.0, "summary": "He crashes.",
                 "clip": {"score": 62, "hook": "HE CRASHED", "title": "The crash", "description": "Speed crashes the car.",
                          "hashtags": ["gta6"], "rating": {"hook": 6, "value": 62}}}, "Words judge: nothing clip-worthy")
    g8 = s8.job["suggestions"][0]
    check("the AI's hook, title and score are kept with the suggestion", (g8["hook"], g8["title"], g8["score"]), ("HE CRASHED", "The crash", 62))
    s9 = session(cr)
    M4 = {"hook": 4, "share": 3, "engagement": 3, "emotion": 6.4}
    s9._verdict({"t": 100, "start": 60.0, "end": 90.0, "viral": 41, "marks": M4}, "Words judge: nothing clip-worthy")
    s9._verdict({"t": 130, "start": 60.0, "end": 90.0}, "Passed over: stronger moments this hour")
    check("the viral rating and its four marks go with the suggestion; one the AI never saw is not rated",
          [(g["viral"], g["score"], g["marks"]) for g in s9.job["suggestions"]], [(41, 41, M4), (None, 0, None)])
    check("...a suggestion with only the AI's own moment keeps that score", g8["viral"], 62)

    # Rate it: the AI rates a suggestion it never rated, from the words in its stretch
    (Path(s9.job["dir"]) / "words.json").write_text(__import__("json").dumps(s9.words), encoding="utf-8")
    seen = {}
    def fake_rate(words, *a, **k):
        seen["words"] = [w["w"] for w in words]
        return {"marks": M4, "score": 41, "summary": "Speed reads chat."}
    real_rate = brain.rate_stretch
    brain.rate_stretch = fake_rate
    g9 = suggest.rate(s9.job, "sg002")
    brain.rate_stretch = real_rate
    check("Rate it: the stretch's words are rated, and the rating is kept",
          (seen["words"][0], seen["words"][-1], g9["viral"], g9["score"], g9["marks"], g9["summary"]),
          ("w60", "w90", 41, 41, M4, "Speed reads chat."))
    try:
        suggest.rate(s9.job, "sg404"); gone = None
    except KeyError:
        gone = "KeyError"
    check("...a suggestion that is gone cannot be rated", gone, "KeyError")

    # the rating itself: four marks, a quarter each
    check("rating4: four marks of 10 make 100", (brain.rating4({"hook": 10, "share": 10, "engagement": 10, "emotion": 10}),
          brain.rating4({"hook": 4, "share": 3, "engagement": 3, "emotion": 6.4}), brain.rating4(None)), (100, 41, None))
    rq = {}
    def fake_ai(prompt, schema=None, purpose="", **_):
        rq.update(prompt=prompt, schema=schema)
        return {"summary": "s", "marks": {"hook": 2, "share": 2, "engagement": 2, "emotion": 2}}, "m", 5
    real_ai = brain.ask_ai
    brain.ask_ai = fake_ai
    r = brain.rate_stretch(s9.words[60:90], "title <x>", "Speed")
    brain.ask_ai = lambda *a, **k: ({"marks": {"hook": 2}}, "m", 5)
    try:
        brain.rate_stretch(s9.words[60:90]); half = None
    except ValueError as e:
        half = str(e)
    brain.ask_ai = real_ai
    check("rate_stretch: asks for the four marks, the transcript fenced as untrusted", (r["score"], rq["schema"] is brain.RATE_SCHEMA,
          "untrusted" in rq["prompt"] and "<transcript>" in rq["prompt"] and "title <x>" not in rq["prompt"]), (20, True, True))
    check("...and refuses an answer without all four", half, "The AI did not give a rating. Try again.")
    try:
        brain.rate_stretch([]); empty = None
    except ValueError as e:
        empty = str(e)
    check("...or a stretch where nobody talks", empty, "Nobody talks in this part, so there is nothing to rate.")
    suggest.flush(s8)
    c8 = suggest.clip(s8.job, g8["id"])
    check("...and become the draft's, with no extra AI call", (c8["hook"], c8["title"], c8["description"], c8["hashtags"], c8["score"], asked),
          ("HE CRASHED", "The crash", "Speed crashes the car.", ["gta6"], 62, []))
    brain.describe = real_describe

    # the Live Transcriber keeps its own moments instead
    s5 = session(cr, mode="pick")
    s5._verdict({"t": 100, "start": 60.0, "end": 90.0}, "Words judge: nothing clip-worthy")
    check("Live Transcriber recordings don't keep suggestions", (s5.job.get("suggestions", []), "sid" in s5.verdicts[-1]), ([], False))

    # after a restart with nothing left of the stream
    s6 = session(cr)
    s6._verdict({"t": 100, "start": 60.0, "end": 90.0}, "Words judge: nothing clip-worthy")
    for x in s6.buf.glob("*"):
        x.unlink()
    suggest.after_restart()
    check("a piece that was never saved before a restart says so", s6.job["suggestions"][0]["hold"]["status"], "error")

# ---- Clip last 30s / 60s: a draft that opens in the editor, with a minute more kept before it
with ctx.as_user(auth.LOCAL_ID):
    sh = session(cr)
    sh.samples = 150 * 16000                       # 2:30 recorded
    sid = sh.clip_last(30)
    gh = suggest.get(sh.job, sid)
    check("Clip last 30s keeps the last 30 seconds as a suggestion that becomes a draft by itself",
          (gh["start"], gh["end"], gh["pad"], gh.get("auto"), gh["result"]), (120.0, 150.0, [60.0, 5.0], True, "Clipped by you: the last 30s"))
    check("...quoting what was said there", gh["summary"].endswith("w149\u201d"), True)
    sh.user_stopped = True                         # nothing more comes in: save it now
    n_rendered = len(rendered)
    suggest.flush(sh)
    dh = next((c for c in sh.job["clips"] if c.get("sug") == sid), None)
    check("once its piece is saved it is a draft clip, nothing rendered", (bool(dh), dh and dh["status"], len(rendered) - n_rendered), (True, "draft", 0))
    check("...the clip is the last 30 seconds", (dh["start"], dh["end"]), (120.0, 150.0))
    check("...with a minute before it kept, so an earlier start catches the hook", dh["src_offset"] <= 60.5, True)
    check("...and it leaves the suggestions", suggest.get(sh.job, sid), None)
    sh.job["status"] = "done"
    real_put = jobs._q.put
    jobs._q.put = lambda *a, **k: None
    jobs.request_render(sh.job["id"], dh["id"], {"start": -8, "end": 140})
    check("a start before what is kept (even below 0) moves to the earliest kept second, never an error",
          (dh["start"], dh["status"]), (dh["src_offset"], "queued"))
    dh["status"] = "draft"
    jobs.request_render(sh.job["id"], dh["id"], {"start": 70, "end": 9999})
    check("...and an end past it to the last kept second", (dh["start"], dh["end"]), (70.0, dh["src_end"]))
    jobs._q.put = real_put
    sp = session(cr, mode="pick")
    sp.samples = 150 * 16000
    check("the Live Transcriber keeps its own hand moments", (sp.clip_last(30), len(sp.manual), sp.job.get("suggestions", [])),
          (None, 1, []))
    check("the AI's suggestions keep 30 s before and 10 s after", suggest.PAD, (30.0, 10.0))
    check("clips the AI makes keep 30 s before them too", live.PAD_BEFORE, 30)

# ---- the routes
BASE = "http://127.0.0.1:8765"
H = {"Origin": BASE}
c = TestClient(server.app, base_url=BASE, follow_redirects=False)
with ctx.as_user(auth.LOCAL_ID):
    s7 = session(cr)
    s7._verdict({"t": 100, "start": 60.0, "end": 90.0}, "Words judge: nothing clip-worthy")
    s7._verdict({"t": 130, "start": 70.0, "end": 100.0}, "Words judge: nothing clip-worthy")
    suggest.flush(s7)
J = s7.job["id"]
r = c.post(f"/api/jobs/{J}/suggestions/sg001/clip", headers=H)
check("clipping a suggestion gives a draft", (r.status_code, r.json().get("id"), r.json().get("status"), "src" in r.json()),
      (200, "clip001", "draft", False))
pv = c.get(f"/media/{J}/clip001.src.mp4")
check("the editor can play the draft's piece of stream", (pv.status_code, pv.headers.get("content-type")), (200, "video/mp4"))
check("no other file names are served", (c.get(f"/media/{J}/clip001.src.ts").status_code, c.get(f"/media/{J}/clip001.x.mp4").status_code), (404, 404))
queued = []
real_q = jobs._q.put
jobs._q.put = lambda item, *a, **k: queued.append(item)
s7.job["status"] = "done"            # a recording that has stopped (a running one is "live")
mk = c.post(f"/api/jobs/{J}/clips/clip001/render", json={"hook": "MY HOOK", "start": 62, "end": 85, "res": 720}, headers=H)
jobs._q.put = real_q
made = next(x for x in s7.job["clips"] if x["id"] == "clip001")
check("Make clip renders it with what the clipper set", (mk.status_code, made["status"], made["hook"], made["start"], made["end"], made["res"], queued),
      (200, "queued", "MY HOOK", 62.0, 85.0, 720, [("clip", J, "clip001")]))
check("clipping it again", c.post(f"/api/jobs/{J}/suggestions/sg001/clip", headers=H).status_code, 404)
check("a suggestion id that isn't one", c.post(f"/api/jobs/{J}/suggestions/..%2Fx/clip", headers=H).status_code, 404)
check("another website cannot clip or delete", (c.post(f"/api/jobs/{J}/suggestions/sg002/clip",
      headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}).status_code,
      c.delete(f"/api/jobs/{J}/suggestions/sg002", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}).status_code), (403, 403))
with ctx.as_user("u_someone_else"):
    other = jobs.create({"type": "live", "url": "https://www.twitch.tv/y", "name": "y live"}, {}, enqueue=False, kind="live")
    other["suggestions"] = [{"id": "sg001", "start": 1, "end": 2, "hold": {"status": "ready", "file": "suggest/sg001.ts"}}]
    jobs.save(other)
check("someone else's suggestions cannot be clipped or deleted",
      (c.post(f"/api/jobs/{other['id']}/suggestions/sg001/clip", headers=H).status_code,
       c.delete(f"/api/jobs/{other['id']}/suggestions/sg001", headers=H).status_code), (404, 404))
real_rate = brain.rate_stretch
brain.rate_stretch = lambda *a, **k: {"marks": {"hook": 1, "share": 1, "engagement": 1, "emotion": 1}, "score": 10, "summary": ""}
rr = c.post(f"/api/jobs/{J}/suggestions/sg002/rate", headers=H)
def _busy(*a, **k):
    raise RuntimeError("The AI did not answer: busy")
brain.rate_stretch = _busy
rb = c.post(f"/api/jobs/{J}/suggestions/sg002/rate", headers=H)
brain.rate_stretch = real_rate
check("the Rate it route", (rr.status_code, rr.json().get("viral"), rb.status_code), (200, 10, 502))
check("someone else's suggestion cannot be rated", c.post(f"/api/jobs/{other['id']}/suggestions/sg001/rate", headers=H).status_code, 404)
check("deleting a suggestion", (c.delete(f"/api/jobs/{J}/suggestions/sg002", headers=H).status_code, s7.job["suggestions"]), (200, []))
check("deleting it again", c.delete(f"/api/jobs/{J}/suggestions/sg002", headers=H).status_code, 404)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
