"""Spotify through a logged-in headless Chromium (no developer app, no Premium needed).

  spotify_browser.py login   start Chromium with remote debugging on 127.0.0.1:9222 and wait (30 min)
                             until you log in to Spotify through chrome://inspect
  spotify_browser.py harvest print taste data (top artists/tracks, liked songs, own playlists, library) as JSON

The browser profile (cookies) lives in state/browser. Nothing calls Spotify's API directly: the pages are
opened like a person would and the JSON the web player itself fetches is read.
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


def op_name(resp):
    req = resp.request
    try:
        body = req.post_data_json or {}
        if body.get("operationName"):
            return body["operationName"]
    except Exception:  # noqa: BLE001
        pass
    q = dict(x.split("=", 1) for x in req.url.split("?", 1)[-1].split("&") if "=" in x)
    return q.get("operationName", req.url.split("?")[0].rsplit("/", 1)[-1])


def track_uris(js):
    return {uri_of(d) for d in walk(js) if kind(d) == "track"}


def visit(page, url, label, scrolls=0, store=None, stop=None):
    """Open url, scroll the main view up to `scrolls` times, return [(operationName, json)] the web player
    fetched. With `stop` (track uris seen before), scrolling ends as soon as one of them shows up."""
    got = []

    def on_resp(r):
        if "pathfinder" in r.url or "spclient" in r.url:
            got.append(r)
    page.on("response", on_resp)
    out = []
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        page.mouse.move(800, 500)
        quiet, checked = 0, 0

        def hit_known():
            nonlocal checked
            new, checked = got[checked:], len(got)
            for r in new:
                try:
                    if r.ok and "json" in (r.headers.get("content-type") or "") and track_uris(r.json()) & stop:
                        return True
                except Exception:  # noqa: BLE001
                    pass
            return False

        for _ in range(scrolls):
            if stop and hit_known():
                break
            n = len(got)
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1500)
            quiet = quiet + 1 if len(got) == n else 0
            if quiet >= 4:
                break
        for r in got:
            try:
                if r.ok and "json" in (r.headers.get("content-type") or ""):
                    out.append((op_name(r), r.json()))
            except Exception:  # noqa: BLE001
                pass
    finally:
        page.remove_listener("response", on_resp)
    if store is not None:
        store.append((label, out))
    return out


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def uri_of(d):
    """'spotify:<kind>:<id>' from uri or _uri, else None."""
    u = d.get("uri") or d.get("_uri")
    return u if isinstance(u, str) and u.count(":") == 2 else None


def kind(d):
    u = uri_of(d)
    return u.split(":")[1] if u else None


def name_of(d):
    return (d.get("profile") or {}).get("name") or d.get("name")


def artist_obj(d):
    u, name = uri_of(d), name_of(d)
    return {"id": u.split(":")[2], "name": name} if u and name else None


def first_artist(d):
    for key in ("artists", "firstArtist"):
        items = (d.get(key) or {}).get("items") or []
        for x in items:
            a = artist_obj(x) if kind(x) == "artist" else None
            if a:
                return a
    return None


def album_obj(d, artist=None):
    a = first_artist(d) or artist
    u = uri_of(d)
    if not u or not d.get("name") or not a:
        return None
    date = d.get("date") or {}
    year = date.get("year") or (date.get("isoString") or "")[:4]
    return {"id": u.split(":")[2], "name": d["name"], "artist": a, "type": (d.get("type") or "").upper(),
            "year": int(year) if str(year).isdigit() else None}


def tracks_in(responses):
    """[(artist, album)] for every track in the responses, in order, deduped by track uri."""
    seen, out = set(), []
    for _, js in responses:
        for d in walk(js):
            u = uri_of(d)
            if kind(d) == "track" and "albumOfTrack" in d and u not in seen:
                seen.add(u)
                a = first_artist(d)
                if a:
                    out.append({"uri": u, "artist": a, "album": album_obj(d["albumOfTrack"], a)})
    return out


def artists_in(responses):
    seen, out = set(), []
    for _, js in responses:
        for d in walk(js):
            if kind(d) != "artist" or not name_of(d):
                continue
            u = uri_of(d)
            if u not in seen:
                seen.add(u)
                out.append(artist_obj(d))
    return out


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def cmd_harvest():
    """Everything the taste sync needs, as JSON on stdout. Only reads what the web player itself shows.
    stdin (optional JSON): {"known_liked": [track uris], "skip_playlists": [playlist ids]} from the DB:
    Liked Songs (newest first) is scrolled only until a known track; skipped playlists are not opened."""
    try:
        hint = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        hint = {}
    known_liked, skip = set(hint.get("known_liked") or []), set(hint.get("skip_playlists") or [])
    res = {"top_artists": [], "top_tracks": [], "liked": [], "playlists": [], "library_albums": [],
           "library_artists": []}
    with sync_playwright() as p:
        ctx = launch(p)
        try:
            if not logged_in(ctx):
                raise SystemExit("not logged in to Spotify: run `music_picks.py login`")
            page = ctx.new_page()
            home = visit(page, "https://open.spotify.com/", "home")
            me = next((js["data"]["me"]["profile"] for op, js in home if op == "profileAttributes"), None)
            if not me:
                raise SystemExit("no profile in the web player (session expired? run `music_picks.py login`)")
            user = me["username"]
            me_uri = f"spotify:user:{user}"

            # library: sidebar, scrolled until every page is loaded
            lib_items, page2 = {}, ctx.new_page()
            got = []
            page2.on("response", lambda r: got.append(r) if "pathfinder" in r.url else None)
            page2.goto("https://open.spotify.com/", wait_until="domcontentloaded")
            page2.wait_for_timeout(6000)
            page2.mouse.move(150, 500)
            for _ in range(40):
                page2.mouse.wheel(0, 3000)
                page2.wait_for_timeout(1200)
            for r in got:
                try:
                    if op_name(r) == "libraryV3" and r.ok:
                        for it in r.json()["data"]["me"]["libraryV3"]["items"]:
                            d = it["item"].get("data") or {}
                            lib_items[it["item"].get("_uri") or d.get("uri")] = d
                except Exception:  # noqa: BLE001
                    pass
            page2.close()
            playlists = []
            for uri, d in lib_items.items():
                t = d.get("__typename")
                if t == "Album":
                    al = album_obj(d)
                    if al:
                        res["library_albums"].append(al)
                elif t == "Artist":
                    a = artist_obj(d)
                    if a:
                        res["library_artists"].append(a)
                elif t == "Playlist" and uri:
                    owner = ((d.get("ownerV2") or {}).get("data") or {}).get("uri")
                    name = d.get("name") or ""
                    own = bool(user) and owner == me_uri and name != "Your Episodes"
                    if own or name.startswith("Your Top Songs"):
                        playlists.append((uri.split(":")[2], name))
            log(f"library: {len(lib_items)} items, {len(res['library_albums'])} albums, "
                f"{len(res['library_artists'])} artists, own playlists: {[n for _, n in playlists]}")

            top_a = visit(page, f"https://open.spotify.com/user/{user}/top/artists", "top-artists", 6)
            res["top_artists"] = artists_in([(op, js) for op, js in top_a if "top" in op.lower()]) or artists_in(top_a)
            top_t = visit(page, f"https://open.spotify.com/user/{user}/top/tracks", "top-tracks", 6)
            res["top_tracks"] = tracks_in([(op, js) for op, js in top_t if "top" in op.lower()]) or []
            liked = visit(page, "https://open.spotify.com/collection/tracks", "liked", 60, stop=known_liked)
            res["liked"] = tracks_in([(op, js) for op, js in liked if op.startswith("fetch")])
            for pid, name in playlists[:25]:
                if pid in skip:
                    continue
                got_pl = visit(page, f"https://open.spotify.com/playlist/{pid}", name, 15)
                tr = tracks_in([(op, js) for op, js in got_pl if op.startswith("fetchPlaylist")])
                res["playlists"].append({"id": pid, "name": name, "tracks": tr})
        finally:
            ctx.close()
    log({k: len(v) for k, v in res.items()}, "playlist tracks:", sum(len(p["tracks"]) for p in res["playlists"]))
    print(json.dumps(res))


if __name__ == "__main__":
    {"login": cmd_login, "harvest": cmd_harvest}.get((sys.argv + [""])[1], lambda: print(__doc__))()
