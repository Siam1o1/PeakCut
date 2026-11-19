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

se = session()
check("no credentials: the poller does nothing and does not start", (se._poll_viewer_clips(), se.spike, se.viewer_clips), (False, None, {}))

# with a fake Helix: a burst becomes a strong spike once, and is not repeated
se = session(); se.clock0 = c0
CLIPS[:] = [clip("a", "u1", 100), clip("b", "u2", 120)]
se._helix = twitch.Helix("cid", "sec", get=get, post=post)
check("a burst of viewers' clips sets a strong spike", (se._poll_viewer_clips(), bool(se.spike) and se.spike["z"], bool(se.spike) and se.spike["viewer_clips"]), (True, 10, 2))
check("the spike says why", se.spike["why"], "Viewers clipped this (2 people)")
first = se.spike; se.spike = None; se._poll_viewer_clips()
check("the same clips do not make it again", se.spike, None)
check("seen clips are kept", sorted(se.viewer_clips), ["a", "b"])

# pick mode lists it straight away, like a hand mark
sp = session(mode="pick"); sp.clock0 = c0; sp.samples = 16000 * 400
sp._helix = twitch.Helix("cid", "sec", get=get, post=post)
sp._poll_viewer_clips()
check("pick mode lists it as from viewers", [p["from"] for p in sp.job["points"]], ["viewers"])

# the first failure is logged once
msgs = []
creators.log = lambda cid, m: msgs.append(m)
sf = session(); sf.clock0 = c0
sf._helix = hb
sf._poll_viewer_clips(); sf._poll_viewer_clips()
check("the first failure writes one line", len(msgs), 1)
check("no secret in the log", all("sec" not in m and "cid" not in m for m in msgs), True)
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS"); sys.exit(1 if fails else 0)
