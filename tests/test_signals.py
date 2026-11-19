import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-signals-"); os.environ["DATABASE_URL"] = ""
os.environ["PEAKCUT_FETCH_MODEL"] = "0"
sys.path.insert(0, ".")
import numpy as np
from studio import signals
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

calm = [10.0, 11.0, 9.0, 10.0, 10.5, 9.5] * 50
check("a value at the median scores 0", round(signals.robust_z(calm, 10.0), 2), 0.0)
check("a big jump scores high", signals.robust_z(calm, 20.0) > 5)
check("a flat history does not divide by zero", np.isfinite(signals.robust_z([5.0] * 300, 6.0)))
check("one wild old value barely moves the baseline",
      abs(signals.robust_z(calm + [1000.0], 20.0) - signals.robust_z(calm, 20.0)) < 0.5)
s = calm * 4 + [30.0] * 5
check("series_z looks at the last seconds against the 15 minutes before", signals.series_z(s) > 5)
check("too little history and no seed: 0", signals.series_z([1.0] * 20), 0.0)
check("too little history with a seed uses the seed", signals.series_z([10.0] * 20 + [30.0] * 5,
      seed={"median": 10.0, "mad": 1.0}) > 5)
check("chatters per 1,000 viewers", signals.per_viewer(5, 2500), 2.0)
check("viewers unknown: the count itself", signals.per_viewer(5, None), 5.0)
top = signals.HourlyTop(2)
check("the first N in an hour go", (top.admit(50, 0), top.admit(40, 10)), (True, True))
check("a weaker one when the hour is full does not", top.admit(30, 20), False)
check("a stronger one replaces the weakest", (top.admit(60, 30), sorted(s for _, s in top.sent)), (True, [50, 60]))
check("an hour later there is room again", top.admit(10, 3700), True)

# ---- settings
from studio import creators
c = creators._clean({"sig_baseline": False, "jump_z": "5"})
check("the setting is saved as on/off and a number", (c["sig_baseline"], c["jump_z"]), (False, 5.0))
check("jump_z is held between 2 and 8", (creators._clean({"jump_z": 1})["jump_z"], creators._clean({"jump_z": 99})["jump_z"]), (2.0, 8.0))
check("a jump_z that is not a number is left out", "jump_z" in creators._clean({"jump_z": float("nan")}), False)
check("new creators start with the baseline on", (creators.DEFAULTS["sig_baseline"], creators.DEFAULTS["jump_z"]), (True, 3.5))

# ---- a live session on a made-up job
from studio import live
def session(**settings):
    d = tempfile.mkdtemp(prefix="pc-signals-live-")
    st = dict(creators.DEFAULTS, sig_baseline=True, jump_z=3.5, live_per_hour=2); st.update(settings)
    return live.Session({"title": "t", "uploader": "u", "dir": d, "creator": "", "ai": {}, "clips": [], "points": [],
                         "source": {"url": "", "name": "t"}}, st)
rng = np.random.default_rng(1)
calm_db = list(-30 + rng.normal(0, 1.0, 900))
se = session()
se.db = calm_db + [-12.0] * 5
se._watch_spikes()
check("a loud jump is a spike with 'loudness' in why", bool(se.spike) and "loudness" in se.spike["why"], True)
check("the spike carries its z", bool(se.spike) and se.spike.get("z", 0) >= 3.5, True)
check("the card numbers are filled", se.base.get("loud_z", 0) >= 3.5 and se.base.get("jump_z") == 3.5, True)
se = session(); se.db = calm_db + [-30.0] * 5; se._watch_spikes()
check("no jump, no spike", se.spike, None)
se = session(); se.db = calm_db[:30]; se._watch_spikes()
check("under a minute: still learning", se.base.get("warming"), True)
se = session(); se.db = calm_db[:70]; se._watch_spikes()
check("a minute in the baseline starts to work", "warming" not in se.base, True)
# chat: many different people at once, against a quiet chat
se = session(); se.db = calm_db + [-30.0] * 5
se.chat_on = True
se.chat_users = {t: {f"p{t}"} for t in range(0, 905, 2)}
for t in range(900, 905): se.chat_users[t] = {f"x{t}_{i}" for i in range(8)}
se._watch_spikes()
check("a crowd of new chatters is a spike", bool(se.spike) and "chat" in se.spike["why"], True)
# the old rule when the setting is off
se = session(sig_baseline=False)
se.db = [-30.0] * 200 + [-30 + 20 * np.log10(1.8)] * 5
se._watch_spikes()
check("old rule: a 60% jump triggers", bool(se.spike) and "%" in se.spike["why"], True)
se = session(sig_baseline=False)
se.db = [-30.0 + (i % 2) * 0.1 for i in range(200)] + [-29.5] * 5
se._watch_spikes()
check("old rule: a small rise on a quiet stream does not", se.spike, None)
# a chat spike for the clip judge
se = session(); se.samples = 16000 * 905
se.chat_on = True; se.chat_total = 500
se.chat_users = {t: {f"p{t}"} for t in range(0, 905, 2)}
for t in range(890, 905): se.chat_users[t] = {f"x{t}_{i}" for i in range(8)}
check("_chat_spike uses the baseline", se._chat_spike(890, 898), True)
se2 = session(); se2.samples = 16000 * 905
check("_chat_spike without chat is False", se2._chat_spike(890, 898), False)
se = session(live_per_hour=1)
check("the strongest-N rule is built from live_per_hour", se.top.n, 1)

