# Live Transcriber Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Live Transcriber view in PeakCut: record a stream, show its transcript live, list the moments that could go
viral, cut any of them with a chosen lead-in and length, send a cut to the editor, and a new "Game + cam" layout.

**Architecture:** A live job started from the new view carries `mode: "pick"`. `studio/live.py` keeps recording,
transcribing and judging as today, but in pick mode hands accepted moments to a new module, `studio/picks.py`, which
owns points, holds (the 2 minutes of stream kept around each point) and cuts (the clips the clipper downloads), and
turns a cut into a normal PeakCut video job for editing. `studio/render.py` gains the split layout. The page
(`web/index.html`) gains one view, an edit panel and a box editor.

**Tech Stack:** Python 3.10+, FastAPI, ffmpeg/ffprobe, sherpa-onnx (unchanged), plain HTML/CSS/JS with no build step.

**Spec:** `docs/superpowers/specs/2026-11-19-live-transcriber-design.md`

## Global Constraints

- Hold: 60 s before and 60 s after the moment (`HOLD_BEFORE = HOLD_AFTER = 60`).
- Lead-in ("Seconds before"): 0 to 60, default 20. Length: 5 to 120 seconds, default the creator's `max_len`.
- Without an AI `peak`: `t = min(start + 20, end)`.
- Split layout key `"split"`, label "Gameplay on top, facecam below", short label "Game + cam"; boxes are
  `[x, y, w, h]` fractions of the frame; `cam_pct` 25 to 50, default 40.
- Pick-mode recordings are shown only in the Live Transcriber view, never as a live card in a creator's Clips list.
- Who is asking always comes from `ctx.uid()`; routes reach only the asker's jobs (`_job_or_404`, `live.mine`).
- Anything that changes what the page shows goes through `jobs.save(job)` (which calls `ctx.bump`).
- Text from the stream goes into prompts only through `brain._safe` (already true for `judge_transcript`/`describe`).
- Comments, messages and UI text in plain language for people who do not program (see `AGENTS.md`).
- Pages: plain HTML/CSS/JS, existing palette, no shadows, hover states change colour only.
- Run `python tests/run.py` before starting and after every task that touches `studio/`.
- Tests are standalone scripts printing `PASS name` / `FAIL name` (see `tests/test_tools.py`) and are listed in
  `tests/run.py`. Run one directly with `python tests/<file>.py` from the PeakCut folder.
- Commits: only when the user has said to commit. If they have, commit each task on a branch
  `live-transcriber` (create it first: `git switch -c live-transcriber`), ending each message with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Files

| File | Change |
|---|---|
| `studio/brain.py` | `peak` in the clip schema and rules; `judge_transcript` returns it |
| `studio/picks.py` (new) | Points, holds, cuts, sending a cut to the editor, restart tidy-up |
| `studio/live.py` | Pick mode in `Session`; `start_session(..., mode)`; restart keeps the mode |
| `studio/render.py` | `"split"` layout: `clean_split`, `geometry`, `_filter`, `render_clip` |
| `studio/creators.py` | New creator setting `split` |
| `studio/jobs.py` | `request_render` accepts `split`; `render_one` falls back to the creator's boxes; `queue_clip` |
| `studio/media.py` | `frame()` |
| `studio/server.py` | New routes; media route for holds and cuts |
| `web/index.html` | Live Transcriber view, cut Edit panel, box editor, split layout in the lists |
| `tests/fakestream.py` (new) | A pretend stream and test videos made with ffmpeg |
| `tests/test_picks.py`, `tests/test_split.py`, `tests/test_transcriber_routes.py` (new) | Tests |
| `tests/run.py`, `AGENTS.md`, `README.md` | List the new tests and the new view |

---

### Task 1: The AI names the moment itself (`peak`)

**Files:**
- Modify: `studio/brain.py` (`SCHEMA` near line 14, `RULES` near line 52, `judge_transcript` near line 363)
- Create: `tests/test_picks.py`

**Interfaces:**
- Produces: each item from `brain.judge_transcript(...)` has a `"peak"` key: a float inside `start..end`, or `None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_picks.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_picks.py`
Expected: a `KeyError: 'peak'` traceback on the first check.

- [ ] **Step 3: Implement**

In `studio/brain.py`, add to `SCHEMA["items"]["properties"]` (after `"reason"`):

```python
            "peak": {"type": "NUMBER"},
```

In `RULES`, after the `- score: ...` line, add:

```
- peak: the [seconds] value of the single instant people would share (the punchline, the reaction, the play).
  It lies between start and end.
```

Add next to `_tidy`:

```python
def _peak(c, s, e):
    """The moment itself, if the AI named one inside the clip; None otherwise."""
    p = _num(c.get("peak"), -1.0)
    return round(p, 2) if s <= p <= e else None
```

In `judge_transcript`, replace `clips.append(_tidy(c, s, e))` with:

```python
        t = _tidy(c, s, e)
        t["peak"] = _peak(c, s, e)
        clips.append(t)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_picks.py`
Expected: five `PASS` lines and `ALL PASSED`.

- [ ] **Step 5: Run the whole suite** — `python tests/run.py` → `All tests passed.`

- [ ] **Step 6: Commit** (only if the user said to commit)

```bash
git add studio/brain.py tests/test_picks.py
git commit -m "Live: the AI names the moment itself (peak) inside each clip it suggests"
```

---

### Task 2: The maths of points and cuts

**Files:**
- Create: `studio/picks.py`
- Modify: `tests/test_picks.py`

**Interfaces:**
- Produces: `picks.HOLD_BEFORE`, `HOLD_AFTER`, `LEAD_DEFAULT`, `LEAD_MAX`, `LEN_MIN`, `LEN_MAX` (floats);
  `picks.point_time(c: dict) -> float`;
  `picks.plan_cut(t, hold_from, hold_to, lead, length) -> (start: float, end: float, note: str)`, raising
  `ValueError` for non-numbers.

- [ ] **Step 1: Write the failing tests** — in `tests/test_picks.py`, above the `# (later tasks ...)` line, add:

```python
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
```

- [ ] **Step 2: Run** `python tests/test_picks.py` — Expected: `ModuleNotFoundError` or `ImportError` for `picks`.

- [ ] **Step 3: Implement** — create `studio/picks.py`:

```python
"""Live Transcriber: the moments a clipper can pick from a live stream (points), the stretch of stream kept around
each one so it can still be cut after the buffer has moved on (holds), and the clips cut from them (cuts)."""
import math

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
```

- [ ] **Step 4: Run** `python tests/test_picks.py` — Expected: `ALL PASSED`.

- [ ] **Step 5: Commit** (only if allowed)

```bash
git add studio/picks.py tests/test_picks.py
git commit -m "Live Transcriber: where a moment is, and how a clip around it is cut"
```

---

### Task 3: The "Game + cam" layout in the renderer

**Files:**
- Modify: `studio/render.py` (`LAYOUTS` line 81, `geometry` line 111, `_filter` line 230, `render_clip` line 244,
  `_render_in` line 262)
- Create: `tests/fakestream.py`, `tests/test_split.py`

**Interfaces:**
- Produces: `render.LAYOUTS["split"]`; `render.clean_split(v) -> dict | None` returning
  `{"cam": [x,y,w,h], "game": [x,y,w,h], "cam_pct": int}`; `render.geometry(src_w, src_h, layout, split=None)`;
  `render._filter(layout, fg_h, split=None)`; `render.render_clip` reads `clip["split"]`.
- Produces: `tests/fakestream.make_video(path, seconds=4, size="1280x720")` and
  `tests/fakestream.make_buffer(buf, seconds=150, size="320x180")`.

- [ ] **Step 1: Create the test helpers** — `tests/fakestream.py`:

```python
"""Pretend videos and a pretend live stream for tests: a test picture and a tone, made with ffmpeg, so nothing needs
the internet. make_buffer writes 6-second pieces exactly as a recording does (p01_00000.ts ... and p01.csv)."""
import subprocess
from pathlib import Path


def _src(seconds, size):
    return ["-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]


def make_video(path: Path, seconds=4, size="1280x720"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *_src(seconds, size), "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def make_buffer(buf: Path, seconds=150, size="320x180"):
    buf.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *_src(seconds, size), "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    "-f", "segment", "-segment_time", "6", "-reset_timestamps", "1", "-segment_format", "mpegts",
                    "-segment_list", "p01.csv", "-segment_list_type", "csv", "p01_%05d.ts"], cwd=buf, check=True)
    return buf
```

- [ ] **Step 2: Write the failing test** — `tests/test_split.py`:

```python
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
```

- [ ] **Step 3: Run** `python tests/test_split.py` — Expected: `FAIL the layout is offered` then an `AttributeError`
  for `clean_split`.

- [ ] **Step 4: Implement** in `studio/render.py`:

Add `import math` to the imports. Replace the `LAYOUTS` line with:

```python
LAYOUTS = {"blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)",
           "split": "Gameplay on top, facecam below"}
SPLIT_GAME = [0.0, 0.0, 1.0, 1.0]   # with no gameplay box drawn, the whole frame goes on top


def clean_split(v):
    """The boxes for the split layout, or None when there is no usable facecam box. A box is [x, y, w, h] as parts of
    the frame (0 to 1); anything running off the frame is pulled back inside."""
    if not isinstance(v, dict):
        return None

    def box(b):
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            return None
        try:
            x, y, w, h = (float(n) for n in b)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(n) for n in (x, y, w, h)):
            return None
        x, y = min(max(x, 0.0), 0.95), min(max(y, 0.0), 0.95)
        w, h = min(max(w, 0.05), 1.0 - x), min(max(h, 0.05), 1.0 - y)
        return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]

    cam = box(v.get("cam"))
    if not cam:
        return None
    try:
        pct = int(float(v.get("cam_pct", 40)))
    except (TypeError, ValueError):
        pct = 40
    return {"cam": cam, "game": box(v.get("game")) or list(SPLIT_GAME), "cam_pct": max(25, min(50, pct))}
```

Replace `geometry` with:

```python
def geometry(src_w, src_h, layout, split=None):
    """Where the video sits in the 1080x1920 frame, and where hook and captions go."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout == "split" and not split:
        layout = "blur"  # no facecam box drawn yet: the usual layout
    if layout == "split":
        game_h = H - int(round(H * split["cam_pct"] / 100 / 2)) * 2
        return layout, game_h, 230, game_h          # captions across the line between gameplay and facecam
    if layout == "blur":
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2
    if layout == "crop":
        return layout, fg_h, 300, 1380
    return layout, fg_h, max(230, top - 190), min(1640, top + fg_h + 180)
```

Replace `_filter` with:

```python
def _filter(layout, fg_h, split=None):
    if layout == "split":
        gx, gy, gw, gh = split["game"]
        cx, cy, cw, ch = split["cam"]
        cam_h = H - fg_h
        return (f"[0:v]fps={config.OUT_FPS},split=2[g][c];"
                f"[g]crop=iw*{gw}:ih*{gh}:iw*{gx}:ih*{gy},"
                f"scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[top];"
                f"[c]crop=iw*{cw}:ih*{ch}:iw*{cx}:ih*{cy},"
                f"scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[bot];"
                f"[top][bot]vstack=inputs=2,setsar=1[v0];")
    if layout == "crop":
        return f"[0:v]fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0];"
    return (f"[0:v]fps={config.OUT_FPS},split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1[v0];")
```

In `render_clip`, replace the `geometry` line and the `_render_in` call:

```python
    split = clean_split(clip.get("split"))
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, clip.get("layout", "blur"), split)
```

```python
        return _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, split)
```

Change the `_render_in` signature and its filter line:

```python
def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, split=None) -> dict:
```

```python
    fc = _filter(layout, fg_h, split) + f"[v0]ass={name}.ass:fontsdir=f[v]"
```

- [ ] **Step 5: Run** `python tests/test_split.py` — Expected: `ALL PASSED` (the render takes a few seconds).

- [ ] **Step 6: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 7: Commit** (only if allowed)

```bash
git add studio/render.py tests/fakestream.py tests/test_split.py
git commit -m "New layout: gameplay on top, facecam below"
```

---

### Task 4: Facecam boxes saved per creator, used by every clip

**Files:**
- Modify: `studio/creators.py` (`DEFAULTS` line 17, `_clean` near line 52)
- Modify: `studio/jobs.py` (`request_render` line 338, `render_one` line 364; add `queue_clip`)
- Modify: `tests/test_split.py`

**Interfaces:**
- Consumes: `render.clean_split` (Task 3).
- Produces: creator key `"split"` (`None` or the cleaned dict); `jobs.queue_clip(jid: str, cid: str) -> None`;
  `jobs.request_render` accepts `changes["split"]`; `render_one` copies the creator's boxes onto a split clip
  that has none.

- [ ] **Step 1: Write the failing test** — in `tests/test_split.py`, before the `print("ALL PASSED"...` line, add:

```python
# ---- boxes saved on the creator
from studio import creators, ctx, jobs
ctx.set_uid("u_split1")
cr = creators.create({"name": "Box Tester", "split": BOXES})
check("a creator keeps their facecam boxes", creators.get(cr["id"])["split"], render.clean_split(BOXES))
check("junk boxes are not kept", creators.update(cr["id"], {"split": {"cam": "x"}})["split"], None)
creators.update(cr["id"], {"split": BOXES})
job = jobs.create({"type": "file", "path": str(src), "name": "src.mp4"}, {"layout": "split"}, enqueue=False, creator=cr["id"])
job.update(source_path=str(src), width=1280, height=720, duration=4.0, status="done",
           clips=[{"id": "clip001", "start": 0.0, "end": 3.0, "score": 0, "hook": "", "title": "", "description": "",
                   "hashtags": [], "highlight_words": [], "style": "bounce", "layout": "split", "status": "queued", "version": 0}])
brain_fail = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no AI in tests"))
from studio import brain; brain.ask_ai = brain_fail
jobs.render_one(job, job["clips"][0], [{"w": "hello", "s": 0.2, "e": 0.6}])
c0 = job["clips"][0]
check("a split clip with no boxes of its own uses the creator's", (c0["status"], c0.get("split"), c0.get("layout")),
      ("done", render.clean_split(BOXES), "split"))
jobs._q.put = lambda *a, **k: None                       # nothing is rendered by this check, only recorded
jobs.request_render(job["id"], "clip001", {"layout": "split", "split": {"cam": [0, 0, 0.3, 0.3], "cam_pct": 30}})
check("re-making a clip can change its boxes", job["clips"][0]["split"], {"cam": [0.0, 0.0, 0.3, 0.3], "game": [0.0, 0.0, 1.0, 1.0], "cam_pct": 30})
```

- [ ] **Step 2: Run** `python tests/test_split.py` — Expected: `FAIL a creator keeps their facecam boxes` (got `KeyError`
  or `None`).

- [ ] **Step 3: Implement**

`studio/creators.py`: in `DEFAULTS`, after `"channels": {}`, add `"split": None` (inside the dict). In `_clean`,
before the final `else:` branch, add:

```python
        elif k == "split":
            from . import render
            v = render.clean_split(v)
```

and change the guard at the top of the loop so an explicit junk value still clears the boxes:
`if k not in data or (data[k] is None and k != "split"):` — keep everything else as it is.

`studio/jobs.py`: in `request_render`, after the `for k in ("hook", "title", ...)` loop, add:

```python
    if "split" in changes:
        new["split"] = render.clean_split(changes["split"])
```

In `render_one`, right after `clip["status"] = "rendering"` and `save(job)`, add:

```python
    if clip.get("layout") == "split" and not render.clean_split(clip.get("split")) and job.get("creator"):
        boxes = render.clean_split((creators.get(job["creator"]) or {}).get("split"))
        if boxes:
            clip["split"] = boxes     # a copy: drawing new boxes later does not change this clip
```

Add after `request_render`:

```python
def queue_clip(jid, cid):
    """Make one clip of a video on the worker, like a re-make."""
    _q.put(("clip", jid, cid))
```

- [ ] **Step 4: Run** `python tests/test_split.py` — Expected: `ALL PASSED`.

- [ ] **Step 5: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 6: Commit** (only if allowed)

```bash
git add studio/creators.py studio/jobs.py tests/test_split.py
git commit -m "Facecam boxes are saved per creator and used by their split clips"
```

---

### Task 5: Pick mode: points and holds

**Files:**
- Modify: `studio/picks.py`, `studio/live.py`
- Modify: `tests/test_picks.py`

**Interfaces:**
- Consumes: `picks.point_time` (Task 2); `Session.segments`, `Session.clock_fix`, `Session._align`, `Session.lock`,
  `Session.words`, `Session.ended`, `Session.dir` (existing, `studio/live.py`).
- Produces:
  - `picks.add_point(s, c: dict) -> dict | None` (c has `start`, `end`, optional `peak`, `score`, `reason`, `from`);
  - `picks.waiting(s) -> list`; `picks.flush_holds(s) -> None`; `picks.save_hold(s, p) -> None`;
  - `live.Session.pick: bool`; `live.Session.mark(t: float) -> dict | None`;
  - `live.start_session(creator, url=None, mode="auto")`; pick jobs have `job["mode"] == "pick"`,
    `job["points"] == []`, `job["cuts"] == []` at start.
  - A point: `{"id": "pt001", "t", "start", "end", "score", "reason", "text", "from", "made_at",
    "hold": {"status": "waiting"|"saving"|"ready"|"error", "file": "points/pt001.mp4"|None, "from", "to", "error"}}`.

- [ ] **Step 1: Write the failing tests** — in `tests/test_picks.py`, above `# (later tasks ...)`, add:

```python
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
```

