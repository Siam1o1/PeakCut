"""Deleting a creator, with accounts off (the default): their videos and recordings are kept under "No creator"
unless asked otherwise, and a live recording is stopped first. Runs in-process with its own data folder."""
import os, sys, tempfile, threading, time
from pathlib import Path
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-dc-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="", PEAKCUT_FETCH_MODEL="0",
                  PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402
from studio import auth, config, creators, ctx, db, jobs, live, server  # noqa: E402
from tests.fakestream import make_video  # noqa: E402
db.init()
ctx.set_uid(auth.LOCAL_ID)          # the test itself acts as the local owner
fails = []
BASE = "http://127.0.0.1:8765"
H = {"Origin": BASE}
c = TestClient(server.app, base_url=BASE, follow_redirects=False)
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

def new_creator(name):
    return c.post("/api/creators", json={"name": name}, headers=H).json()["id"]

# ---- 1 and 2: a creator with a video and a recording, deleted and kept
a = new_creator("Keeper")
with ctx.as_user(auth.LOCAL_ID):
    v = jobs.create({"type": "url", "url": "https://example.com/v", "name": "v"}, {}, enqueue=False, creator=a)
    f = make_video(Path(v["dir"]) / "source.mp4", seconds=2, size="320x180")
    v.update(status="done", source_path=str(f), clips=[{"id": "c1", "status": "done", "src": str(Path(v["dir"]) / "clips" / "c1.src.mp4")}])
    (Path(v["dir"]) / "clips").mkdir(exist_ok=True)
    (Path(v["dir"]) / "clips" / "c1.src.mp4").write_bytes(b"x")
    jobs.save(v)
    p = jobs.create({"type": "live", "url": "https://www.twitch.tv/x", "name": "x live"}, {}, enqueue=False, creator=a, kind="live")
    pf = make_video(Path(p["dir"]) / "points" / "pt001.mp4", seconds=2, size="320x180")
    p.update(mode="pick", points=[], cuts=[], status="done")
    jobs.save(p)
r = c.delete(f"/api/creators/{a}", headers=H)
check("deleting a creator", (r.status_code, r.json().get("kept")), (200, 2))
check("the creator is gone from the list", any(x["id"] == a for x in c.get("/api/creators").json()), False)
check("the creator's folder is gone", creators.root(a).exists(), False)
mine = {j["id"]: j for j in c.get("/api/jobs").json()}
home = (config.user_dir(auth.LOCAL_ID) / "jobs").resolve()
check("both jobs are kept, with no creator", (v["id"] in mine, p["id"] in mine, mine[v["id"]]["creator"], mine[p["id"]]["creator"]), (True, True, None, None))
check("both folders are now under the owner's own folder", all(Path(j["dir"]).resolve().parent == home for j in (v, p)), True)
check("the files moved with them", (Path(v["dir"], "source.mp4").is_file(), Path(p["dir"], "points", "pt001.mp4").is_file()), (True, True))
check("the video's source_path points at the moved file", (v["source_path"], Path(v["source_path"]).is_file()), (str(Path(v["dir"]) / "source.mp4"), True))
check("a clip's src points into the new folder", (v["clips"][0]["src"].startswith(v["dir"]), Path(v["clips"][0]["src"]).is_file()), (True, True))
check("the job is still the same registered job", jobs._jobs[v["id"]] is v and jobs._jobs[v["id"]]["dir"] == v["dir"], True)

# ---- 3: with their clips
b = new_creator("Sweeper")
with ctx.as_user(auth.LOCAL_ID):
    w = jobs.create({"type": "url", "url": "https://example.com/w", "name": "w"}, {}, enqueue=False, creator=b)
    make_video(Path(w["dir"]) / "source.mp4", seconds=2, size="320x180"); w.update(status="done"); jobs.save(w)
r = c.delete(f"/api/creators/{b}?with_clips=1", headers=H)
check("deleting with their clips", (r.status_code, r.json().get("kept"), any(x["id"] == w["id"] for x in c.get("/api/jobs").json()),
                                    Path(w["dir"]).exists(), creators.root(b).exists()), (200, 0, False, False, False))

# ---- 4 and 5: a live recording is stopped first
d = new_creator("Streamer")

with ctx.as_user(auth.LOCAL_ID):
    creators.update(d, {"auto_watch": True})
    lj = jobs.create({"type": "live", "url": "https://www.twitch.tv/z", "name": "z live"}, {}, enqueue=False, creator=d, kind="live")
    lj.update(mode="pick", points=[], cuts=[], status="live")
    jobs.save(lj)
    s = live.Session(lj, creators.get(d))
    live.SESSIONS[lj["id"]] = s
import studio.account as account, types
_real_sleep, naps = time.sleep, []
account.time = types.SimpleNamespace(sleep=lambda x: (naps.append(x), _real_sleep(0.05)), time=time.time)   # 40 naps of 0.05 s, not 20 s
r = c.delete(f"/api/creators/{d}", headers=H)
check("a recording that will not finish gives 409", (r.status_code, r.json().get("detail")), (409, "The recording is stopping. Try again in a moment."))
check("the recording was told to stop", s.user_stopped, True)
check("the creator is still there after the 409", creators.get(d) is not None, True)
with ctx.as_user(auth.LOCAL_ID):
    check("auto-watch is off so nothing restarts it", (creators.get(d)["auto_watch"], live.active_for(d)), (False, None))
naps.clear()
r = c.delete(f"/api/creators/{d}", headers=H)
check("a recording already told to stop answers 409 at once, without waiting again", (r.status_code, naps), (409, []))
live.SESSIONS.pop(lj["id"], None)