# the strongest moments of the hour: a weaker jump is passed over once the hour is full
se = session(live_per_hour=1)
se.words = [{"w": "word", "s": float(i), "e": i + 0.5} for i in range(40)]
se.t_done = 40.0
se.top.admit(9.0, __import__("time").time())
se._ai_check((0.0, 40.0), "Jump: loudness z 4.0", 4.0)
check("a weaker jump is passed over when the hour is full", se.verdicts[-1]["result"], "Passed over: stronger moments this hour")

# ---- fix round 1
import time as _time
def fill_chat(se, rate, secs, burst=None, seed=3):
    """Chat as the reader thread adds it, through the real trimming path, one second at a time."""
    r = np.random.default_rng(seed)
    for sec in range(secs):
        k = int(r.poisson(rate))
        if burst and sec >= secs - 5:
            k = burst
        for i in range(k):
            se.chat_users.setdefault(sec, set()).add(f"u{sec}_{i}")
            se.chat_kept = live._trim_names(se.chat_users, sec, se.chat_kept)
    se.chat_on = True
check("chat names are kept for the whole 15-minute baseline", live.CHAT_KEEP >= 960, True)
se = session(); se.db = [-30.0] * 1000 + [-30.0] * 5
fill_chat(se, 0.8, 1005, burst=3)
se.samples = 1005 * 16000
z = se._chat_z(1004)
check("ordinary chat plus a small burst is a moderate z, not 50", 0 < z < 3.5, True)
check("...and no chat jump is raised", (se._watch_spikes(), se.spike)[1], None)
check("trimming keeps at least 15 minutes of names", len([t for t in se.chat_users if t >= 1004 - 930]) > 400, True)
se = session(); fill_chat(se, 0.3, 1005, burst=3); se.samples = 1005 * 16000
z = se._chat_z(1004)
check("sparse chat with a burst gives a finite, sensible z", 2 < z < 20, True)
se = session(); se.chat_on = True
for sec in range(1000, 1005): se.chat_users[sec] = {f"b{sec}_{i}" for i in range(3)}
se.chat_total = 15
check("a burst on an empty chat is judged by the floor (3/s over 0.5/s floor)", round(se._chat_z(1004), 1), round(3 / (1.4826 * 0.5), 1))
check("a thin chat (1/s) over empty does not count", (lambda s: (s.chat_users.update({t: {"a"} for t in range(1000, 1005)}), s._chat_z(1004))[1])(session()), 0.0)
# each second is scaled by the viewer count of its own time
se = session(); se.chat_on = True
se.chat_users = {t: {f"p{t}_{i}" for i in range(2)} for t in range(0, 100)}
se.viewer_log = [(0, 1000), (50, 2000)]
ser = se._chat_series(99, 100)[0]
check("a second before the count changed uses the old count", (ser[10], ser[90]), (2.0, 1.0))
# a due must-clip bypasses the hourly limit; a pick session is limited too
se = session(live_per_hour=1); se.job["clips"] = [{"score": 0, "made_at": 0}]
se.words = [{"w": "word", "s": float(i), "e": i + 0.5} for i in range(40)]; se.t_done = 40.0
se.top.admit(9.0, _time.time())
se.settings["must_clip_min"] = 1
se._ai_check((0.0, 40.0), "Jump: loudness z 4.0", 4.0)
check("a due must-clip is not passed over", se.verdicts[-1]["result"] != "Passed over: stronger moments this hour", True)
se = session(live_per_hour=1); se.pick = True; se.job["mode"] = "pick"
se.words = [{"w": "word", "s": float(i), "e": i + 0.5} for i in range(40)]; se.t_done = 40.0
se.top.admit(9.0, _time.time())
se._ai_check((0.0, 40.0), "Jump: loudness z 4.0", 4.0)
check("pick mode: a second, weaker jump in the hour is passed over", se.verdicts[-1]["result"], "Passed over: stronger moments this hour")

# a burst inside s..e that has faded by e+2 still counts
se = session(); se.samples = 1005 * 16000; fill_chat(se, 0.8, 1005, seed=5)
for t in range(1000, 1005): se.chat_users.pop(t, None)
se.chat_users.update({t: {f"f{t}_{i}" for i in range(8)} for t in range(990, 997)})
check("a burst inside s..e that faded by e+2 still counts", se._chat_spike(990, 996), True)
check("...and quiet chat over the same stretch does not", session()._chat_spike(990, 996), False)
# a count that goes missing after being known does not change the units
se = session(); fill_chat(se, 0.8, 1005, burst=6); se.samples = 1005 * 16000
se.viewer_log = [(0, 2000)]; se.viewers = 2000
z1 = se._chat_z(1004)
se.viewers = None
check("a viewer count that becomes None keeps z the same", (z1 > 0, se._chat_z(1004) == z1), (True, True))
se = session(); se.viewers = 7
def _none(*a):
    se.stop_flag.set()          # one round only
    return {"viewers": None}
_old = live.profiles.live_shared
live.profiles.live_shared = _none
try:
    se._watch_viewers()
finally:
    live.profiles.live_shared = _old
check("a poll that finds nothing does not clear a known count", se.viewers, 7)

