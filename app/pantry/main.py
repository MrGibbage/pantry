"""Pantry: the household's weekly dinner-picking and list-review page.

Flow per week: Claude posts ~10 dinner ideas (CLI) -> household picks 5 here -> Claude posts a
draft list (CLI) -> household marks need/have here -> "Send to Mealie" puts the needed items on
the Mealie Grocery list -> shop with Mealie -> /receipt clears what was bought -> rate dinners.
"""
import json
import os
import re
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, mealie, receipts

HERE = Path(__file__).parent
VERSION = os.environ.get("PANTRY_VERSION", "1")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(recipe_url=mealie.recipe_url, list_url=mealie.list_url,
                             version=VERSION)

SECTIONS = [
    ("recipes", "For this week's dinners"),
    ("staples", "Every-week staples"),
    ("usual", "Usually bought"),
    ("extra", "Added by you"),
    ("onhand", "Probably on hand"),
]
TRIGGER = Path(os.environ.get("PANTRY_TRIGGER_DIR", "/data/trigger"))
PUBLIC_PATHS = ("/static/", "/healthz", "/manifest.webmanifest", "/sw.js", "/favicon.ico")


@app.on_event("startup")
def _startup():
    db.init()


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        return await call_next(request)
    ok, who, via = auth.identify(request)
    if not ok:
        return Response("Not allowed.", status_code=403, media_type="text/plain")
    request.state.who = who
    request.state.via = via
    if path.startswith("/api/") and request.method == "POST":
        # CSRF guard: our own JS always sends this header; a cross-site form can't.
        if request.headers.get("x-pantry") != "1":
            return Response("Missing header.", status_code=400)
        if not who:
            return JSONResponse({"error": "Pick your name first."}, status_code=401)
    elif not who and via == "lan" and path != "/who" and not (path == "/share" and
                                                              request.method == "POST"):
        return RedirectResponse(f"/who?next={request.url.path}", status_code=303)
    return await call_next(request)


# ---------- helpers ----------

def week_or_none(c, week_id=None):
    if week_id:
        return c.execute("SELECT * FROM weeks WHERE id=?", (week_id,)).fetchone()
    return db.current_week(c)


def ctx(request, c, **kw):
    week = kw.pop("week", None) or week_or_none(c)
    base = {"request": request, "who": request.state.who, "via": request.state.via,
            "week": dict(week) if week else None, "names": auth.NAMES,
            "mealie_on": mealie.enabled(), "receipts_waiting": receipts.waiting()}
    if week:
        w = week["id"]
        base["counts"] = {
            "picked": c.execute("SELECT COUNT(*) FROM proposals WHERE week_id=? AND picked=1",
                                (w,)).fetchone()[0],
            "proposals": c.execute("SELECT COUNT(*) FROM proposals WHERE week_id=?",
                                   (w,)).fetchone()[0],
            "items": c.execute("SELECT COUNT(*) FROM list_items WHERE week_id=?",
                               (w,)).fetchone()[0],
            "undecided": c.execute(
                "SELECT COUNT(*) FROM list_items WHERE week_id=? AND decision='' "
                "AND default_decision=''", (w,)).fetchone()[0],
            "requests": c.execute("SELECT COUNT(*) FROM requests WHERE week_id=? "
                                  "AND status='open'", (w,)).fetchone()[0],
        }
    job = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    base["job"] = dict(job) if job else None
    base["next_shop_day"] = next_shop_day(c)
    base.update(kw)
    return base


def next_shop_day(c):
    """The first Sunday after both today and the newest week."""
    start = date.today()
    last = c.execute("SELECT MAX(id) FROM weeks").fetchone()[0]
    if last:
        start = max(start, date.fromisoformat(last) + timedelta(days=1))
    return (start + timedelta(days=(6 - start.weekday()) % 7)).isoformat()


def effective(item):
    return item["decision"] or item["default_decision"]


# ---------- pages ----------