d2 = new_creator("Streamer Two")
with ctx.as_user(auth.LOCAL_ID):
    lj2 = jobs.create({"type": "live", "url": "https://www.twitch.tv/q", "name": "q live"}, {}, enqueue=False, creator=d2, kind="live")
    lj2.update(mode="pick", points=[], cuts=[], status="live"); jobs.save(lj2)
    s2 = live.Session(lj2, creators.get(d2)); live.SESSIONS[lj2["id"]] = s2
threading.Timer(0.3, lambda: setattr(s2, "finished", True)).start()      # well inside the 2 s the stub waits
r = c.delete(f"/api/creators/{d2}", headers=H)
check("once the recording has wrapped up the delete works", (r.status_code, creators.get(d2) is None, lj2["creator"]), (200, True, None))
live.SESSIONS.pop(lj2["id"], None)
with ctx.as_user(auth.LOCAL_ID):
    check("nothing can restart a recording for them", live.active_for(d2), None)

# ---- a recording that has already ended on its own is waited for, not stopped again
d3 = new_creator("Ended Already")
with ctx.as_user(auth.LOCAL_ID):
    lj3 = jobs.create({"type": "live", "url": "https://www.twitch.tv/e", "name": "e live"}, {}, enqueue=False, creator=d3, kind="live")
    lj3.update(mode="pick", points=[], cuts=[], status="live"); jobs.save(lj3)
    s3 = live.Session(lj3, creators.get(d3)); s3.ended = True; live.SESSIONS[lj3["id"]] = s3
threading.Timer(0.3, lambda: (lj3.update(status="done"), setattr(s3, "finished", True))).start()    # wrapping up marks the video done
r = c.delete(f"/api/creators/{d3}", headers=H)
check("an ended recording keeps its own wording and is not stopped again", (r.status_code, s3.user_stopped, s3.end_note), (200, False, "Stream ended"))
live.SESSIONS.pop(lj3["id"], None)

# ---- work still writing into a finished video's folder blocks the delete, and nothing is moved
def busy_case(name, mod):
    cid = new_creator(name)
    with ctx.as_user(auth.LOCAL_ID):
        j = jobs.create({"type": "url", "url": "https://example.com/b", "name": "b"}, {}, enqueue=False, creator=cid)
        j.update(status="done", clips=[], cuts=[], points=[]); mod(j)
        make_video(Path(j["dir"]) / "source.mp4", seconds=1, size="320x180"); jobs.save(j)
    before = j["dir"]
    r = c.delete(f"/api/creators/{cid}", headers=H)
    check(name + " blocks the delete", (r.status_code, r.json().get("detail"), creators.get(cid) is not None, j["dir"], Path(before, "source.mp4").is_file(), j["creator"]),
          (409, "One of this creator's videos is still being made. Wait for it to finish first.", True, before, True, cid))
busy_case("a clip being re-made", lambda j: j.update(clips=[{"id": "k", "status": "rendering"}]))
busy_case("a clip waiting to render", lambda j: j.update(clips=[{"id": "k", "status": "queued"}]))
busy_case("a cut being made", lambda j: j.update(cuts=[{"id": "u", "status": "making"}]))
busy_case("a kept stream being saved", lambda j: j.update(points=[{"id": "pt1", "hold": {"status": "saving"}}]))
busy_case("a kept stream still waiting", lambda j: j.update(points=[{"id": "pt1", "hold": {"status": "waiting"}}]))

# ---- the destination already exists: refuse, leave everything where it was
e = new_creator("Clash")
with ctx.as_user(auth.LOCAL_ID):
    ej = jobs.create({"type": "url", "url": "https://example.com/e", "name": "e"}, {}, enqueue=False, creator=e)
    make_video(Path(ej["dir"]) / "source.mp4", seconds=1, size="320x180"); ej.update(status="done"); jobs.save(ej)
    clash = config.user_dir(auth.LOCAL_ID) / "jobs" / Path(ej["dir"]).name
    clash.mkdir(parents=True)
old_dir = ej["dir"]
r = c.delete(f"/api/creators/{e}", headers=H)
check("a folder already at the destination gives 409 and moves nothing", (r.status_code, ej["dir"], Path(old_dir, "source.mp4").is_file(), ej["creator"], creators.get(e) is not None), (409, old_dir, True, e, True))

# ---- a job that is already in the owner's own folder is kept without moving
f2 = new_creator("Already Home")
with ctx.as_user(auth.LOCAL_ID):
    hj = jobs.create({"type": "url", "url": "https://example.com/h", "name": "h"}, {}, enqueue=False, creator=f2)
    home_dir = config.user_dir(auth.LOCAL_ID) / "jobs" / hj["id"]
    home_dir.mkdir(parents=True, exist_ok=True)
    import shutil; shutil.rmtree(hj["dir"]); hj["dir"] = str(home_dir)
    make_video(home_dir / "source.mp4", seconds=1, size="320x180"); hj.update(status="done"); jobs.save(hj)
r = c.delete(f"/api/creators/{f2}", headers=H)
check("a job already in the owner's folder is kept in place", (r.status_code, r.json().get("kept"), hj["dir"], (home_dir / "source.mp4").is_file(), hj["creator"]), (200, 1, str(home_dir), True, None))

# ---- 6: someone else's creator
with ctx.as_user("u_other"):
    other = creators.create({"name": "Not Yours"})["id"]
check("another user's creator is not found", c.delete(f"/api/creators/{other}", headers=H).status_code, 404)
with ctx.as_user("u_other"):
    check("and nothing happened to it", creators.get(other) is not None, True)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
