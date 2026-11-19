"""The maths behind finding moments in a live stream. Nothing here reads files or the network, so every part can be
tested on its own. live.py feeds it what it hears and sees; it answers with numbers."""
import re
import unicodedata

import numpy as np

MAD_TO_SD = 1.4826          # turns a median distance into something that reads like a standard deviation


def robust_z(history, now_value, floor=None) -> float:
    """How unusual `now_value` is against `history`: (now - median) / MAD. A median and a median distance are not
    pulled about by a few wild values, unlike an average. `floor` stops a silent stretch from dividing by zero."""
    h = np.asarray(history, dtype=np.float64)
    if h.size < 2:
        return 0.0
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med)))
    lo = floor if floor is not None else max(1e-6, 0.05 * abs(med))
    return float((now_value - med) / (MAD_TO_SD * max(mad, lo)))


def series_z(series, now_len=5, window=900, exclude=10, seed=None, floor=None) -> float:
    """The last `now_len` seconds of a per-second series against the `window` seconds before them, leaving out the
    `exclude` seconds just before now (a moment builds up). With under 60 seconds of history, a saved typical value
    (`seed`: median and MAD from the creator's profile) is used instead, or 0 if there is none. `floor` is the
    smallest spread to divide by: a count series that is mostly zero has no spread at all."""
    s = np.asarray(series, dtype=np.float64)
    if s.size < now_len:
        return 0.0
    now = float(s[-now_len:].mean())
    hist = s[max(0, s.size - exclude - window): max(0, s.size - exclude)]
    if hist.size < 60:
        if not seed:
            return 0.0
        return float((now - seed["median"]) / (MAD_TO_SD * max(seed["mad"], 1e-6)))
    return robust_z(hist, now, floor)


def per_viewer(count, viewers):
    """Chatters per 1,000 viewers when the viewer count is known, so a big stream and a small one are measured alike."""
    return float(count) * 1000.0 / viewers if viewers else float(count)


class HourlyTop:
    """Keeps the strongest N candidates of any rolling hour. A new one goes if there is room, or if it is stronger
    than the weakest one sent in the last hour (which is then dropped from the count)."""

    def __init__(self, n):
        self.n = max(1, int(n))
        self.sent = []          # (time, score)

    def admit(self, score, now) -> bool:
        self.sent = [(t, s) for t, s in self.sent if now - t < 3600]
        if len(self.sent) < self.n:
            self.sent.append((now, score))
            return True
        weakest = min(range(len(self.sent)), key=lambda i: self.sent[i][1])
        if score > self.sent[weakest][1]:
            self.sent[weakest] = (now, score)
            return True
        return False


# ---------------------------------------------------------------- what chat says
DEFAULT_WORDS = {
    "laugh": ["lul", "kekw", "lol", "lmao", "lmfao", "haha", "omegalul", "icant", "xd"],
    "shock": ["wtf", "omg", "no way", "what", "holy", "monkas", "d:"],
    "hype": ["pog", "pogchamp", "poggers", "lets go", "letsgo", "w", "ez", "clutch", "gg"],
    "clip": ["clip", "clip it", "clip that", "clipped", "clipit"],
    "negative": ["l", "cringe", "boring", "ratio", "mid", "trash"],
}
# Chat helpers that talk on their own: their lines are not people reacting.
BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "soundalerts", "sery_bot",
        "botrixoficial", "kofistreambot", "streamstickers", "commanderroot"}
_TAGS = re.compile(r"^@(\S+) ")
_NICK = re.compile(r":([A-Za-z0-9_]{1,25})!")


def is_bot(name):
    n = (name or "").lower()
    return n in BOTS or n.endswith("bot")


def parse_irc(line):
    """Read one Twitch chat line. A message gives the person (their id when Twitch sends tags, otherwise their
    nickname) and the text; a USERNOTICE (raid, sub, gift) gives only what kind it is. Anything else is None.
    What kind of line it is comes from the command word right after the sender, never from the text."""
    tags = {}
    m = _TAGS.match(line)
    if m:
        tags = dict(kv.split("=", 1) if "=" in kv else (kv, "") for kv in m.group(1).split(";"))
        line = line[m.end():]
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line.partition(" ")
    command, _, params = line.partition(" ")
    if command == "USERNOTICE":
        return {"kind": "notice", "user": tags.get("user-id", ""), "text": "", "notice": tags.get("msg-id", "")}
    if command != "PRIVMSG" or not params.startswith("#"):
        return None
    nick = _NICK.search(prefix)
    name = tags.get("display-name") or (nick.group(1) if nick else "")
    text = params.partition(" :")[2]
    return {"kind": "msg", "user": tags.get("user-id") or (nick.group(1).lower() if nick else ""), "name": name,
            "text": text.strip(), "notice": ""}