@app.get("/who", response_class=HTMLResponse)
def who_page(request: Request, next: str = "/"):
    return templates.TemplateResponse("who.html", {"request": request, "names": auth.NAMES,
                                                   "next": next if next.startswith("/") else "/",
                                                   "who": request.state.who, "via": request.state.via,
                                                   "week": None, "version": VERSION})


@app.post("/who")
async def who_set(request: Request):
    form = await request.form()
    name = form.get("name", "")
    nxt = form.get("next", "/")
    if name not in auth.NAMES:
        raise HTTPException(400)
    r = RedirectResponse(nxt if str(nxt).startswith("/") else "/", status_code=303)
    r.set_cookie("pantry_who", name, max_age=400 * 86400, httponly=True, samesite="lax")
    return r


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.conn() as c:
        x = ctx(request, c)
        if x["week"]:
            w = x["week"]["id"]
            x["picked"] = db.rows(c.execute(
                "SELECT * FROM proposals WHERE week_id=? AND picked=1 ORDER BY position", (w,)))
            x["requests"] = db.rows(c.execute(
                "SELECT * FROM requests WHERE week_id=? ORDER BY id DESC LIMIT 5", (w,)))
        voice = []
        if mealie.enabled():
            try:
                voice = [i for i in mealie.grocery_items() if i["source"] != "claude"]
            except mealie.MealieError:
                voice = None
        x["voice"] = voice
        cw = db.cooking_week(c, date.today())
        x["cooking"] = None
        if cw:
            x["cooking"] = {"week": dict(cw), "dishes": db.rows(c.execute(
                "SELECT * FROM proposals WHERE week_id=? AND picked=1 ORDER BY position, id",
                (cw["id"],)))}
        return templates.TemplateResponse("home.html", x)


@app.get("/dinners", response_class=HTMLResponse)
def dinners(request: Request):
    with db.conn() as c:
        x = ctx(request, c)
        if x["week"]:
            w = x["week"]["id"]
            x["proposals"] = db.rows(c.execute(
                "SELECT * FROM proposals WHERE week_id=? ORDER BY picked DESC, position, id", (w,)))
            x["requests"] = db.rows(c.execute(
                "SELECT * FROM requests WHERE week_id=? ORDER BY id DESC", (w,)))
        ideas, idea_err = [], ""
        if mealie.enabled():
            try:
                taken = {p["slug"] for p in x.get("proposals", [])} | \
                        {r["slug"] for r in x.get("requests", []) if r["slug"]}
                ideas = [i for i in mealie.idea_recipes() if i["slug"] not in taken]
            except mealie.MealieError as e:
                idea_err = str(e)
        x.update(ideas=ideas, idea_err=idea_err, idea_tag=mealie.IDEA_TAG)
        return templates.TemplateResponse("dinners.html", x)


@app.get("/list", response_class=HTMLResponse)
def shopping_list(request: Request):
    with db.conn() as c:
        x = ctx(request, c)
        groups = []
        if x["week"]:
            w = x["week"]["id"]
            items = db.rows(c.execute(
                "SELECT * FROM list_items WHERE week_id=? ORDER BY position, id", (w,)))
            for key, title in SECTIONS:
                its = [i for i in items if i["section"] == key]
                if its or key == "extra":
                    groups.append({"key": key, "title": title, "items": its})
            x["need_count"] = sum(1 for i in items if effective(i) == "need")
            x["unsent"] = sum(1 for i in items if effective(i) == "need" and not i["mealie_item_id"])
        voice, voice_err = [], ""
        if mealie.enabled():
            try:
                voice = [i for i in mealie.grocery_items() if i["source"] != "claude"]
            except mealie.MealieError as e:
                voice_err = str(e)
        x.update(groups=groups, voice=voice, voice_err=voice_err)
        return templates.TemplateResponse("list.html", x)


@app.get("/rate", response_class=HTMLResponse)
def rate(request: Request):
    with db.conn() as c:
        x = ctx(request, c)
        weeks = db.rows(c.execute("SELECT * FROM weeks ORDER BY id DESC LIMIT 3"))
        blocks = []
        for wk in weeks:
            dishes = db.rows(c.execute(
                "SELECT p.slug, p.name, p.recipe_id, r.rating, r.note, r.by FROM proposals p "
                "LEFT JOIN ratings r ON r.week_id=p.week_id AND r.slug=p.slug "
                "WHERE p.week_id=? AND p.picked=1 ORDER BY p.position", (wk["id"],)))
            if dishes:
                blocks.append({"week": wk, "dishes": dishes})
        x["blocks"] = blocks
        return templates.TemplateResponse("rate.html", x)


