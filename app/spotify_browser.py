"""Spotify through a logged-in headless Chromium (no developer app, no Premium needed).

  spotify_browser.py login   start Chromium with remote debugging on 127.0.0.1:9222 and wait (30 min)
                             until you log in to Spotify through chrome://inspect
  spotify_browser.py token   print a fresh web-player access token (JSON) to stdout

The browser profile (cookies) lives in state/browser; only the access token the web player itself
uses is taken from it.
"""
import json
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILE = ROOT / "state" / "browser"
UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")


def launch(p, debug=False):
    PROFILE.mkdir(parents=True, exist_ok=True)
    PROFILE.chmod(0o700)
    args = ["--disable-blink-features=AutomationControlled"]
    if debug:
        args += ["--remote-debugging-port=9222", "--remote-debugging-address=127.0.0.1"]
    return p.chromium.launch_persistent_context(
        str(PROFILE), headless=True, channel="chromium", args=args, user_agent=UA,
        viewport={"width": 1280, "height": 860}, locale="en-US")


def logged_in(ctx):
    return any(c["name"] == "sp_dc" and c["value"] for c in ctx.cookies("https://open.spotify.com"))


def cmd_login(minutes=30):
    with sync_playwright() as p:
        ctx = launch(p, debug=True)
        if logged_in(ctx):
            print("already logged in")
            ctx.close()
            return
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.spotify.com/en/login?continue=https%3A%2F%2Fopen.spotify.com%2F")
        print("Chromium is waiting for your Spotify login on 127.0.0.1:9222 "
              f"(up to {minutes} min). Open chrome://inspect via the SSH tunnel.", flush=True)
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            if logged_in(ctx):
                page.wait_for_timeout(3000)
                print("logged in ✓ — session saved", flush=True)
                ctx.close()
                return
            page.wait_for_timeout(2000)
        ctx.close()
        raise SystemExit("timed out waiting for login")


def cmd_token():
    """Open the web player and capture the bearer token it sends to Spotify's APIs."""
    found = {}

    def on_request(req):
        auth = req.headers.get("authorization", "")
        if auth.startswith("Bearer ") and ("spotify.com" in req.url) and "token" not in found:
            found["token"] = auth[7:]
            found["client_token"] = req.headers.get("client-token", "")

    with sync_playwright() as p:
        ctx = launch(p)
        if not logged_in(ctx):
            ctx.close()
            raise SystemExit("not logged in to Spotify: run `spotify_browser.py login`")
        page = ctx.new_page()
        page.on("request", on_request)
        page.goto("https://open.spotify.com/collection/tracks", wait_until="domcontentloaded")
        for _ in range(60):
            if "token" in found:
                break
            page.wait_for_timeout(1000)
        ctx.close()
    if "token" not in found:
        raise SystemExit("web player sent no token (session expired? run login again)")
    found["expires_at"] = time.time() + 45 * 60
    print(json.dumps(found))


if __name__ == "__main__":
    {"login": cmd_login, "token": cmd_token}.get((sys.argv + [""])[1], lambda: print(__doc__))()
