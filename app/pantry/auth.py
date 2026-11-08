"""Who is this request from?

Two ways in, both without a Pantry login:

1. Through Cloudflare (away from home). Cloudflare Access signs a JWT with the user's email.
   We verify it against the team's public keys and the app's AUD tag. A request that carries
   any Cloudflare header but no valid JWT is refused, so a missing or misconfigured Access app
   fails closed instead of opening the app to the internet.
2. From the home network (LAN DNS points the name at a reverse proxy on the LAN). No identity is
   available, so the user picks their name once and it's kept in a cookie. Unverified, which is
   fine for a grocery list on a trusted LAN. Allowed only from PANTRY_LAN (and, through
   the proxy, only when the forwarded client is private too). The Docker host itself and Docker
   bridge addresses are refused, because that's where the cloudflared tunnel connects from.
"""
import ipaddress
import json
import os
import time
import urllib.request

import jwt

TEAM = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")      # e.g. yourteam.cloudflareaccess.com
AUD = os.environ.get("CF_ACCESS_AUD", "")
ALLOW_LAN = os.environ.get("PANTRY_ALLOW_LAN", "1") == "1"
# LAN access is off unless PANTRY_LAN is set (fail closed).
LAN = ipaddress.ip_network(os.environ["PANTRY_LAN"]) if os.environ.get("PANTRY_LAN") else None
CADDY = os.environ.get("PANTRY_CADDY_IP", "")       # the reverse proxy on the LAN
DOCKER_HOST_IP = os.environ.get("PANTRY_HOST_IP", "")  # never trusted as a LAN client
PRIVATE = [ipaddress.ip_network(n) for n in
           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]

# "email=Name,email=Name"
USERS = {}
for pair in filter(None, os.environ.get("PANTRY_USERS", "").split(",")):
    email, _, name = pair.partition("=")
    USERS[email.strip().lower()] = name.strip() or email.strip()
NAMES = ([n.strip() for n in os.environ.get("PANTRY_NAMES", "").split(",") if n.strip()]
         or sorted(set(USERS.values())) or ["Me"])

CF_HEADERS = ("cf-access-jwt-assertion", "cf-ray", "cf-connecting-ip", "cf-ipcountry", "cdn-loop")

_jwks = {"at": 0, "client": None}


def _jwk_client():
    if not _jwks["client"] or time.time() - _jwks["at"] > 3600:
        _jwks["client"] = jwt.PyJWKClient(f"https://{TEAM}/cdn-cgi/access/certs")
        _jwks["at"] = time.time()
    return _jwks["client"]


def _verify(token):
    if not (TEAM and AUD and token):
        return None
    try:
        key = _jwk_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUD,
                            issuer=f"https://{TEAM}")
    except Exception:
        return None
    return (claims.get("email") or "").lower() or None


def _ip(s):
    try:
        return ipaddress.ip_address((s or "").strip())
    except ValueError:
        return None


def _is_private(ip):
    return ip is not None and any(ip in n for n in PRIVATE)


def identify(request):
    """Return (allowed, who, via). who is a display name or '' (LAN user who hasn't picked)."""
    h = request.headers
    if any(k in h for k in CF_HEADERS):
        email = _verify(h.get("cf-access-jwt-assertion"))
        if not email:
            return False, "", "cloudflare"
        return True, USERS.get(email, email.split("@")[0].title()), "cloudflare"

    if not ALLOW_LAN or LAN is None:
        return False, "", "lan"
    src = _ip(request.client.host if request.client else "")
    if src is None or src not in LAN or str(src) == DOCKER_HOST_IP:
        return False, "", "lan"
    if CADDY and str(src) == CADDY:
        first = (h.get("x-forwarded-for") or "").split(",")[0]
        if first and not _is_private(_ip(first)):
            return False, "", "lan"
    who = request.cookies.get("pantry_who", "")
    return True, who if who in NAMES else "", "lan"
