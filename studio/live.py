"""Live sessions: record a stream into a rolling buffer, transcribe as it goes, let the AI pick clips."""
import collections
import json
import re
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import alerts, auth, brain, config, creators, ctx, facecam, games, jobs, load, media, picks, profiles, signals, transcribe, twitch, usage

# ":username!user@host PRIVMSG #channel :the message"

SESSIONS: dict = {}
SEG_SEC = 6           # length of each buffer piece
PAD = 8               # seconds kept either side of a clip so it can be re-trimmed later
CHECK_EVERY = 180     # look at the stream every 3 minutes...
QUIET_MAX = 600       # ...but only ask the AI when something happened, or at most every 10 minutes when calm
WINDOW = 240          # seconds of transcript shown to the AI per check
CHAT_WORDS_KEEP = 120  # seconds of what chat said (the words themselves) kept in memory
CHAT_KEEP = 960      # seconds of chat names kept: the baseline reads the last 15 minutes, plus the few seconds being judged
SR = 16000


_NO_TWITCH_TOLD = set()     # creators already told that Twitch credentials are missing


class NotLive(Exception):
    pass


def _trim_names(users: dict, second: int, kept: int) -> int:
    """Drop the chat names older than CHAT_KEEP, and say when this was last done.

    Only the last few minutes are ever read, and a stream that runs for eight hours would otherwise hold
    a name for every second of it. Called under the session's lock, from the chat reader thread."""
    if second - kept < 60:
        return kept              # walking the whole dict on every message would cost more than it saves
    for t in [t for t in users if t < second - CHAT_KEEP]:
        del users[t]
    return second


def media_safe(e) -> str:
    from . import safe
    return safe.friendly(e, 300)


