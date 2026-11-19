"""The "Game + cam" layout: gameplay on top, the streamer's facecam below. Checks the boxes are cleaned, where things
sit, the ffmpeg recipe, and a real render of a test video. Runs with its own empty data folder."""
import os, sys, tempfile
from pathlib import Path
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-split-"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from studio import config, media, render
from tests.fakestream import make_video
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

BOXES = {"cam": [0.72, 0.05, 0.25, 0.3], "game": [0, 0, 1, 1], "cam_pct": 40}
check("the layout is offered", render.LAYOUTS.get("split"), "Gameplay on top, facecam below")
check("good boxes are kept", render.clean_split(BOXES), {"cam": [0.72, 0.05, 0.25, 0.3], "game": [0.0, 0.0, 1.0, 1.0], "cam_pct": 40})
check("no facecam box: nothing", render.clean_split({"game": [0, 0, 1, 1]}), None)
check("junk: nothing", (render.clean_split("x"), render.clean_split({"cam": [1, "a", 3]}), render.clean_split({"cam": [0, 0, float("nan"), 1]})), (None, None, None))
check("a box running off the frame is pulled back inside",
      render.clean_split({"cam": [0.9, 0.9, 0.5, 0.5]})["cam"], [0.9, 0.9, 0.1, 0.1])
check("an infinite facecam height does not crash", render.clean_split({**BOXES, "cam_pct": float("inf")})["cam_pct"], 40)
check("no gameplay box: the whole frame", render.clean_split({"cam": [0, 0, 0.2, 0.2]})["game"], [0.0, 0.0, 1.0, 1.0])
check("facecam height is held between 25 and 50", (render.clean_split({**BOXES, "cam_pct": 90})["cam_pct"], render.clean_split({**BOXES, "cam_pct": 3})["cam_pct"]), (50, 25))
sp = render.clean_split(BOXES)
check("40% facecam: gameplay is 1152 high and the captions sit on the line between",
      render.geometry(1280, 720, "split", sp), ("split", 1152, 230, 1152))
check("split with no boxes falls back to the blurred fill", render.geometry(1280, 720, "split", None)[0], "blur")
f = render._filter("split", 1152, sp)
check("the recipe crops the facecam and stacks the two", all(x in f for x in ("crop=iw*0.25:ih*0.3:iw*0.72:ih*0.05", "vstack=inputs=2", f"{config.OUT_W}:1152", f"{config.OUT_W}:768", "[v0];")))

# ---- a real render
config.FONTS_DIR.mkdir(parents=True, exist_ok=True); render.install_fonts()
src = make_video(Path(os.environ["PEAKCUT_DATA"]) / "src.mp4", seconds=4)
out = render.render_clip(src, Path(os.environ["PEAKCUT_DATA"]) / "out", "c1",
                         {"start": 0.0, "end": 3.0, "layout": "split", "split": BOXES, "hook": "TEST", "style": "bounce"},
                         [{"w": "hello", "s": 0.2, "e": 0.6}, {"w": "there", "s": 0.7, "e": 1.1}], 1280, 720)
meta = media.probe(Path(os.environ["PEAKCUT_DATA"]) / "out" / out["file"])
check("a split clip is rendered at 1080x1920", (out["layout"], meta["width"], meta["height"]), ("split", 1080, 1920))
for lay in ("split", "blur"):
    o7 = render.render_clip(src, Path(os.environ["PEAKCUT_DATA"]) / "out", "r7" + lay,
                            {"start": 0.0, "end": 3.0, "layout": lay, "split": BOXES, "hook": "TEST", "style": "bounce", "res": 720},
                            [{"w": "hello", "s": 0.2, "e": 0.6}], 1280, 720)
    m7 = media.probe(Path(os.environ["PEAKCUT_DATA"]) / "out" / o7["file"])
    check(f"a {lay} clip with res 720 is rendered at 720x1280", (m7["width"], m7["height"]), (720, 1280))
    from PIL import Image
    with Image.open(Path(os.environ["PEAKCUT_DATA"]) / "out" / o7["cover"]) as cv:
        check(f"...and its cover picture follows ({lay})", cv.size, (720, 1280))
o9 = render.render_clip(src, Path(os.environ["PEAKCUT_DATA"]) / "out", "r9",
                        {"start": 0.0, "end": 3.0, "layout": "blur", "hook": "", "style": "bounce", "res": 999}, [], 1280, 720)
m9 = media.probe(Path(os.environ["PEAKCUT_DATA"]) / "out" / o9["file"])
check("an unknown res renders at 1080x1920", (m9["width"], m9["height"]), (1080, 1920))

# ---- the export size as an option and a creator setting
from studio import jobs as _jobs
check("res 720 is kept", _jobs.clean_options({"res": 720}), {"res": 720})
check("res as text is kept", _jobs.clean_options({"res": "1080"}), {"res": 1080})
check("an unknown res falls back to 1080", _jobs.clean_options({"res": 500}), {"res": 1080})
check("the default res is 1080", _jobs.DEFAULTS["res"], 1080)