def _is_emoji(ch):
    return unicodedata.category(ch) == "So"


def _words(text):
    """The words of a message, the same way for every language: split on spaces, trim punctuation around each
    word (keeping apostrophes inside it and emotes like D:), and make each emoji a word of its own."""
    out = []
    for piece in text.casefold().replace("’", "'").split():
        run = ""
        parts = []
        for ch in piece:
            if _is_emoji(ch):
                parts.append(run)
                parts.append(ch)
                run = ""
            elif unicodedata.category(ch) in ("Mn", "Cf", "Me"):   # emoji joiners and variation marks
                continue
            else:
                run += ch
        parts.append(run)
        for part in parts:
            if len(part) == 1 and _is_emoji(part):
                out.append(part)
                continue
            core = part
            while core and unicodedata.category(core[0]).startswith("P"):
                core = core[1:]
            while core and unicodedata.category(core[-1]).startswith("P"):
                core = core[:-1]
            if core:
                out.append(core + (":" if part.endswith(":") and len(core) == 1 else ""))
    return out


def analyze_window(msgs, words=None):
    """What a stretch of chat says, counted in people: how many talked, the share who sent the most common word
    (convergence), how many people each reaction type covers, and how many asked for a clip. Bots are left out,
    and so is a long message that was sent more than once by fewer than three people (copy and paste)."""
    words = words or DEFAULT_WORDS
    long_by = {}
    for user, text in msgs:
        if len(text.split()) > 6:
            long_by.setdefault(text.strip().casefold(), []).append(user)
    def copied(t):
        users = long_by.get(t.strip().casefold(), [])
        return len(t.split()) > 6 and len(users) > 1 and len(set(users)) < 3
    people = {}
    for u, t in msgs:
        if not u or is_bot(u) or copied(t):
            continue
        people.setdefault(u, []).append(" ".join(_words(t)))
    out = {"people": len(people), "convergence": 0.0, "top": "", "top_kind": "",
           "types": {k: 0 for k in words}, "clip_people": 0}
    if not people:
        return out
    phrases = {kind: [" " + " ".join(_words(p)) + " " for p in lst if _words(p)] for kind, lst in words.items()}
    seen = {}
    for u, texts in people.items():
        toks = set(w for t in texts for w in t.split())
        for w in toks:
            seen[w] = seen.get(w, 0) + 1
        for kind, lst in phrases.items():        # a phrase has to sit inside one message
            if any(p in " " + t + " " for t in texts for p in lst):
                out["types"][kind] += 1
    out["clip_people"] = out["types"].get("clip", 0)
    if not seen:
        return out
    top = max(seen, key=seen.get)
    own = [k for k, lst in phrases.items() if " " + top + " " in lst and out["types"][k]]
    common = max(out["types"], key=out["types"].get)
    out.update(top=top, convergence=seen[top] / len(people),
               top_kind=(max(own, key=out["types"].get) if own else (common if out["types"][common] else "")))
    return out


def word_lists(settings):
    """The five reaction word lists: the creator's own (comma separated) or the defaults."""
    out = {}
    for kind, default in DEFAULT_WORDS.items():
        raw = str((settings or {}).get(f"words_{kind}") or "").strip()
        out[kind] = [w.strip().lower() for w in raw.split(",") if w.strip()] or list(default)
    return out


def measure_lag(loud, chat, max_lag=20, default=5):
    """How many seconds chat reacts after the streamer does: the shift that lines the two up best."""
    a = np.asarray(loud, dtype=np.float64); b = np.asarray(chat, dtype=np.float64)
    n = min(a.size, b.size)
    if n < 120 or b[:n].std() < 1e-9 or a[:n].std() < 1e-9:
        return default
    a = (a[:n] - a[:n].mean()) / a[:n].std(); b = (b[:n] - b[:n].mean()) / b[:n].std()
    best, best_r = default, 0.1
    for lag in range(0, max_lag + 1):
        r = float(np.mean(a[: n - lag] * b[lag:]))
        if r > best_r:
            best, best_r = lag, r
    return best


def clip_edges(peak, lines, min_len, max_len, calm_after=None, lead_min=3):
    """Where a clip around `peak` starts and ends: at the start of the sentence that sets it up, and at the end of
    the sentence after chat has calmed down, kept within min_len and max_len."""
    starts = [l["s"] for l in lines if l["s"] <= peak - lead_min]
    start = max(starts) if starts else peak - lead_min
    want_end = max(peak + 2, calm_after if calm_after is not None else peak + 2)
    ends = [l["e"] for l in lines if l["e"] >= want_end]
    end = min(ends) if ends else want_end
    end = min(max(end, start + min_len), start + max_len)
    if end - start < min_len:
        start = end - min_len
    return round(max(0.0, start), 2), round(end, 2)