- [ ] **Step 2: Run** `python tests/test_picks.py` — Expected: failure at `s.pick` (`AttributeError`).

- [ ] **Step 3: Implement points and holds** — append to `studio/picks.py` (and extend its imports):

```python
import time
from pathlib import Path

from . import jobs, media, safe


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
```

- [ ] **Step 4: Implement pick mode in `studio/live.py`**

Add `picks` to the `from . import ...` line. In `Session.__init__`, after `self.job, self.settings = job, settings`:

```python
        self.pick = job.get("mode") == "pick"   # Live Transcriber: moments are listed for the clipper, not clipped
```

In `_ai_check`:
- after `st = self.settings`, add `prev = self.job["points"] if self.pick else self.job["clips"]`;
- change the hourly cap test to `if len(recent) >= st["live_per_hour"] and not must and not self.pick:`;
- pass `prev` instead of `self.job["clips"]` as the 8th argument of `brain.judge_transcript`;
- change the gap test to `if any(abs(c["start"] - k["start"]) < st["min_gap_min"] * 60 for k in prev):`;
- just before `self._add_clip(c)`, add
  `c["from"] = "jump" if (why or "").startswith("Jump") else "said" if why else "ai"`;
- in the `except` around `brain.judge_transcript`, so a jump or "clip it" is still listed when the AI cannot be
  asked, replace the body with:

```python
        except Exception as e:  # noqa: BLE001
            self.job["ai"]["last_error"] = str(e)[-200:]
            if self.pick and window:      # no AI: a jump in loudness or chat, or "clip it", is still worth listing
                jump = (why or "").startswith("Jump")
                t = now - (self.AUTO_AFTER if jump else self.SAID_AFTER + 5)
                picks.add_point(self, {"start": ws, "end": now, "peak": max(ws, t), "score": 0, "reason": why or "",
                                       "from": "jump" if jump else "said"})
            return self._verdict(verdict, "AI error: " + str(e)[-80:])
```

In `_triggers`, change the appended request to
`self.manual.append({"start": max(0.0, t - 30), "end": t + 2, "t": t, "from": "trigger", "why": f'Someone said "{p}"'})`.

In `_manual_step`, at the top of the loop body (after `self.manual.remove(req)`), add:

```python
            if self.pick:          # a trigger word in pick mode: list the moment, no AI call needed
                t = req.get("t", req["end"])
                picks.add_point(self, {"start": req["start"], "end": req["end"], "peak": max(req["start"], t - 5),
                                       "score": 0, "reason": req.get("why") or "Marked", "from": req.get("from", "trigger")})
                continue
```

(`t - 5`: people say "clip that" a few seconds after the moment.)

Replace `_add_clip` with:

```python
    def _add_clip(self, c):
        if self.pick:
            return picks.add_point(self, c)
        self.n += 1
        c.update(id=f"clip{self.n:03d}", style=self.settings["style"], layout=self.settings["layout"],
                 status="queued", version=0, made_at=time.time())
        with self.lock:
            self.job["clips"].append(c)
        jobs.save(self.job)
        jobs.submit_live(lambda: self._render(c))
        return c
```

Add after `clip_last`:

```python
    def mark(self, t):
        """The clipper marked this moment in the transcript (Live Transcriber)."""
        return picks.add_point(self, {"start": t, "end": t, "peak": t, "score": 0, "reason": "Marked by you", "from": "you"})
```

In `_analyze`, right after `self._manual_step()`, add:

```python
                if self.pick:
                    picks.flush_holds(self)
```

and change the loop's exit test so a waiting hold is never lost:

```python
            holds = self.pick and bool(picks.waiting(self))
            if (self.user_stopped and not self.manual and not holds) or (self.ended and not busy and len(self.pending) < SR * 2 and not self.manual and not holds):
                break
```

In `start_session`, change the signature to `def start_session(creator: dict, url=None, mode="auto") -> dict:` and after
`job.update(status="live", stage="Connecting to the stream")` add:

```python
    if mode == "pick":
        job.update(mode="pick", points=[], cuts=[])
```

In `recover`, before `def cleanup():`, add:

```python
    for p in job.get("points", []):
        if p["hold"]["status"] in ("waiting", "saving"):     # interrupted: save it from the pieces still on disk
            p["hold"]["status"] = "saving"
            jobs.submit_live(lambda p=p: picks.save_hold(s, p))
```

In `start()`, make restarts keep the mode: append `j.get("mode") or "auto"` to the `paused` tuple, and in
`_resume_recordings` loop over `for uid, cid, url, mode in paused:`, call `start_session(c, url, mode)` and append
`(uid, cid, url, mode)` to `left`.

- [ ] **Step 5: Run** `python tests/test_picks.py` — Expected: `ALL PASSED`.

- [ ] **Step 6: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 7: Commit** (only if allowed)

```bash
git add studio/picks.py studio/live.py tests/test_picks.py
git commit -m "Live Transcriber: moments are listed, and two minutes of stream are kept around each"
```

---

### Task 6: Cuts: the clips the clipper downloads

**Files:**
- Modify: `studio/picks.py`, `studio/live.py` (`start()`), `tests/test_picks.py`

**Interfaces:**
- Consumes: `picks.plan_cut` (Task 2), points and holds (Task 5).
- Produces: `picks.point_of(job, pid) -> dict` and `picks.cut_of(job, cut_id) -> dict` (raise `LookupError`);
  `picks.make_cut(job, pid, lead, length) -> dict`; `picks.delete_point(job, pid)`; `picks.delete_cut(job, cut_id)`;
  `picks.after_restart()`. A cut: `{"id": "cut001", "point", "lead", "length", "start", "end", "note",
  "status": "making"|"ready"|"error", "file": "cuts/cut001.mp4"|None, "error", "made_at"}`.

- [ ] **Step 1: Write the failing tests** — in `tests/test_picks.py`, above `# (later tasks ...)`, add:

```python
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
```

- [ ] **Step 2: Run** `python tests/test_picks.py` — Expected: `AttributeError: ... has no attribute 'make_cut'`.

- [ ] **Step 3: Implement** — append to `studio/picks.py`:

```python
def point_of(job, pid):
    p = next((p for p in job.get("points", []) if p["id"] == pid), None)
    if not p:
        raise LookupError("No such moment")
    return p


def cut_of(job, cut_id):
    c = next((c for c in job.get("cuts", []) if c["id"] == cut_id), None)
    if not c:
        raise LookupError("No such clip")
    return c


def make_cut(job, pid, lead, length):
    """A clip `lead` seconds before the moment and `length` long, cut exactly from the kept stretch. Made on the live
    render worker; the cut is listed at once as "making"."""
    p = point_of(job, pid)
    h = p["hold"]
    if h["status"] in ("waiting", "saving"):
        raise ValueError("This moment's video is still being saved. Try again in a few seconds.")
    if h["status"] != "ready":
        raise ValueError(h.get("error") or "This moment's video could not be kept.")
    start, end, note = plan_cut(p["t"], h["from"], h["to"], lead, length)
    cut = {"id": _next_id(job, "next_cut", "cut"), "point": pid, "lead": round(p["t"] - start, 2),
           "length": round(end - start, 2), "start": start, "end": end, "note": note, "status": "making",
           "file": None, "error": None, "made_at": time.time()}
    job.setdefault("cuts", []).append(cut)
    jobs.save(job)
    jobs.submit_live(lambda: _encode_cut(job, p, cut))
    return cut


def _encode_cut(job, p, cut):
    d = Path(job["dir"])
    (d / "cuts").mkdir(exist_ok=True)
    try:
        media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cut['start'] - p['hold']['from']:.2f}",
                   "-i", str(d / p["hold"]["file"]), "-t", f"{cut['end'] - cut['start']:.2f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(d / "cuts" / f"{cut['id']}.mp4")])
        cut.update(status="ready", file=f"cuts/{cut['id']}.mp4", error=None)
    except Exception as e:  # noqa: BLE001
        cut.update(status="error", error=safe.friendly(e, 300))
    jobs.save(job)


def _unlink(job, rel):
    if rel:
        (Path(job["dir"]) / rel).unlink(missing_ok=True)


def delete_cut(job, cut_id):
    c = cut_of(job, cut_id)
    if c["status"] == "making":
        raise ValueError("This clip is still being made. Try again in a moment.")
    _unlink(job, c.get("file"))
    job["cuts"].remove(c)
    jobs.save(job)


def delete_point(job, pid):
    p = point_of(job, pid)
    if p["hold"]["status"] == "saving" or any(c["point"] == pid and c["status"] == "making" for c in job.get("cuts", [])):
        raise ValueError("This moment is still being saved. Try again in a moment.")
    for c in [c for c in job.get("cuts", []) if c["point"] == pid]:
        _unlink(job, c.get("file"))
        job["cuts"].remove(c)
    _unlink(job, p["hold"].get("file"))
    job["points"].remove(p)
    jobs.save(job)


def after_restart():
    """Cuts that were being made when PeakCut stopped: marked so the clipper can make them again."""
    for job in jobs.all_jobs():
        stuck = [c for c in job.get("cuts", []) if c.get("status") == "making"]
        for c in stuck:
            c.update(status="error", error="PeakCut was closed while this clip was being made. Make it again.")
        if stuck:
            jobs.save(job)
```