@app.get("/share", response_class=HTMLResponse)
def share(request: Request, url: str = "", text: str = "", title: str = ""):
    """PWA share target: share a recipe link (Mealie or anywhere) into this week's requests."""
    link = url or next((t for t in text.split() if t.startswith("http")), "")
    with db.conn() as c:
        x = ctx(request, c)
        x.update(share_url=link, share_text=(title or text).strip(),
                 share_slug=mealie.slug_from_url(link))
        return templates.TemplateResponse("share.html", x)


@app.post("/share", response_class=HTMLResponse)
async def share_post(request: Request):
    """Share target (POST, multipart): photos go to the receipt flow, links to requests."""
    too_big = int(request.headers.get("content-length") or 0) > receipts.MAX_REQUEST
    form = None if too_big else await request.form(max_files=receipts.MAX_FILES + 1)
    files = [f for f in (form.getlist("photos") if form else []) if getattr(f, "filename", None)]
    if too_big or files:
        if request.state.via != "lan":
            return receipts_page(request, error="Receipt photos can only be sent from home "
                                 "(on the home Wi-Fi). They show card details, so they never "
                                 "go through Cloudflare.", status=403)
        if too_big:
            return receipts_page(request, error="That's too much at once. Send fewer photos.",
                                 status=413)
        try:
            token, _ = await receipts.to_pending(files)
        except receipts.UploadError as e:
            return receipts_page(request, error=str(e), status=400)
        return RedirectResponse(f"/receipts?p={token}", status_code=303)
    url, text, title = (form.get(k, "") if form else "" for k in ("url", "text", "title"))
    return share(request, url=str(url), text=str(text), title=str(title))


def receipts_page(request, pending="", error="", status=200):
    with db.conn() as c:
        x = ctx(request, c)
    x.update(home=request.state.via == "lan", inbox_ok=receipts.enabled(), error=error,
             token=pending, pending=receipts.pending(pending) if pending else [])
    return templates.TemplateResponse("receipts.html", x, status_code=status)


@app.get("/receipts", response_class=HTMLResponse)
def receipts_get(request: Request, p: str = ""):
    if request.state.via != "lan":
        p = ""
    return receipts_page(request, pending=p)


@app.get("/receipts/pending/{token}/{name}")
def receipts_preview(token: str, name: str, request: Request):
    if request.state.via != "lan":
        raise HTTPException(403)
    f = receipts.pending_file(token, name)
    if not f:
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "no-store"})


@app.get("/img/{recipe_id}")
def image(recipe_id: str):
    if not recipe_id.replace("-", "").isalnum():
        raise HTTPException(404)
    if recipe_id == "none":   # a pick that isn't in Mealie (yet)
        return Response(NO_PHOTO, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})
    try:
        with urllib.request.urlopen(mealie.image_url(recipe_id), timeout=8) as r:
            body, ctype = r.read(), r.headers.get("Content-Type", "image/webp")
    except Exception:
        # No photo in Mealie (or Mealie unreachable): a plate drawing instead of a broken image.
        # Cached for a day only, so a photo added in Mealie later shows up.
        return Response(NO_PHOTO, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})
    return Response(body, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})