# ---- task 2: what chat says
tagged = "@badge-info=;badges=;display-name=Ann;user-id=11 :ann!ann@ann.tmi.twitch.tv PRIVMSG #x :KEKW that was insane"
p = signals.parse_irc(tagged)
check("a tagged chat line is read", (p["kind"], p["user"], p["text"]), ("msg", "11", "KEKW that was insane"))
check("a raid notice is a notice", signals.parse_irc("@msg-id=raid;user-id=9 :tmi.twitch.tv USERNOTICE #x")["notice"], "raid")
check("an untagged line still works", signals.parse_irc(":bob!bob@bob.tmi.twitch.tv PRIVMSG #x :hi")["user"], "bob")
check("bots are known", (signals.is_bot("Nightbot"), signals.is_bot("my_cool_bot"), signals.is_bot("robert")), (True, True, False))
msgs = [(f"u{i}", "KEKW") for i in range(8)] + [("u8", "lol nice"), ("u9", "what")]
w = signals.analyze_window(msgs)
check("convergence is the share of people sending the top word", (w["top"], round(w["convergence"], 2), w["people"]), ("kekw", 0.8, 10))
check("reaction types count people", (w["types"]["laugh"], w["types"]["shock"]), (9, 1))
clip = [(f"u{i}", "clip it") for i in range(3)] + [("u0", "clip it")]
check("clip requests count distinct people", signals.analyze_window(clip)["clip_people"], 3)
pasta = [("a", "this is a very long copied message here again"), ("b", "this is a very long copied message here again")]
check("copypasta from fewer than 3 people is ignored", signals.analyze_window(pasta)["people"], 0)
check("bots are ignored", signals.analyze_window([("Nightbot", "KEKW")])["people"], 0)
check("word lists from settings", signals.word_lists({"words_laugh": "haha, xd"})["laugh"], ["haha", "xd"])
check("empty word lists fall back to the defaults", signals.word_lists({})["hype"], signals.DEFAULT_WORDS["hype"])
check("new settings have defaults", (creators.DEFAULTS["sig_chat_words"], creators.DEFAULTS["chat_window"]), (True, 8))
check("chat_window is held between 5 and 10", (creators._clean({"chat_window": 1})["chat_window"], creators._clean({"chat_window": 99})["chat_window"]), (5, 10))
check("a word list is saved as text", creators._clean({"words_clip": " a, b "})["words_clip"], "a, b")

def chat_line(user, text, second=None):
    return f"@user-id={user};display-name=N{user} :n{user}!n{user}@n{user}.tmi.twitch.tv PRIVMSG #x :{text}"
se = session(); se.chat_on = True
se.samples = 10 * 16000
for i in range(6): se.take_chat_line(chat_line(100 + i, "KEKW"))
check("tagged chat feeds the people count and the words", (len(se.chat_users[10]), len(se.chat_msgs[10])), (6, 6))
se.samples = 200 * 16000
se.take_chat_line(chat_line(1, "hi"))
check("chat text is kept for 120 seconds only", sorted(se.chat_msgs), [200])
se = session(); se.samples = 50 * 16000
se.take_chat_line(":Nightbot!nightbot@x.tmi.twitch.tv PRIVMSG #x :Welcome")
check("a bot never enters the people or the words", (se.chat_users, se.chat_msgs), ({}, {}))
se.take_chat_line("@msg-id=raid;user-id=9 :tmi.twitch.tv USERNOTICE #x")
check("a raid pauses the words for 30 seconds", se.raid_until, 80)
se.take_chat_line(chat_line(1, "KEKW"))
check("raid messages are not kept as reactions", se.chat_msgs, {})
se.samples = 90 * 16000
se.take_chat_line(chat_line(1, "KEKW"))
check("after the raid they are kept again", list(se.chat_msgs), [90])
se.take_chat_line("@msg-id=sub;user-id=9 :tmi.twitch.tv USERNOTICE #x")
check("other notices are ignored", se.raid_until, 80)

def talk(se, second, n, text="KEKW"):
    se.chat_msgs[second] = [(f"p{second}_{i}", text) for i in range(n)]
cw = session(); cw.db = calm_db[:100]
for s_ in range(92, 100): talk(cw, s_, 1)
r = cw.chat_window(92, 99)
check("chat_window analyses that stretch", (r["people"], r["top"]), (8, "kekw"))
check("a stretch with nothing in it is empty", cw.chat_window(10, 20)["people"], 0)
cw._watch_spikes()
check("a crowd saying one word is a spike", bool(cw.spike) and "KEKW" in cw.spike["why"] and "laugh" in cw.spike["why"], True)
check("the spike keeps the window numbers", cw.spike["chat"]["people"], 8)
check("the card shows the latest convergence", cw.base.get("conv"), 1.0)
cw = session(sig_chat_words=False); cw.db = calm_db[:100]
for s_ in range(92, 100): talk(cw, s_, 1)
cw._watch_spikes()
check("the setting switches it off", (cw.spike, cw.conv_hist), (None, []))
cw = session(); cw.db = calm_db[:100]
for s_ in range(92, 100): cw.chat_msgs[s_] = [(f"p{s_}_{i}", f"thing{s_}x word{s_}y") for i in range(1)]
cw._watch_spikes()
check("scattered talk is not a spike", cw.spike, None)
cw = session(); cw.db = calm_db[:100]
for s_ in range(92, 100): cw.chat_msgs[s_] = [(f"c{s_}", "clip it")] if s_ < 95 else []
cw._watch_spikes()
check("three people asking for a clip is a spike", bool(cw.spike) and "clip" in cw.spike["why"], True)
# the rolling history of convergence
cw = session(); cw.db = calm_db[:100]
for s_ in range(92, 100): talk(cw, s_, 1)
cw._watch_spikes()
check("each analysed window adds to the convergence history", [(t, round(c, 1)) for t, c in cw.conv_hist], [(100, 1.0)])
cw.conv_hist.append((150, 0.5))
cw.db = calm_db[:100] + [-30.0] * 1000
for s_ in range(1085, 1100): talk(cw, s_, 1)
cw._watch_spikes()
check("the history keeps only the last 15 minutes", all(t >= len(cw.db) - 900 for t, _ in cw.conv_hist) and len(cw.conv_hist) >= 1, True)

