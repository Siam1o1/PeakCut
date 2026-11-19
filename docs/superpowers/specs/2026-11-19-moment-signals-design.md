# Better moment finding, viral rating, creator memory, delete creator, export resolution: design

Date: 2026-11-19. Status: approved in chat.

## Goal

Find the moments of a live stream that are worth clipping more reliably, rate how viral each one could be, learn
each creator's habits over time, and add two small features: deleting a creator, and choosing 720p or 1080p.

Keep everything that works now. Each step is a separate change with its own tests, and `python tests/run.py` must
pass after every step. Every new signal can be switched off in the creator's Setup.

## Order and delivery

1. Step 8 (delete creator) and step 9 (export resolution) first, merged into `main` as soon as they pass.
2. Then steps 1 to 7 in order, on the branch `improve-signals`. Each builds on the one before. They are merged into
   `main` at the end, with the owner's go-ahead.

## Where things live

| Part | File |
|---|---|
| Signal maths: baseline, z-scores, chat windows, fusion, timing | `studio/signals.py` (new). Pure functions, no I/O, so they are easy to test |
| Twitch chat reading (tags, notices) and the session's use of the signals | `studio/live.py` |
| Twitch Helix calls (token, user id, stream start, clips) | `studio/twitch.py` (new) |
| Candidate log | `studio/signals.py` writes `creators/<id>/signals.jsonl` through a small helper |
| Viral rating sub-scores and the formula | `studio/brain.py` (prompt + parsing), weights in `studio/signals.py` |
| Creator memory | `studio/memory.py` (new): read, merge with the AI, save, reset |
| Settings | `studio/creators.py` `DEFAULTS` and `_clean` |
| Pages | `web/index.html`: Setup "Moment finding" card, Home creators table, Edit resolution, rating display |

`data/` in the step texts means PeakCut's data folder (`config.DATA_DIR`, usually `PeakCut/data`); a creator's
folder is `creators.root(cid)`.

## Step 8: delete a creator

- The Home page's **Creators** table gets a Delete button on every row. The existing button at the bottom of a
  creator's Setup stays.
- The confirm dialog names the creator and asks for the name to be typed (the existing `askTyped`). It has a
  checkbox **Also delete this creator's clips and recordings**, off by default.
- Deleting:
  1. stops a live recording of that creator (as Stop does) and turns their auto-watch off;
  2. waits for nothing else: if a video of theirs is still being made, it refuses in plain words (as today);
  3. removes the creator's settings, posting rules, activity log and behavior profile (the creator folder);
  4. with the checkbox off, moves each of their videos and recordings out of the creator folder into the owner's own
     jobs folder (`config.user_dir(uid)/jobs/<jid>`), sets `creator` to `None` and saves, so they show under All
     videos with no creator. Paths stored in the job (`dir`, `source_path`, clip `src`) are rewritten to the new
     folder. With the checkbox on, they are deleted as today.
- Who may delete: the route finds the creator through `creators.get(cid)`, which only returns the asker's own
  creators, so with accounts on only the owner can delete. A test proves another user gets 404.
- Route: `DELETE /api/creators/{cid}?with_clips=1` (the flag is optional; default keeps the clips).

## Step 9: export resolution

- A choice of **720p (720x1280)** or **1080p (1080x1920)**, default 1080p, on the layout part of:
  the Live Transcriber's Edit panel, the per-clip editor, and the Options of a new video.
- Stored on each clip as `res` (720 or 1080). Rendering always builds the 1080x1920 picture (so every layout,
  caption and heading position stays as it is) and, for 720, scales it to 720x1280 as the last step of the same
  ffmpeg filter (`scale=720:1280:flags=lanczos`). The cover picture follows the video.
- The last choice is remembered per creator (creator setting `res`, saved whenever a clip of theirs is rendered
  with a choice) and is used for clips made automatically from live streams and for new videos of that creator.
- Live streams are recorded at up to 720p today, so 1080p from a live stream is upscaled. This is said in a small
  note next to the choice in the Edit panel.

## Step 1: one baseline

