# Delete Creator and Export Resolution Implementation Plan (steps 8 and 9)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete a creator from the Home creators table (optionally keeping their clips), and choose 720p or
1080p for every rendered clip, remembered per creator.

**Architecture:** Deleting moves or removes the creator's job folders in `studio/account.py`, behind the existing
`DELETE /api/creators/{cid}` route, after stopping their recording. Resolution is a clip field `res` that
`studio/render.py` applies as a final scale in the same ffmpeg filter; jobs, live clips and Live Transcriber Edit
fill it from the creator's remembered choice.

**Tech Stack:** Python 3.12 (FastAPI), ffmpeg, plain HTML/CSS/JS.

**Spec:** `docs/superpowers/specs/2026-11-19-moment-signals-design.md` (sections "Step 8" and "Step 9").

## Global Constraints

- Run every Python command with `"F:\New folder (2)\PeakCut\.venv\Scripts\python.exe"` and `PYTHONUTF8=1`.
  `tests/run.py` must pass after each task (inside this worktree "the new-version notice" may fail: it reads `.git`
  as a folder; it passes in the main checkout).
- Who is asking always comes from `ctx.uid()`; with accounts on, only a creator's owner can delete it.
- Anything that changes what a user sees goes through `jobs.save` / `ctx.bump`.
- Plain-language comments and UI text; existing palette; no shadows; hover changes colour only.
- Commit each task on branch `improve-signals`, ending the message with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: Delete a creator, keeping or deleting their clips (step 8)

**Files:**
- Modify: `studio/account.py` (`delete_creator`, ~line 114)
- Modify: `studio/server.py` (`delete_creator` route, ~line 997)
- Modify: `web/index.html` (Home creators table in `drawOverview`, ~line 1978; `delcreator` handler, ~line 2585;
  a new `askDeleteCreator` next to `askTyped`, ~line 1817)
- Create: `tests/test_delete_creator.py`; Modify: `tests/run.py` (add it)

**Interfaces:**
- Produces: `account.delete_creator(cid: str, with_clips: bool = False) -> {"ok": bool, "videos": int, "kept": int}`;
  `DELETE /api/creators/{cid}?with_clips=1`.

- [ ] **Step 1: Write the failing test** — `tests/test_delete_creator.py`, in-process like `tests/test_local.py`
  (accounts off, `TestClient(server.app)`, `Origin` header). It must check:
  1. a creator with one finished video and one finished pick-mode recording (made with `jobs.create(...)` under
     `ctx.as_user(auth.LOCAL_ID)`, each with a real small file in its folder, e.g. `tests.fakestream.make_video`)
     is deleted with `DELETE /api/creators/{cid}` → 200; the creator is gone from `GET /api/creators`; the
     creator's folder (`creators.root(cid)`) no longer exists;
  2. both jobs still exist (`GET /api/jobs`), have `creator` None, their `dir` is under
     `config.user_dir(auth.LOCAL_ID) / "jobs"`, and the file inside each still exists at the new path; for the video
     job, `source_path` points at the moved file; for a clip with `src` set, `src` points into the new folder;
  3. a second creator deleted with `?with_clips=1` takes its job with it (job gone, folder gone);
  4. a creator with a live session registered in `live.SESSIONS` (a `live.Session` on a pick job, not started) is
     stopped first: after the call the session has `user_stopped` True; when the session never finishes within the
     wait, the call answers 409 "The recording is stopping. Try again in a moment." (make `finished` stay False);
     when `finished` becomes True (set it from a short timer thread), the delete succeeds;
  5. the creator's `auto_watch` cannot restart a recording: `live.active_for(cid)` is None afterwards;
  6. another user's creator id (created under `ctx.as_user("u_other")`) answers 404 for the local user.

- [ ] **Step 2: Run** `PYTHONUTF8=1 "<venv python>" tests/test_delete_creator.py` → FAIL (jobs are deleted, not kept).

- [ ] **Step 3: Implement** `studio/account.py`:

```python
def _rehome(job, uid) -> None:
    """Move a video or recording out of a creator's folder into its owner's own folder, keeping every file."""
    old = Path(job["dir"])
    new = config.user_dir(uid) / "jobs" / old.name
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    o, n = str(old), str(new)
    fix = lambda p: (n + p[len(o):]) if isinstance(p, str) and p.startswith(o) else p
    job["dir"] = n
    for k in ("source_path",):
        if job.get(k):
            job[k] = fix(job[k])
    if isinstance(job.get("source"), dict) and job["source"].get("path"):
        job["source"]["path"] = fix(job["source"]["path"])
    for c in job.get("clips", []):
        if c.get("src"):
            c["src"] = fix(c["src"])
    job["creator"] = None
    jobs.save(job)


def delete_creator(cid: str, with_clips: bool = False) -> dict:
    """Remove a creator: their settings, posting rules, activity log and what PeakCut learned about them. Their
    videos and recordings are kept (moved to "No creator") unless with_clips is set. A recording of theirs is
    stopped first."""
    c = creators.get(cid)
    if not c:
        return {"ok": False}
    if c.get("auto_watch"):
        creators.update(cid, {"auto_watch": False})      # the watcher must not start a new recording meanwhile
    s = live.active_for(cid) or next((x for x in live.SESSIONS.values()
                                      if x.job.get("creator") == cid and x.job.get("user") == ctx.uid()
                                      and not x.finished), None)
    if s:
        if not s.user_stopped:
            s.stop("Stopped because the creator was deleted")
        for _ in range(40):                               # up to 20 s for the recording to wrap up
            if s.finished:
                break
            time.sleep(0.5)
        else:
            raise Busy("The recording is stopping. Try again in a moment.")
    mine = [j for j in jobs.list_jobs() if j.get("creator") == cid]
    if any(_busy(j) for j in mine):
        raise Busy("One of this creator's videos is still being made. Wait for it to finish first.")
    kept = 0
    for j in mine:
        if with_clips:
            jobs.delete(j["id"])
        else:
            _rehome(j, ctx.uid())
            kept += 1
    with creators._lock:
        creators._mine().pop(cid, None)
    shutil.rmtree(creators.root(cid), ignore_errors=True)
    ctx.bump()
    return {"ok": True, "videos": len(mine), "kept": kept}
```

  Add `import time`, `from pathlib import Path` and `config` to the imports if missing. `_busy(j)` already
  exists; check it treats a finished pick recording (status "done") as not busy.

  `studio/server.py`:

```python
@app.delete("/api/creators/{cid}")
def delete_creator(cid: str, with_clips: int = 0):
    _throttle("cdel:" + ctx.uid(), 30, 600)
    try:
        r = account.delete_creator(cid, with_clips=bool(with_clips))
    except account.Busy as e:
        raise HTTPException(409, str(e))
    except ValueError:
        raise HTTPException(404, "No such creator")
    if not r.get("ok"):
        raise HTTPException(404, "No such creator")
    return r
```

- [ ] **Step 4: Page.** Add next to `askTyped`:

```js
function askDeleteCreator(c) {
  return new Promise(res => {
    const d = $("#formdlg");
    d.innerHTML = `<form method="dialog" class="dlg"><h2>Delete ${esc(c.name)}?</h2>
      <p class="help">This removes ${esc(c.name)}, their settings, posting rules, activity log and what PeakCut learned about them. It cannot be undone. A recording of theirs is stopped first.</p>
      <label class="ck"><input type="checkbox" id="dc_clips"> Also delete this creator's clips and recordings</label>
      <p class="sub" id="dc_note">Their clips and recordings stay, under All videos with no creator.</p>
      <label class="f"><span>Type <b>${esc(c.name)}</b> to confirm</span><input id="dc_name" autocomplete="off" spellcheck="false"></label>
      <div class="dlg-foot"><button class="btn" value="no">Cancel</button><button class="btn danger" value="yes" id="dc_ok" disabled>Delete creator</button></div></form>`;
    const i = $("#dc_name"), box = $("#dc_clips");
    i.addEventListener("input", () => { $("#dc_ok").disabled = i.value.trim().toLowerCase() !== c.name.trim().toLowerCase(); });
    box.addEventListener("change", () => { $("#dc_note").textContent = box.checked ? "Their clips and recordings are deleted too." : "Their clips and recordings stay, under All videos with no creator."; });
    d.onclose = () => res(d.returnValue === "yes" ? {withClips: box.checked} : null); d.returnValue = "no"; d.showModal(); i.focus();
  });
}
```

  Replace the `delcreator` handler body to use it, with the creator taken from `d.c` when given (Home table) or
  from `creator()` (Setup page):

