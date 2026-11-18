# Live Transcriber: design

Date: 2026-11-19. Status: approved in chat, waiting for review of this file.

## What it is for

A clipper watches a streamer live. PeakCut records the stream, shows the full transcript as it is spoken, and lists
the moments that could go viral, each with the reason. The clipper chooses a moment, sets how many seconds before it
the clip starts (20 by default) and how long the clip is, and downloads it. Any downloaded clip can then be sent to
PeakCut's editor (caption style, layout, quality) to make a vertical clip and post it. The editor gains a new layout:
gameplay on top, the streamer's facecam below.

Decisions made with the user:

- Built inside PeakCut, as a new **Live Transcriber** view. Not a separate app.
- Streams are in English, so the existing Parakeet speech model is kept.
- The AI does not cut clips by itself here. It lists moments; the clipper picks.
- The whole stream is **not** kept. Only a short piece around each moment is kept (see "Holds").
- Downloads are the original 16:9 video, cut only. Vertical clips with captions come from the Edit button.
- The facecam is marked by hand: the clipper draws boxes on a frame, once per creator.

Out of scope: keeping the full stream, other languages, finding the face automatically, changes to posting.

## How it fits into what exists

`studio/live.py` already records a stream into a rolling buffer of 6-second pieces, transcribes it, and asks the AI
(`brain.judge_transcript` and `brain.judge_signals`) about the words, loudness and Twitch chat. Today each moment the
judges accept goes to `Session._add_clip`, which renders a finished vertical clip straight away.