# ---- task 2 fix round 1
for label, txt in (("a lone question mark", "?"), ("emoji only", "😂😂"), ("Russian", "привет"), ("an empty message", "")):
    try:
        r = signals.analyze_window([(f"u{i}", txt) for i in range(5)])
        ok = (r["convergence"] <= 1.0 and isinstance(r["top"], str))
    except Exception as e:  # noqa: BLE001
        ok = False
    check(f"{label} does not crash the analysis", ok, True)
check("nothing readable gives no convergence", signals.analyze_window([(f"u{i}", "?") for i in range(5)])["convergence"], 0.0)
r = signals.analyze_window([(f"u{i}", "😂😂 😂") for i in range(5)])
check("emoji are words: top 😂, everyone sent it", (r["top"], r["convergence"]), ("😂", 1.0))
check("a custom list with emoji and Korean matches",
      signals.analyze_window([("a", "ㅋㅋ"), ("b", "😂"), ("c", "hi")], {"laugh": ["ㅋㅋ", "😂"]})["types"]["laugh"], 2)
check("non-Latin words converge", signals.analyze_window([(f"u{i}", "Привет!") for i in range(4)])["top"], "привет")
check("a phrase with an apostrophe matches in any case",
      signals.analyze_window([("a", "LET'S GO!!")], {"hype": ["let's go"]})["types"]["hype"], 1)
check("an emote like D: still matches", signals.analyze_window([("a", "D:")], {"shock": ["d:"]})["types"]["shock"], 1)
check("a phrase does not match across two messages of one person",
      signals.analyze_window([("a", "let"), ("a", "go")], {"hype": ["let go"]})["types"]["hype"], 0)
long1 = "this is my own long genuine message about the game today"
check("a unique long message from one person stays", signals.analyze_window([("a", long1), ("b", "hi")])["people"], 2)
check("a long message copied by 3 people stays", signals.analyze_window([(u, long1) for u in "abc"])["people"], 3)
check("the same long text twice by one person is dropped", signals.analyze_window([("a", long1), ("a", long1)])["people"], 0)
w = signals.analyze_window([(f"u{i}", "KEKW") for i in range(4)] + [("u5", "wtf"), ("u6", "wtf")])
check("the kind shown is the top word's own type", w["top_kind"], "laugh")
w = signals.analyze_window([(f"u{i}", "ok") for i in range(4)] + [("u5", "lol"), ("u6", "lol")])
check("a top word with no type shows the most common type", w["top_kind"], "laugh")
tm = signals.parse_irc("@user-id=3 :bob!bob@bob.tmi.twitch.tv PRIVMSG #x :did you see USERNOTICE  : that")
check("a message that mentions USERNOTICE stays a message", tm["kind"], "msg")
check("a notice is told from the command position", signals.parse_irc(":tmi.twitch.tv USERNOTICE #x")["kind"], "notice")
se = session(); se.chat_on = True; se.samples = 100 * 16000
se.take_chat_line("@msg-id=raid;user-id=9 :tmi.twitch.tv USERNOTICE #x")
se.take_chat_line(chat_line(1, "KEKW"))
check("raid messages stay out of the chatter counts too", (se.chat_users, se.chat_msgs), ({}, {}))
se.samples = 140 * 16000
se.take_chat_line(chat_line(1, "KEKW"))
check("after a raid chatters count again", len(se.chat_users[140]), 1)
# the word shown in why is capped
cw = session(); cw.db = calm_db[:100]
for s_ in range(92, 100): cw.chat_msgs[s_] = [(f"p{s_}_{i}", "x" * 80) for i in range(1)]
cw._watch_spikes()
check("a very long word is cut in the spike text", bool(cw.spike) and len(cw.spike["why"]) < 80, True)
# a slow pass reads every window it missed, in order
cw = session(); cw.db = calm_db[:100]; cw.words_read = 76
cw._watch_spikes()
check("missed windows are all read in order", [t for t, _ in cw.conv_hist], [84, 92, 100])

# ---- timing: chat lag, and where a clip starts and ends
loud = np.zeros(600); chat = np.zeros(600)
for p_ in (100, 250, 400): loud[p_:p_+3] = 10; chat[p_+7:p_+10] = 10
check("chat that follows loudness by 7 s has a lag of 7", signals.measure_lag(loud, chat), 7)
check("no relation: the default", signals.measure_lag(np.random.default_rng(1).random(600), np.zeros(600)), 5)
lines = [{"s": 0, "e": 4, "text": "a"}, {"s": 5, "e": 9, "text": "b"}, {"s": 10, "e": 14, "text": "c"},
         {"s": 15, "e": 19, "text": "d"}, {"s": 20, "e": 24, "text": "e"}, {"s": 25, "e": 29, "text": "f."}]