```js
    if (a === "delcreator") {
      const c = S.creators.find(x => x.id === (d.c || creatorId())); if (!c) return;
      const ans = await askDeleteCreator(c); if (!ans) return;
      const r = await api(`/api/creators/${encodeURIComponent(c.id)}${ans.withClips ? "?with_clips=1" : ""}`, {method:"DELETE"});
      S.creators = S.creators.filter(x => x.id !== c.id);
      if (ans.withClips) S.jobs = S.jobs.filter(j => j.creator !== c.id); else S.jobs.forEach(j => { if (j.creator === c.id) j.creator = null; });
      if (creatorId() === c.id) go("overview"); else render();
      toast(ans.withClips ? "Creator deleted, with their clips." : `Creator deleted. ${r.kept ? r.kept + " video" + (r.kept === 1 ? "" : "s") + " kept under All videos." : ""}`, "ok");
      return tick();
    }
```

  In the Home creators table add a last header cell `<th></th>` and, at the end of each row,
  `<td class="num"><button class="btn sm icon ghost" data-act="delcreator" data-c="${esc(r.id)}" title="Delete ${esc(r.name)}" aria-label="Delete ${esc(r.name)}">${ic("trash")}</button></td>`.
  Update the Setup page's danger-card text to say clips can be kept.

- [ ] **Step 5: Check.** `node --check` on the page's extracted inline script; the new test → ALL PASSED;
  `tests/run.py` (add `("deleting a creator, keeping or deleting their clips", ["tests/test_delete_creator.py"])`).

- [ ] **Step 6: Commit** — "Delete a creator from the Home page, and keep their clips if you like".

---

### Task 2: Export resolution 720p or 1080p (step 9)

**Files:**
- Modify: `studio/render.py` (`render_clip`, `_render_in`)
- Modify: `studio/jobs.py` (`DEFAULTS`, `clean_options`, `create`, clip creation in `_run_job` ~line 771,
  `request_render`, `render_one`)
- Modify: `studio/creators.py` (`DEFAULTS`, `_clean`)
- Modify: `studio/live.py` (`_add_clip`)
- Modify: `studio/picks.py` (`edit_cut`)
- Modify: `web/index.html` (composer Options ~line 1417 and `jobOptions` ~line 1482; clip editor `drawClipDlg`
  layout line ~2182; `openCutEdit` ~2369/2375)
- Test: `tests/test_split.py` (render size), `tests/test_picks.py` (Edit keeps `res`), a small check in
  `tests/test_delete_creator.py` is not needed.

**Interfaces:**
- Produces: clip field `res` (720 or 1080, default 1080); job option `res`; creator setting `res`;
  `render._render_in(..., face=None, split=None, res=1080)`.

- [ ] **Step 1: Failing tests.**
  - `tests/test_split.py`: render the same 3-second test clip with `"res": 720` → `media.probe` gives 720x1280,
    and with no `res` → 1080x1920 (existing check). Also with `layout` "blur" at 720.
  - `tests/test_picks.py` (Edit section): `picks.edit_cut(job, cut, {"layout": "blur", "res": 720})` → new clip
    `res == 720`, rendered file 720x1280, and the creator's saved `res` becomes 720; a junk value (`"res": 999`)
    falls back to the creator's saved choice.
  - `jobs.clean_options({"res": 720})` → `{"res": 720}`; `{"res": 500}` → falls back to 1080.