- `signals.robust_z(values, now_value) = (now - median) / (1.4826 * MAD)`, with a floor on the MAD so a silent or
  empty stretch does not divide by zero. The history is the last 15 minutes (900 one-second samples), leaving out
  the last 10 seconds.
- Two series per session: loudness (per second, linear amplitude from the existing dB values) and unique chatters
  per second. When the viewer count is known (from `profiles.py`, refreshed about once a minute), chatters are
  divided by viewers before the z-score.
- A jump is when either z passes a threshold (setting `jump_z`, default 3.5). It replaces `_watch_spikes`'s +60%
  rule and `_chat_spike`'s 3x rule (one shared function for both).
- Top-N per hour: candidates are ranked by their fused score (step 5; until step 5, by the larger z). At most
  `live_per_hour` candidates (default 4) reach the AI judges in any rolling hour; a stronger candidate can still go
  when the hour is full if it beats the weakest one already sent, which is then marked as passed over. No fixed
  absolute cutoff.
- Setting: **Use the new baseline** (`sig_baseline`, on). Off gives today's rules back.
- Before 60 seconds of history the session says "learning the stream" as today. With a saved behavior profile
  (step 7) the saved typical values start the baseline instead.

## Step 2: what chat says

- The chat reader asks Twitch for the `twitch.tv/tags` and `twitch.tv/commands` capabilities (checked against
  Twitch's IRC documentation when built) so each message carries the sender's id and badges, and notices arrive
  as `USERNOTICE`.
- Ignored: messages from known bots (a list in `signals.py`, e.g. Nightbot, StreamElements, Streamlabs, Moobot,
  Fossabot, plus any name ending in "bot"), `USERNOTICE` events (raids, subs, gift subs) and the few seconds of
  chat right after a raid, and copypasta: the same long message (more than 6 words) sent by fewer than 3 people.
- Per window of 8 seconds (setting range 5 to 10):
  - **convergence**: the share of unique chatters whose message contains the window's most common word or emote;
  - **reaction type** counts per type: laugh, shock, hype, clip request, negative. Each type has an editable word
    list per creator (defaults in `signals.py`, e.g. laugh: LUL, KEKW, lol, lmao, haha, OMEGALUL; shock: WTF, omg,
    no way, what; hype: Pog, PogChamp, LETS GO, W; clip: clip, clip it, clip that; negative: L, cringe, boring);
  - **clip requests from distinct users**.
- Only counts and words are kept; message text is not stored beyond the window.
- Settings: **Read what chat says** (`sig_chat_words`, on), the window length, and the five word lists.

## Step 3: viewer clips

- When `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` are in `.env` (or `*_FILE`), `studio/twitch.py` gets an app
  token (client credentials), the broadcaster's user id, and the stream's start time, then every 90 seconds calls
  Get Clips for the broadcaster since the recording started.
- Each new clip is mapped to stream time: the clip's end is its `created_at` minus the stream start, mapped onto
  the session clock using the time the recording started; its start is end minus `duration`. Clips already seen
  are skipped by id.
- A burst (2 or more clips from different creators within 60 seconds) is a strong signal and becomes a candidate.
- Without credentials, or on any API error, it skips quietly (one line in the activity log the first time).
- Setting: **Use viewers' Twitch clips** (`sig_viewer_clips`, on; only does anything with credentials).

## Step 4: timing

- Lag: per stream, the delay between loudness/voice peaks and the chat peaks that follow them is measured by
  cross-correlation over the last 15 minutes (0 to 20 seconds, default 5 until measured). Chat signals are shifted
  back by it before they are fused.
- Clip edges, replacing `AUTO_BEFORE`/`AUTO_AFTER`:
  - start at the beginning of the sentence that sets the moment up: the first sentence start (word timestamps,
    `brain.make_lines`) at least 3 seconds before the peak and not more than `max_len` before the end;
  - end when chat convergence is back to normal (z under 1) or at the next sentence end after that, whichever is
    later, within `min_len` and `max_len`.
- The windows sent to the AI judges use these edges plus a little context.
- Setting: **Smart clip edges** (`sig_timing`, on).

## Step 5: one score, and a log

- `signals.fuse(values, weights)` combines the signals into one score from 0 to 100: loudness z, chat z,
  convergence, reaction counts, clip requests, viewer-clip burst. Weights per creator (setting `sig_weights`, a
  small dict with defaults), editable in Setup as numbers.
- Only the top candidates (step 1's top-N) go to the AI judges.
- Every candidate is logged as one JSON line in `creators/<id>/signals.jsonl`: time, recording id, its signal
  values, fused score, whether it was sent to the AI, the AI's verdict and rating (step 6), and later whether the
  clipper downloaded it (a cut was made), sent it to Edit, or posted it, and the post's views when known. Updates
  are appended as small lines keyed by the candidate id, so the file is never rewritten. Nothing from chat but
  numbers is written.
- Setting: **Keep a log for learning** (`sig_log`, on).

## Step 6: viral rating

- The words judge (`brain.judge_transcript`) also returns, per moment, sub-scores 0 to 10: `hook` (the first 3
  seconds), `alone` (makes sense without context), `emotion` (funny, shocking, hype), `payoff`, `share`, and a
  one-line `why`.
- The rating is computed in code: `rating = round(10 * sum(w_i * s_i) / sum(w_i))`, weights in `signals.py`
  (default hook 3, alone 2, emotion 2, payoff 2, share 1). It replaces the AI's own `score`, so `min_score` keeps
  working. If the sub-scores are missing, the AI's `score` is used as before.
- Shown on every moment in the Live Transcriber and on clips in the clip list and clip editor: the rating, the five
  sub-scores and the reason. Moments are sorted by rating, highest first (a toggle keeps "newest first").
- The rating is written to the step 5 log; the clip's real views are added there later when PeakCut posted it.

## Step 7: what PeakCut learns about each creator

- Files: `creators/<id>/behavior.md` (plain words, under about 400 words, each change dated) and
  `creators/<id>/behavior.json` (numbers: typical loudness, chat rate, convergence, lag, and counts).
- After each stream that had at least one confirmed moment, one AI call compares the transcript, loudness and
  chat numbers around confirmed moments (downloaded, sent to Edit, posted, viewer-clip bursts, high-view clips from
  the log) with ordinary stretches, and returns an updated profile: how the creator reacts to unusual moments
  (screams, goes silent, laughs, repeated phrases), catchphrases and reaction words, normal energy, moment types
  that got views, and common false alarms. The AI is given the current profile and told to merge, not append; the
  code trims it to about 400 words. Raw chat is never sent or written: only counts and the creator's own words.
- At the next stream the profile goes into both AI judges' prompts and the viral rating's prompt (through
  `brain._safe`, as untrusted text), and the saved numbers start the step 1 baseline.
- Setup shows the profile with Edit, Save and Reset.
- Setting: **Learn this creator's habits** (`sig_memory`, on).

## Setup: "Moment finding"

A new card in the creator's Setup with the switches above, the chat window, the five word lists, the fusion
weights, and the behavior profile. Off switches give back today's behaviour for that part.

## Errors, in plain words

- Twitch API not set up or failing: skipped, one activity-log line.
- The AI does not answer at the end of a stream: the profile is left as it was.
- Deleting a creator while a video is being made: refused, as today.

## Testing

- `tests/test_signals.py` (new): robust z, MAD floor, chatters per viewer, top-N per hour, chat windows
  (convergence, reaction types, distinct clip requests, bots, copypasta, raid/sub notices), lag measurement, clip
  edges, fusion, rating formula, log lines.
- `tests/test_twitch.py` (new): clip-to-stream-time mapping and burst detection with Helix answers replaced.
- `tests/test_memory.py` (new): merge, size limit, dated changes, no raw chat, reset, profile in prompts.
- Delete creator and resolution: added to `tests/test_transcriber_routes.py`-style in-process checks (owner only,
  keep vs delete clips, paths rewritten; 720 output really is 720x1280).
- The whole suite after every step.