s_, e_ = signals.clip_edges(16, lines, min_len=10, max_len=30, calm_after=22)
check("starts at a sentence start at least 3 s before the peak", s_, 10)
check("ends at the sentence end after chat calms down", e_, 24)
s_, e_ = signals.clip_edges(16, lines, min_len=10, max_len=12, calm_after=40)
check("never longer than max_len", e_ - s_ <= 12)
s_, e_ = signals.clip_edges(16, lines, min_len=20, max_len=30, calm_after=None)
check("never shorter than min_len", e_ - s_ >= 20)
check("the setting is on by default", creators.DEFAULTS["sig_timing"], True)
check("the setting is kept as a bool", creators._clean({"sig_timing": 0})["sig_timing"], False)
tw = [{"s": float(i), "e": i + 0.8, "w": ("go." if i % 5 == 4 else "w")} for i in range(300)]
# the lag is re-measured every 5 minutes
tl = session(); tl.db = [-30.0] * 700
for p_ in (100, 250, 400, 550): tl.db[p_:p_+3] = [-10.0] * 3
tl.chat_on = True
for t_ in range(700): tl.chat_users[t_] = {f"q{t_}"} if t_ % 2 else set()
for p_ in (100, 250, 400, 550):
    for t_ in range(p_ + 7, p_ + 10): tl.chat_users[t_] = {f"y{t_}_{i}" for i in range(6)}
tl._watch_lag(700)
check("the session measures the chat lag", tl.lag, 7)
tl.lag_at = 700; tl.db = [-30.0] * 800; tl._watch_lag(800)
check("not measured again within 5 minutes", tl.lag, 7)
# a chat jump is shifted back by the lag; a loudness jump is not
sh = session(); sh.lag = 6; sh.db = calm_db + [-30.0] * 5
sh.chat_on = True
sh.chat_users = {t: {f"p{t}"} for t in range(0, 905, 2)}
for t in range(900, 905): sh.chat_users[t] = {f"x{t}_{i}" for i in range(8)}
sh._watch_spikes()
check("a chat-born spike is shifted back by the lag", sh.spike and sh.spike["t"], 902 - 6)
sh = session(); sh.lag = 6; sh.db = calm_db + [-12.0] * 5; sh._watch_spikes()
check("a loudness spike is not shifted", sh.spike and sh.spike["t"], 902)
sh = session(sig_timing=False); sh.lag = 6; sh.db = calm_db + [-30.0] * 5; sh.chat_on = True
sh.chat_users = {t: {f"p{t}"} for t in range(0, 905, 2)}
for t in range(900, 905): sh.chat_users[t] = {f"x{t}_{i}" for i in range(8)}
sh._watch_spikes()
check("timing off: no shift", sh.spike and sh.spike["t"], 902)
sh = session(); sh.lag = 6; sh.db = calm_db[:100]
for s_ in range(92, 100): talk(sh, s_, 1)
sh._watch_spikes()
check("a chat-words spike is shifted back too", sh.spike and sh.spike["t"], 97 - 6)
# edges: a sentence start before the moment, and the first calm reading after it
ed = session(min_len=10, max_len=40); ed.words = tw
ed.conv_hist = [(100 + 8 * i, 0.9 if i < 3 else 0.1) for i in range(6)] + [(60, 0.1), (68, 0.1), (76, 0.1), (84, 0.1)]
ed.conv_hist.sort()
check("calm_after is the first reading under z 1 after the moment", ed._calm_after(101), 124)
check("calm_after is None without readings", session()._calm_after(50), None)
a_, b_ = ed._edges(101)
check("edges start at a sentence start before the moment", (a_ <= 98, b_ >= 124), (True, True))
ed2 = session(sig_timing=False); ed2.words = tw
check("timing off: the old fixed window", ed2._edges(101), (71.0, 121.0))
# the no-AI fallback in pick mode lists the edges
fb = session(min_len=10, max_len=40); fb.pick = True; fb.words = tw; fb.t_done = 250
fb.job["points"] = []
import studio.brain as _brain
_old = _brain.judge_transcript
def _boom(*a, **k): raise RuntimeError("no ai")
_brain.judge_transcript = _boom
fb._ai_check((70, 130), "Jump: x", 5.0, peak=100)
_brain.judge_transcript = _old
check("the fallback lists a point from the edges", len(fb.job["points"]) == 1 and fb.job["points"][0]["end"] - fb.job["points"][0]["start"] >= 10, True)

