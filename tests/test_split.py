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

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
