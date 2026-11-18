
"""The Live Transcriber's routes, with accounts off (the default): what they answer, and that nobody reaches anyone
else's recording. Runs against the code directly with its own data folder; no stream, no AI, no internet."""
import os, sys, tempfile
from pathlib import Path
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-ltr-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="", PEAKCUT_FETCH_MODEL="0",
                  PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402
from studio import auth, creators, ctx, db, jobs, live, picks, server  # noqa: E402
from tests.fakestream import make_video  # noqa: E402
db.init()
fails = []
BASE = "http://127.0.0.1:8765"
H = {"Origin": BASE}
c = TestClient(server.app, base_url=BASE, follow_redirects=False)
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)
jobs.submit_live = lambda fn: fn()

cr = c.post("/api/creators", json={"name": "Route Tester"}, headers=H).json()
r = c.post("/api/transcriber/start", json={"creator": cr["id"]}, headers=H)
check("starting with no channel link says what to do", (r.status_code, "channel link" in r.json()["detail"]), (400, True))
check("starting for a creator that does not exist", c.post("/api/transcriber/start", json={"creator": "nobody"}, headers=H).status_code, 404)

with ctx.as_user(auth.LOCAL_ID):
    job = jobs.create({"type": "live", "url": "https://www.twitch.tv/x", "name": "x live"}, {}, enqueue=False, creator=cr["id"], kind="live")
    job.update(mode="pick", points=[], cuts=[], status="live", next_point=1)
    hold = make_video(Path(job["dir"]) / "points" / "pt001.mp4", seconds=30, size="640x360")
    job["points"].append({"id": "pt001", "t": 70.0, "start": 60.0, "end": 80.0, "score": 80, "reason": "r", "text": "", "from": "ai",
                          "made_at": 0, "hold": {"status": "ready", "file": "points/pt001.mp4", "from": 50.0, "to": 80.0, "error": None}})
    jobs.save(job)
    s = live.Session(job, creators.get(cr["id"]))
    s.words = [{"w": f"w{i}", "s": float(i), "e": i + 0.4} for i in range(0, 90)]
    s.t_done = 90.0
    live.SESSIONS[job["id"]] = s
with ctx.as_user("u_someone_else"):
    other = jobs.create({"type": "live", "url": "https://www.twitch.tv/y", "name": "y live"}, {}, enqueue=False, kind="live")
    other.update(mode="pick", points=[], cuts=[]); jobs.save(other)

J = job["id"]
t = c.get(f"/api/live/{J}/transcript?after=-1").json()
check("the transcript comes in lines with times", (len(t["lines"]) > 3, t["lines"][0]["text"].startswith("w0"), t["live"]), (True, True, True))
last = t["lines"][-1]["e"]
check("asking after the last line gives nothing new", c.get(f"/api/live/{J}/transcript?after={last}").json()["lines"], [])
p = c.post(f"/api/live/{J}/points", json={"t": 30}, headers=H)
check("marking a moment by hand", (p.status_code, p.json().get("from")), (200, "you"))
check("marking the same moment twice", c.post(f"/api/live/{J}/points", json={"t": 31}, headers=H).status_code, 409)
check("marking a time that has not happened yet", c.post(f"/api/live/{J}/points", json={"t": 5000}, headers=H).status_code, 400)
s.analysis_done = True
m = c.post(f"/api/live/{J}/points", json={"t": 50}, headers=H)
check("marking after the recording's analysis has finished", (m.status_code, m.json()["detail"]), (404, "This recording has already ended."))
s.analysis_done = False
k = c.post(f"/api/live/{J}/points/pt001/cuts", json={"lead": 10, "length": 15}, headers=H)
check("making a cut", (k.status_code, k.json().get("status"), k.json().get("start")), (200, "ready", 60.0))
cid = k.json()["id"]
f = c.get(f"/media/{J}/cuts/{cid}.mp4")
check("the cut can be played and downloaded", (f.status_code, f.headers["content-type"], "attachment" in c.get(f"/media/{J}/cuts/{cid}.mp4?download=1").headers.get("content-disposition", "")), (200, "video/mp4", True))
check("the kept stream can be played", c.get(f"/media/{J}/points/pt001.mp4").status_code, 200)
check("no other folders or file types", (c.get(f"/media/{J}/buffer/p01_00000.ts").status_code, c.get(f"/media/{J}/points/job.json").status_code), (404, 404))
fr = c.get(f"/api/live/{J}/cuts/{cid}/frame")
check("a picture from the cut, for drawing boxes", (fr.status_code, fr.headers["content-type"], fr.content[:2]), (200, "image/jpeg", b"\xff\xd8"))
check("bad numbers for a cut", c.post(f"/api/live/{J}/points/pt001/cuts", json={"lead": "x", "length": 15}, headers=H).status_code, 400)
check("a cut of a moment that does not exist", c.post(f"/api/live/{J}/points/pt404/cuts", json={"lead": 1, "length": 9}, headers=H).status_code, 404)
O = other["id"]
check("someone else's recording cannot be read, marked, cut or played",
      (c.get(f"/api/live/{O}/transcript").status_code, c.post(f"/api/live/{O}/points", json={"t": 1}, headers=H).status_code,
       c.post(f"/api/live/{O}/points/pt001/cuts", json={"lead": 1, "length": 9}, headers=H).status_code,
       c.get(f"/media/{O}/cuts/cut001.mp4").status_code), (404, 404, 404, 404))
check("another website cannot make cuts", c.post(f"/api/live/{J}/points/pt001/cuts", json={"lead": 1, "length": 9},
      headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}).status_code, 403)
check("deleting a cut", (c.delete(f"/api/live/{J}/cuts/{cid}", headers=H).status_code, any(x["id"] == cid for x in job["cuts"])), (200, False))
check("deleting a moment", c.delete(f"/api/live/{J}/points/pt001", headers=H).status_code, 200)
check("pick-mode recordings carry their moments to the page", "points" in c.get(f"/api/jobs/{J}").json(), True)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
