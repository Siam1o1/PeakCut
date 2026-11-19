import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-twitch-"); os.environ["DATABASE_URL"] = ""
os.environ["PEAKCUT_FETCH_MODEL"] = "0"
for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_CLIENT_ID_FILE", "TWITCH_CLIENT_SECRET_FILE"):
    os.environ.pop(k, None)
sys.path.insert(0, ".")
from datetime import datetime, timezone
from studio import config, creators, live, twitch
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

class R:
    def __init__(self, data, code=200): self.data, self.status_code = data, code
    def json(self): return self.data
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError("http")

calls = {"post": 0, "get": []}
def post(url, data=None, timeout=None, **kw):
    calls["post"] += 1
    assert "id.twitch.tv/oauth2/token" in url and data["grant_type"] == "client_credentials" and timeout == 15
    return R({"access_token": "tok", "expires_in": 3600})
CLIPS = []
def get(url, params=None, headers=None, timeout=None, **kw):
    calls["get"].append((url, params, headers)); assert timeout == 15
    if url.endswith("/users"): return R({"data": [{"id": "42"}]})
    if url.endswith("/streams"): return R({"data": [{"started_at": "2026-11-19T10:00:00Z"}]})
    if url.endswith("/clips"): return R({"data": CLIPS})
    return R({}, 404)

h = twitch.Helix("cid", "sec", get=get, post=post)
check("user_id", h.user_id("x"), "42")
check("stream_start parses the time", h.stream_start("42"), datetime(2026, 11, 19, 10, tzinfo=timezone.utc).timestamp())
check("the token is fetched once and reused", (h.clips("42", 1.0), calls["post"]), ([], 1))
check("requests carry the id and bearer token", calls["get"][-1][2], {"Client-Id": "cid", "Authorization": "Bearer tok"})
check("clips asks for the date, 100 at most", (calls["get"][-1][1]["broadcaster_id"], calls["get"][-1][1]["first"],
      calls["get"][-1][1]["started_at"].endswith("Z")), ("42", 100, True))

