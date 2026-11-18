"""Live Transcriber: the moments a clipper can pick from a live stream (points), the stretch of stream kept around
each one so it can still be cut after the buffer has moved on (holds), and the clips cut from them (cuts)."""
import math

HOLD_BEFORE, HOLD_AFTER = 60.0, 60.0    # seconds of stream kept either side of a moment
LEAD_DEFAULT, LEAD_MAX = 20.0, 60.0     # "seconds before": how far before the moment a clip starts
LEN_MIN, LEN_MAX = 5.0, 120.0           # how long a clip can be


def point_time(c: dict) -> float:
    """Where the moment itself is: the AI's peak, else 20 seconds after its start (so the default lead-in gives back
    the AI's own start), never past its end."""
    if c.get("peak") is not None:
        return float(c["peak"])
    return min(float(c["start"]) + LEAD_DEFAULT, float(c["end"]))


def _s(x):
    return str(round(x))


def plan_cut(t, hold_from, hold_to, lead, length):
    """Start and end of a clip `lead` seconds before the moment `t` and `length` long, kept inside what was saved
    (hold_from..hold_to), and one sentence saying what had to change ("" when nothing did)."""
    try:
        lead, length = float(lead), float(length)
    except (TypeError, ValueError):
        raise ValueError("Those numbers aren't valid.")
    if not (math.isfinite(lead) and math.isfinite(length)):
        raise ValueError("Those numbers aren't valid.")
    lead = max(0.0, min(LEAD_MAX, lead))
    length = max(LEN_MIN, min(LEN_MAX, length))
    start, notes = t - lead, []
    if start < hold_from:
        start = hold_from
        notes.append(f"only {_s(t - hold_from)} seconds were kept before this moment, so the clip starts there")
    end = start + length
    if end > hold_to:
        end = hold_to
        if end - start < LEN_MIN:
            start = max(hold_from, end - LEN_MIN)
        notes.append(f"only {_s(hold_to - t)} seconds were kept after this moment, "
                     f"so the clip is {_s(end - start)} seconds long")
    note = "; ".join(notes)
    return round(start, 2), round(end, 2), (note[:1].upper() + note[1:] + ".") if note else ""