- [ ] **Step 2: Run them** → FAIL.

- [ ] **Step 3: Implement.**
  - `render.py`: `RES = {720: (720, 1280), 1080: (1080, 1920)}`. In `render_clip` read
    `res = clip.get("res") if clip.get("res") in RES else 1080` and pass `res=res` to `_render_in`. In `_render_in`
    add `res=1080` and build the final label so the scale is the last step:

```python
    tail = "" if res == 1080 else f",scale={RES[res][0]}:{RES[res][1]}:flags=lanczos"
    fc = _filter(layout, fg_h, split if layout == "split" else face) + f"[v0]ass={name}.ass:fontsdir=f{tail}[v]"
```

    (The cover picture is taken from the finished mp4, so it follows.) Keep `ass` the last positional argument of
    `_render_in` calls (a test reads it as `call_args.args[-1]`); pass `res` by keyword.
  - `jobs.py`: `DEFAULTS["res"] = 1080`; in `clean_options` add
    `elif k == "res": v = int(v) if str(v) in ("720", "1080") else DEFAULTS["res"]` (before the numeric
    fallthrough; `res` is not in `LIMITS`); in `create` add `"res"` to the keys copied from the creator; in
    `_run_job` clip creation add `res=o.get("res", 1080)`; in `request_render` accept
    `if "res" in changes: new["res"] = clean_options({"res": changes["res"]}).get("res", 1080)`; in `render_one`,
    after a successful render of a clip whose job has a creator, remember the choice:
    `if job.get("creator") and clip.get("res") in (720, 1080): creators.update(job["creator"], {"res": clip["res"]})`
    (wrapped in try/except so a vanished creator cannot fail the render).
  - `creators.py`: `DEFAULTS["res"] = 1080`; `_clean`: `elif k == "res": v = int(v) if str(v) in ("720", "1080") else 1080`.
  - `live.py` `_add_clip`: add `res=self.settings.get("res", 1080)` to the clip.
  - `picks.py` `edit_cut`: `res = int(options["res"]) if str(options.get("res")) in ("720", "1080") else cr.get("res", 1080)`;
    put `"res": res` in the job options and on the clip.
  - Page:
    - Composer Options: rename the existing "Video quality" heading to "Download quality" and add an
      "Export size" block right after it:

```js
      <div class="ob"><div class="ot">Export size</div><input type="hidden" id="o_res" value="${c?.res ?? 1080}"><div class="seg" role="group" aria-label="Export size">
        <button type="button" data-seg="o_res" data-v="720" aria-pressed="${(c?.res ?? 1080) == 720}">720p<small>720x1280, smaller file</small></button>
        <button type="button" data-seg="o_res" data-v="1080" aria-pressed="${(c?.res ?? 1080) == 1080}">1080p<small>1080x1920</small></button></div></div>
```

      and in `jobOptions` add `res:n("#o_res")`.
    - Clip editor: next to Layout add
      `<label class="f">Export size<select data-f="res">${opts({720:"720p (720x1280)", 1080:"1080p (1080x1920)"}, c.res || 1080)}</select></label>`
      (the `remake` handler already sends every `data-f`; `Number()` is applied to number inputs only, so the
      server's `clean_options` converts the string).
    - Cut Edit panel (`openCutEdit`): after the Layout block add a `choice("ce_res", [{v:720, label:"720p", prev:"<b>720x1280</b>"}, {v:1080, label:"1080p", prev:"<b>1080x1920</b>"}], cr.res || 1080)`
      under the heading "Export size" with the note `<p class="sub">Live streams are recorded at up to 720p, so 1080p from a stream is made larger, not sharper.</p>`,
      and add `res: Number($("#ce_res").value)` to the request body.

- [ ] **Step 4: Run** the three changed test files, `node --check` on the page script, then `tests/run.py`.

- [ ] **Step 5: Commit** — "Choose 720p or 1080p for every clip, remembered per creator".