NO_PHOTO = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" preserveAspectRatio="xMidYMid slice">
<rect width="120" height="120" fill="#2f6b4f" fill-opacity=".16"/>
<g fill="none" stroke="#5aa883" stroke-width="3" stroke-linecap="round" stroke-opacity=".75">
<circle cx="60" cy="60" r="24"/><circle cx="60" cy="60" r="15" stroke-opacity=".4"/>
<path d="M27 40v12a5 5 0 0 0 10 0V40M32 40v40"/>
<path d="M90 40c-6 3-7 12-6 20h6v20"/>
</g></svg>"""


@app.get("/favicon.ico")
def favicon():
    return Response((HERE / "static" / "icon-192.png").read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/healthz")
def healthz():
    with db.conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/sw.js")
def sw():
    js = (HERE / "static" / "sw.js").read_text().replace("__VERSION__", VERSION)
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return Response((HERE / "static" / "manifest.webmanifest").read_text(),
                    media_type="application/manifest+json")


# ---------- API (JSON, same-origin, X-Pantry: 1) ----------

async def body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def need_week(c):
    w = db.current_week(c)
    if not w:
        raise HTTPException(409, "No week yet.")
    return w


@app.post("/api/proposals/{pid}/pick")
async def api_pick(pid: int, request: Request):
    b = await body(request)
    picked = 1 if b.get("picked") else 0
    who = request.state.who
    with db.conn() as c:
        p = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
        c.execute("UPDATE proposals SET picked=?, picked_by=?, picked_at=? WHERE id=?",
                  (picked, who if picked else "", db.now() if picked else "", pid))
        db.log(c, "pick" if picked else "unpick", p["week_id"], who, p["slug"])
        n = c.execute("SELECT COUNT(*) FROM proposals WHERE week_id=? AND picked=1",
                      (p["week_id"],)).fetchone()[0]
    return {"picked": picked, "by": who if picked else "", "count": n}


@app.post("/api/proposals/{pid}/cooked")
async def api_cooked(pid: int, request: Request):
    b = await body(request)
    with db.conn() as c:
        p = c.execute("SELECT * FROM proposals WHERE id=? AND picked=1", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
        at = db.now() if b.get("cooked") else ""
        c.execute("UPDATE proposals SET cooked_at=? WHERE id=?", (at, pid))
        db.log(c, "cooked" if at else "uncooked", p["week_id"], request.state.who, p["slug"])
        n = c.execute("SELECT COUNT(*) FROM proposals WHERE week_id=? AND picked=1 "
                      "AND cooked_at != ''", (p["week_id"],)).fetchone()[0]
    return {"cooked": bool(at), "count": n}


@app.post("/api/requests")
async def api_request(request: Request):
    b = await body(request)
    text, url = (b.get("text") or "").strip()[:1000], (b.get("url") or "").strip()[:2000]
    slug, name, recipe_id = (b.get("slug") or mealie.slug_from_url(url)), "", ""
    if not (text or url or slug):
        raise HTTPException(400, "Empty.")
    if slug and mealie.enabled():
        try:
            r = mealie.recipe(slug)
            name, recipe_id = r["name"], r["recipe_id"]
        except mealie.MealieError:
            pass
    who = request.state.who
    with db.conn() as c:
        w = need_week(c)
        cur = c.execute("INSERT INTO requests (week_id, text, url, slug, name, recipe_id, by, "
                        "created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (w["id"], text, url, slug, name or b.get("name", ""), recipe_id, who,
                         db.now()))
        db.log(c, "request", w["id"], who, {"text": text, "url": url, "slug": slug})
    return {"id": cur.lastrowid}


@app.post("/api/requests/{rid}/delete")
async def api_request_delete(rid: int, request: Request):
    with db.conn() as c:
        r = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404)
        c.execute("DELETE FROM requests WHERE id=?", (rid,))
        db.log(c, "request_delete", r["week_id"], request.state.who, dict(r))
    return {"ok": True}


@app.post("/api/items/{iid}/decide")
async def api_decide(iid: int, request: Request):
    b = await body(request)
    d = b.get("decision") or ""
    if d not in ("need", "have", ""):
        raise HTTPException(400)
    who = request.state.who
    with db.conn() as c:
        it = c.execute("SELECT * FROM list_items WHERE id=?", (iid,)).fetchone()
        if not it:
            raise HTTPException(404)
        c.execute("UPDATE list_items SET decision=?, decided_by=?, decided_at=? WHERE id=?",
                  (d, who if d else "", db.now() if d else "", iid))
        db.log(c, "decide", it["week_id"], who, {"item": it["name"], "decision": d})
    return {"decision": d, "effective": d or it["default_decision"]}


@app.post("/api/items")
async def api_add_item(request: Request):
    b = await body(request)
    name = (b.get("name") or "").strip()[:200]
    if not name:
        raise HTTPException(400)
    who = request.state.who
    with db.conn() as c:
        w = need_week(c)
        cur = c.execute("INSERT INTO list_items (week_id, position, section, name, decision, "
                        "decided_by, decided_at) VALUES (?,?,?,?,?,?,?)",
                        (w["id"], 9999, "extra", name, "need", who, db.now()))
        db.log(c, "add_item", w["id"], who, name)
    return {"id": cur.lastrowid}


@app.post("/api/items/{iid}/delete")
async def api_delete_item(iid: int, request: Request):
    with db.conn() as c:
        it = c.execute("SELECT * FROM list_items WHERE id=? AND section='extra'", (iid,)).fetchone()
        if not it:
            raise HTTPException(404)
        c.execute("DELETE FROM list_items WHERE id=?", (iid,))
        db.log(c, "delete_item", it["week_id"], request.state.who, it["name"])
    return {"ok": True}


@app.post("/api/send-to-mealie")
async def api_send(request: Request):
    if not mealie.enabled():
        raise HTTPException(409, "Mealie isn't connected yet; ask Claude to send the list.")
    who = request.state.who
    with db.conn() as c:
        w = need_week(c)
        items = db.rows(c.execute("SELECT * FROM list_items WHERE week_id=? ORDER BY position, id",
                                  (w["id"],)))
        try:
            existing = {i["text"].lower().strip() for i in mealie.grocery_items()}
        except mealie.MealieError as e:
            raise HTTPException(502, str(e))
        sent = skipped = 0
        for it in items:
            if effective(it) != "need" or it["mealie_item_id"]:
                continue
            text = f"{it['name']} ({it['qty']})" if it["qty"] else it["name"]
            if it["name"].lower().strip() in existing or text.lower() in existing:
                c.execute("UPDATE list_items SET mealie_item_id='dup' WHERE id=?", (it["id"],))
                skipped += 1
                continue
            try:
                r = mealie.add_grocery_item(text, w["id"])
            except mealie.MealieError as e:
                raise HTTPException(502, f"Sent {sent} before an error: {e}")
            mid = (r or {}).get("id") or ((r or {}).get("createdItems") or [{}])[0].get("id", "sent")
            c.execute("UPDATE list_items SET mealie_item_id=? WHERE id=?", (mid, it["id"]))
            existing.add(it["name"].lower().strip())
            sent += 1
        if w["status"] in ("picking", "listing", "review"):
            c.execute("UPDATE weeks SET status='shopping', updated_at=? WHERE id=?",
                      (db.now(), w["id"]))
        db.log(c, "send_to_mealie", w["id"], who, {"sent": sent, "already_there": skipped})
    return {"sent": sent, "already_there": skipped}


@app.post("/api/voice/{item_id}/have")
async def api_voice_have(item_id: str, request: Request):
    if not mealie.enabled():
        raise HTTPException(409)
    if not item_id.replace("-", "").isalnum():
        raise HTTPException(400)
    try:
        items = {i["id"]: i for i in mealie.grocery_items()}
        if item_id not in items:
            raise HTTPException(404)
        mealie.delete_grocery_item(item_id)
    except mealie.MealieError as e:
        raise HTTPException(502, str(e))
    with db.conn() as c:
        w = db.current_week(c)
        db.log(c, "voice_have", w["id"] if w else "", request.state.who, items[item_id]["text"])
    return {"ok": True}


def lan_only(request):
    if request.state.via != "lan":
        raise HTTPException(403, "Receipt photos can only be sent from home.")


@app.post("/api/receipts")
async def api_receipts(request: Request):
    lan_only(request)
    if int(request.headers.get("content-length") or 0) > receipts.MAX_REQUEST:
        raise HTTPException(413, "That's too much at once. Send fewer photos.")
    form = await request.form(max_files=receipts.MAX_FILES + 1)
    try:
        names = await receipts.to_inbox(form.getlist("photos"))
    except receipts.UploadError as e:
        raise HTTPException(400, str(e))
    with db.conn() as c:
        w = db.current_week(c)
        db.log(c, "receipt_upload", w["id"] if w else "", request.state.who, names)
    return {"saved": len(names), "waiting": receipts.waiting()}


@app.post("/api/receipts/confirm")
async def api_receipts_confirm(request: Request):
    lan_only(request)
    b = await body(request)
    try:
        if b.get("discard"):
            receipts.discard(b.get("token", ""))
            return {"saved": 0}
        names = receipts.confirm(b.get("token", ""))
    except receipts.UploadError as e:
        raise HTTPException(400, str(e))
    with db.conn() as c:
        w = db.current_week(c)
        db.log(c, "receipt_upload", w["id"] if w else "", request.state.who, names)
    return {"saved": len(names), "waiting": receipts.waiting()}


@app.post("/api/jobs/plan-week")
async def api_plan_week(request: Request):
    """Ask Claude to plan a week. A timer on the Docker host picks the job up and runs Claude
    Code there; Pantry itself never runs anything."""
    b = await body(request)
    wk = (b.get("week") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", wk):
        raise HTTPException(400, "Pick a shopping day.")
    try:
        d = date.fromisoformat(wk)
    except ValueError:
        raise HTTPException(400, "That's not a real date.")
    if not date.today() <= d <= date.today() + timedelta(days=21):
        raise HTTPException(400, "Pick a day in the next three weeks.")
    who = request.state.who
    with db.conn() as c:
        if c.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone():
            raise HTTPException(409, "Claude is already working on it.")
        if c.execute("SELECT 1 FROM proposals WHERE week_id=?", (wk,)).fetchone():
            raise HTTPException(409, "That week already has ideas.")
        note = (b.get("note") or "").strip()[:2000]
        cur = c.execute("INSERT INTO jobs (kind, week_id, requested_by, requested_at, note) "
                        "VALUES ('plan_week', ?, ?, ?, ?)", (wk, who, db.now(), note))
        db.log(c, "job_request", wk, who, {"kind": "plan_week", "note": note})
    # Wake the runner on the host: a systemd path unit watches this directory. Pantry can only
    # create the file; what runs is fixed on the host side.
    try:
        TRIGGER.mkdir(exist_ok=True)
        (TRIGGER / f"job-{cur.lastrowid}").touch()
    except OSError:
        pass   # the runner also sweeps the queue on start, so a missed wake-up isn't fatal
    return {"id": cur.lastrowid}


@app.post("/api/ratings")
async def api_rate(request: Request):
    b = await body(request)
    week_id, slug = b.get("week_id", ""), b.get("slug", "")
    rating = int(b.get("rating") or 0)
    if rating not in (-1, 0, 1):
        raise HTTPException(400)
    note = (b.get("note") or "").strip()[:500]
    who = request.state.who
    with db.conn() as c:
        p = c.execute("SELECT name FROM proposals WHERE week_id=? AND slug=?",
                      (week_id, slug)).fetchone()
        if not p:
            raise HTTPException(404)
        c.execute("INSERT INTO ratings (week_id, slug, name, rating, note, by, at) "
                  "VALUES (?,?,?,?,?,?,?) ON CONFLICT(week_id, slug) DO UPDATE SET "
                  "rating=excluded.rating, note=excluded.note, by=excluded.by, at=excluded.at",
                  (week_id, slug, p["name"], rating, note, who, db.now()))
        db.log(c, "rate", week_id, who, {"slug": slug, "rating": rating, "note": note})
    return {"ok": True}


@app.post("/api/week/note")
async def api_week_note(request: Request):
    b = await body(request)
    with db.conn() as c:
        w = need_week(c)
        c.execute("UPDATE weeks SET note=?, updated_at=? WHERE id=?",
                  ((b.get("note") or "")[:2000], db.now(), w["id"]))
        db.log(c, "week_note", w["id"], request.state.who, b.get("note", ""))
    return {"ok": True}