In `studio/live.py` `start()`, add `picks.after_restart()` as its first line.

- [ ] **Step 4: Run** `python tests/test_picks.py` — Expected: `ALL PASSED`.

- [ ] **Step 5: Commit** (only if allowed)

```bash
git add studio/picks.py studio/live.py tests/test_picks.py
git commit -m "Live Transcriber: cut a clip around a moment with a chosen lead-in and length"
```

---

### Task 7: Edit: a cut becomes a normal PeakCut clip

**Files:**
- Modify: `studio/picks.py`, `tests/test_picks.py`

**Interfaces:**
- Consumes: `jobs.create`, `jobs.save`, `jobs.submit`, `jobs.render_one` (existing), `jobs.queue_clip` is NOT used
  here (the first render needs `describe` first), `render.clean_split` (Task 3), `brain.describe` (existing).
- Produces: `picks.edit_cut(job, cut_id, options: dict) -> dict` (the new video job). `options` keys:
  `style`, `layout`, `hook`, `split` (all optional). The new job: `kind "video"`, `source {"type": "cut", ...}`,
  `from_live {"job", "cut"}`, one clip `clip001` covering the whole cut; the cut gets `"edited": <new job id>`.

- [ ] **Step 1: Write the failing test** — in `tests/test_picks.py`, above `# (later tasks ...)`, add:

```python
# ---- Edit: a cut goes to the editor
from studio import brain, config, render
config.FONTS_DIR.mkdir(parents=True, exist_ok=True); render.install_fonts()
jobs.submit = lambda fn: fn()
def fake_ai(prompt, schema=None, purpose=""):
    if "clipped this moment by hand" in prompt:
        return [{"start": 0, "end": 0, "score": 0, "hook": "NO WAY", "title": "He did it", "description": "d",
                 "hashtags": ["gaming"], "reason": "r"}], "m", 5
    raise RuntimeError("no AI for captions in tests")
brain.ask_ai = fake_ai
live.SESSIONS[job["id"]] = s                           # the live words come from the session, as while recording
cm = picks.make_cut(job, m["id"], 10, 20)
creators.update(cr["id"], {"split": {"cam": [0.7, 0.0, 0.3, 0.3], "cam_pct": 40}})
new = picks.edit_cut(job, cm["id"], {"style": "hype", "layout": "split"})
nc = new["clips"][0]
check("the cut becomes a video under the same creator", (new["creator"], new["kind"], new["source"]["type"]), (cr["id"], "video", "cut"))
check("...with one clip covering the whole cut", (nc["start"], round(nc["end"]), nc["style"], nc["layout"]), (0.0, 20, "hype", "split"))
check("...rendered, with the AI's hook and the creator's facecam boxes", (nc["status"], nc["hook"], nc["split"]["cam"]), ("done", "NO WAY", [0.7, 0.0, 0.3, 0.3]))
words = __import__("json").loads((Path(new["dir"]) / "words.json").read_text(encoding="utf-8"))
check("...and the live words, moved to start at zero", (words[0]["s"], words[0]["w"]), (0.0, f"w{int(cm['start'])}"))
check("the cut remembers it was edited", picks.cut_of(job, cm["id"])["edited"], new["id"])
new2 = picks.edit_cut(job, cm["id"], {"hook": "my own words", "layout": "blur"})
check("a hook typed by the clipper is used as it is", new2["clips"][0]["hook"], "my own words")
try:
    picks.edit_cut(job, cm["id"], {"layout": "nonsense"}); said = "accepted"
except ValueError:
    said = "refused"
check("an unknown layout falls back instead of failing", said, "accepted")
```

- [ ] **Step 2: Run** `python tests/test_picks.py` — Expected: `AttributeError: ... 'edit_cut'`.

- [ ] **Step 3: Implement** — append to `studio/picks.py` (and add `import json`, `import shutil` at the top, and
  `brain, creators, render, usage` to the `from . import ...` line):

```python
def _words_of(job):
    """The live transcript: from the running session, else from the words saved next to the recording."""
    from . import live
    s = live.SESSIONS.get(job["id"])
    if s:
        with s.lock:
            return list(s.words)
    try:
        return json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def edit_cut(job, cut_id, options):
    """Send a cut to the editor: it becomes a normal video of the same creator with one clip covering the whole cut,
    which is then made (captions, layout, hook) like any other clip and can be re-made and posted from there."""
    cut = cut_of(job, cut_id)
    if cut["status"] != "ready":
        raise ValueError("This clip is still being made. Try again in a moment.")
    p = point_of(job, cut["point"])
    cr = (creators.get(job["creator"]) if job.get("creator") else None) or {}
    style = options.get("style") if options.get("style") in render.STYLES else cr.get("style", "bounce")
    layout = options.get("layout") if options.get("layout") in render.LAYOUTS else cr.get("layout", "blur")
    hook = str(options.get("hook") or "").strip()[:60]
    boxes = render.clean_split(options.get("split"))
    words = [dict(w, s=round(w["s"] - cut["start"], 2), e=round(w["e"] - cut["start"], 2))
             for w in _words_of(job) if cut["start"] <= w["s"] <= cut["end"]]
    m, s = divmod(int(p["t"]), 60)
    new = jobs.create({"type": "cut", "path": "", "name": f"{job.get('title') or 'Live stream'} at {m}:{s:02d}"},
                      {"style": style, "layout": layout}, enqueue=False, creator=job.get("creator"))
    dest = Path(new["dir"]) / "source.mp4"
    shutil.copyfile(Path(job["dir"]) / cut["file"], dest)
    meta = media.probe(dest)
    (Path(new["dir"]) / "words.json").write_text(json.dumps(words), encoding="utf-8")
    clip = {"id": "clip001", "start": 0.0, "end": round(meta["duration"], 2), "score": p["score"],
            "reason": p["reason"], "hook": hook, "title": "", "description": "", "hashtags": [], "highlight_words": [],
            "category": "other", "style": style, "layout": layout, "status": "queued", "version": 0,
            "made_at": time.time()}
    if boxes:
        clip["split"] = boxes
    new["source"]["path"] = str(dest)
    new.update(source_path=str(dest), status="done", stage="Made from a live moment", title=new["source"]["name"],
               uploader=job.get("uploader", ""), duration=meta["duration"], width=meta["width"],
               height=meta["height"], clips=[clip], from_live={"job": job["id"], "cut": cut_id}, remake=["clip001"])
    jobs.save(new)
    cut["edited"] = new["id"]
    jobs.save(job)
    title, up, text = job.get("title") or "", job.get("uploader") or "", " ".join(w["w"] for w in words)

    def make():
        usage.set_context(new.get("creator"), title or "Live stream")
        d = brain.describe(text, title, up)
        for k in ("title", "description", "hashtags", "highlight_words", "category"):
            clip[k] = d.get(k, clip[k])
        if not hook:
            clip["hook"] = d.get("hook", "")
        jobs.render_one(new, clip, words)
        new.pop("remake", None)                 # made: nothing to pick up after a restart
        jobs.save(new)

    jobs.submit(make)
    return new
```

(`remake=["clip001"]` makes `jobs._pick_up_after_restart` re-queue the clip if PeakCut stops before it is made.)

- [ ] **Step 4: Run** `python tests/test_picks.py` — Expected: `ALL PASSED`.

- [ ] **Step 5: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 6: Commit** (only if allowed)

```bash
git add studio/picks.py tests/test_picks.py
git commit -m "Live Transcriber: Edit turns a downloaded clip into a normal PeakCut clip"
```

---

### Task 8: The routes

**Files:**
- Modify: `studio/media.py` (add `frame`), `studio/server.py`
- Create: `tests/test_transcriber_routes.py`

**Interfaces:**
- Consumes: everything in `picks` (Tasks 2, 5, 6, 7), `live.start_session(..., mode="pick")`, `live.mine`.
- Produces (all JSON unless said): `POST /api/transcriber/start {creator, url}` → public job;
  `GET /api/live/{jid}/transcript?after=` → `{"lines": [{"s","e","text"}], "live": bool}`;
  `POST /api/live/{jid}/points {t}` → point; `DELETE /api/live/{jid}/points/{pid}`;
  `POST /api/live/{jid}/points/{pid}/cuts {lead, length}` → cut; `DELETE /api/live/{jid}/cuts/{cut}`;
  `POST /api/live/{jid}/cuts/{cut}/edit {style, layout, hook, split}` → public new job;
  `GET /api/live/{jid}/cuts/{cut}/frame` and `GET /api/jobs/{jid}/clips/{cid}/frame` → `image/jpeg`;
  `GET /media/{jid}/{points|cuts}/{name}.mp4` → the file (`?download=1` to save).
  `media.frame(src: Path, at: float) -> bytes`.

