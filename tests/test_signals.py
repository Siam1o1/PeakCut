import os, sys, tempfile
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

print("ALL PASSED" if not fails else f"{len(fails)} failed"); sys.exit(1 if fails else 0)