The Live Transcriber reuses all of that. A live job started from the new view has `job["mode"] = "pick"`. In that mode
accepted moments become **points** instead of clips. Everything else (recording, transcription, the two judges, the
loudness and chat jumps, someone saying "clip it", the creator's trigger words) works as it does now. A live job
started anywhere else keeps today's behaviour, unchanged.

## Parts

### 1. Points (`studio/live.py`)

A point is a moment worth clipping:

```
{"id": "pt003", "t": 1834.2, "start": 1814.2, "end": 1859.0, "score": 86,
 "reason": "...", "text": "the words around t", "from": "ai" | "jump" | "said" | "trigger" | "you",
 "hold": {"status": "waiting" | "saving" | "ready" | "error", "file": "points/pt003.mp4", "from": 1774.2, "to": 1894.2,
          "error": null},
 "made_at": 1763560000.0}
```

- `t` is the viral moment itself: the punchline, the reaction, the thing people would share. The words judge is asked
  for it as a new `peak` field (seconds), next to its `start` and `end`. It is kept only if it lies inside
  `start..end`. Without it, `t = min(start + 20, end)`, so a 20-second lead-in gives back the AI's own start.
- In pick mode, `_add_clip` is replaced by `_add_point`. The hourly cap (`live_per_hour`) does not apply, because a
  point costs nothing to render. The minimum gap between moments (`min_gap_min`) still applies.
- Trigger words and someone saying "clip" become points too, with `from` saying which.
- The clipper can mark any transcript line as a point (`from: "you"`, `t` = the line's start, no AI call). Its reason
  is "Marked by you".
- With no AI key, loudness and chat jumps still make points (reason: what jumped), as PeakCut does now.

### 2. Holds: keeping the moment after the buffer moves on

The rolling buffer keeps `buffer_min` minutes (20 by default) and deletes older pieces. A clipper may choose a moment
much later, or after the stream ends. So when a point is made, PeakCut keeps a **hold**: the stream from 60 seconds
before `t` to 60 seconds after it, copied out of the buffer without re-encoding into `points/<id>.mp4` in the job
folder (MP4, so the page can play it). At 720p that is about 30 MB per point.

- A new point's hold is `waiting`. Each pass of the session loop checks the waiting holds; once the buffer reaches
  `t + 60` (or the stream has ended) the hold is handed to the live render worker (`jobs.submit_live`), so the worker
  never sits idle waiting for the stream. Saving joins the pieces like `Session._render` does today, lines the hold
  up with the transcript clock using the existing `Session._align`, and copies the result into MP4.
- The session does not finish while any hold is still waiting, so pressing Stop never loses a moment.
- `hold.from` and `hold.to` are on the transcript clock, so the page can show words against the video.
- If the stream ends before `t + 60`, the hold ends where the stream did. If the pieces are already gone, the hold is
  marked `error` with "That part of the stream is no longer in the buffer."
- Holds are kept until the clipper deletes the recording. The buffer cleanup at the end of a session does not touch
  the `points/` folder.

### 3. Cuts: the downloaded clips

```
{"id": "cut002", "point": "pt003", "lead": 20, "length": 45, "start": 1814.2, "end": 1859.2,
 "status": "making" | "ready" | "error", "file": "cuts/cut002.mp4", "error": null}
```

- On each point the clipper sets **Seconds before** (`lead`, 0 to 60, default 20) and **Length** (5 to 120 seconds,
  default the creator's `max_len`). `start = t - lead`, `end = start + length`.
- The cut must fit inside the hold. If it does not, it is moved or shortened to fit, and the page says how (for
  example "Only 38 seconds were kept after this moment, so the clip is 58 seconds long").
- The cut is re-encoded (H.264 and AAC, `veryfast`) so it starts exactly where asked and plays everywhere. A 45-second
  720p cut takes a few seconds.
- A point can have several cuts. Each one is listed with Download, Edit and Delete.

### 4. Edit: from a cut to a vertical clip

Edit opens a panel with PeakCut's existing editing choices: caption style, layout (including the new split layout),
and the hook text. (Video quality is not offered: it only decides what is downloaded, and the cut already exists.) **Make vertical clip** then:

1. creates a normal PeakCut video job (`kind: "video"`, source `{"type": "cut", "path": <the cut>}`) under the same
   creator, with one clip covering the whole cut;
2. fills the clip's hook, title and description with `brain.describe` on the cut's words (one AI call), as manual live
   clips do today;
3. gives it the live transcript's words for that stretch, shifted to start at zero, so no new transcription is needed;
4. renders it with `jobs.render_one`.

The page then opens that creator's Clips list, where the clip can be re-edited and posted exactly like any other
clip. The cut itself stays in the Live Transcriber.

### 5. The split layout: gameplay on top, facecam below

A new entry in `render.LAYOUTS`: `"split": "Gameplay on top, facecam below"` (short name "Game + cam").

- Two boxes on the source frame, as fractions of its width and height: `game` (default: the whole frame) and `cam`
  (no default; it must be drawn). Plus `cam_pct`, how much of the height the facecam takes: 40 by default, 25 to 50.
- The boxes are saved on the creator (new creator setting `split`) and copied onto each clip when it is rendered, so
  changing them later does not change clips already made. A clip without a creator keeps its own copy only.
- Rendering: the source is cropped twice. The game box is scaled to fill `1080 x (1920 - cam height)` and the cam box
  to fill `1080 x cam height` (each scaled up and centre-cropped to keep its shape), and they are stacked. Captions
  sit across the line between the two; the hook stays at the top.
- **The box editor:** when Game + cam is chosen and no cam box is set, the editor shows a frame from the clip
  (new route that returns one JPEG frame). The clipper drags to draw the cam box, then the game box; boxes can be
  moved and resized. A small preview shows the 9:16 result. Save stores the boxes on the creator.
- The split layout appears everywhere layouts are offered: the Options of a new video, a creator's Setup, the
  per-clip edit form, and the Edit panel of a cut. The box editor needs a picture, so it opens from the per-clip
  edit form and the Edit panel, where there is a clip to take a frame from. Choosing split there with no cam box
  opens the box editor first. Elsewhere, a creator with no cam box gets "Blurred fill" until boxes are drawn.

### 6. The Live Transcriber view (`web/index.html`)

A new item in the left navigation, **Live Transcriber**.

- **Start:** choose a creator (or make one from the pasted link) and press Start. This calls a new route that starts a
  live job in pick mode.
- **While recording**, two columns:
  - the transcript, every line with its time, newest at the bottom, following the stream unless the clipper scrolls
    up; each line has a "Mark" button;
  - the points, newest first: time, score, reason, the words around `t`, the hold's state, the Seconds before and
    Length controls, a player of the kept stretch that starts where the clip would start, and **Download clip**.
    The file downloads by itself as soon as the cut is ready.
- A Stop button, the recording time, and the buffer length, as on today's live card.
- Pick-mode recordings are shown only in this view, not as a live card in the creator's Clips list, so the two
  kinds of recording are never confused. Starting one counts against the same recording limits as today.
- **Finished recordings** stay listed with their points and cuts until deleted. Their transcript stays in
  `words.json` in the job folder (the recording already writes it as it goes).
- The transcript is fetched in pages by a new route (`after` = last line time) instead of riding on `/api/sync`, so
  the page's regular check stays small.

## New and changed routes

| Route | What it does |
|---|---|
| `POST /api/transcriber/start` | Start a pick-mode recording for a creator (and link) |
| `GET /api/live/{jid}/transcript?after=` | Transcript lines after a time |
| `POST /api/live/{jid}/points` | Mark a moment by hand (`t`) |
| `POST /api/live/{jid}/points/{pid}/cuts` | Make a cut (`lead`, `length`) |
| `DELETE /api/live/{jid}/points/{pid}` and `.../cuts/{cut}` | Delete a point (and its hold and cuts) or one cut |
| `POST /api/live/{jid}/cuts/{cut}/edit` | Send a cut to the editor (style, layout, quality, hook) |
| `GET /api/live/{jid}/cuts/{cut}/frame` and `GET /api/jobs/{jid}/clips/{cid}/frame` | One JPEG frame, for the box editor |
| `GET /media/{jid}/{points or cuts}/{name}.mp4` | Holds and cuts (the existing `/media/{jid}/{name}` serves clips) |

All of them work out who is asking from the session (`ctx.uid()`), only reach that user's own jobs, and call
`ctx.bump()` when something the page shows has changed. Points, holds and cuts are listed in `jobs.public(job)`
without server paths.

## Errors, in plain words

- Stream offline when starting: "This channel isn't live right now."
- Hold could not be kept: shown on the point, with the reason. The point stays so its words can still be read.
- A cut that had to be moved or shortened: said on the cut.
- AI not answering: points still come from loudness, chat and trigger words; the existing AI warning bar shows.
- Disk nearly full: the existing storage guard stops new recordings; holds and cuts already made are kept.

## Testing

- New tests next to the existing ones in `tests/`, run by `python tests/run.py`:
  - cut maths: lead and length clamped to the hold, with the right message;
  - `peak` handling: kept inside start..end, the fallback otherwise;
  - the split layout's ffmpeg filter for a few box shapes, and a real render of a short ffmpeg test video;
  - the new routes refuse other users' jobs and bad numbers;
  - a pick-mode session fed a local test video in place of a stream: points appear, holds are saved, a cut downloads
    and its length is right.
- The whole existing test run before and after.
- By hand at the end: a real English Twitch or YouTube stream, from Start to a posted-ready vertical clip in the split
  layout.
