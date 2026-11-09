"""Claude's side of Pantry. Run inside the container:

    docker exec -i pantry pantry <command> [args] [< input.json]

Commands
  status                      JSON summary of the current week
  week WEEK [--status S]      create week WEEK (YYYY-MM-DD shopping day) or set its status
  note WEEK [--text T]        Claude's note shown on the home page (--text, or stdin)
  propose WEEK SLUG [--reason R] [--kcal N --protein N --estimate]
                              add one idea; name, id, tags and nutrition come from Mealie.
                              --kcal/--protein only when Mealie has none (then --estimate)
  propose WEEK --reset        drop the week's UNPICKED ideas (picks are kept)
  drop-week WEEK              delete a week that has no ideas, list or requests yet
  proposals WEEK              stdin JSON list -> replace UNPICKED proposals (picks are kept)
                              [{slug, name, recipe_id, kcal, protein, tags[], reason}]
  list WEEK                   stdin JSON list -> replace list items, keeping decisions and
                              Mealie ids for items with the same section+name
                              [{section, name, qty, detail, recipes[], default}]
                              section: recipes | staples | usual | onhand ; default: need|have|''
  answer REQUEST_ID [--text T]  Claude's reply to a request (marks it done; --text or stdin)
  mark-sent WEEK              stdin JSON {item_id: mealie_item_id} (when Claude sent items)
  dump WEEK                   everything for that week as JSON (picks, decisions, requests,
                              ratings, events) for the grocery repo
  events [--since ISO]        activity log
  requests                    open requests from any week (asked after a week ended land there)
  history [N]                 the last N weeks (default 8): picks, who picked, ratings, notes
  recipes [--ingredients]     every Mealie recipe: slug, name, id, tags, servings, nutrition
                              (kcal, protein, ...), the Nutrition note, dates; read-only
  jobs                        recent jobs (household's "Plan next week" requests)
  jobs claim                  take the oldest queued job (marks it running) -> JSON, or {}
  jobs finish ID done|failed  stdin text -> message shown to the household
"""
import json
import sys
from datetime import datetime, timedelta, timezone

from . import db, mealie