- [ ] **Step 1: Write the failing test** — `tests/test_transcriber_routes.py`:

```python
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
```

- [ ] **Step 2: Run** `python tests/test_transcriber_routes.py` — Expected: 404s / FAIL lines (routes missing).

- [ ] **Step 3: Implement `media.frame`** — append to `studio/media.py`:

```python
def frame(src: Path, at: float) -> bytes:
    """One picture from a video, as JPEG, for drawing the facecam box on."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at):.2f}", "-i", str(src), "-frames:v", "1",
                        "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                       capture_output=True, creationflags=NO_WINDOW, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("Couldn't take a picture from this video.")
    return p.stdout
```

- [ ] **Step 4: Implement the routes** — in `studio/server.py`, add `picks` to the
  `from . import (accounts, ai, ...)` list, then add after `clip_now` (the `/api/live/{jid}/clip` route):

```python
# ---------------------------------------------------------------- Live Transcriber
def _pick_job(jid: str):
    job = _job_or_404(jid)
    if job.get("mode") != "pick":
        raise HTTPException(404, "No such recording")
    return job


def _picks_call(fn, *a):
    try:
        return fn(*a)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e) or "Those numbers aren't valid.")


@app.post("/api/transcriber/start")
def transcriber_start(body: dict = Body(...)):
    _need_verified()
    _quota()
    try:
        c = creators.get(str(body.get("creator") or ""))
    except ValueError:
        c = None
    if not c:
        raise HTTPException(404, "Choose a creator first.")
    url = str(body.get("url") or "").strip()[:500] or None
    try:
        return jobs.public(live.start_session(c, url, mode="pick"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/live/{jid}/transcript")
def transcript_lines(jid: str, after: float = -1.0):
    job = _pick_job(jid)
    s = live.mine(jid)
    words = picks._words_of(job)
    lines = brain.make_lines([w for w in words if w["s"] > after])
    return {"lines": [{"s": round(l["s"], 2), "e": round(l["e"], 2), "text": l["text"]} for l in lines][-400:],
            "live": bool(s and not s.ended)}


@app.post("/api/live/{jid}/points")
def mark_point(jid: str, body: dict = Body(...)):
    _pick_job(jid)
    s = live.mine(jid)
    if not s or s.ended:
        raise HTTPException(404, "This recording has already ended")
    _throttle("mark:" + ctx.uid(), 30, 60, "That's a lot of marks at once. Wait a moment.")
    try:
        t = float(body.get("t"))
    except (TypeError, ValueError):
        raise HTTPException(400, "That time isn't valid.")
    if not (0 <= t <= s.t_done):
        raise HTTPException(400, "That moment hasn't been transcribed yet.")
    p = s.mark(t)
    if not p:
        raise HTTPException(409, "That moment is already in the list.")
    return p


@app.delete("/api/live/{jid}/points/{pid}")
def delete_point(jid: str, pid: str):
    _picks_call(picks.delete_point, _pick_job(jid), pid)
    return {"ok": True}


@app.post("/api/live/{jid}/points/{pid}/cuts")
def new_cut(jid: str, pid: str, body: dict = Body(...)):
    job = _pick_job(jid)
    _throttle("cut:" + ctx.uid(), 30, 600, "That's a lot of clips in a short time. Wait a few minutes.")
    return _picks_call(picks.make_cut, job, pid, body.get("lead", picks.LEAD_DEFAULT), body.get("length", 60))


@app.delete("/api/live/{jid}/cuts/{cut}")
def delete_cut(jid: str, cut: str):
    _picks_call(picks.delete_cut, _pick_job(jid), cut)
    return {"ok": True}


@app.post("/api/live/{jid}/cuts/{cut}/edit")
def edit_cut(jid: str, cut: str, body: dict = Body(default={})):
    _need_verified()
    _quota()
    _job_slots()
    job = _pick_job(jid)
    _throttle("cutedit:" + ctx.uid(), 20, 600, "That's a lot of edits in a short time. Wait a few minutes.")
    return jobs.public(_picks_call(picks.edit_cut, job, cut, body if isinstance(body, dict) else {}))


@app.get("/api/live/{jid}/cuts/{cut}/frame")
def cut_frame(jid: str, cut: str):
    job = _pick_job(jid)
    c = _picks_call(picks.cut_of, job, cut)
    if c["status"] != "ready":
        raise HTTPException(409, "This clip is still being made.")
    try:
        img = media.frame(Path(job["dir"]) / c["file"], min(2.0, (c["end"] - c["start"]) / 2))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/jobs/{jid}/clips/{cid}/frame")
def clip_frame(jid: str, cid: str):
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    src = Path(clip["src"]) if clip.get("src") else Path(job.get("source_path") or "")
    if not src.is_file():
        raise HTTPException(404, "The video this clip was cut from is no longer kept.")
    try:
        img = media.frame(src, clip["start"] - clip.get("src_offset", 0.0) + 1)
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
```

And next to `media_file` (the `/media/{jid}/{name}` route):

```python
@app.get("/media/{jid}/{folder}/{name}")
def media_sub(jid: str, folder: str, name: str, download: int = 0):
    """Live Transcriber files: the kept stretch around each moment, and the clips cut from them."""
    if folder not in ("points", "cuts") or not re.fullmatch(r"[\w\-]+\.mp4", name):
        raise HTTPException(404)
    f = Path(_job_or_404(jid)["dir"]) / folder / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, filename=name if download else None, headers={"Cache-Control": "private, max-age=300"})
```

Check `brain` is in the server's `from . import (...)` list; add it if it is not.

- [ ] **Step 5: Run** `python tests/test_transcriber_routes.py` — Expected: `ALL PASSED`.

- [ ] **Step 6: List the new tests** — in `tests/run.py`, add to `runs` after the storage entry:

```python
                ("live transcriber: moments, kept stream, cuts, edit", ["tests/test_picks.py"]),
                ("the game + cam layout", ["tests/test_split.py"]),
                ("live transcriber routes", ["tests/test_transcriber_routes.py"]),
```

- [ ] **Step 7: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 8: Commit** (only if allowed)

```bash
git add studio/media.py studio/server.py tests/test_transcriber_routes.py tests/run.py
git commit -m "Live Transcriber routes: transcript, moments, cuts, edit, frames"
```

---

### Task 9: The Live Transcriber view

**Files:**
- Modify: `web/index.html` (CSS block, `<dialog>` list near line 830, `drawShell` line 1246, `ROUTES` line 1303,
  `go` line 1322, `drawJobs` line 1787, `render` line 2162, the `submit` listener line 2209, the click handler
  from line 2237)

**Interfaces:**
- Consumes: the routes of Task 8; `S.jobs` items with `mode`, `points`, `cuts`; helpers `api`, `send`, `put`, `esc`,
  `ic`, `fmt`, `toast`, `ask`, `picker`, `stepper`, `scoreCls`, `jobOf`, `tick`.
- Produces: view key `"transcriber"` at `/app/transcriber`; functions `drawTranscriber()`, `ltPoll()`,
  `openCutEdit(jid, cutId)` (filled in by Task 10; here it only needs to exist).

- [ ] **Step 1: Hide pick-mode recordings from the old lists** — in `drawJobs`, change
  `let list = cid ? S.jobs.filter(j => j.creator === cid) : S.jobs;` to

```js
  let list = (cid ? S.jobs.filter(j => j.creator === cid) : S.jobs).filter(j => j.mode !== "pick");
```

- [ ] **Step 2: Navigation and routing**

In `drawShell`, after the `nav("library", ...)` item (before `` `<h4><span>Creators</span></h4>` ``), add:

```js
    nav("transcriber", navIc("radio", "transcriber"), "Live Transcriber",
      S.jobs.some(j => j.mode === "pick" && j.status === "live") ? `<span class="sb-tag rec" title="Recording now"><i></i>Rec</span>` : "") +
```

In the `title` map add `transcriber:"Live Transcriber"`, and below the other `sub` lines add
`if (S.view === "transcriber") sub = "Watch the transcript live, pick the moments, download the clips.";`.

`ROUTES`: add `transcriber: "transcriber"`.

In `go`, before the final `else view.innerHTML = ...` add:

```js
  else if (v === "transcriber") { view.innerHTML = `<div id="ltstart"></div><div class="lt-live" id="ltlive" hidden><section class="lt-tr"><h3>Transcript</h3><div id="ltlines" class="lt-lines" aria-live="off"></div></section><section><h3>Moments</h3><div id="ltpoints"></div></section></div><div id="ltdone"></div>`; S.ltJ = null; }
```

In `render`, add `if (S.view === "transcriber") drawTranscriber();`.

- [ ] **Step 3: CSS** — add to the stylesheet (keep the palette; no shadows):