# ---- timing fixes: the moment is always inside; calm means after the reaction; wait until settled
s_, e_ = signals.clip_edges(100, [{"s": 30, "e": 33}], 15, 60)
check("a setup line far back still leaves the moment inside", (s_ <= 100 <= e_, e_ - s_ <= 60), (True, True))
s_, e_ = signals.clip_edges(1, [], 15, 60)
check("at the start of a stream the clip is still min_len long", (s_, e_ - s_ >= 15), (0.0, True))
s_, e_ = signals.clip_edges(16, lines, min_len=10, max_len=12, calm_after=40)
check("a far calm point cannot push the moment out", s_ <= 16 <= e_, True)
cd = session(); cd.lag = 5; cd.settings["chat_window"] = 8
cd.conv_hist = [(60, .1), (68, .1), (76, .1), (84, .1), (92, .1), (100, .1), (104, .1), (112, .9), (120, .9), (128, .1)]
check("calm_after skips a calm reading from before the reaction", cd._calm_after(101), 128)
cd.conv_hist = [(60, .1), (68, .1), (76, .1), (84, .1), (92, .1), (100, .1), (104, .1), (112, .9)]
check("calm_after waits while chat is still excited", cd._calm_after(101), None)
cd.conv_hist = [(60, .1), (68, .1), (76, .1), (84, .1), (92, .1), (100, .1), (108, .1), (116, .1)]
check("never excited: the first window that starts after the reaction", cd._calm_after(101), 116)
lg = session(); lg.lag = 9; lg.db = [-30.0] * 700; lg.chat_on = True
lg.chat_users = {t_: {f"q{t_}"} for t_ in range(700)}
lg._watch_lag(700)
check("no pattern keeps the lag measured before", lg.lag, 9)
# firing waits until the clip is settled
wt = session(min_len=10, max_len=40); wt.words = tw; wt.chat_on = True; wt.lag = 5
wt.conv_hist = [(60, .1), (68, .1), (76, .1), (84, .1), (92, .1), (100, .1), (108, .9), (116, .9)]
wt.t_done = 130
check("not settled while chat has not calmed", wt._settled(101)[0], False)
wt.conv_hist.append((124, .1)); wt.t_done = 129
check("not settled before the transcript reaches the end", wt._settled(101)[0], False)
wt.t_done = 200
check("settled once chat calmed and the transcript is there", wt._settled(101)[0], True)
wt.conv_hist = wt.conv_hist[:-1]; wt.t_done = 200
check("or when the clip is as long as it may be", wt._settled(101)[0], True)
nt = session(sig_timing=False); nt.t_done = 121
check("timing off: the old t + 20 trigger", (nt._settled(101)[0], session(sig_timing=False)._settled(101)[0]), (True, False))
vp = session(min_len=10, max_len=40); vp.pick = True; vp.words = tw; vp.chat_on = True; vp.lag = 5
vp.conv_hist = [(60, .1), (68, .1), (76, .1), (84, .1), (92, .1), (100, .1), (108, .9), (116, .9)]
vp.t_done = 130; vp.job["points"] = []
vp._viewer_burst({"t": 101, "people": 3})
check("a viewer burst waits for settled edges", (len(vp.job["points"]), len(vp.viewer_points)), (0, 1))
vp.ended = True; vp._flush_viewer_points()
check("and is listed anyway once the stream ended", len(vp.job["points"]), 1)

# ---- nothing waiting is lost on Stop, and a merged viewer point is marked
vs = session(min_len=10, max_len=40); vs.pick = True; vs.words = tw; vs.chat_on = True; vs.lag = 5
vs.conv_hist = [(60, .1), (68, .1), (76, .1), (84, .1), (92, .1), (100, .1), (108, .9), (116, .9)]
vs.t_done = 130; vs.job["points"] = []
vs._viewer_burst({"t": 101, "people": 3})
vs.user_stopped = True; vs._flush_viewer_points()
check("Stop lists a waiting viewer point", (len(vs.job["points"]), vs.viewer_points), (1, []))
vm = session(min_len=10, max_len=40); vm.pick = True; vm.words = tw; vm.ended = True
vm.job["points"] = [{"id": "pt1", "t": 98.0, "from": "jump", "start": 80, "end": 120}]
vm._viewer_burst({"t": 101, "people": 4})
check("a viewer point merged into an existing one marks it", (len(vm.job["points"]), vm.job["points"][0]["viewers"], vm.job["points"][0]["from"]), (1, 4, "jump"))

# ---- one fused score, and the log
# noisy-OR: score = 100 * (1 - prod((1 - v) ** (w / max_w))); default max weight is 2 (viewer clips)
check("fuse: one full signal is enough (loud z 6 -> v 1 -> 0 ** .5 = 0 -> 100)", signals.fuse({"loud": 6}), 100)
check("fuse: loud z 3 -> v .5, w 1 of 2: .5 ** .5 = .7071 -> 29", signals.fuse({"loud": 3}), 29)
check("fuse: loud z 3 and chat z 3 -> .7071 * .7071 = .5 -> 50 (two half signals beat one)", signals.fuse({"loud": 3, "chat": 3}), 50)
check("fuse: two half-strength signals score more than either alone", signals.fuse({"loud": 3, "chat": 3}) > signals.fuse({"loud": 3}), True)
check("fuse: nothing known is 0", signals.fuse({}), 0)
check("fuse: all weights 0 is 0", signals.fuse({"loud": 6}, {k: 0 for k in signals.DEFAULT_WEIGHTS}), 0)
check("fuse: more of everything is 100", signals.fuse({"loud": 9, "chat": 9, "convergence": 1, "reactions": 9, "clip_requests": 9, "viewer_clips": 9}), 100)
check("fuse: weights change the result (loud weight 2 = the top: .5 ** 1 -> 50)", signals.fuse({"loud": 3}, {"loud": 2}), 50)
check("fuse: a weight of 0 leaves a signal out", signals.fuse({"loud": 6, "chat": 3}, {"loud": 0}), round(100 * (1 - 0.5 ** 0.5)))
check("fuse: a 3-viewer burst is high by itself", signals.fuse({"viewer_clips": 3}) >= 80, True)
check("fuse: one viewer clip: v 1/3, w 2 of 2: 2/3 -> 33", signals.fuse({"viewer_clips": 1}), 33)
check("fuse: viewer clips count for more than clip requests", signals.fuse({"viewer_clips": 1}) > signals.fuse({"clip_requests": 1}), True)
lg = signals.Log(os.path.join(os.environ["PEAKCUT_DATA"], "x", "signals.jsonl"))
cid = lg.add({"job": "j1", "values": {"loud": 4.2}, "score": 55, "sent": False, "kind": "loud"})
lg.update(cid, sent=True, verdict="Clipped: both judges agreed")
lg.update(cid, downloaded=True)
got = lg.read()[cid]
check("log: add, update, read folds in order", (got["score"], got["sent"], got["downloaded"], got["values"]["loud"]), (55, True, True, 4.2))
check("log: one line per call", len(open(lg.path, encoding="utf-8").read().splitlines()), 3)
try:
    lg.add({"job": "j1", "values": {}, "score": 1, "sent": False, "top": "x" * 41}); refused = False