def normalize(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    m = re.match(r"^(https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+))/?(?:[?#].*)?$", url)
    return m.group(1) + "/live" if m else url


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def resolve(url: str) -> dict:
    """Ask yt-dlp for the stream's playable address. Raises NotLive if the channel is offline."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _Quiet(),
            "format": "b[height<=720]/bv*[height<=720]+ba/b"}
    if media.is_youtube(url) and media.youtube_paused():
        raise NotLive(media.YT_BLOCKED)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        if media.note_refusal(url, e):
            raise NotLive(media.YT_BLOCKED) from e
        raise NotLive(str(e)[-200:]) from e
    if not info or not info.get("is_live"):
        raise NotLive("This channel is not live right now.")
    fmts = info.get("requested_formats") or [info]
    title = info.get("title") or ""
    if "(live)" in title and info.get("description"):  # Twitch puts the real stream title here
        title = str(info["description"]).splitlines()[0][:120]
    return {"urls": [f["url"] for f in fmts], "title": title,
            "uploader": info.get("uploader") or info.get("channel") or ""}


def active():
    return [s for s in SESSIONS.values() if not s.finished and not s.user_stopped]


def active_mine():
    u = ctx.uid()
    return [s for s in active() if s.job["user"] == u]


def active_for(cid):
    return next((s for s in active_mine() if s.job["creator"] == cid), None)


def mine(jid):
    """The signed-in user's recording with this id, or None."""
    s = SESSIONS.get(jid)
    return s if s and s.job["user"] == ctx.uid() else None


class Session:
    def __init__(self, job, settings):
        self.job, self.settings = job, settings
        self.analysis_done = False    # the analysis loop has finished: nothing more can be listed
        self.pick = job.get("mode") == "pick"   # Live Transcriber: moments are listed for the clipper, not clipped
        self.dir = Path(job["dir"])
        self.buf = self.dir / "buffer"
        self.buf.mkdir(exist_ok=True)
        (self.dir / "clips").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stop_flag = threading.Event()
        self.ended = False            # recording has stopped (stream over, stopped, or failed)
        self.finished = False         # everything is wrapped up
        self.end_note = "Stream ended"
        self.user_stopped = False     # Stop pressed: wrap up at once, no last AI check
        self.proc = None
        self.pending = bytearray()    # audio received but not yet transcribed
        self.clock_fix = 0.0          # how far the video-piece clock is behind the audio clock, last measured
        self.pending_base = 0         # sample index where `pending` starts
        self.samples = 0              # total audio samples received
        self.db = []                  # loudness per second
        self.words = []
        self.t_done = 0.0             # transcript is complete up to this second
        self.segments = []            # (path, start, end) on the session clock
        self.seg_clock = 0.0
        self.csv_seen = {}
        self.part = 0
        self.chat = collections.Counter()   # second -> messages
        self.chat_users = {}                 # second -> the people who talked, when it can be told
        self.chat_kept = 0                   # the second this was last pruned at
        self.chat_msgs = {}                  # second -> (person, text) for the last two minutes; memory only, never saved
        self.raid_until = 0                  # stream second until which chat words are not kept (a raid just arrived)
        self.words_read = 0                  # the second chat was last read for what it says
        self.conv_hist = []                  # (second, convergence) of each chat window read, last 15 minutes
        self.lag = 5                         # seconds chat takes to react after the streamer, measured every 5 minutes
        self.lag_at = 0                      # the second the lag was last measured
        self.chat_total = 0
        self.chat_on = False
        self.last_check = 0.0
        self.words_at_check = 0
        self.checks = 0
        self.rejected = 0
        self.verdicts = []
        self.last_trigger = -999.0
        self.started_at = time.time()
        self.skipped = 0
        self.last_ai = 0.0
        self.tokens = 0
        self.manual = []
        self.n = 0
        self.spike = None             # a jump in loudness or chat waiting for its transcript
        self.said = None              # someone said "clip": that moment, waiting for its transcript
        self.last_said = -999.0
        self.last_spike = -999.0      # when the last jump was sent to the AI (stream seconds)
        self.base = {}                # running averages the jumps are measured against, shown on the card
        self.spikes = 0
        self.viewer_log = []          # (second, viewers) each time the count was read, so each second is scaled by its own
        self.viewers = None           # how many people are watching, when the platform says (checked about once a minute)
        self.clock0 = None            # unix time the first audio arrived: viewers' clip times are counted from it
        self.viewer_clips = {}        # clip id -> its times, for the Twitch clips already seen
        self.viewer_used = set()      # clips already part of a burst that was reported
        self.viewer_points = []       # pick mode: viewer bursts waiting until their clip edges are settled
        self.viewer_queue = []        # bursts waiting for the next free moment to be sent (clip mode)
        self._helix = None
        self._helix_user = None
        self._helix_told = set()
        self.top = signals.HourlyTop(settings.get("live_per_hour", 4))   # keeps the strongest moments of each hour
        self.zs = {"loud": 0.0, "chat": 0.0}   # how unusual loudness and chat are now, against the last 15 minutes
        self.game = ""                # which game this stream is, when it can be told
        self.facecam_box = None       # where the webcam is, found once the first clip is made
        self.facecam_checked = False  # the look is made once, even when it finds nothing
        self.ai_busy = 0.0            # when the AI check now running was started, 0 when none is

    def _ask_ai(self, window=None, why=None, score=None, peak=None, cand=None):
        """Ask the AI on its own thread, so a slow answer never stops the recording.

        While the AI thinks, the speech keeps being written and a "clip that" still works. One check
        runs at a time; if the running one is stuck far past its own timeout, a new one is let through.
        """
        now = time.time()
        if self.ai_busy and now - self.ai_busy < config.LIVE_AI_TIMEOUT + 60:
            signals.note(self._log(), cand, sent=False, result="AI busy")
            return

        def work():
            try:
                self._ai_check(window, why, score, peak, cand)
            except Exception as e:  # noqa: BLE001
                self.job["ai"]["last_error"] = str(e)[-200:]
            finally:
                self.ai_busy = 0.0
        self.ai_busy = now
        threading.Thread(target=ctx.bind(work), daemon=True, name="ai_check").start()

    def _find_facecam(self, src):
        """Where the webcam is in the stream, once. Only a sure answer is used.

        One frame goes to the AI as well, and whatever it says is measured again on the
        picture. A stream where the webcam is a square picture along the right edge, or
        one where there is no webcam at all and the game has panels of its own, is told
        apart from a real camera by what moves: a person sits still, the game does not."""
        try:
            box = facecam.detect(src, ask=brain.look_for_facecam)
        except Exception:  # noqa: BLE001 - a missing facecam is not a failure
            return None
        if not box or box.get("conf") != "high":
            return None
        self.job["facecam"] = [box["x"], box["y"], box["w"], box["h"]]
        return self.job["facecam"]

    # ---------------------------------------------------------------- the game this stream is
    def _game(self):
        """The game, from what the creator set or from what the stream's title says.

        The stream title is the best place to look: on launch night it says the game."""
        if self.game:
            return self.game
        found = games.detect(self.settings.get("game"), self.job.get("title"), self.job.get("uploader"),
                             self.job.get("source", {}).get("name"))
        self.game = found[0] or ""
        if self.game:
            try:
                self.job["game"] = self.game
            except Exception:  # noqa: BLE001
                pass
        return self.game

    # ---------------------------------------------------------------- recording
    def start(self):
        SESSIONS[self.job["id"]] = self
        threading.Thread(target=ctx.bind(self._ingest), daemon=True).start()
        threading.Thread(target=ctx.bind(self._analyze), daemon=True).start()
        if self._baseline():
            threading.Thread(target=self._watch_viewers, daemon=True, name="viewers").start()
        m = re.match(r"^https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)", self.job["source"]["url"])
        if m:
            threading.Thread(target=ctx.bind(self._chat), args=(m.group(1).lower(),), daemon=True).start()
            if self.settings.get("sig_viewer_clips", True):
                threading.Thread(target=ctx.bind(self._watch_viewer_clips), daemon=True, name="viewer_clips").start()

    def _baseline(self):
        """True when moments are found against the last 15 minutes of this stream, not by the old fixed rules."""
        return bool(self.settings.get("sig_baseline", True))

    def _watch_viewers(self):
        """About once a minute, ask the platform how many people are watching. It runs on its own thread and any error
        is ignored: without a count, chat is simply measured per chatter instead of per viewer."""
        url = self.job["source"]["url"]
        while not self.stop_flag.is_set() and not self.ended:
            try:
                v = profiles.live_shared(profiles.platform_of(url), url).get("viewers")
                if v:           # a poll that finds nothing keeps the last known count, so units never change midway
                    self.viewers = int(v)
                    with self.lock:
                        self.viewer_log.append((int(self.samples / SR), self.viewers))
                        del self.viewer_log[:-1000]
            except Exception:  # noqa: BLE001
                pass
            self.stop_flag.wait(60)

    def _watch_viewer_clips(self):
        """Every 90 seconds, look at the clips viewers made of this channel. Does nothing without Twitch credentials."""
        if not config.twitch_app():
            if self.job["creator"] not in _NO_TWITCH_TOLD:       # once per creator, not at every recording
                _NO_TWITCH_TOLD.add(self.job["creator"])
                creators.log(self.job["creator"], "Viewers' clips skipped: no Twitch credentials in .env")
            return
        while not self.stop_flag.is_set() and not self.ended:
            try:
                self._poll_viewer_clips()
            except Exception:  # noqa: BLE001
                pass
            self.stop_flag.wait(90)

    def _viewer_trouble(self, key, msg):
        if key not in self._helix_told:        # each kind of trouble is said once per recording, then quiet
            self._helix_told.add(key)
            creators.log(self.job["creator"], msg)

    def _poll_viewer_clips(self):
        """One look at the viewers' clips. A burst of clips from different people is a strong moment.
        True when it asked Twitch."""
        if not (self.clock0 or self.samples):
            return False
        if not self._helix:
            app = config.twitch_app()
            if not app:
                return False
            self._helix = twitch.Helix(*app)
        m = re.match(r"^https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)", self.job["source"]["url"])
        if not m:
            return False
        if not self._helix_user:
            self._helix_user = self._helix.user_id(m.group(1).lower())
            if not self._helix_user:
                self._viewer_trouble("user", "Viewers' clips skipped: Twitch would not answer (check the credentials in .env)")
                return True
        now = time.time()
        if self.samples:        # session seconds stand still while the stream is down, so re-anchor at every look
            self.clock0 = now - self.samples / SR
        # Twitch lists the most viewed clips first, so only a recent window is asked for: new clips are never cut off
        got = self._helix.clips(self._helix_user, max(self.started_at - 60, now - 600))
        if not self._helix.ok:
            self._viewer_trouble("clips", "Viewers' clips: Twitch gave an error when asked for clips (will keep trying quietly)")
            return True
        for t in twitch.clip_times(got, self.clock0):
            if t["end"] > 0 and t["id"] not in self.viewer_clips:      # the first mapping is kept
                self.viewer_clips[t["id"]] = t
        fresh = [t for i, t in self.viewer_clips.items() if i not in self.viewer_used]
        for b in twitch.bursts(fresh):
            self.viewer_used |= set(b["ids"])
            self._viewer_burst(b)
        return True

    def _flush_viewer_queue(self):
        """Send a waiting burst to the AI once no other jump is waiting for its transcript."""
        if self.viewer_queue:
            with self.lock:
                if not self.spike:
                    b = self.viewer_queue.pop(0)
                    self.spike = {"t": float(b["t"]), "why": f"Viewers clipped this ({b['people']} people)",
                                  "z": 10 + b["people"], "viewer_clips": b["people"],
                                  "sig": {"loud": self.zs["loud"], "chat": self.zs["chat"]}}
                    self.spikes += 1

    def _flush_viewer_points(self):
        """List the viewer bursts whose clip edges are settled, or all of them once the stream has ended or Stop was
        pressed (with the best edges known by then), so that nothing waiting is lost."""
        with self.lock:                  # the viewer poller adds to this list while the analysis thread empties it
            waiting = list(self.viewer_points)
        for b in waiting:
            ready, start, end = self._settled(b["t"])
            if ready or self.ended or self.user_stopped:
                with self.lock:
                    if b not in self.viewer_points:
                        continue
                    self.viewer_points.remove(b)
                p = picks.add_point(self, {"start": start, "end": end, "peak": b["t"], "score": 0,
                                           "reason": f"Viewers clipped this ({b['people']} people)", "from": "viewers",
                                           "cand": b.get("cand")})
                if p is None:      # the same moment was listed already (by the AI or a jump): note the viewers on it
                    marked, into = False, None
                    with self.lock:
                        for q in self.job["points"]:
                            if abs(q["t"] - b["t"]) < 10:
                                q["viewers"] = max(int(q.get("viewers") or 0), int(b["people"]))
                                if b.get("cand") and not q.get("cand"):
                                    q["cand"] = b["cand"]       # the point now follows the viewers' candidate
                                elif b.get("cand"):
                                    into = q["cand"]
                                marked = True
                                break
                    if into:
                        signals.note(self._log(), b["cand"], merged_into=str(into)[:40])
                    if marked:
                        jobs.save(self.job)      # outside the lock, like every other save
                    ctx.bump()

    def _viewer_burst(self, b):
        why = f"Viewers clipped this ({b['people']} people)"
        if self.pick:      # like a mark made by hand: always listed
            sp = {"t": b["t"], "viewer_clips": b["people"]}
            b["cand"] = self._candidate(sp)      # a made-by-hand style point skips the AI, so it is logged as not sent
            if self._timing():     # listed once its start and end are settled, see _flush_viewer_points
                with self.lock:
                    self.viewer_points.append(b)
                self._flush_viewer_points()
            else:
                picks.add_point(self, {"start": max(0.0, b["t"] - 30), "end": b["t"] + 10, "peak": b["t"], "score": 0,
                                       "reason": why, "from": "viewers", "cand": b["cand"]})
        else:              # sent to the AI like a jump, and strong enough to rank high; waits if another jump is pending
            with self.lock:
                self.viewer_queue.append(b)
            self._flush_viewer_queue()

    def stop(self, note="Stopped by you"):
        """Instant for the user: the card and the creator show as stopped straight away. Clips already being made
        still finish in the background, but nothing new is transcribed or sent to the AI."""
        self.end_note = note
        self.user_stopped = True
        self.stop_flag.set()
        with self.lock:
            self.job.update(status="done", stage=note)
        jobs.save(self.job)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _ingest(self):
        """Record the stream. However this ends, even when ffmpeg cannot start at all, the session is marked as
        ended, so it stops showing as recording and gives its place to the next one."""
        try:
            self._ingest_loop()
        except Exception as e:  # noqa: BLE001
            self.job["error"] = f"Recording stopped: {media_safe(e)}"
        finally:
            self.ended = True

    def _ingest_loop(self):
        fails = 0
        url = self.job["source"]["url"]
        while not self.stop_flag.is_set():
            try:
                info = resolve(url)
            except NotLive as e:
                fails += 1
                if self.samples == 0 or fails >= 3:
                    if self.samples == 0:
                        self.job["error"] = f"Could not start recording: {e}"
                    break
                self.stop_flag.wait(10)
                continue
            if not self.job.get("uploader"):
                self.job.update(title=info["title"], uploader=info["uploader"])
            self.part += 1
            with self.lock:
                self.seg_clock = max(self.seg_clock, self.samples / SR)
            p = f"p{self.part:02d}"
            cmd = ["ffmpeg", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_delay_max", "10"]
            for u in info["urls"]:
                cmd += ["-i", u]
            a_in = "1:a:0" if len(info["urls"]) > 1 else "0:a:0"
            cmd += ["-map", "0:v:0", "-map", a_in, "-c", "copy", "-f", "segment", "-segment_time", str(SEG_SEC),
                    "-reset_timestamps", "1", "-segment_format", "mpegts", "-segment_list", f"{p}.csv",
                    "-segment_list_type", "csv", f"{p}_%05d.ts",
                    "-map", a_in, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
            with open(self.dir / "ffmpeg.log", "ab") as errlog:
                self.proc = subprocess.Popen(cmd, cwd=self.buf, stdout=subprocess.PIPE, stderr=errlog,
                                             creationflags=media.NO_WINDOW)
                got = 0
                while True:
                    data = self.proc.stdout.read(SR * 2)  # one second of 16-bit mono
                    if not data:
                        break
                    data = data[: len(data) // 2 * 2]
                    got += len(data)
                    x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    level = float(20 * np.log10(np.sqrt((x ** 2).mean() + 1e-10))) if len(x) else -100.0
                    if not self.clock0:
                        self.clock0 = time.time()
                    with self.lock:
                        self.pending += data
                        self.samples += len(data) // 2
                        self.db.append(level)
                self.proc.wait()
            fails = 0 if got > SR * 2 * 20 else fails + 1
            if fails >= 3:
                break
            self.stop_flag.wait(3)

    def _scan_segments(self):
        """Pick up buffer pieces ffmpeg has finished writing."""
        with self.lock:
            for i in range(1, self.part + 1):
                f = self.buf / f"p{i:02d}.csv"
                if not f.exists():
                    continue
                rows = [r.split(",") for r in f.read_text(encoding="utf-8", errors="ignore").splitlines() if r.count(",") >= 2]
                for name, a, b in (r[:3] for r in rows[self.csv_seen.get(i, 0):]):
                    try:
                        dur = max(0.0, float(b) - float(a))
                    except ValueError:
                        continue
                    self.segments.append((self.buf / name, self.seg_clock, self.seg_clock + dur))
                    self.seg_clock += dur
                self.csv_seen[i] = len(rows)
            keep_from = self.seg_clock - self.settings["buffer_min"] * 60
            while self.segments and self.segments[0][2] < keep_from:
                self.segments.pop(0)[0].unlink(missing_ok=True)

    # ---------------------------------------------------------------- chat (Twitch only)
    def _chat(self, channel):
        while not self.stop_flag.is_set() and not self.ended:
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                # ask for the tags: they carry each person's id and the kind of notice (raid, sub)
                s.sendall(f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nNICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{channel}\r\n".encode())
                s.settimeout(30)
                rest = b""
                self.chat_on = True
                while not self.stop_flag.is_set() and not self.ended:
                    try:
                        data = s.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    *lines, rest = (rest + data).split(b"\r\n")
                    for line in lines:
                        if line.startswith(b"PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif b" PRIVMSG #" in line or b" USERNOTICE " in line:
                            self.take_chat_line(line.decode("utf-8", "replace"))
                s.close()
            except Exception:  # noqa: BLE001
                pass
            self.chat_on = False
            self.stop_flag.wait(5)

    def take_chat_line(self, text):
        """One line from Twitch chat. Counts it (messages and different people), and keeps what was said for the
        last two minutes, in memory only: the words are never written anywhere. Bots are skipped, a raid pauses
        the words for 30 seconds (a raid flood is not a reaction), and other notices (subs, gifts) are ignored."""
        p = signals.parse_irc(text)
        if not p:
            return
        with self.lock:
            second = int(self.samples / SR)
            if p["kind"] == "notice":
                if p["notice"] == "raid":
                    self.raid_until = second + 30
                return
            if signals.is_bot(p.get("name") or p["user"]):
                return
            self.chat[second] += 1
            self.chat_total += 1
            if second < self.raid_until:     # a raid's flood is neither a reaction nor a crowd of chatters
                return
            # how many different people, not how many messages: a wave of the same
            # emotes is not a crowd getting excited
            if p["user"]:
                self.chat_users.setdefault(second, set()).add(p["user"])
                self.chat_kept = _trim_names(self.chat_users, second, self.chat_kept)
            if self.settings.get("sig_chat_words", True) and p["user"]:
                self.chat_msgs.setdefault(second, []).append((p["user"], p["text"]))
                for t in [t for t in self.chat_msgs if t < second - CHAT_WORDS_KEEP]:
                    del self.chat_msgs[t]

    def chat_window(self, t0, t1):
        """What chat said from second t0 to t1 (included), see signals.analyze_window."""
        with self.lock:
            msgs = [m for t in range(int(t0), int(t1) + 1) for m in self.chat_msgs.get(t, ())]
        return signals.analyze_window(msgs, signals.word_lists(self.settings))

    def _watch_chat_words(self, n):
        """Every few seconds, read what the last stretch of chat said. A crowd sending the same word, or three
        people asking for a clip, is a jump on its own. Every reading is also kept (second, convergence) for the
        last 15 minutes, so that a later step can find when chat calmed down."""
        if not self.settings.get("sig_chat_words", True) or n < 60:
            return
        win = max(5, min(10, int(self.settings.get("chat_window", 8) or 8)))
        if not self.words_read or n - self.words_read > CHAT_WORDS_KEEP:
            self.words_read = n - win          # first time, or far behind: nothing older than this is kept anyway
        while self.words_read + win <= n:      # a slow pass reads every window it missed, in order
            self.words_read += win
            self._read_chat_window(self.words_read, win)

    def _read_chat_window(self, end, win):
        w = self.chat_window(end - win, end - 1)
        self.conv_hist.append((end, w["convergence"]))
        while self.conv_hist and self.conv_hist[0][0] < end - 900:
            self.conv_hist.pop(0)
        jz = float(self.settings.get("jump_z", 3.5))
        why, strength = None, 0.0
        if w["clip_people"] >= 3:
            why, strength = f"Chat: {w['clip_people']} people asked for a clip", min(1.0, w["clip_people"] / 6)
        elif w["people"] >= 5 and w["convergence"] >= 0.5 and w["top"]:
            said = round(w["convergence"] * w["people"])
            why = f"Chat: {said} people said {w['top'][:25].upper()}" + (f" ({w['top_kind']})" if w["top_kind"] else "")
            strength = w["convergence"]
        if why and not self.spike and end - self.last_spike >= self.AUTO_GAP:
            self.spike = {"t": max(0.0, float(end - 3) - self._shift()), "why": why, "z": jz + 2 * strength, "chat": w,
                          "sig": {"loud": self.zs["loud"], "chat": self.zs["chat"]}}
            self.last_spike = end
            self.spikes += 1

    def _chat_spike(self, s, e):
        """True if chat was clearly busier than usual during (and just after) these seconds: the busiest five seconds anywhere in s to
        e+6, against the 15 minutes before them (the old rule: at least 3x busier than the stream's average).

        The crowd is counted as people, not messages: ten people saying "GG" is a reaction, one
        person saying "GG" ten times is a bot."""
        elapsed = self.samples / SR
        if self._baseline():
            # every five-second stretch inside s..e+6 is judged against the 15 minutes before it: the best one counts,
            # so a burst that had faded by the end of the stretch is still seen
            last = int(e) + 6
            best = max(self._chat_z(t) for t in range(min(int(s) + 4, last), last + 1))
            return best >= float(self.settings.get("jump_z", 3.5))
        if not self.chat_total or elapsed < 60:
            return False
        avg = self.chat_total / elapsed
        a, b = int(s), int(e) + 6
        rate = sum(self.chat.get(t, 0) for t in range(a, b + 1)) / (b - a + 1)
        if rate <= max(3 * avg, 1.0):
            return False
        people = self.chat_people()
        if not people:
            return True
        p_avg = sum(len(v) for v in people.values()) / max(1, len(people))
        p_rate = sum(len(people.get(t, ())) for t in range(a, b + 1)) / (b - a + 1)
        return p_rate >= max(2.0, 1.5 * p_avg)

    def _chat_series(self, end, length):
        """(the series, the viewer count its last second was scaled by). Different people talking in each second up to `end`, per 1,000 viewers when the viewer count is known.
        None when there is no chat to read. Where names cannot be told, messages are counted instead."""
        people = self.chat_people()
        rng = range(max(0, end - length + 1), end + 1)
        with self.lock:
            log = list(self.viewer_log)
        at, i, known = [], 0, (log[0][1] if log else None)    # the count known at each second, carried forward
        for t in rng:
            while i < len(log) and log[i][0] <= t:
                known, i = log[i][1], i + 1
            at.append(known)
        if people:
            return [signals.per_viewer(len(people.get(t, ())), v) for t, v in zip(rng, at)], at[-1]
        if self.chat_on or self.chat_total:
            return [signals.per_viewer(self.chat.get(t, 0), v) for t, v in zip(rng, at)], at[-1]
        return None, None

    def _chat_z(self, end, now_len=5):
        """How unusual chat is in the `now_len` seconds ending at second `end` (included), against the 15 minutes
        before them. A chat that is nearly silent has almost no spread, so a couple of messages would score huge:
        that needs at least a small crowd. The viewer count used for the crowd and the floor is the one the series
        itself was scaled by, so they are always in the same units."""
        series, v = self._chat_series(end, 920 + now_len + 10)
        if not series:
            return 0.0
        if sum(series[-now_len:]) / now_len < signals.per_viewer(1.5, v):
            return 0.0
        floor = signals.per_viewer(self.CHAT_FLOOR, v)   # half a chatter a second: below that is just quiet
        return min(signals.series_z(series, now_len=now_len, exclude=now_len + 5, floor=floor), self.Z_CAP)

    def chat_people(self):
        """Second -> how many different people were talking, where that is known.

        A copy is taken under the lock: the chat reader thread adds to it as it goes."""
        with self.lock:
            return {t: set(names) for t, names in self.chat_users.items()}

    # ---------------------------------------------------------------- analysis
    def _transcribe_step(self):
        with self.lock:
            n = len(self.pending) // 2
            hurry = self.ended or bool(self.manual)
            if n < (SR if hurry else SR * 7):   # about every 7 s, so "what was just said" really is live
                return False
            audio = np.frombuffer(bytes(self.pending[: n * 2]), dtype=np.int16)
            base = self.pending_base
        cut = n
        if not hurry or n > SR * 24:
            # cut at the quietest moment between 4 and 6.5 s, so no word is split; the rest waits for the next pass
            _, cut = next(transcribe._chunks(audio, 4.0, 6.5))
        words = transcribe.transcribe(audio[:cut])
        off = base / SR
        for w in words:
            w["s"], w["e"] = round(w["s"] + off, 2), round(w["e"] + off, 2)
        with self.lock:
            del self.pending[: cut * 2]
            self.pending_base += cut
            self.words += words
            self.t_done = self.pending_base / SR
        self._triggers(words)
        (self.dir / "words.json").write_text(json.dumps(self.words), encoding="utf-8")
        return True

    AUTO_BEFORE, AUTO_AFTER, AUTO_GAP = 30, 20, 60   # seconds of stream sent around a jump; least time between jumps
    CHAT_FLOOR = 0.5                                  # chatters per second: the least spread chat is judged against
    Z_CAP = 50.0                                      # a z-score above this is not told apart: it is a moment either way
    JUMP = 0.6                                        # 60% louder or busier than the last three minutes

    def _watch_spikes(self):
        """Always on. Keep a running average of loudness and chat over the last three minutes (leaving out the
        last few seconds), and mark a moment when the last five seconds jump a set percentage above it. Nothing is
        sent yet: the AI gets that moment once its transcript is ready, with the half minute before it."""
        self._flush_viewer_queue()
        self._flush_viewer_points()
        if self._baseline():
            self._watch_spikes_z()
        else:
            self._watch_spikes_old()
        self._watch_chat_words(len(self.db))
        self._watch_lag(len(self.db))
        if self.conv_hist:
            self.base["conv"] = round(self.conv_hist[-1][1], 2)     # the card shows how much chat agrees

    def _watch_spikes_old(self):
        with self.lock:
            n = len(self.db)
            if n < 60:
                self.base = {"warming": True}
                return
            db = np.array(self.db[max(0, n - 190):], dtype=np.float32)
            people = self.chat_people()
            if people:
                chat = [len(people.get(t, ())) for t in range(max(0, n - 190), n)]
            else:
                chat = [self.chat.get(t, 0) for t in range(max(0, n - 190), n)] if (self.chat_on or self.chat_total) else None
        amp = 10 ** (db / 20)                       # loudness as plain level, so a percentage means something
        base, cur = float(amp[:-10].mean()), float(amp[-5:].mean())
        loud_jump = cur / max(base, 1e-6) - 1
        pct = self.JUMP
        why = []
        if loud_jump >= pct:
            why.append(f"loudness +{round(loud_jump * 100)}%")
        chat_base = chat_now = None
        if chat is not None:
            chat_base = sum(chat[:-10]) / max(1, len(chat) - 10)
            chat_now = sum(chat[-8:]) / 8          # chat is bursty, so look at a slightly longer stretch
            if chat_now >= max(1.5, chat_base * (1 + pct)):
                why.append(f"chat +{round((chat_now / max(chat_base, 0.01) - 1) * 100)}%")
        self.base = {"loud_db": round(20 * np.log10(max(base, 1e-6)), 1), "loud_now": round(loud_jump * 100),
                     "chat": round(chat_base, 2) if chat_base is not None else None,
                     "chat_now": round(chat_now, 2) if chat_now is not None else None, "pct": round(pct * 100)}
        if why and not self.spike and n - self.last_spike >= self.AUTO_GAP:
            self.spike = {"t": float(n - 3), "why": ", ".join(why)}
            self.last_spike = n
            self.spikes += 1

    def _watch_spikes_z(self):
        """The same watch, measured against the last 15 minutes of this stream: how far the last five seconds of
        loudness, and of different chatters (per 1,000 viewers when known), sit from what is usual here. A middle
        value and a middle distance are used, so a long shout earlier does not hide the next one."""
        with self.lock:
            n = len(self.db)
            if n < 60:
                self.base = {"warming": True}
                return
            loud = 10 ** (np.array(self.db[max(0, n - 930):], dtype=np.float64) / 20)   # plain level, not decibels
            zc = self._chat_z(n - 1)      # the seconds n-5..n-1: the one still filling is left out, as for loudness
        zl = min(signals.series_z(loud), self.Z_CAP)
        self.zs = {"loud": zl, "chat": zc}
        jz = float(self.settings.get("jump_z", 3.5))
        self.base = {"loud_z": round(zl, 1), "chat_z": round(zc, 1), "jump_z": jz}
        why = []
        if zl >= jz:
            why.append(f"loudness z {zl:.1f}")
        if zc >= jz:
            why.append(f"chat z {zc:.1f}")
        if why and not self.spike and n - self.last_spike >= self.AUTO_GAP:
            chat_only = zc >= jz and zl < jz      # only a reaction in chat: the streamer did it a few seconds earlier
            self.spike = {"t": max(0.0, float(n - 3) - (self._shift() if chat_only else 0)), "why": ", ".join(why),
                          "z": max(zl, zc), "sig": {"loud": zl, "chat": zc}}
            self.last_spike = n
            self.spikes += 1

    def _timing(self):
        return bool(self.settings.get("sig_timing", True))

    def _shift(self):
        """Seconds to move a chat-born moment back, so it lands on what the streamer did, not on the reaction."""
        return self.lag if self._timing() else 0

    def _watch_lag(self, n):
        """Every 5 minutes, measure how many seconds chat follows loudness in this stream."""
        if not self._timing() or n < 300 or n - self.lag_at < 300:
            return
        self.lag_at = n
        with self.lock:
            loud = 10 ** (np.array(self.db[max(0, n - 900):n], dtype=np.float64) / 20)   # the same seconds as the chat series
        series, _ = self._chat_series(n - 1, 900)
        if series:
            self.lag = signals.measure_lag(loud, series, default=self.lag)    # no pattern found: keep what was measured

    def _calm_after(self, t):
        """The first second after t when chat agreed no more than usual again (convergence z under 1), or None
        when there are no chat readings."""
        hist = list(self.conv_hist)
        base = [c for s, c in hist if s <= t] or [c for _, c in hist]
        win = max(5, min(10, int(self.settings.get("chat_window", 8) or 8)))
        react = t + self.lag                    # chat reacts a few seconds after the moment: look only from there on
        calm = lambda c: signals.robust_z(base, c, floor=0.1) < 1
        up = next((i for i, (s, c) in enumerate(hist) if s >= react and not calm(c)), None)
        if up is not None:                      # chat did get excited: calm is the first reading after that
            return next((s for s, c in hist[up + 1:] if calm(c)), None)
        for s, c in hist:                       # never excited: the first window that starts after the reaction time
            if s - win > react and calm(c):
                return s
        return None

    def _settled(self, t):
        """(ready, start, end): whether a moment at second t can be sent now. The transcript must reach five seconds
        past the end, and chat must have calmed down after the reaction (or there is no chat, or the clip is
        already as long as it may be)."""
        start, end = self._edges(t)
        if not self._timing():
            return self.t_done >= t + self.AUTO_AFTER, start, end
        has_chat = self.settings.get("sig_chat_words", True) and (self.chat_on or self.chat_total)
        calm = self._calm_after(t) is not None
        ok = self.t_done >= end + 5 and (calm or not has_chat or self.t_done >= start + float(self.settings["max_len"]) + 5)
        return ok, start, end

    def _edges(self, t):
        """(start, end) of a clip for a moment at second t. With smart edges: from the sentence that sets it up to
        the sentence end once chat has calmed down. Otherwise the old fixed stretch around it."""
        if not self._timing():
            return max(0.0, t - self.AUTO_BEFORE), t + self.AUTO_AFTER
        st = self.settings
        with self.lock:
            words = [w for w in self.words if t - 90 <= w["s"] <= t + float(st["max_len"]) + 30]
        return signals.clip_edges(t, brain.make_lines(words), float(st["min_len"]), float(st["max_len"]),
                                  self._calm_after(t))

    def _ai_check(self, window=None, why=None, score=None, peak=None, cand=None):
        """Judge 1 reads only the words. If it finds a moment, judge 2 reads only the loudness and chat activity
        for the same stretch, without seeing the words. A clip is made when both say yes (or as set in Setup)."""
        st = self.settings
        prev = self.job["points"] if self.pick else self.job["clips"]
        trigger = why                 # what started this check; `why` is reused below for the judges' verdict
        every = int(st.get("ai_every_min", 3)) or 3
        if window:                    # just the stretch around a jump, or around someone saying "clip"
            ws, now = max(0.0, window[0]), window[1]
        else:
            now = self.t_done
            ws = max(0.0, now - max(every, 3) * 60 - 90)  # each check overlaps the last by 90 s, so nothing falls between
        self.last_check = self.t_done
        win = [w for w in self.words if ws <= w["s"] <= now]
        recent = [c for c in prev if c.get("score", 0) > 0 and time.time() - c.get("made_at", 0) < 3600]
        last_clip = max((c.get("made_at", 0) for c in prev), default=self.started_at)
        must = st["must_clip_min"] > 0 and time.time() - last_clip >= st["must_clip_min"] * 60
        verdict = {"t": round(now), "transcript": None, "reaction": None, "trigger": why, "cand": cand}
        if len(win) < 8:
            self.skipped += 1
            return self._verdict(verdict, "Skipped: nobody talking")
        strongest = self._baseline() and not must and (trigger or "").startswith("Jump") and score is not None
        if strongest:    # the strongest few moments of the hour are kept, whatever order they came in
            if not self.top.admit(float(score), time.time()):
                self.skipped += 1
                return self._verdict(verdict, "Passed over: stronger moments this hour")
        elif len(recent) >= st["live_per_hour"] and not must and not self.pick:
            self.skipped += 1
            return self._verdict(verdict, f"Skipped: {st['live_per_hour']} clips this hour already")
        usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
        title, up = self.job["title"], self.job["uploader"]
        try:
            cands, tok = brain.judge_transcript(win, ws, now, title, up, st["min_len"], st["max_len"], prev,
                                                n=1 if every <= 2 else 2, must=must, game=self._game(),
                                                timeout=config.LIVE_AI_TIMEOUT)  # Auto (0) asks for one
        except Exception as e:  # noqa: BLE001
            self.job["ai"]["last_error"] = str(e)[-200:]
            if self.pick and window:      # no AI: a jump in loudness or chat, or "clip it", is still worth listing
                jump = (why or "").startswith("Jump")
                t = now - (self.AUTO_AFTER if jump else self.SAID_AFTER + 5)
                a, b = ws, now
                if jump and peak is not None and self._timing():
                    a, b = self._edges(peak)
                    t = peak
                picks.add_point(self, {"start": a, "end": b, "peak": max(a, t), "score": 0, "reason": why or "",
                                       "from": "jump" if jump else "said", "cand": cand})
            return self._verdict(verdict, "AI error: " + str(e)[-80:])
        self.checks += 1
        self.tokens += tok
        keep = [c for c in cands if c["score"] >= (40 if must else st["min_score"])]
        verdict["transcript"] = max((c["score"] for c in cands), default=0)
        if cand and cands and cands[0].get("rating"):
            # The marks the AI gave the best moment. The one-line reason is text, and the log keeps numbers only.
            signals.note(self._log(), cand, rating={k: v for k, v in cands[0]["rating"].items() if k != "why"})
        if not keep:
            return self._verdict(verdict, "Words judge: nothing clip-worthy")
        z = brain.loud_zscore(np.array(self.db, dtype=np.float32))
        lo, hi = int(ws), int(now) + 1
        people = self.chat_people()
        if people:
            chat = [len(people.get(t, ())) for t in range(lo, hi)]
            unit = "people"
        else:
            chat = [self.chat.get(t, 0) for t in range(lo, hi)] if (self.chat_on or self.chat_total) else None
            unit = "messages"
        try:
            sig, tok2 = brain.judge_signals(z[lo:hi], chat, lo, now, unit=unit, timeout=config.LIVE_AI_TIMEOUT)
            self.checks += 1
            self.tokens += tok2
        except Exception as e:  # noqa: BLE001
            sig = []
            self.job["ai"]["last_error"] = str(e)[-200:]
        added = 0
        for c in keep:
            match = [m for m in sig if m["end"] >= c["start"] - 10 and m["start"] <= c["end"] + 10 and m["score"] >= 50]
            rs = max((m["score"] for m in match), default=0)
            verdict["reaction"] = rs
            both = bool(match)
            ok = both or (st["decide_mode"] == "either" and c["score"] >= 90) or (must and added == 0)
            if not ok:
                self.rejected += 1
                self._verdict(verdict, "Reaction judge said no, not clipped")
                continue
            if any(abs(c["start"] - k["start"]) < st["min_gap_min"] * 60 for k in prev):
                self._verdict(verdict, "Too close to the last clip")
                continue
            why = "Both judges agreed" if both else ("Words judge was very sure" if c["score"] >= 90 and
                                                     st["decide_mode"] == "either" else "Forced: no clip for a while")
            c["judges"] = {"words": c["score"], "reaction": rs or None, "why": why}
            c["reason"] = f"{c['reason']} ({why.lower()}.)"
            if cand:
                c["cand"] = cand
            c["from"] = "viewers" if "Viewers clipped" in (trigger or "") else "jump" if (trigger or "").startswith("Jump") else "said" if trigger else "ai"
            if self._add_clip(c) is None:       # pick mode: this moment is already in the list
                self._verdict(verdict, "Already in the list")
                continue
            self._verdict(verdict, "Clipped: " + why.lower())
            added += 1
            must = False
            if added >= (1 if every <= 2 else 2):
                break

    def _verdict(self, v, result):
        v = dict(v)
        cand = v.pop("cand", None)
        self.verdicts.append(dict(v, result=result))
        del self.verdicts[:-6]
        if cand:           # what happened to this candidate, for the log (short labels and numbers only)
            sent = not result.startswith(("Skipped", "Passed over"))
            signals.note(self._log(), cand, sent=sent, result=self._label(result), words=v.get("transcript"),
                         reaction=v.get("reaction"))

    @staticmethod
    def _label(result):
        """A short fixed label for the log: never the text of an error, which could hold anything."""
        return "ai error" if result.startswith("AI error") else result[:signals.MAX_TEXT]

    def _log(self):
        try:
            return signals.log_for(self.job.get("creator"))
        except Exception:  # noqa: BLE001
            return None

    def _candidate(self, sp):
        """Score a jump with everything known about it, put that score on it (`score`, used to rank the strongest
        moments of the hour) and write it to the log. Returns the candidate's id, or None when the log is off.
        Only numbers and short labels are logged: never what chat said."""
        sig = sp.get("sig") or {}
        v = {"loud": sig.get("loud", self.zs["loud"]), "chat": sig.get("chat", self.zs["chat"]),
             "viewer_clips": sp.get("viewer_clips") or 0}
        w = sp.get("chat")
        if w:
            v.update(convergence=w["convergence"], clip_requests=w["clip_people"],
                     reactions=min(w["people"], sum(w["types"].get(k, 0) for k in ("laugh", "shock", "hype"))))
        fused = signals.fuse(v, self.settings.get("sig_weights"))
        sp["score"] = fused
        kind = "viewers" if v["viewer_clips"] else (w.get("top_kind") or "chat") if w else ("loud" if v["loud"] >= v["chat"] else "chat")
        log = self._log()
        if log is None:
            return None
        try:
            sp["cand"] = log.add({"job": str(self.job.get("id") or "")[:40], "t": round(time.time()), "at": round(sp["t"]),
                                  "values": {k: round(float(x), 2) for k, x in v.items()}, "score": fused,
                                  "sent": False, "kind": kind[:40]})
        except Exception:  # noqa: BLE001
            return None
        return sp["cand"]

    CLIP_WORDS = re.compile(r"\bclip(\s+(it|that|this))?\b", re.I)
    SAID_BEFORE, SAID_AFTER, SAID_GAP = 60, 15, 45

    def _triggers(self, new_words):
        """Built in: when someone says "clip" ("clip it", "clip that"), the AI looks at that moment as soon as its
        transcript is ready; people say it right after something worth clipping. Then the creator's own trigger
        phrases from Setup, which clip straight away with no AI check."""
        for w in new_words or []:
            if self.CLIP_WORDS.search(re.sub(r"[^a-z' ]", "", w["w"].lower())) and w["s"] - self.last_said > self.SAID_GAP \
                    and not self.said:
                self.said = {"t": w["s"], "why": 'Someone said "clip"'}
                self.last_said = w["s"]
        phrases = [p.strip().lower() for p in self.settings.get("trigger_words", "").split(",") if p.strip()]
        if not phrases or not new_words:
            return
        key = [re.sub(r"[^a-z0-9']", "", w["w"].lower()) for w in new_words]
        for p in phrases:
            parts = [re.sub(r"[^a-z0-9']", "", x) for x in p.split()]
            for i in range(len(key) - len(parts) + 1):
                if key[i:i + len(parts)] == parts:
                    t = new_words[i]["s"]
                    if t - self.last_trigger > 45:
                        self.last_trigger = t
                        self.manual.append({"start": max(0.0, t - 30), "end": t + 2, "t": t, "from": "trigger", "why": f'Someone said "{p}"'})

    def _manual_step(self):
        for req in [m for m in self.manual if m["end"] <= self.t_done or self.ended]:
            if self.pick:          # a trigger word in pick mode: list the moment, no AI call needed
                self.manual.remove(req)
                t = req.get("t", req["end"])
                picks.add_point(self, {"start": req["start"], "end": req["end"], "peak": max(req["start"], t - 5),
                                       "score": 0, "reason": req.get("why") or "Marked", "from": req.get("from", "trigger")})
                continue
            text = " ".join(w["w"] for w in self.words if req["start"] <= w["s"] <= req["end"])
            usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
            try:
                c = brain.describe(text, self.job["title"], self.job["uploader"], game=self._game(),
                                   timeout=config.LIVE_AI_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                # Someone asked for this clip by hand. A slow or broken AI costs it its hook, not the clip.
                self.job["ai"]["last_error"] = str(e)[-200:]
                c = {"score": 0, "category": "other", "hook": "", "title": self.job["title"],
                     "description": "", "hashtags": [], "highlight_words": []}
            self.manual.remove(req)
            c.update(start=round(req["start"], 2), end=round(min(req["end"], self.samples / SR), 2), score=0)
            if req.get("why"):
                c["reason"] = req["why"]
            self._add_clip(c)

    def _res(self):
        """The export size to use now: the creator's current choice (it may have changed since this recording began)."""
        r = (creators.get(self.job["creator"]) or {}).get("res") if self.job.get("creator") else None
        return r if r in (720, 1080) else self.settings.get("res", 1080)

    def _add_clip(self, c):
        if self.pick:
            return picks.add_point(self, c)
        self.n += 1
        c.update(id=f"clip{self.n:03d}", style=self.settings["style"], layout=self.settings["layout"], res=self._res(),
                 captions=self.job["options"].get("captions", True), status="queued", version=0, made_at=time.time())
        with self.lock:
            self.job["clips"].append(c)
        jobs.save(self.job)
        jobs.submit_live(lambda: self._render(c))
        return c

    def _render(self, clip):
        """Runs on the render worker: copy the needed buffer pieces, then make the clip."""
        deadline = time.time() + 45
        while time.time() < deadline:
            self._scan_segments()
            if self.ended or (self.segments and self.segments[-1][2] >= clip["end"] + 1):
                break
            time.sleep(1)
        with self.lock:
            segs = [s for s in self.segments if s[2] > clip["start"] - PAD and s[1] < clip["end"] + PAD and s[0].exists()]
        if not segs or segs[0][1] > clip["start"] + 2:
            clip.update(status="error", error="That part of the stream is no longer in the buffer.")
            jobs.save(self.job)
            return
        if "width" not in self.job:
            meta = media.probe(segs[0][0])
            self.job.update(width=meta["width"], height=meta["height"])
        lst = self.dir / "clips" / f"{clip['id']}.txt"
        lst.write_text("".join(f"file '{s[0].as_posix()}'\n" for s in segs), encoding="utf-8")
        src = self.dir / "clips" / f"{clip['id']}.src.ts"
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                       "-c", "copy", str(src)])
        except Exception as e:  # noqa: BLE001
            clip.update(status="error", error=media_safe(e))
            jobs.save(self.job)
            return
        finally:
            lst.unlink(missing_ok=True)
        # Only the gameplay layout uses a webcam, and only once the clip's own file is written: the
        # webcam is looked for in it, once per stream.
        if clip.get("layout") == "gameplay":
            if not self.facecam_checked:
                self.facecam_box = self._find_facecam(src)
                self.facecam_checked = True
            if self.facecam_box:
                clip["facecam"] = self.facecam_box
        dur = segs[-1][2] - segs[0][1]
        offset = self._align(src, segs[0][1] + self.clock_fix, dur)
        self.clock_fix = offset - segs[0][1]  # the two clocks drift ~4 s an hour; start the next clip from here
        clip.update(src=str(src), src_offset=round(offset, 2), src_end=round(offset + dur, 2))
        clip["start"] = max(clip["start"], clip["src_offset"])
        clip["end"] = min(clip["end"], clip["src_end"])
        jobs.render_one(self.job, clip, list(self.words))

    def _align(self, src, guess, dur):
        """Where `src` really starts on the transcript clock. The video pieces and the audio feed keep separate
        clocks that drift apart, so transcribe the clip's own audio and line its words up with the live words.
        Local speech model only: no AI calls."""
        key = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
        try:
            wav = src.with_suffix(".wav")
            media.extract_audio(src, wav)
            heard = transcribe.transcribe(media.load_audio(wav))
            wav.unlink(missing_ok=True)
            with self.lock:
                near = [w for w in self.words if guess - 15 <= w["s"] <= guess + dur + 15]
            diffs = []
            for h in heard:
                k = key(h["w"])
                if len(k) < 3:
                    continue
                m = [w["s"] for w in near if key(w["w"]) == k and abs(w["s"] - (guess + h["s"])) < 8]
                if m:
                    diffs.append(min(m, key=lambda s: abs(s - (guess + h["s"]))) - h["s"])
            if len(diffs) >= 4:
                return float(np.median(diffs))
        except Exception:  # noqa: BLE001 - fall back to the piece clock
            pass
        return guess

    def clip_last(self, seconds):
        end = self.samples / SR
        if end < 5:
            raise ValueError("Nothing has been recorded yet.")
        self.manual.append({"start": max(0.0, end - seconds), "end": end})

    def mark(self, t):
        """The clipper marked this moment in the transcript (Live Transcriber)."""
        if not self.pick or self.analysis_done:
            raise ValueError("This recording has already ended.")
        return picks.add_point(self, {"start": t, "end": t, "peak": t, "score": 0, "reason": "Marked by you", "from": "you"})

    def _publish(self):
        elapsed = self.samples / SR
        lines = brain.make_lines([w for w in self.words if w["s"] >= self.t_done - 45])[-5:]
        with self.lock:
            sec = int(elapsed)
            self.job.update(
                duration=elapsed,
                live={"elapsed": round(elapsed), "buffer_sec": round(self.segments[-1][2] - self.segments[0][1]) if self.segments else 0,
                      "lines": [l["text"] for l in lines], "level": [round(x) for x in self.db[-60:]],
                      "chat": [self.chat.get(t, 0) for t in range(max(0, sec - 60), sec)] if self.chat_on or self.chat_total else None,
                      "checks": self.checks, "skipped": self.skipped, "tokens": self.tokens, "words": len(self.words),
                      "rejected": self.rejected, "verdicts": list(reversed(self.verdicts)),
                      "every_min": int(self.settings.get("ai_every_min", 3)) or 3,
                      "auto": None,
                      "pending_clips": len(self.manual)})
        jobs.save(self.job)

    def _analyze(self):
        cid = self.job["creator"]
        started = False
        while True:
            busy = False
            try:
                if not self.user_stopped and not self.ended:      # a recording cannot run for ever
                    if time.time() - self.started_at > config.MAX_LIVE_HOURS * 3600:
                        self.stop(f"Stopped after {config.MAX_LIVE_HOURS:g} hours, the longest one recording can run")
                self._scan_segments()
                busy = self._transcribe_step()
                if not started and self.samples:
                    started = True
                    self.job.update(status="live", stage="Recording")
                    creators.log(cid, f"Recording started: {self.job['title']}")
                self._manual_step()
                if self.pick:
                    picks.flush_holds(self)
                # Three ways the AI is asked: on the creator's schedule (every N minutes), and always, when the
                # stream suddenly gets louder or chat busier, or when someone says "clip".
                every = int(self.settings.get("ai_every_min", 3)) or 3
                self._watch_spikes()
                sp, said = self.spike, self.said
                if self.user_stopped:
                    pass
                elif said and (self.t_done >= said["t"] + self.SAID_AFTER or (self.ended and not busy)):
                    self.said = None
                    self._ask_ai((said["t"] - self.SAID_BEFORE, said["t"] + self.SAID_AFTER), said["why"])
                # A jump waits until its end is known: the transcript has reached it and chat has calmed down. When
                # chat never calms, it waits at most until t + max_len + 5. Jumps found meanwhile are skipped by
                # the usual gap between jumps (AUTO_GAP), so a waiting one is never replaced.
                elif sp and (self._settled(sp["t"])[0] or (self.ended and not busy)):
                    self.spike = None
                    cand = self._candidate(sp)
                    if self._timing():
                        a, b = self._edges(sp["t"])
                        self._ask_ai((a - 10, b + 5), "Jump: " + sp["why"], sp["score"], peak=sp["t"], cand=cand)
                    else:
                        self._ask_ai((sp["t"] - self.AUTO_BEFORE, sp["t"] + self.AUTO_AFTER), "Jump: " + sp["why"], sp["score"], cand=cand)
                elif self.t_done - self.last_check >= every * 60 or (self.ended and not busy and self.t_done > self.last_check):
                    self._ask_ai()
                self._publish()
            except Exception as e:  # noqa: BLE001
                self.job["ai"]["last_error"] = str(e)[-200:]
            # Leaving here must not happen while an AI check is still running: it may still be adding a
            # clip, and cleanup() deletes the buffer that clip is cut from. That is true after a stop as
            # well as at the end of a stream, so both wait for it. Stopping is still instant on the page:
            # stop() has already written the status. In the Live Transcriber, a moment whose stretch of
            # stream is not saved yet holds the session open the same way, so pressing Stop never loses it.
            with self.lock:
                pending_viewers = bool(self.viewer_points)
            holds = self.pick and (bool(picks.waiting(self)) or pending_viewers)
            if ((self.user_stopped and not self.manual and not self.ai_busy and not holds)
                    or (self.ended and not busy and not self.ai_busy and not holds
                        and len(self.pending) < SR * 2 and not self.manual)):
                break
            if not busy:
                time.sleep(2)
        self.analysis_done = True
        failed = bool(self.job.get("error")) and not started
        self.job.update(status="error" if failed else "done", stage="Could not record" if failed else self.end_note)
        jobs.save(self.job)
        made = f"{len(self.job['points'])} moments" if self.pick else f"{len(self.job['clips'])} clips"
        creators.log(cid, f"Recording finished ({self.end_note.lower()}): {made}, "
                          f"{round(self.samples / SR / 60)} min" if not failed else f"Recording failed: {self.job['error']}")

        if failed:
            alerts.notify(f"recording failed for {self.job['source']['name']}: {self.job['error']}")
        else:
            alerts.notify(f"recording finished for {self.job['source']['name']}: {made}, "
                          f"{round(self.samples / SR / 60)} min.")

        def cleanup():  # runs after any queued renders, then frees the buffer
            for f in self.buf.glob("*"):
                f.unlink(missing_ok=True)
            self.finished = True
            SESSIONS.pop(self.job["id"], None)

        jobs.submit_live(cleanup)


def recover(job):
    """After a restart: finish the clips of an interrupted recording from the stream pieces still on disk."""
    c = creators.get(job["creator"]) or dict(creators.DEFAULTS)
    s = Session(job, c)
    s.ended, s.end_note = True, "Recording stopped when the studio closed"
    s.part = len(list(s.buf.glob("p*.csv")))
    s.seg_clock = 0.0
    s.csv_seen = {}
    with s.lock:
        for i in range(1, s.part + 1):
            f = s.buf / f"p{i:02d}.csv"
            rows = [r.split(",") for r in f.read_text(encoding="utf-8", errors="ignore").splitlines() if r.count(",") >= 2]
            for name, a, b in (r[:3] for r in rows):
                try:
                    dur = max(0.0, float(b) - float(a))
                except ValueError:
                    continue
                s.segments.append((s.buf / name, s.seg_clock, s.seg_clock + dur))
                s.seg_clock += dur
            s.csv_seen[i] = len(rows)
    try:
        s.words = json.loads((s.dir / "words.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        s.words = []
    s.n = len(job["clips"])
    todo = [k for k in job["clips"] if k.get("status") == "queued"]
    for k in todo:
        jobs.submit_live(lambda k=k: s._render(k))

    for p in job.get("points", []):
        if p["hold"]["status"] in ("waiting", "saving"):     # interrupted: save it from the pieces still on disk
            p["hold"]["status"] = "saving"
            jobs.submit_live(lambda p=p: picks.save_hold(s, p))

    def cleanup():
        for f in s.buf.glob("*"):
            f.unlink(missing_ok=True)
        job.pop("recover", None)
        jobs.save(job)
        what = f"{len(job.get('points', []))} moment(s)" if job.get("mode") == "pick" else f"{len(todo)} clip(s)"
        creators.log(job["creator"], f"Finished {what} from a recording interrupted by a restart")

    jobs.submit_live(cleanup)


def start_session(creator: dict, url=None, mode="auto") -> dict:
    url = normalize(url or creator.get("channel") or "")
    if not re.match(r"^https?://[^/]+\.[^/]+/", url + "/"):
        raise ValueError("Add this creator's channel link first.")
    if active_for(creator["id"]):
        raise ValueError("This creator is already being recorded.")
    if not media.site_allowed(url):
        raise ValueError("Channel links must be from YouTube, Twitch or Kick.")
    if media.is_youtube(url) and media.youtube_paused():
        raise ValueError(media.YT_BLOCKED.replace("download videos", "record YouTube streams")
                         .replace("Upload the video file instead, or use a Twitch or Kick link. YouTube links",
                                  "Twitch and Kick streams still work. YouTube"))
    if len(active_mine()) >= config.MAX_LIVE_PER_USER:
        raise ValueError(f"You're already recording {len(active_mine())} streams, the most this server allows at once. Stop one first.")
    if len(active()) >= config.MAX_LIVE_TOTAL or (active() and load.busy()):
        raise ValueError("PeakCut is recording as many streams as it can right now. Try again in a little while.")
    from . import tools
    if tools.missing():
        raise ValueError(tools.HELP)
    job = jobs.create({"type": "live", "url": url, "name": f"{creator['name']} live"}, {}, enqueue=False,
                      creator=creator["id"], kind="live")
    job.update(status="live", stage="Connecting to the stream")
    if mode == "pick":
        job.update(mode="pick", points=[], cuts=[])
    jobs.save(job)
    Session(job, creator).start()
    return job


def _watch_user(uid):
    """Start recording any of this user's auto-record creators who have gone live."""
    from . import profiles
    with ctx.as_user(uid):
        try:
            todo = [c for c in creators.all_creators() if c["auto_watch"] and c["channel"]]
            if not todo:
                return
        except Exception:  # noqa: BLE001
            return
        for c in todo:
            _watch_one(c)


def check_now(cid):
    """Auto-record was just turned on: look now, on a thread, instead of waiting for the next round."""
    uid = ctx.uid()

    def run():
        from . import profiles
        with ctx.as_user(uid):
            c = creators.get(cid)
            if not c or not c["auto_watch"] or not c["channel"]:
                return
            for url in c.get("channels", {}).values():
                profiles._live.pop(url.rstrip("/").lower(), None)     # a fresh answer, not one from two minutes ago
            _watch_one(c)
            profiles.refresh(cid, force=True)
    threading.Thread(target=run, daemon=True).start()


def _watch_one(c):
    """Start recording this auto-record creator if they are live on any of their channels."""
    from . import profiles
    try:
        c = creators.get(c["id"])                     # the creator may have been deleted or switched off while waiting
        if not c or not c.get("auto_watch"):
            return
        if active_for(c["id"]) or len(active_mine()) >= config.MAX_LIVE_PER_USER or len(active()) >= config.MAX_LIVE_TOTAL or (active() and load.busy()):
            return
        # the chosen channel first, then their other platforms
        urls = [c["channel"]] + [u for u in c.get("channels", {}).values() if u != c["channel"]]
        for url in urls:
            # one shared "are they live?" answer per channel, however many people follow it
            if not profiles.live_shared(profiles.platform_of(url), url).get("live"):
                continue
            if media.is_youtube(url):
                # check YouTube will hand over the stream before making a recording that could only fail
                if media.youtube_paused():
                    continue
                try:
                    resolve(normalize(url))
                except NotLive:
                    continue
            start_session(c, url)
            where = creators.platform_key(url).title()
            creators.log(c["id"], f"Went live on {where}: recording started automatically")
            alerts.notify(f"{c['name']} went live on {where}, recording started.")
            break
    except NotLive:
        pass
    except Exception as e:  # noqa: BLE001
        creators.log(c["id"], f"Auto-record check failed: {str(e)[-150:]}")


def watch_loop():
    """Every two minutes, start recording any creator set to auto-record who has gone live. Covers every user,
    several at a time, so the round stays short however many accounts there are."""
    from concurrent.futures import ThreadPoolExecutor
    time.sleep(20)
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="watch")
    while True:
        t0 = time.time()
        try:
            list(pool.map(_watch_user, auth.user_ids()))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(15, 120 - (time.time() - t0)))


def _resume_recordings(paused):
    """Recordings that were running when the server stopped: start recording the same channel again, if the
    stream is still on. Tried for a few minutes, since the server may come back a little before the network."""
    time.sleep(10)
    for _ in range(6):
        left = []
        for uid, cid, url, mode in paused:
            with ctx.as_user(uid):
                try:
                    c = creators.get(cid)
                    if not c or active_for(cid):
                        continue
                    resolve(normalize(url))      # raises NotLive if the stream has ended: no empty recording is made
                    start_session(c, url, mode)
                    creators.log(cid, "Recording picked up again after a server update")
                except NotLive:
                    continue                  # the stream ended while the server was away
                except Exception:  # noqa: BLE001 - try again shortly
                    left.append((uid, cid, url, mode))
        if not left:
            return
        paused = left
        time.sleep(30)


def start():
    picks.after_restart()
    paused = []
    for j in jobs.all_jobs():
        if j.get("recover"):
            with ctx.as_user(j["user"]):
                recover(j)
        if j.get("was_live") and j.get("creator"):
            paused.append((j["user"], j["creator"], j["source"]["url"], j.get("mode") or "auto"))
            j.pop("was_live", None)
            jobs.save(j)
    if paused:
        threading.Thread(target=_resume_recordings, args=(paused,), daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