```css
.lt-start{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:24px;padding:12px;border-radius:14px;background:var(--muted)}
.lt-start .composer-input{flex:1;min-width:220px}
.lt-live{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;margin-bottom:28px}
.lt-live h3,.lt-h{font-size:13px;font-weight:600;color:var(--dim);margin:0 0 8px}
.lt-lines{height:min(62vh,640px);overflow:auto;border:1px solid var(--line);border-radius:12px;padding:6px}
.lt-lines .ln{display:grid;grid-template-columns:52px 1fr auto;gap:8px;align-items:start;padding:6px;border-radius:8px}
.lt-lines .ln:hover{background:var(--muted)}
.lt-lines time{color:var(--faint);font-variant-numeric:tabular-nums;font-size:12px;padding-top:2px}
.lt-lines .ln .btn{opacity:0}.lt-lines .ln:hover .btn,.lt-lines .ln .btn:focus{opacity:1}
.pt{border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px;display:flex;flex-direction:column;gap:8px}
.pt-head{display:flex;align-items:center;gap:8px}.pt-head .grow{flex:1}
.pt video{width:100%;border-radius:8px;background:#17110E;aspect-ratio:16/9}
.pt .q{color:var(--dim);font-style:italic}
.cut{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px;border-radius:10px;background:var(--muted)}
.cut .grow{flex:1}
@media (max-width:860px){.lt-live{grid-template-columns:1fr}.lt-lines{height:45vh}}
```

- [ ] **Step 4: The view** — add a new section after the clip editor block (after the `#clipdlg` listeners):

```js
/* ============================================================ Live Transcriber */
const FROM = {ai:"AI", jump:"Loud moment", said:'"Clip it"', trigger:"Trigger word", you:"Marked by you"};
const ltJobs = () => S.jobs.filter(j => j.mode === "pick");
function ltKeep() {          // keep what the clipper typed in the number boxes across redraws
  S.ltIn = S.ltIn || {};
  $$("#ltlive input[id^=lead_], #ltlive input[id^=len_], #ltdone input[id^=lead_], #ltdone input[id^=len_]").forEach(i => {
    const [k, pid] = i.id.split("_"); (S.ltIn[pid] = S.ltIn[pid] || {})[k] = Number(i.value); });
}
function ltVal(p) {
  const c = S.creators.find(x => x.id === S.ltCreator) || {};
  const v = (S.ltIn || {})[p.id] || {};
  return {lead: v.lead ?? 20, len: v.len ?? (c.max_len || 60)};
}
function cutHtml(j, c) {
  const act = c.status === "making" ? `<span class="sub">Making…</span>`
    : c.status === "error" ? `<span class="err" style="margin:0">${esc(c.error)}</span>`
    : `<a class="btn sm" href="/media/${j.id}/${esc(c.file)}?download=1">${ic("down")}Download</a>
       <button class="btn sm" data-act="cutedit" data-j="${j.id}" data-c="${esc(c.id)}">${ic("sliders")}${c.edited ? "Edit again" : "Edit"}</button>`;
  return `<div class="cut"><span class="grow">${ic("film")} ${fmt(c.end - c.start)} clip from ${fmt(c.start)}</span>${act}
    <button class="btn sm icon ghost" data-act="cutdel" data-j="${j.id}" data-c="${esc(c.id)}" aria-label="Delete this clip">${ic("trash")}</button>
    ${c.note ? `<p class="sub" style="flex-basis:100%;margin:0">${esc(c.note)}</p>` : ""}</div>`;
}
function ptHtml(j, p) {
  const h = p.hold, v = ltVal(p), cuts = (j.cuts || []).filter(c => c.point === p.id);
  const kept = h.status === "ready" ? `<video src="/media/${j.id}/${esc(h.file)}#t=${Math.max(0, p.t - v.lead - h.from).toFixed(1)}" preload="metadata" controls playsinline></video>
      <div class="nsteps">${stepper("lead_" + p.id, "Seconds before", v.lead, 0, 60, 5, "s")}${stepper("len_" + p.id, "Length", v.len, 5, 120, 5, "s")}</div>
      <button class="btn primary" data-act="ptcut" data-j="${j.id}" data-p="${esc(p.id)}">${ic("down")}Download clip</button>`
    : h.status === "error" ? `<div class="err" style="margin:0">${esc(h.error)}</div>`
    : `<p class="sub">Keeping this part of the stream… it is ready a minute after the moment.</p>`;
  return `<div class="pt"><div class="pt-head"><b>${fmt(p.t)}</b>${p.score ? `<span class="badge ${scoreCls(p.score)}" style="position:static">${p.score}</span>` : ""}
      <span class="pill">${esc(FROM[p.from] || "AI")}</span><span class="grow"></span>
      <button class="btn sm icon ghost" data-act="ptdel" data-j="${j.id}" data-p="${esc(p.id)}" aria-label="Delete this moment">${ic("trash")}</button></div>
    ${p.reason ? `<p style="margin:0">${esc(p.reason)}</p>` : ""}${p.text ? `<p class="sub q" style="margin:0">“${esc(p.text)}”</p>` : ""}
    ${kept}${cuts.map(c => cutHtml(j, c)).join("")}</div>`;
}
function drawTranscriber() {
  const st = $("#ltstart"); if (!st) return;
  const busy = document.activeElement && document.activeElement.closest?.("#ltlive, #ltdone") && document.activeElement.matches("input");
  ltKeep();
  const live = ltJobs().find(j => j.status === "live"), done = ltJobs().filter(j => j.status !== "live");
  if (!S.ltCreator) S.ltCreator = live?.creator || S.creators[0]?.id || "";
  if (live) S.ltCreator = live.creator;
  put(st, live ? `<div class="lt-start"><span class="pill live"><span class="dot"></span>Live</span><b>${esc(live.title || live.source.name)}</b>
        <span class="sub">${esc(live.stage)} · ${fmt(live.live?.elapsed || 0)} recorded</span><span class="grow"></span>
        <button class="btn danger" data-act="stoplive" data-j="${live.id}">${ic("stop")}Stop</button></div>`
    : `<form class="lt-start" id="ltform">${picker("lt_creator", "Creator", [{v:"", label:"New creator from the link", icon:"plus"}, ...S.creators.map(x => ({v:x.id, label:x.name}))], S.ltCreator)}
        <div class="composer-input">${ic("link")}<input id="lt_url" placeholder="Stream link (empty: use the creator's channel)" autocomplete="off" aria-label="Stream link"></div>
        <button class="btn primary" id="lt_go"><span class="rdot"></span>Start</button></form>`);
  $("#ltlive").hidden = !live;
  if (live && !busy) put($("#ltpoints"), (live.points || []).length ? [...live.points].reverse().map(p => ptHtml(live, p)).join("")
    : `<p class="sub">Moments appear here as they happen. You can also press Mark on any line of the transcript.</p>`);
  if (!busy) put($("#ltdone"), done.length ? `<h3 class="lt-h">Finished recordings</h3>` + done.map(j => `<div class="pt" style="gap:12px">
      <div class="pt-head"><b>${esc(j.title || j.source.name)}</b><span class="sub">${when(j.created)} · ${(j.points || []).length} moments</span><span class="grow"></span>
        <button class="btn sm icon ghost" data-act="deljob" data-j="${j.id}" aria-label="Delete this recording">${ic("trash")}</button></div>
      ${[...(j.points || [])].reverse().map(p => ptHtml(j, p)).join("")}</div>`).join("")
    : live ? "" : `<div class="empty"><img src="/brand/favicon.svg" alt=""><b>Nothing recorded yet</b>Choose a creator, paste their stream link if you like, and press Start.</div>`);
  for (const j of ltJobs()) for (const c of j.cuts || []) if (c.status === "ready" && S.ltAuto?.has(c.id)) {
    S.ltAuto.delete(c.id);
    const a = document.createElement("a"); a.href = `/media/${j.id}/${c.file}?download=1`; a.download = ""; document.body.append(a); a.click(); a.remove();
  }
}
async function ltPoll() {
  const j = ltJobs().find(x => x.status === "live"), box = $("#ltlines");
  if (!box || !j) return;
  if (S.ltJ !== j.id) { S.ltJ = j.id; S.ltAfter = -1; box.innerHTML = ""; }
  const r = await api(`/api/live/${j.id}/transcript?after=${S.ltAfter}`);
  if (!r.lines.length) return;
  const follow = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.insertAdjacentHTML("beforeend", r.lines.map(l => `<div class="ln"><time>${fmt(l.s)}</time><span>${esc(l.text)}</span>
    <button class="btn sm ghost" data-act="ltmark" data-j="${j.id}" data-t="${l.s}">Mark</button></div>`).join(""));
  S.ltAfter = r.lines[r.lines.length - 1].e;
  if (follow) box.scrollTop = box.scrollHeight;
}
setInterval(() => { if (S.view === "transcriber" && !document.hidden) ltPoll().catch(() => {}); }, 4000);
async function ltStart() {
  let cid = $("#lt_creator").value; const url = $("#lt_url").value.trim();
  if (!cid) {
    if (!url) return toast("Paste the stream link, or choose a creator.", "bad");
    const name = (url.match(/(?:twitch\.tv|kick\.com|youtube\.com)\/@?([\w.-]+)/i) || [])[1] || "New creator";
    cid = (await send("/api/creators", {name, channel: url})).id;
  }
  const j = await send("/api/transcriber/start", {creator: cid, url});
  S.ltCreator = cid; if (!jobOf(j.id)) S.jobs.unshift(j);
  toast("Recording started. The transcript appears in a few seconds.", "ok"); render(); tick();
}
function openCutEdit(jid, cutId) { toast("Editing arrives in the next step.", "bad"); }   // replaced in Task 10
```

