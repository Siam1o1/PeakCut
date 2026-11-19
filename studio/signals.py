"""The maths behind finding moments in a live stream. Nothing here reads files or the network, so every part can be
tested on its own. live.py feeds it what it hears and sees; it answers with numbers."""
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