except ValueError:
    refused = True
check("log: a text longer than 40 characters is refused", refused, True)
try:
    lg.update(cid, verdict="y" * 41); refused = False
except ValueError:
    refused = True
check("log: an update with long text is refused too", refused, True)
check("log: a refused line is not written", len(open(lg.path, encoding="utf-8").read().splitlines()), 3)
check("log_for: no creator, no log", signals.log_for(""), None)
from studio import ctx
ctx._uid.set("0123456789abcdef0123456789abcdef")
cr = creators.create({"name": "LogTest"})
check("log_for: on by default", isinstance(signals.log_for(cr["id"]), signals.Log), True)
check("log_for: the file sits in the creator's folder", str(signals.log_for(cr["id"]).path).endswith("signals.jsonl") and cr["id"] in str(signals.log_for(cr["id"]).path), True)
creators.update(cr["id"], {"sig_log": False})
check("log_for: off when the setting is off", signals.log_for(cr["id"]), None)
check("sig_weights keeps known keys and clips to 0-10", creators._clean({"sig_weights": {"loud": "3", "chat": 99, "nope": 1, "reactions": "x"}})["sig_weights"], {"loud": 3.0, "chat": 10.0})
check("sig_log is a yes or no", creators._clean({"sig_log": 0})["sig_log"], False)
check("the log is on by default", creators.DEFAULTS["sig_log"], True)

# ---- the session scores, logs and carries the candidate id
lp = os.path.join(os.environ["PEAKCUT_DATA"], "sess", "signals.jsonl")
import contextlib
@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)
def logged(**st):
    x = session(**st); x.log = signals.Log(lp + str(id(x))); x._log = lambda x=x: x.log
    return x
lx = logged(live_per_hour=1)
lx.zs = {"loud": 6.0, "chat": 0.0}
sp = {"t": 100.0, "why": "loudness z 6.0", "z": 6.0, "sig": {"loud": 6.0, "chat": 0.0}}
cand = lx._candidate(sp)
rec = lx.log.read()[cand]
check("a candidate is logged with its fused score", (rec["score"], rec["values"]["loud"], rec["sent"]), (signals.fuse({"loud": 6.0}), 6.0, False))
check("the candidate id is kept on the spike", sp["cand"], cand)
lx._verdict({"t": 1, "cand": cand}, "Passed over: stronger moments this hour")
check("a passed-over candidate is marked not sent, with the reason", (lx.log.read()[cand]["sent"], lx.log.read()[cand]["result"]), (False, "Passed over: stronger moments this hour"))
lx._verdict({"t": 1, "cand": cand, "transcript": 88, "reaction": 70}, "Clipped: both judges agreed")
r2 = lx.log.read()[cand]
check("the judges' verdict is added", (r2["sent"], r2["words"], r2["reaction"]), (True, 88, 70))
lx._verdict({"t": 1, "cand": cand}, "AI error: " + "e" * 80)
check("an error is logged as a fixed label, never its text", lx.log.read()[cand]["result"], "ai error")
chatsp = {"t": 50.0, "why": "Chat", "z": 5.0, "chat": {"people": 9, "convergence": 0.7, "top": "x" * 60, "top_kind": "hype",
          "types": {"laugh": 1, "shock": 1, "hype": 2, "clip": 4, "negative": 3}, "clip_people": 4}}
cc = lx._candidate(chatsp)
rc = lx.log.read()[cc]
check("chat window numbers are logged, never the word", (rc["values"]["convergence"], rc["values"]["clip_requests"], "x" * 60 in open(lx.log.path, encoding="utf-8").read()), (0.7, 4, False))
check("the kind label is short", rc.get("kind"), "hype")
check("reactions count laugh, shock and hype only (not negative, not clip)", rc["values"]["reactions"], 4)
lw = logged(sig_weights={"loud": 0, "chat": 0, "convergence": 0, "reactions": 0, "clip_requests": 0, "viewer_clips": 1})
vsp = {"t": 5.0, "why": "v", "z": 13, "viewer_clips": 3}
wc = lw._candidate(vsp)
check("the creator's weights are used", lw.log.read()[wc]["score"], 100)
lh = logged(); lh.pick = True; lh.words = []; lh.ended = True; lh.job["points"] = []
lh._viewer_burst({"t": 101, "people": 3})
check("a viewer point made in pick mode carries its candidate", bool(lh.job["points"]) and lh.job["points"][0].get("cand") in lh.log.read(), True)
lm = logged(); lm.pick = True; lm.ended = True
lm.job["points"] = [{"id": "pt1", "t": 98.0, "from": "jump", "start": 80, "end": 120}]
saved = []
with patched(live.jobs, "save", lambda j, _s=saved: _s.append(1)):
    lm._viewer_burst({"t": 101, "people": 4})
check("marking an existing point with viewers saves the job", len(saved) >= 1, True)
check("a merged viewer burst gives its id to a point that had none", lm.job["points"][0].get("cand") in lm.log.read(), True)
ln = logged(); ln.pick = True; ln.ended = True
ln.job["points"] = [{"id": "pt1", "t": 98.0, "from": "jump", "start": 80, "end": 120, "cand": "other1"}]
with patched(live.jobs, "save", lambda j: None):
    ln._viewer_burst({"t": 101, "people": 4})