- [ ] **Step 5: Actions** — in the `submit` listener add
  `if (e.target.id === "ltform") { e.preventDefault(); ltStart().catch(x => toast(x.message, "bad")); }`.
  In the `change` listener (`document.addEventListener("change", async e => {`), after `const t = e.target;`, add
  `if (t.id === "lt_creator") { S.ltCreator = t.value; return; }` so the chosen creator survives redraws.
  In the click handler, before `if (a === "resendverify")`, add:

```js
    if (a === "ltmark") { await send(`/api/live/${d.j}/points`, {t:Number(d.t)}); toast("Added to the moments.", "ok"); return tick(); }
    if (a === "ptcut") {
      const lead = Number($("#lead_" + d.p)?.value ?? 20), length = Number($("#len_" + d.p)?.value ?? 60);
      const cut = await send(`/api/live/${d.j}/points/${d.p}/cuts`, {lead, length});
      (S.ltAuto = S.ltAuto || new Set()).add(cut.id);
      toast(cut.note || "Making your clip… it downloads by itself when it is ready.", "ok"); return tick();
    }
    if (a === "ptdel") { if (!await ask("Delete this moment?", "Its saved stream and the clips cut from it are deleted too.", "Delete", true)) return;
      await api(`/api/live/${d.j}/points/${d.p}`, {method:"DELETE"}); return tick(); }
    if (a === "cutdel") { if (!await ask("Delete this clip?", "The file is deleted. The moment stays, so you can cut it again.", "Delete", true)) return;
      await api(`/api/live/${d.j}/cuts/${d.c}`, {method:"DELETE"}); return tick(); }
    if (a === "cutedit") return openCutEdit(d.j, d.c);
```

- [ ] **Step 6: Check it by hand** — start PeakCut in a throwaway copy:

```bash
PEAKCUT_DATA=../pc-try PEAKCUT_PORT=8766 python -m studio.server
```

Open http://127.0.0.1:8766/app/transcriber. Check: the nav item shows; the empty state shows; with a creator that has
no channel, Start says "Add this creator's channel link first."; old creators' Clips lists look unchanged. With a
real English stream (any live Twitch channel): the transcript fills every few seconds, Mark adds a moment, after a
minute the moment shows a player and Download clip, and the clip downloads by itself when made. Stop ends the
recording and it moves to Finished recordings. Check at phone width (375 px) that the two columns stack.

- [ ] **Step 7: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 8: Commit** (only if allowed)

```bash
git add web/index.html
git commit -m "Live Transcriber view: live transcript, moments, and clip downloads"
```

---

### Task 10: Edit panel and box editor

**Files:**
- Modify: `web/index.html` (`<dialog>` list, `LAYOUT_SHORT` line 1386, CSS, `drawClipDlg` line 2123,
  `remake` action line 2439, the `openCutEdit` stub from Task 9)

**Interfaces:**
- Consumes: `POST /api/live/{jid}/cuts/{cut}/edit`, both `/frame` routes, `PUT /api/creators/{cid}` with `split`.
- Produces: `openCutEdit(jid, cutId)`, `openBoxes(src, cid, cur, done)` where `done(split)` gets
  `{game, cam, cam_pct}`.

- [ ] **Step 1: Layout lists and dialog** — add `<dialog id="boxdlg"></dialog>` after `<dialog id="formdlg"></dialog>`.
  Change `LAYOUT_SHORT` to `{blur:"Blurred fill", zoom:"Zoomed in", crop:"Fill screen", split:"Game + cam"}`. Add CSS:

```css
.lay-prev.split .ph{display:grid;grid-template-rows:3fr 2fr;gap:2px}
.lay-prev.split .ph i{display:none}
.lay-prev.split .ph::before,.lay-prev.split .ph::after{content:"";background:var(--line-2);border-radius:2px}
.boxes{width:min(760px,calc(100vw - 32px))}
.bx-stage{position:relative;user-select:none;touch-action:none;border-radius:10px;overflow:hidden;background:#17110E}
.bx-stage img{display:block;width:100%}
.bx{position:absolute;border:2px solid #fff;border-radius:4px;cursor:move}
.bx.cam{border-color:#FF8B33}.bx.game{border-color:#6BD7FF}
.bx span{position:absolute;left:4px;top:4px;font-size:11px;font-weight:600;color:#fff;background:rgba(0,0,0,.55);padding:1px 6px;border-radius:4px}
.bx i{position:absolute;right:-6px;bottom:-6px;width:12px;height:12px;border-radius:50%;background:#fff;cursor:nwse-resize}
.bx-row{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.bx-row canvas{border-radius:8px;background:#17110E;width:135px;height:240px}
```

- [ ] **Step 2: The box editor** — add after the Live Transcriber section:

```js
/* ---- the facecam box editor: drag on a frame to draw the facecam and the gameplay boxes */
function openBoxes(src, cid, cur, done) {
  const B = {game: [...(cur?.game || [0, 0, 1, 1])], cam: cur?.cam ? [...cur.cam] : null, cam_pct: cur?.cam_pct || 40, mode: "cam"};
  const d = $("#boxdlg");
  d.innerHTML = `<div class="dlg boxes"><h2>Show where the facecam is</h2>
    <p class="help">Drag on the picture to draw the <b>facecam</b> box. Then choose Gameplay and draw what goes on top (the whole picture if you skip it). Drag a box to move it, or its corner dot to resize it.</p>
    <div class="seg" role="group" aria-label="Which box"><button type="button" data-bx="cam" aria-pressed="true">Facecam box</button><button type="button" data-bx="game" aria-pressed="false">Gameplay box</button></div>
    <div class="bx-row"><div class="bx-stage" id="bxStage" style="flex:1;min-width:260px"><img id="bxImg" src="${esc(src)}" alt="A picture from the clip" draggable="false">
        <div class="bx game" id="bx_game"><span>Gameplay</span><i></i></div><div class="bx cam" id="bx_cam"><span>Facecam</span><i></i></div></div>
      <div><canvas id="bxCv" width="270" height="480" aria-label="How the clip will look"></canvas>
        <label class="f">Facecam height <b id="bxPctV">${B.cam_pct}%</b><input type="range" id="bxPct" min="25" max="50" value="${B.cam_pct}"></label></div></div>
    <div class="dlg-foot"><button type="button" class="btn" id="bxNo">Cancel</button><button type="button" class="btn primary" id="bxOk">Save boxes</button></div></div>`;
  const img = $("#bxImg"), stage = $("#bxStage");
  const clamp = b => { b[2] = Math.min(Math.max(b[2], 0.05), 1); b[3] = Math.min(Math.max(b[3], 0.05), 1);
    b[0] = Math.min(Math.max(b[0], 0), 1 - b[2]); b[1] = Math.min(Math.max(b[1], 0), 1 - b[3]); return b; };
  function show() {
    for (const k of ["game", "cam"]) { const el = $("#bx_" + k), b = B[k];
      el.hidden = !b; if (b) Object.assign(el.style, {left: b[0] * 100 + "%", top: b[1] * 100 + "%", width: b[2] * 100 + "%", height: b[3] * 100 + "%"}); }
    const cv = $("#bxCv"), g = cv.getContext("2d"), W = cv.width, Hh = cv.height, camH = Math.round(Hh * B.cam_pct / 100);
    g.fillStyle = "#17110E"; g.fillRect(0, 0, W, Hh);
    if (!img.naturalWidth) return;
    const draw = (b, dy, dh) => { const sw = b[2] * img.naturalWidth, sh = b[3] * img.naturalHeight, s = Math.max(W / sw, dh / sh);
      const cw = W / s, ch = dh / s; g.drawImage(img, b[0] * img.naturalWidth + (sw - cw) / 2, b[1] * img.naturalHeight + (sh - ch) / 2, cw, ch, 0, dy, W, dh); };
    draw(B.game, 0, Hh - camH);
    if (B.cam) draw(B.cam, Hh - camH, camH);
  }
  img.onload = show; img.onerror = () => toast("Couldn't load a picture from this clip.", "bad");
  let drag = null;
  const at = e => { const r = img.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]; };
  stage.onpointerdown = e => {
    const [x, y] = at(e), b = B[B.mode];
    if (e.target.tagName === "I" && e.target.parentElement.id === "bx_" + B.mode) drag = {kind: "size"};
    else if (b && x >= b[0] && x <= b[0] + b[2] && y >= b[1] && y <= b[1] + b[3]) drag = {kind: "move", dx: x - b[0], dy: y - b[1]};
    else { B[B.mode] = [x, y, 0.05, 0.05]; drag = {kind: "draw", x, y}; }
    stage.setPointerCapture(e.pointerId); show();
  };
  stage.onpointermove = e => {
    if (!drag) return; const [x, y] = at(e), b = B[B.mode];
    if (drag.kind === "move") { b[0] = x - drag.dx; b[1] = y - drag.dy; }
    else if (drag.kind === "size") { b[2] = x - b[0]; b[3] = y - b[1]; }
    else { b[0] = Math.min(x, drag.x); b[1] = Math.min(y, drag.y); b[2] = Math.abs(x - drag.x); b[3] = Math.abs(y - drag.y); }
    clamp(b); show();
  };
  stage.onpointerup = () => { drag = null; };
  d.querySelectorAll("[data-bx]").forEach(btn => btn.onclick = () => { B.mode = btn.dataset.bx;
    d.querySelectorAll("[data-bx]").forEach(o => o.setAttribute("aria-pressed", String(o === btn))); });
  $("#bxPct").oninput = e => { B.cam_pct = Number(e.target.value); $("#bxPctV").textContent = B.cam_pct + "%"; show(); };
  $("#bxNo").onclick = () => d.close();
  $("#bxOk").onclick = async () => {
    if (!B.cam) return toast("Draw the facecam box first.", "bad");
    const split = {game: B.game.map(n => +n.toFixed(4)), cam: B.cam.map(n => +n.toFixed(4)), cam_pct: B.cam_pct};
    try {
      if (cid) { const c = await send(`/api/creators/${encodeURIComponent(cid)}`, {split}, "PUT"); const o = S.creators.find(x => x.id === cid); if (o) o.split = c.split; }
      d.close(); done(split);
    } catch (x) { toast(x.message, "bad"); }
  };
  d.showModal();
}
```

