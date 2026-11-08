"""Small Mealie client. Pantry has its own non-admin Mealie user; its token comes from
the env_file named in compose.override.yml (MEALIE_TOKEN). Without a token, Pantry still works, it just can't show
live voice items, Pantry-tagged ideas, or send the list itself (Claude does it instead)."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("MEALIE_URL", "").rstrip("/")            # LAN URL, for the API
PUBLIC = os.environ.get("MEALIE_PUBLIC_URL", BASE).rstrip("/")  # for links people tap
GROUP = os.environ.get("MEALIE_GROUP_SLUG", "home")
LIST_ID = os.environ.get("MEALIE_GROCERY_LIST_ID", "")
IDEA_TAG = os.environ.get("MEALIE_IDEA_TAG", "pantry")   # tag slug
TOKEN = os.environ.get("MEALIE_TOKEN", "")


class MealieError(Exception):
    pass


def enabled():
    return bool(BASE and TOKEN and LIST_ID)


def recipe_url(slug):
    return f"{PUBLIC}/g/{GROUP}/r/{slug}" if slug else ""


def list_url():
    return f"{PUBLIC}/shopping-lists/{LIST_ID}" if LIST_ID else f"{PUBLIC}/shopping-lists"


def image_url(recipe_id):
    """LAN URL for a recipe photo (Mealie serves media without auth)."""
    return f"{BASE}/api/media/recipes/{recipe_id}/images/min-original.webp"


def _req(method, path, body=None, timeout=10):
    if not TOKEN:
        raise MealieError("no Mealie token configured")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + "/api" + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise MealieError(f"Mealie {method} {path}: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise MealieError(f"Mealie unreachable: {e}") from None


def grocery_items(include_checked=False):
    q = urllib.parse.quote(f'shoppingListId="{LIST_ID}"')
    data = _req("GET", f"/households/shopping/items?perPage=200&queryFilter={q}")
    out = []
    for it in data.get("items", []):
        if it.get("checked") and not include_checked:
            continue
        out.append({
            "id": it["id"],
            "text": (it.get("display") or it.get("note") or "").strip(),
            "checked": it.get("checked", False),
            "source": (it.get("extras") or {}).get("source", ""),
        })
    return out


def add_grocery_item(text, week_id):
    return _req("POST", "/households/shopping/items", {
        "shoppingListId": LIST_ID, "note": text, "isFood": False, "quantity": 0,
        "checked": False, "extras": {"source": "claude", "pantry_week": week_id}})


def delete_grocery_item(item_id):
    _req("DELETE", f"/households/shopping/items/{item_id}")


def idea_recipes():
    """Recipes tagged with the Pantry idea tag in Mealie."""
    data = _req("GET", f"/recipes?perPage=50&orderBy=updatedAt&orderDirection=desc"
                       f"&tags={urllib.parse.quote(IDEA_TAG)}")
    # Mealie ignores a tags filter it can't resolve (e.g. before the tag exists) and returns
    # every recipe, so check each recipe's own tags too.
    want = IDEA_TAG.lower()
    return [{"slug": r["slug"], "name": r["name"], "recipe_id": r["id"]}
            for r in data.get("items", [])
            if any(want in ((t.get("slug") or "").lower(), (t.get("name") or "").lower())
                   for t in r.get("tags") or [])]


def recipe(slug):
    r = _req("GET", f"/recipes/{urllib.parse.quote(slug)}")
    n = r.get("nutrition") or {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return {"slug": r["slug"], "name": r["name"], "recipe_id": r["id"],
            "kcal": num(n.get("calories")), "protein": num(n.get("proteinContent")),
            "tags": [t["name"] for t in r.get("tags") or []]}


SLUG_RE = re.compile(r"/r/([a-z0-9-]+)")


def slug_from_url(url):
    if not url or "mealie" not in url:
        return ""
    m = SLUG_RE.search(urllib.parse.urlparse(url).path)
    return m.group(1) if m else ""