vid = [k for k in ln.log.read() if k != "other1"][0]
check("or logs merged_into when the point already had one", (ln.job["points"][0]["cand"], ln.log.read()[vid].get("merged_into")), ("other1", "other1"))
# the rating of the best moment reaches the log (numbers only), and a point keeps its rating
rk = logged(min_len=10, max_len=40, min_score=99); rk.pick = True; rk.words = tw; rk.t_done = 250; rk.job["points"] = []
rc = rk._candidate({"t": 100.0, "why": "x", "z": 5.0, "sig": {"loud": 5.0}})
rated = {"start": 80.0, "end": 110.0, "score": 70, "reason": "r", "peak": None,
         "rating": {"hook": 7, "alone": 7, "emotion": 7, "payoff": 7, "share": 7, "why": "a line that is longer than forty characters for sure", "value": 70}}
_old = _brain.judge_transcript
_brain.judge_transcript = lambda *a, **k: ([dict(rated)], 5)
rk._ai_check((70, 130), "Jump: x", 5.0, peak=100, cand=rc)
_brain.judge_transcript = _old
check("the rating is logged without its text", rk.log.read()[rc].get("rating"), {"hook": 7, "alone": 7, "emotion": 7, "payoff": 7, "share": 7, "value": 70})

# an AI check dropped because another is running is logged
lb = logged(); bc = lb._candidate({"t": 5.0, "why": "x", "z": 5.0, "sig": {"loud": 5.0}})
lb.ai_busy = time.time()
lb._ask_ai((0, 10), "Jump: x", 50, cand=bc)
check("a candidate dropped while the AI is busy is logged", lb.log.read()[bc].get("result"), "AI busy")
# the real path: the creator's own log file, read back
cr2 = creators.create({"name": "RealLog"})
rl = session(); rl.job["creator"] = cr2["id"]
rid = rl._candidate({"t": 7.0, "why": "x", "z": 5.0, "sig": {"loud": 5.0}})
rl._verdict({"t": 1, "cand": rid, "transcript": 90}, "Clipped: both judges agreed")
fromfile = signals.log_for(cr2["id"]).read()
check("the real log path writes the creator's signals.jsonl", (rid in fromfile, fromfile[rid]["sent"], fromfile[rid]["words"],
      (creators.root(cr2["id"]) / "signals.jsonl").exists()), (True, True, 90, True))

# ---- outcomes reach the log
from studio import picks
lq = logged()
cid2 = lq._candidate({"t": 5.0, "why": "x", "z": 5.0, "sig": {"loud": 5.0}})
check("note() writes an update", (signals.note(lq.log, cid2, downloaded=True), lq.log.read()[cid2]["downloaded"]), (None, True))
check("note() with no candidate or no log does nothing", (signals.note(None, "a", x=1), signals.note(lq.log, "", x=1)), (None, None))

from studio import posting
seen = []
pp = {"creator": "c1", "cand": "abc", "views": None}
with patched(signals, "note_for", lambda cr, cand, **f: seen.append((cr, cand, f))):
    posting._set_views(pp, 12); posting._set_views(pp, 12); posting._set_views(pp, 30)
check("views are logged once per change", seen, [("c1", "abc", {"views": 12}), ("c1", "abc", {"views": 30})])

from studio import picks
seen.clear()
jb = {"id": "jx", "creator": "c9", "dir": tempfile.mkdtemp(), "points": [{"id": "pt1", "t": 100.0, "score": 0, "reason": "", "cand": "cc1",
      "hold": {"status": "ready", "file": "points/pt1.mp4", "from": 40.0, "to": 160.0, "error": None}}], "cuts": []}
with patched(signals, "note_for", lambda cr, cand, **f: seen.append((cr, cand, f))), \
        patched(live.jobs, "submit_live", lambda f: None), patched(live.jobs, "save", lambda j: None):
    picks.make_cut(jb, "pt1", 10, 30)
check("making a cut logs it as downloaded", seen, [("c9", "cc1", {"downloaded": True})])

# ---- viral rating
ten = {"hook": 10, "alone": 10, "emotion": 10, "payoff": 10, "share": 10}
check("rating: all tens is 100", signals.rating(ten), 100)
check("rating: all fives is 50", signals.rating({k: 5 for k in ten}), 50)
check("rating: the hook counts three times", signals.rating({**{k: 0 for k in ten}, "hook": 10}), 30)
check("rating: a missing sub-score gives None", signals.rating({"hook": 9, "alone": 9, "emotion": 9, "payoff": 9}), None)
check("rating: nothing at all gives None", signals.rating(None), None)
check("rating: values outside 0 to 10 are clamped", (signals.rating({k: 99 for k in ten}), signals.rating({k: -4 for k in ten})), (100, 0))
check("rating: text that is not a number gives None", signals.rating({**ten, "hook": "high"}), None)

# a point keeps the rating
with patched(live.jobs, "save", lambda j: None):
    pk = session(); pk.pick = True; pk.job["points"] = []
    pnt = picks.add_point(pk, {"start": 80.0, "end": 110.0, "peak": 100.0, "score": 70, "reason": "r", "rating": {"hook": 7, "value": 70}})
check("a point keeps the rating", pnt["rating"], {"hook": 7, "value": 70})

print("ALL PASSED" if not fails else f"{len(fails)} failed"); sys.exit(1 if fails else 0)