- [ ] **Step 3: The cut Edit panel** — replace the `openCutEdit` stub from Task 9 with:

```js
function openCutEdit(jid, cutId) {
  const j = jobOf(jid), cut = j?.cuts?.find(c => c.id === cutId); if (!cut) return;
  const cr = S.creators.find(c => c.id === j.creator) || {};
  const frame = `/api/live/${jid}/cuts/${cutId}/frame`;
  let split = cr.split || null;
  const d = $("#formdlg");
  d.innerHTML = `<form method="dialog" class="dlg" style="width:min(640px,calc(100vw - 32px))"><h2>Make a vertical clip</h2>
    <p class="help">PeakCut adds captions in the style you choose and puts the clip in ${esc(cr.name || "this creator")}'s clips, where you can change it again and post it.</p>
    <label class="f">Big text<input id="ce_hook" maxlength="60" placeholder="Leave empty and the AI writes one"></label>
    <div class="ob"><div class="ot">Caption style</div>${choice("ce_style", Object.entries(S.status.styles).map(([k, v]) => ({v:k, label:v, prev:capPrev(k)})), cr.style || "bounce")}</div>
    <div class="ob"><div class="ot">Layout</div>${choice("ce_layout", Object.entries(S.status.layouts).map(([k, v]) => ({v:k, label:LAYOUT_SHORT[k] || v, title:v, prev:`<span class="lay-prev ${esc(k)}"><span class="ph"><i></i></span></span>`})), cr.layout || "blur")}</div>
    <p class="sub" id="ce_box">${split ? "Facecam box saved for this creator." : "For Game + cam, draw the facecam box once."} <button type="button" class="btn sm" id="ce_draw">${split ? "Change the boxes" : "Draw the facecam box"}</button></p>
    <div class="dlg-foot"><button class="btn" value="no">Cancel</button><button type="button" class="btn primary" id="ce_go">${ic("scissors")}Make vertical clip</button></div></form>`;
  const go_ = async () => {
    const body = {hook: $("#ce_hook").value.trim(), style: $("#ce_style").value, layout: $("#ce_layout").value};
    if (body.layout === "split") { if (!split) return openBoxes(frame, j.creator, null, s => { split = s; go_(); }); body.split = split; }
    try {
      const nj = await send(`/api/live/${jid}/cuts/${cutId}/edit`, body);
      d.close(); if (!jobOf(nj.id)) S.jobs.unshift(nj);
      toast("Making your vertical clip…", "ok");
      if (j.creator) go("c:" + j.creator); else go("library");
      tick();
    } catch (x) { toast(x.message, "bad"); }
  };
  $("#ce_draw").onclick = () => openBoxes(frame, j.creator, split, s => { split = s; $("#ce_box").firstChild.textContent = "Facecam box saved for this creator. "; });
  $("#ce_go").onclick = go_;
  d.showModal();
}
```

- [ ] **Step 4: The per-clip editor** — in `drawClipDlg`, after the `<div class="two">` holding Caption style and
  Layout, add:

```js
        <p class="sub">For Game + cam: <button type="button" class="btn sm" data-act="clipboxes">${c.split || S.creators.find(x => x.id === j.creator)?.split ? "Change the facecam box" : "Draw the facecam box"}</button></p>
```

In the click handler, before `if (a === "remake")`, add:

```js
    if (a === "clipboxes") {
      const j = jobOf(D.jid), c = j?.clips.find(x => x.id === D.cid); if (!c) return;
      const cur = c.split || S.creators.find(x => x.id === j.creator)?.split || null;
      return openBoxes(`/api/jobs/${D.jid}/clips/${D.cid}/frame`, j.creator, cur, s => { D.split = s; toast("Boxes saved. Choose Game + cam and press Save and re-make clip.", "ok"); });
    }
```

Change the `remake` action body to send the boxes when the layout is split:

```js
      const body = {}; $$("#clipdlg [data-f]").forEach(i => body[i.dataset.f] = i.type === "number" ? Number(i.value) : i.value);
      if (body.layout === "split") {
        const j = jobOf(D.jid), c = j?.clips.find(x => x.id === D.cid);
        const boxes = D.split || c?.split || S.creators.find(x => x.id === j?.creator)?.split;
        if (!boxes) return toast("Draw the facecam box first: press Draw the facecam box.", "bad");
        body.split = boxes;
      }
      await send(`/api/jobs/${D.jid}/clips/${D.cid}/render`, body); toast("Re-making the clip…", "ok"); return tick();
```

and in `openClip` reset it: `D.split = null;` next to `D.sig = "";`.

- [ ] **Step 5: Check it by hand** (throwaway copy as in Task 9): from a cut, Edit → choose Game + cam → the box
  editor opens on a frame of the cut → draw the facecam box → the preview shows gameplay over facecam → Save →
  Make vertical clip → the creator's Clips list shows the new clip being made, then done, 1080x1920, with the
  facecam at the bottom. Open that clip: re-make with another facecam height works. Change a creator's Setup
  layout to Game + cam: the select shows it. Check at 375 px width that the box editor fits.

- [ ] **Step 6: Run** `python tests/run.py` — Expected: `All tests passed.`

- [ ] **Step 7: Commit** (only if allowed)

```bash
git add web/index.html
git commit -m "Edit panel for downloaded clips, and a box editor for the Game + cam layout"
```

---

### Task 11: Docs and the full check

**Files:**
- Modify: `README.md` ("What it does" list), `AGENTS.md` (file table)

- [ ] **Step 1: README** — in "What it does", after the **Live clipping** item, add:

```markdown
- **Live Transcriber.** Watch a stream's transcript as it is spoken, see the moments that could go viral with the
  reason, and download any of them: choose how many seconds before the moment the clip starts and how long it is.
  A minute either side of every moment is kept, so you can still cut it after the stream. Send any clip to the
  editor to make it vertical, including a new layout with the gameplay on top and the streamer's facecam below.
```

- [ ] **Step 2: AGENTS.md** — add a row to the file table:

```markdown
| `studio/picks.py` | Live Transcriber: moments (points), the stream kept around them (holds), clips cut from them, Edit |
```

- [ ] **Step 3: Full test run** — `python tests/run.py` → `All tests passed.`

- [ ] **Step 4: End-to-end by hand** — on a real English live stream: Start in Live Transcriber → transcript
  follows the stream → at least one AI moment and one marked moment → after a minute each has a player →
  Download clip with Seconds before 20 and Length 30 gives a 30-second MP4 starting 20 seconds before the moment →
  Edit with Game + cam → the vertical clip appears in the creator's Clips and can be posted or downloaded → Stop →
  the recording is under Finished recordings and its clips can still be cut. Restart PeakCut mid-recording once
  and check moments that were waiting are saved after the restart.

- [ ] **Step 5: Commit** (only if allowed)

```bash
git add README.md AGENTS.md
git commit -m "Docs: the Live Transcriber"
```
