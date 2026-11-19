"""Viewers' Twitch clips, read through Twitch's own API (Helix).

Only used to read clips of the channel being recorded, with a free app made at dev.twitch.tv. Every call has a
15 second limit and any problem gives an empty answer: this never stops a recording."""
import time
from datetime import datetime, timezone

import httpx

API = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TIMEOUT = 15


def _unix(text):
    """'2026-11-19T10:00:00Z' as unix seconds, or None when it is not a time."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _rfc3339(unix):
    return datetime.fromtimestamp(unix, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Helix:
    def __init__(self, client_id, secret, get=httpx.get, post=httpx.post):
        self.client_id, self.secret, self.get, self.post = client_id, secret, get, post
        self.token, self.token_until = None, 0.0

    def _token(self):
        if self.token and time.time() < self.token_until:
            return self.token
        r = self.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.secret,
                                       "grant_type": "client_credentials"}, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        self.token = j["access_token"]
        self.token_until = time.time() + max(60, int(j.get("expires_in", 3600)) - 120)   # renewed a little early
        return self.token

    def _data(self, path, params):
        """The 'data' list of an answer, or None on any problem. Nothing here is logged, so no secret can leak."""
        try:
            headers = {"Client-Id": self.client_id, "Authorization": "Bearer " + self._token()}
            r = self.get(f"{API}/{path}", params=params, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json().get("data")
            return data if isinstance(data, list) else None
        except Exception:  # noqa: BLE001
            return None

    def user_id(self, login):
        d = self._data("users", {"login": login})
        return str(d[0]["id"]) if d and d[0].get("id") else None

    def stream_start(self, user_id):
        """When the channel went live (unix time), or None when it is not live."""
        d = self._data("streams", {"user_id": user_id})
        return _unix(d[0].get("started_at")) if d else None

    def clips(self, user_id, since):
        """Clips of this channel made after `since` (unix time): id, creator_id, created_at, duration."""
        d = self._data("clips", {"broadcaster_id": user_id, "started_at": _rfc3339(since), "first": 100})
        return [c for c in d if isinstance(c, dict)] if d else []


def clip_times(clips, clock0):
    """Each clip as seconds on the recording's clock: it ends when it was made and runs `duration` before that."""
    out = []
    for c in clips:
        made = _unix(c.get("created_at"))
        try:
            dur = float(c.get("duration") or 30)
        except (TypeError, ValueError):
            continue
        if made is None or not c.get("id"):
            continue
        end = made - clock0
        out.append({"id": str(c["id"]), "who": str(c.get("creator_id") or ""), "start": end - dur, "end": end})
    return out


def bursts(times, window=60, min_people=2):
    """Moments several different viewers clipped within `window` seconds. The moment is the middle of those clips.
    One person clipping again and again is not a burst."""
    out, group = [], []

    def close():
        people = {c["who"] or c["id"] for c in group}
        if len(people) >= min_people:
            out.append({"t": sum((c["start"] + c["end"]) / 2 for c in group) / len(group), "people": len(people),
                        "ids": [c["id"] for c in group]})
    for c in sorted(times, key=lambda c: c["end"]):
        if group and c["end"] - group[0]["end"] > window:
            close()
            group = []
        group.append(c)
    if group:
        close()
    return out