def iso(u): return datetime.fromtimestamp(u, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
c0 = 1_800_000_000.0
def clip(i, who, at, dur=30): return {"id": i, "creator_id": who, "created_at": iso(c0 + at), "duration": dur}
ct = twitch.clip_times([clip("a", "u1", 100)], c0)
check("clip_times maps to session seconds", (ct[0]["id"], ct[0]["who"], ct[0]["end"], ct[0]["start"]), ("a", "u1", 100.0, 70.0))
check("a broken clip is skipped", twitch.clip_times([{"id": "x"}, {"created_at": "nonsense"}], c0), [])
two = twitch.clip_times([clip("a", "u1", 100), clip("b", "u2", 130)], c0)
b = twitch.bursts(two)
check("two people 30 s apart make a burst", (len(b), b and b[0]["people"], b and sorted(b[0]["ids"])), (1, 2, ["a", "b"]))
same = twitch.clip_times([clip("a", "u1", 100), clip("b", "u1", 130)], c0)
check("one person clipping twice does not", twitch.bursts(same), [])
far = twitch.clip_times([clip("a", "u1", 100), clip("b", "u2", 400)], c0)
check("clips far apart do not", twitch.bursts(far), [])

def boom(*a, **k): raise OSError("down")
hb = twitch.Helix("cid", "sec", get=boom, post=boom)
check("errors never come out", (hb.user_id("x"), hb.stream_start("1"), hb.clips("1", 0)), (None, None, []))
hs = twitch.Helix("cid", "sec", get=lambda *a, **k: R({}, 401), post=post)
check("a refused request gives nothing", (hs.user_id("x"), hs.clips("1", 0)), (None, []))

check("no credentials: twitch_app is None", config.twitch_app(), None)
os.environ["TWITCH_CLIENT_ID"] = "abc"; os.environ["TWITCH_CLIENT_SECRET"] = "def"; config._kept.clear()
check("credentials from the environment", config.twitch_app(), ("abc", "def"))
check("setting default and saving", (creators.DEFAULTS["sig_viewer_clips"], creators._clean({"sig_viewer_clips": False})["sig_viewer_clips"]), (True, False))
os.environ.pop("TWITCH_CLIENT_ID"); os.environ.pop("TWITCH_CLIENT_SECRET"); config._kept.clear()

def session(url="https://twitch.tv/somebody", **st):
    d = tempfile.mkdtemp(prefix="pc-twitch-live-")
    s = dict(creators.DEFAULTS, live_per_hour=2); s.update(st)
    return live.Session({"title": "t", "uploader": "u", "dir": d, "creator": "", "ai": {}, "clips": [], "points": [],
                         "mode": st.get("mode", "auto"), "source": {"url": url, "name": "somebody"}}, s)

T = time.time()
def ago(i, who, secs, dur=30, views=0):
    return {"id": i, "creator_id": who, "created_at": iso(T - secs), "duration": dur, "view_count": views}
def fake_server(clips):
    """Like Twitch: only clips made since started_at, most viewed first, 100 a page with a cursor."""
    seen = []
    def g(url, params=None, headers=None, timeout=None, **kw):
        if url.endswith("/users"): return R({"data": [{"id": "42"}]})
        since = datetime.fromisoformat(params["started_at"].replace("Z", "+00:00")).timestamp()
        seen.append(params["started_at"])
        pool = sorted((c for c in clips if datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp() >= since),
                      key=lambda c: -c["view_count"])
        at = int(params.get("after") or 0)
        page = pool[at:at + params["first"]]
        more = at + params["first"] < len(pool)
        return R({"data": page, "pagination": {"cursor": str(at + params["first"])} if more else {}})
    return g, seen
def live_session(**kw):
    s = session(**kw); s.started_at = T - 1000; s.samples = 16000 * 1000; s.clock0 = T - 1000
    return s

# no credentials: the guard itself is what stops it (the session is recording, so clock0 is set)
se = live_session()
check("no credentials: the poller does nothing", (se._poll_viewer_clips(), se.spike, se.viewer_clips), (False, None, {}))
msgs = []
creators.log = lambda cid, m: msgs.append(m)
for _ in range(2):
    x = live_session(); x.job["creator"] = "c1"; x.ended = True; x._watch_viewer_clips()
check("the no-credentials line is written once per creator, not per recording", len(msgs), 1)

# a burst becomes a strong spike once, growing with the people
se = live_session()
CLIPS[:] = [ago("a", "u1", 100), ago("b", "u2", 80), ago("c", "u3", 90)]
se._helix = twitch.Helix("cid", "sec", get=get, post=post)
check("a burst of viewers' clips sets a strong spike (10 plus the people)",
      (se._poll_viewer_clips(), bool(se.spike) and se.spike["z"], bool(se.spike) and se.spike["viewer_clips"]), (True, 13, 3))
check("the spike says why", se.spike["why"], "Viewers clipped this (3 people)")
se.spike = None; se._poll_viewer_clips()
check("the same clips do not make it again", (se.spike, sorted(se.viewer_clips)), (None, ["a", "b", "c"]))

# a new burst right after a reported one is found
se = live_session(); se._helix = twitch.Helix("cid", "sec", get=get, post=post)
CLIPS[:] = [ago("a", "u1", 300), ago("b", "u2", 290)]
se._poll_viewer_clips(); se.spike = None
CLIPS[:] = [ago("a", "u1", 300), ago("b", "u2", 290), ago("c", "u3", 240), ago("d", "u4", 230)]
se._poll_viewer_clips()
check("A,B reported, then C,D 50 s later is a second burst", (bool(se.spike), se.spike and se.spike["viewer_clips"], se.viewer_used), (True, 2, {"a", "b", "c", "d"}))

# a pending spike is not overwritten; the burst waits its turn
se = live_session(); se._helix = twitch.Helix("cid", "sec", get=get, post=post)
se.spike = {"t": 5.0, "why": "loudness"}
CLIPS[:] = [ago("a", "u1", 100), ago("b", "u2", 80)]
se._poll_viewer_clips()
check("a pending spike stays and the burst waits", (se.spike["why"], len(se.viewer_queue)), ("loudness", 1))
se.spike = None; se._flush_viewer_queue()
check("then the burst is sent", (se.spike["why"], se.viewer_queue), ("Viewers clipped this (2 people)", []))

# session seconds stand still in an outage: a clip still maps right
se = live_session(); se.samples = 16000 * 970         # 30 s of the stream were never received
se._helix = twitch.Helix("cid", "sec", get=get, post=post)
CLIPS[:] = [ago("a", "u1", 100), ago("b", "u2", 100)]
se._poll_viewer_clips()
check("clip time follows the session clock, not the wall clock", abs(se.viewer_clips["a"]["end"] - 870) < 1.5, True)
se.samples = 16000 * 1000; se.clock0 = None
check("clock0 is re-anchored at each look", (se._poll_viewer_clips(), abs(se.clock0 - (time.time() - 1000)) < 2), (True, True))

# busy channel: more than 100 old, much-viewed clips must not hide a new one
old = [ago(f"o{i}", f"v{i}", 5000 + i, views=500 + i) for i in range(150)]
g, seen = fake_server(old + [ago("n1", "u1", 60), ago("n2", "u2", 50)])
se = live_session(); se._helix = twitch.Helix("cid", "sec", get=g, post=post)
se._poll_viewer_clips()
check("new clips are seen among 150 old popular ones", (sorted(se.viewer_clips), bool(se.spike)), (["n1", "n2"], True))
check("only the last 10 minutes are asked for", datetime.fromisoformat(seen[0].replace("Z", "+00:00")).timestamp() >= T - 600 - 5, True)
many = [ago(f"m{i}", f"v{i}", 200 + i, views=100 - i // 2) for i in range(130)]
g, seen = fake_server(many)
h2 = twitch.Helix("cid", "sec", get=g, post=post)
check("more than a page is followed", len(h2.clips("42", T - 600)), 130)

# a refused token is renewed once
state = {"n": 0}
def g401(url, params=None, headers=None, timeout=None, **kw):
    state["n"] += 1
    return R({}, 401) if state["n"] == 1 else R({"data": [{"id": "42"}]})
calls["post"] = 0
check("a 401 renews the token and tries once more", (twitch.Helix("cid", "sec", get=g401, post=post).user_id("x"), calls["post"]), ("42", 2))
check("a 401 that stays gives nothing", twitch.Helix("cid", "sec", get=lambda *a, **k: R({}, 401), post=post).user_id("x"), None)

# pick mode lists it straight away, like a hand mark
sp = live_session(mode="pick"); sp.samples = 16000 * 400
sp._helix = twitch.Helix("cid", "sec", get=get, post=post)
CLIPS[:] = [ago("a", "u1", 100), ago("b", "u2", 80)]
sp._poll_viewer_clips()
check("pick mode lists it as from viewers", [p["from"] for p in sp.job["points"]], ["viewers"])

# failures are written once each
msgs.clear()
sf = live_session(); sf._helix = hb
sf._poll_viewer_clips(); sf._poll_viewer_clips()
check("a failing user lookup writes one line", len(msgs), 1)
def get_clips_fail(url, params=None, headers=None, timeout=None, **kw):
    return R({"data": [{"id": "42"}]}) if url.endswith("/users") else R({}, 500)
msgs.clear()
sc = live_session(); sc._helix = twitch.Helix("cid", "sec", get=get_clips_fail, post=post)
sc._poll_viewer_clips(); sc._poll_viewer_clips()
check("a failing clips call writes one line", len(msgs), 1)
CLIPS[:] = []
sn = live_session(); sn._helix = twitch.Helix("cid", "sec", get=get, post=post)
msgs.clear(); sn._poll_viewer_clips()
check("no clips is not an error", msgs, [])
check("no secret in the log", all("sec" not in m and "cid" not in m for m in msgs + ["x"]), True)
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS"); sys.exit(1 if fails else 0)