def out(obj):
    json.dump(obj, sys.stdout, indent=1, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def need(c, week_id):
    w = c.execute("SELECT * FROM weeks WHERE id=?", (week_id,)).fetchone()
    if not w:
        sys.exit(f"no week {week_id}; create it with: pantry week {week_id}")
    return w


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    db.init()
    cmd = a.pop(0)
    with db.conn() as c:
        if cmd == "status":
            w = db.current_week(c)
            if not w:
                return out({"week": None})
            wid = w["id"]
            out({
                "week": dict(w),
                "picked": db.rows(c.execute("SELECT slug, name, picked_by FROM proposals "
                                            "WHERE week_id=? AND picked=1", (wid,))),
                "proposals": c.execute("SELECT COUNT(*) FROM proposals WHERE week_id=?",
                                       (wid,)).fetchone()[0],
                "open_requests": db.rows(c.execute("SELECT * FROM requests WHERE week_id=? "
                                                   "AND status='open'", (wid,))),
                "items": c.execute("SELECT COUNT(*) FROM list_items WHERE week_id=?",
                                   (wid,)).fetchone()[0],
                "decided": c.execute("SELECT COUNT(*) FROM list_items WHERE week_id=? AND "
                                     "decision!=''", (wid,)).fetchone()[0],
            })
        elif cmd == "week":
            wid = a.pop(0)
            status = a[a.index("--status") + 1] if "--status" in a else None
            if status and status not in db.STATUSES:
                sys.exit(f"status must be one of {db.STATUSES}")
            if not c.execute("SELECT 1 FROM weeks WHERE id=?", (wid,)).fetchone():
                c.execute("INSERT INTO weeks (id, status, created_at, updated_at) VALUES (?,?,?,?)",
                          (wid, status or "picking", db.now(), db.now()))
                db.log(c, "week_create", wid, "claude")
            elif status:
                c.execute("UPDATE weeks SET status=?, updated_at=? WHERE id=?",
                          (status, db.now(), wid))
                db.log(c, "week_status", wid, "claude", status)
            out(dict(need(c, wid)))
        elif cmd == "note":
            wid = a.pop(0)
            need(c, wid)
            c.execute("UPDATE weeks SET claude_note=?, updated_at=? WHERE id=?",
                      (text_arg(a), db.now(), wid))
            out({"ok": True})
        elif cmd == "proposals":
            wid = a.pop(0)
            need(c, wid)
            items = json.load(sys.stdin)
            picked = {r["slug"] for r in c.execute(
                "SELECT slug FROM proposals WHERE week_id=? AND picked=1", (wid,))}
            c.execute("DELETE FROM proposals WHERE week_id=? AND picked=0", (wid,))
            n = 0
            for i, p in enumerate(items):
                fields = (p.get("name", p["slug"]), p.get("recipe_id", ""), p.get("kcal"),
                          p.get("protein"), json.dumps(p.get("tags", [])), p.get("reason", ""),
                          p.get("source", "claude"))
                if p["slug"] in picked:
                    c.execute("UPDATE proposals SET name=?, recipe_id=?, kcal=?, protein=?, tags=?, "
                              "reason=?, source=?, position=? WHERE week_id=? AND slug=?",
                              fields + (i, wid, p["slug"]))
                else:
                    c.execute("INSERT INTO proposals (name, recipe_id, kcal, protein, tags, reason, "
                              "source, position, week_id, slug) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              fields + (i, wid, p["slug"]))
                    n += 1
                if p.get("picked"):   # a pick made outside Pantry (e.g. in chat)
                    c.execute("UPDATE proposals SET picked=1, picked_by=?, picked_at=? "
                              "WHERE week_id=? AND slug=? AND picked=0",
                              (p.get("picked_by", ""), db.now(), wid, p["slug"]))
            db.log(c, "proposals", wid, "claude", f"{n} new, {len(picked)} picks kept")
            out({"new": n, "picks_kept": len(picked)})
        elif cmd == "list":
            wid = a.pop(0)
            need(c, wid)
            items = json.load(sys.stdin)
            keep = {(r["section"], r["name"].lower()): dict(r) for r in c.execute(
                "SELECT * FROM list_items WHERE week_id=? AND section!='extra'", (wid,))}
            c.execute("DELETE FROM list_items WHERE week_id=? AND section!='extra'", (wid,))
            for i, it in enumerate(items):
                old = keep.get((it["section"], it["name"].lower()), {})
                c.execute("INSERT INTO list_items (week_id, position, section, name, qty, detail, "
                          "recipes, default_decision, decision, decided_by, decided_at, "
                          "mealie_item_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (wid, i, it["section"], it["name"], it.get("qty", ""),
                           it.get("detail", ""), json.dumps(it.get("recipes", [])),
                           it.get("default", ""), old.get("decision", ""),
                           old.get("decided_by", ""), old.get("decided_at", ""),
                           old.get("mealie_item_id", "")))
            st = c.execute("SELECT status FROM weeks WHERE id=?", (wid,)).fetchone()[0]
            if st in ("picking", "listing"):
                c.execute("UPDATE weeks SET status='review', updated_at=? WHERE id=?",
                          (db.now(), wid))
            db.log(c, "list", wid, "claude", f"{len(items)} items")
            out({"items": len(items)})
        elif cmd == "answer":
            rid = int(a.pop(0))
            c.execute("UPDATE requests SET answer=?, status='done' WHERE id=?",
                      (text_arg(a), rid))
            out({"ok": True})
        elif cmd == "mark-sent":
            wid = a.pop(0)
            for iid, mid in json.load(sys.stdin).items():
                c.execute("UPDATE list_items SET mealie_item_id=? WHERE id=? AND week_id=?",
                          (mid, int(iid), wid))
            out({"ok": True})
        elif cmd in ("dump", "export"):
            wid = a.pop(0)
            w = need(c, wid)
            q = lambda sql: db.rows(c.execute(sql, (wid,)))
            out({
                "week": dict(w),
                "proposals": q("SELECT * FROM proposals WHERE week_id=? ORDER BY position"),
                "requests": q("SELECT * FROM requests WHERE week_id=? ORDER BY id"),
                "list": q("SELECT * FROM list_items WHERE week_id=? ORDER BY position, id"),
                "ratings": q("SELECT * FROM ratings WHERE week_id=?"),
                "events": q("SELECT * FROM events WHERE week_id=? ORDER BY id"),
            })
        elif cmd == "events":
            since = a[a.index("--since") + 1] if "--since" in a else ""
            out(db.rows(c.execute("SELECT * FROM events WHERE at > ? ORDER BY id", (since,))))
        elif cmd == "propose":
            wid = a.pop(0)
            need(c, wid)
            if a and a[0] == "--reset":
                n = c.execute("DELETE FROM proposals WHERE week_id=? AND picked=0", (wid,)).rowcount
                return out({"removed": n})
            slug = a.pop(0)
            opt = lambda k: a[a.index(k) + 1] if k in a else None
            try:
                r = mealie.recipe(slug)
            except mealie.MealieError as e:
                sys.exit(f"no Mealie recipe {slug!r}: {e}")
            kcal, protein = r["kcal"], r["protein"]
            reason = opt("--reason") or ""
            if kcal is None and opt("--kcal"):
                kcal = float(opt("--kcal"))
            if protein is None and opt("--protein"):
                protein = float(opt("--protein"))
            if "--estimate" in a and "estimate" not in reason.lower():
                reason = (reason + " · nutrition estimated").strip(" ·")
            pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM proposals WHERE week_id=?",
                            (wid,)).fetchone()[0]
            c.execute("INSERT INTO proposals (week_id, slug, name, recipe_id, kcal, protein, tags, "
                      "reason, position) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(week_id, slug) DO "
                      "UPDATE SET reason=excluded.reason, kcal=excluded.kcal, "
                      "protein=excluded.protein", (wid, r["slug"], r["name"], r["recipe_id"], kcal,
                                                   protein, json.dumps(r["tags"]), reason, pos))
            db.log(c, "propose", wid, "claude", r["slug"])
            out({"slug": r["slug"], "name": r["name"], "kcal": kcal, "protein": protein})
        elif cmd == "drop-week":
            wid = a.pop(0)
            need(c, wid)
            busy = any(c.execute(f"SELECT 1 FROM {t} WHERE week_id=?", (wid,)).fetchone()
                       for t in ("proposals", "list_items", "requests", "ratings"))
            if busy:
                sys.exit(f"week {wid} isn't empty; not dropping it")
            c.execute("DELETE FROM weeks WHERE id=?", (wid,))
            db.log(c, "week_drop", wid, "claude")
            out({"dropped": wid})
        elif cmd == "requests":
            out(db.rows(c.execute("SELECT * FROM requests WHERE status='open' ORDER BY id")))
        elif cmd == "history":
            n = int(a[0]) if a else 8
            weeks = db.rows(c.execute("SELECT id, status, note FROM weeks ORDER BY id DESC LIMIT ?",
                                      (n,)))
            for w in weeks:
                w["picked"] = db.rows(c.execute(
                    "SELECT p.slug, p.name, p.picked_by, p.cooked_at != '' AS cooked, r.rating, r.note AS rating_note "
                    "FROM proposals p LEFT JOIN ratings r ON r.week_id=p.week_id AND r.slug=p.slug "
                    "WHERE p.week_id=? AND p.picked=1 ORDER BY p.position", (w["id"],)))
                w["not_picked"] = [r["slug"] for r in c.execute(
                    "SELECT slug FROM proposals WHERE week_id=? AND picked=0", (w["id"],))]
            out(weeks)
        elif cmd == "recipes":
            out(recipe_catalog("--ingredients" in a))
        elif cmd == "jobs":
            sub = a.pop(0) if a else ""
            if sub == "claim":
                stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(
                    timespec="seconds")
                c.execute("UPDATE jobs SET status='failed', finished_at=?, message='Timed out.' "
                          "WHERE status='running' AND started_at < ?", (db.now(), stale))
                if c.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                    return out({})
                r = c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=(SELECT id "
                              "FROM jobs WHERE status='queued' ORDER BY id LIMIT 1) RETURNING *",
                              (db.now(),)).fetchone()
                if r:
                    db.log(c, "job_start", r["week_id"], "claude", r["kind"])
                out(dict(r) if r else {})
            elif sub == "finish":
                jid, st = int(a.pop(0)), a.pop(0)
                if st not in ("done", "failed"):
                    sys.exit("status must be done or failed")
                msg = sys.stdin.read().strip()[:500] if not sys.stdin.isatty() else ""
                c.execute("UPDATE jobs SET status=?, finished_at=?, message=? WHERE id=?",
                          (st, db.now(), msg, jid))
                db.log(c, "job_" + st, "", "claude", msg)
                out({"ok": True})
            else:
                out(db.rows(c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 10")))
        else:
            sys.exit(f"unknown command {cmd!r}; see: pantry help")


def text_arg(a):
    if "--text" in a:
        return a[a.index("--text") + 1].strip()
    return sys.stdin.read().strip()


def recipe_catalog(with_ingredients=False):
    if not mealie.enabled():
        sys.exit("Mealie isn't connected (no MEALIE_TOKEN)")

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    items, page = [], 1
    while True:
        d = mealie._req("GET", f"/recipes?perPage=100&page={page}")
        items += d.get("items", [])
        if page >= d.get("total_pages", 1):
            break
        page += 1
    catalog = []
    for s in items:
        r = mealie._req("GET", f"/recipes/{s['slug']}")
        n = r.get("nutrition") or {}
        rec = {
            "slug": r["slug"], "name": r["name"], "recipe_id": r["id"],
            "tags": [t["name"] for t in r.get("tags") or []],
            "servings": r.get("recipeServings"),
            "kcal": num(n.get("calories")), "protein": num(n.get("proteinContent")),
            "carbs": num(n.get("carbohydrateContent")), "fat": num(n.get("fatContent")),
            "nutrition_note": next((x.get("text", "") for x in r.get("notes") or []
                                    if (x.get("title") or "").lower() == "nutrition"), ""),
            "added": (r.get("createdAt") or "")[:10], "source": (r.get("orgURL") or "").split("?")[0],
        }
        if with_ingredients:
            rec["ingredients"] = [i.get("display") or i.get("note") or ""
                                  for i in r.get("recipeIngredient") or []]
        catalog.append(rec)
    return catalog


if __name__ == "__main__":
    main()
