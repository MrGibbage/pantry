"""Claude's side of Pantry. Run inside the container:

    docker exec -i pantry pantry <command> [args] [< input.json]

Commands
  status                      JSON summary of the current week
  week WEEK [--status S]      create week WEEK (YYYY-MM-DD shopping day) or set its status
  note WEEK                   stdin text -> Claude's note shown on the home page
  proposals WEEK              stdin JSON list -> replace UNPICKED proposals (picks are kept)
                              [{slug, name, recipe_id, kcal, protein, tags[], reason}]
  list WEEK                   stdin JSON list -> replace list items, keeping decisions and
                              Mealie ids for items with the same section+name
                              [{section, name, qty, detail, recipes[], default}]
                              section: recipes | staples | usual | onhand ; default: need|have|''
  answer REQUEST_ID           stdin text -> Claude's reply to a request (marks it done)
  mark-sent WEEK              stdin JSON {item_id: mealie_item_id} (when Claude sent items)
  dump WEEK                   everything for that week as JSON (picks, decisions, requests,
                              ratings, events) for the grocery repo
  events [--since ISO]        activity log
"""
import json
import sys

from . import db


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
                      (sys.stdin.read().strip(), db.now(), wid))
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
                      (sys.stdin.read().strip(), rid))
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
        else:
            sys.exit(f"unknown command {cmd!r}; see: pantry help")


if __name__ == "__main__":
    main()
