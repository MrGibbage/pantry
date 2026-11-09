# Pantry

A small, mobile-first web page (installable as a PWA) for a household's weekly dinner planning
next to a self-hosted [Mealie](https://mealie.io). An AI assistant (here, Claude, through a CLI
inside the container) proposes dinners and drafts the shopping list; the household picks dinners,
marks each list item need / have, sends the list to Mealie's Grocery list, shops with Mealie, and
rates the dinners afterwards. It can also take receipt photos for a separate receipt-logging
workflow.

FastAPI + SQLite + Jinja templates + a little vanilla JS. No build step, no login.

## Weekly loop

1. **Assistant** creates the week and posts ~10 dinner ideas:
   `docker exec pantry pantry week 2026-11-15` and
   `docker exec -i pantry pantry proposals 2026-11-15 < proposals.json`.
2. **Household** picks 5 on the Dinners tab, leaves requests ("more chicken", a recipe link, a
   Mealie recipe tagged *pantry*) and a note for the week.
3. **Assistant** reads `pantry status` / `pantry dump`, answers requests (`pantry answer ID`), and
   posts the draft list (`pantry list WEEK < list.json`). The week moves to *review*.
4. **Household** taps Need/Have (faded buttons are the assistant's guess), adds extras, and taps
   **Send to Mealie**. Needed items go onto the Mealie Grocery list with
   `extras.source = "claude"`, skipping anything already there. Week moves to *shopping*.
5. Shop with Mealie, checking items off.
6. **Household** answers "Cook again?" yes/no for each dinner (+ optional note). The assistant copies decisions and ratings
   into its long-term records with `pantry dump WEEK`.

`docker exec pantry pantry help` lists every command and the JSON shapes.

## "Plan next week" button

The household can start a week from the home page. Pantry never runs the assistant itself: it
records a job (`jobs` table) and creates a file in `/data/trigger/`. Something on the host
watches that directory (here, a systemd path unit), claims the job with `pantry jobs claim`, runs
the assistant with a short tool allowlist, and reports back with `pantry jobs finish ID
done|failed`. The page shows "Claude is planning…" and refreshes until the ideas arrive. The
assistant posts ideas one per command with `pantry propose WEEK SLUG --reason ...`, which fills
the name, id, tags and nutrition from Mealie.

## Setup

1. Copy `compose.override.example.yml` to `compose.override.yml` (gitignored) and fill it in:
   names, home network, Mealie URLs and Grocery list id, and optionally the receipt inbox.
2. In Mealie, create a non-admin user for Pantry in your household, make an API token for it,
   and put `MEALIE_TOKEN=<token>` in a file **outside the repo** that the override's `env_file`
   points at.
3. `docker compose up -d --build`, then put it behind your LAN reverse proxy.

## Access and identity

There is no Pantry login. See `app/pantry/auth.py`:

- **Home network (the intended use):** LAN DNS points Pantry's name at a reverse proxy on the
  LAN. There's no identity on that path, so the app asks for a name once and keeps it in a
  cookie. Allowed only from `PANTRY_LAN`, never from `PANTRY_HOST_IP` (the Docker host, where a
  tunnel connector would connect from) or Docker bridges. Through the proxy at
  `PANTRY_CADDY_IP`, the forwarded client must also be a private address. With `PANTRY_LAN`
  unset, the LAN path is off. Away from home, a VPN such as Tailscale works (100.64.0.0/10
  counts as private).
- **Cloudflare Access (optional, built but not required):** the app verifies the
  `Cf-Access-Jwt-Assertion` JWT itself (team domain + `CF_ACCESS_AUD`) and maps the email to a
  name. Any request carrying Cloudflare headers without a valid JWT is refused, so with
  `CF_ACCESS_AUD` empty, all tunnel traffic gets 403 (fail closed).
- POSTs to `/api/` need the `X-Pantry: 1` header (CSRF guard).

## Mealie

Pantry uses its own non-admin Mealie user (`MEALIE_TOKEN`). Without it, Pantry still works: it
just can't show live Grocery-list items or *pantry*-tagged ideas, or send the list itself.
Recipe photos come from Mealie's media endpoint over the LAN (no auth needed) via
`/img/<recipe_id>`. Deep links use `MEALIE_PUBLIC_URL/g/<group>/r/<slug>`.

## Receipt photos (home network only)

The Receipts page (and sharing photos to Pantry from a phone's gallery) drops photos into the
directory mounted at `/inbox`. Receipt photos show card details, so uploads are refused on
anything that came through Cloudflare: only the LAN path works. Shared photos wait in
`/inbox/.pending/` until someone taps *Add* (that step carries the `X-Pantry` header, so a
cross-site form can't fill the inbox); unconfirmed ones are deleted after a day. JPEG/PNG only,
20 MB each, 10 per upload. Keep the inbox out of anything that's backed up off-site if that
matters to you.

## Data

`data/pantry.db` (SQLite) holds working state only and is gitignored, as are
`compose.override.yml` and any images.