# ---- boxes saved on the creator
from studio import creators, ctx, jobs
ctx.set_uid("u_split1")
cr = creators.create({"name": "Box Tester", "split": BOXES})
check("a creator keeps their facecam boxes", creators.get(cr["id"])["split"], render.clean_split(BOXES))
check("junk boxes are not kept", creators.update(cr["id"], {"split": {"cam": "x"}})["split"], None)
creators.update(cr["id"], {"split": BOXES})
check("a creator starts at 1080", creators.get(cr["id"])["res"], 1080)
check("a creator keeps 720", creators.update(cr["id"], {"res": 720})["res"], 720)
check("a junk res becomes 1080", creators.update(cr["id"], {"res": 5})["res"], 1080)
creators.update(cr["id"], {"res": 720})
job = jobs.create({"type": "file", "path": str(src), "name": "src.mp4"}, {"layout": "split"}, enqueue=False, creator=cr["id"])
job.update(source_path=str(src), width=1280, height=720, duration=4.0, status="done",
           clips=[{"id": "clip001", "start": 0.0, "end": 3.0, "score": 0, "hook": "", "title": "", "description": "",
                   "hashtags": [], "highlight_words": [], "style": "bounce", "layout": "split", "status": "queued", "version": 0}])
check("a new video takes the creator's export size", job["options"]["res"], 720)
brain_fail = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no AI in tests"))
from studio import brain; brain.ask_ai = brain_fail
jobs.render_one(job, job["clips"][0], [{"w": "hello", "s": 0.2, "e": 0.6}])
c0 = job["clips"][0]
check("a split clip with no boxes of its own uses the creator's", (c0["status"], c0.get("split"), c0.get("layout")),
      ("done", render.clean_split(BOXES), "split"))
jobs._q.put = lambda *a, **k: None                       # nothing is rendered by this check, only recorded
jobs.request_render(job["id"], "clip001", {"layout": "split", "split": {"cam": [0, 0, 0.3, 0.3], "cam_pct": 30}})
check("re-making a clip can change its boxes", job["clips"][0]["split"], {"cam": [0.0, 0.0, 0.3, 0.3], "game": [0.0, 0.0, 1.0, 1.0], "cam_pct": 30})

jobs.request_render(job["id"], "clip001", {"res": "720"})
check("re-making a clip can change its export size", job["clips"][0]["res"], 720)
jobs.request_render(job["id"], "clip001", {"res": 12})
check("a junk size when re-making falls back to 1080", job["clips"][0]["res"], 1080)

# ---- the export size is remembered only when the person chooses it
from studio import live
creators.update(cr["id"], {"res": 1080})
lj = jobs.create({"type": "live", "url": "https://www.twitch.tv/x", "name": "x live"}, {}, enqueue=False,
                 creator=cr["id"], kind="live")
sess = live.Session(lj, creators.get(cr["id"]))          # a recording that began while the creator was on 1080
creators.remember_res(cr["id"], 720)
jobs.submit_live = lambda fn: None
got = sess._add_clip({"start": 1.0, "end": 20.0, "score": 90, "hook": "", "title": "", "description": "", "hashtags": [], "reason": ""})
check("a live clip uses the creator's size as it is now", got["res"], 720)
c1080 = dict(job["clips"][0], res=1080, status="queued")
jobs.render_one(job, c1080, [])
check("rendering a clip does not change the creator's saved size", creators.get(cr["id"])["res"], 720)
# creating a video remembers the size only when the options really named one
creators.update(cr["id"], {"res": 1080})
jobs.create({"type": "file", "path": str(src), "name": "src.mp4"}, {"layout": "blur"}, enqueue=False, creator=cr["id"])
check("a video made without a size choice leaves the saved size alone", creators.get(cr["id"])["res"], 1080)
jobs.create({"type": "file", "path": str(src), "name": "src.mp4"}, {"res": 720}, enqueue=False, creator=cr["id"])
check("a video made with a size in its options remembers it on the creator", creators.get(cr["id"])["res"], 720)
jobs.create({"type": "file", "path": str(src), "name": "src.mp4"}, {"res": 5}, enqueue=False, creator=cr["id"])
check("a junk size in the options is not remembered", creators.get(cr["id"])["res"], 720)
# re-making a clip: the creator's size changes only when the person changed the clip's size
creators.update(cr["id"], {"res": 720})
job["clips"][0]["res"] = 1080
jobs.request_render(job["id"], "clip001", {"res": 1080, "hook": "x"})
check("re-making with an unchanged size leaves the creator's saved size alone", creators.get(cr["id"])["res"], 720)
jobs.request_render(job["id"], "clip001", {"res": 720})
check("re-making with a changed size remembers it", creators.get(cr["id"])["res"], 720)
creators.update(cr["id"], {"res": 1080})
job["clips"][0]["res"] = 1080
jobs.request_render(job["id"], "clip001", {"res": 720})
check("a changed size is remembered on the creator", creators.get(cr["id"])["res"], 720)
from studio import account
gone = creators.create({"name": "Soon Gone"})
account.delete_creator(gone["id"], with_clips=True)
creators.remember_res(gone["id"], 1080)
check("a deleted creator is not brought back", (creators.root(gone["id"]).exists(), creators.get(gone["id"])), (False, None))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
