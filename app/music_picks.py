#!/usr/bin/env python3
"""music-picks: daily album recommendations from Spotify taste, via Claude Code
(subscription), emailed with one-click download through Lidarr (Soulseek via Soularr + torrents).

  music_picks.py login              log in to Spotify once in a headless Chromium (via chrome://inspect)
  music_picks.py sync               pull taste from Spotify into Postgres
  music_picks.py pick [--dry-run]   build today's picks and email them
  music_picks.py serve              run the download-link web handler
"""
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import pathlib
import re
import smtplib
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def read_env_file(p, env):
    if p and pathlib.Path(p).exists():
        for line in pathlib.Path(p).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")


def load_env():
    """.env of this repo; SHARED_ENV (if set there) is read first, so shared values
    (SMTP, DATABASE_URL, RECIPIENTS) can live in one place and be overridden here."""
    own = {}
    read_env_file(ROOT / ".env", own)
    env = {}
    read_env_file(own.get("SHARED_ENV"), env)
    env.update(own)
    env.setdefault("LIDARR_URL", "http://127.0.0.1:8686")
    env.setdefault("SLSKD_URL", "http://127.0.0.1:5030")
    env.setdefault("CLAUDE_MODEL", "opus")
    env.setdefault("PICKS_PER_DAY", "3")
    env.setdefault("SMTP_HOST", "smtp.gmail.com")
    env.setdefault("SMTP_PORT", "465")
    env.setdefault("WEB_PORT", "8791")
    env.setdefault("MAIL_NAME", "Music Picks")
    env["LIDARR_KEY"] = (STATE / "lidarr.key").read_text().strip()
    env["LIDARR_PROFILE"] = (STATE / "lidarr_profile_id").read_text().strip()
    env["LIDARR_METADATA"] = (STATE / "lidarr_metadata_id").read_text().strip()
    p = STATE / "slskd.key"
    env["SLSKD_KEY"] = p.read_text().strip() if p.exists() else ""
    return env


CFG = load_env()


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and")
    s = re.sub(r"\((deluxe|remaster|expanded|anniversary|bonus)[^)]*\)|\[[^\]]*\]", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def akey(artist, title):
    return f"{norm(artist)}|{norm(title)}"


# ---------------------------------------------------------------- Postgres
SCHEMA = """
create table if not exists music_artists (
  spotify_id text primary key, name text not null, genres text[], score real default 0,
  sources text[], updated_at timestamptz default now());
create table if not exists music_albums (
  key text primary key, artist text not null, title text not null, year int, spotify_id text,
  saved boolean default false, updated_at timestamptz default now());
create table if not exists music_sent (
  id bigserial primary key, sent_at timestamptz default now(), batch date not null,
  mbid text not null, artist text, title text, year int, facet text, why text, genres text[],
  known_artist boolean, recipients text[]);
create index if not exists music_sent_mbid on music_sent(mbid);
create table if not exists music_events (
  id bigserial primary key, at timestamptz default now(), mbid text not null, kind text not null, detail jsonb);
"""
_schema_done = False


def db():
    """Short-lived connection: never keep one open across slow work (Neon bills compute while connected)."""
    global _schema_done
    import psycopg
    conn = psycopg.connect(CFG["DATABASE_URL"], autocommit=True, connect_timeout=20)
    if not _schema_done:
        conn.execute(SCHEMA)
        _schema_done = True
    return conn


def event(mbid, kind, **detail):
    try:
        import psycopg.types.json as pj
        with db() as c:
            c.execute("insert into music_events(mbid, kind, detail) values (%s,%s,%s)", (mbid, kind, pj.Jsonb(detail)))
    except Exception as e:  # noqa: BLE001
        log("event log failed:", e)


# ---------------------------------------------------------------- HTTP helpers
def http_json(url, method="GET", body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def lidarr(method, path, body=None):
    return http_json(CFG["LIDARR_URL"] + "/api/v1" + path, method,
                     json.dumps(body).encode() if body is not None else None,
                     {"X-Api-Key": CFG["LIDARR_KEY"], "Content-Type": "application/json"}, timeout=120)


def slskd(path):
    return http_json(CFG["SLSKD_URL"] + "/api/v0" + path, headers={"X-API-Key": CFG["SLSKD_KEY"]}, timeout=20)


# ---------------------------------------------------------------- Spotify
# No developer app: a headless Chromium logged in to open.spotify.com (spotify_browser.py, in .venv with
# Playwright) hands over the web player's own access token; the Web API is then called with it.
TOKEN_FILE = STATE / "spotify_web_token.json"
VENV_PY = ROOT / ".venv" / "bin" / "python"


def spotify_access_token(force=False):
    if not force and TOKEN_FILE.exists():
        t = json.loads(TOKEN_FILE.read_text())
        if t.get("expires_at", 0) > time.time() + 60:
            return t["token"]
    out = subprocess.run([str(VENV_PY), "-I", str(ROOT / "app" / "spotify_browser.py"), "token"],
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"spotify token: {(out.stderr or out.stdout).strip()[-500:]}")
    t = json.loads(out.stdout.strip().splitlines()[-1])
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(t))
    tmp.chmod(0o600)
    tmp.replace(TOKEN_FILE)
    return t["token"]


def spotify(path, params=None):
    url = path if path.startswith("https://") else "https://api.spotify.com/v1" + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    force = False
    for attempt in range(5):
        try:
            return http_json(url, headers={"Authorization": "Bearer " + spotify_access_token(force)})
        except urllib.error.HTTPError as e:
            if e.code == 401 and not force:
                force = True
                continue
            if e.code == 429 and attempt < 4:
                time.sleep(min(int(e.headers.get("Retry-After") or 5), 60))
                continue
            raise


def spotify_pages(path, params, max_items, key=None):
    out, page = [], spotify(path, params)
    while page:
        body = page[key] if key else page
        out += body.get("items") or []
        if len(out) >= max_items or not body.get("next"):
            break
        page = spotify(body["next"])
    return out[:max_items]


def spotify_album_url(artist, title):
    """Direct album link via search; falls back to a search URL."""
    try:
        res = spotify("/search", {"q": f"album:{title} artist:{artist}", "type": "album", "limit": 5})
        for a in res["albums"]["items"]:
            if any(norm(x["name"]) == norm(artist) for x in a["artists"]) and (
                    norm(a["name"]) == norm(title) or norm(a["name"]).startswith(norm(title))):
                return a["external_urls"]["spotify"]
    except Exception as e:  # noqa: BLE001
        log("spotify search failed:", e)
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{artist} {title}")


def sync_spotify():
    """Taste = top artists (3 ranges), top tracks, saved albums/tracks, followed artists, recent plays.
    Known albums (saved, or holding a saved/top track) are never recommended."""
    artists, albums, ok = {}, {}, 0

    def add_artist(a, w, src):
        if not a.get("id"):
            return
        x = artists.setdefault(a["id"], {"name": a["name"], "genres": [], "score": 0.0, "sources": set()})
        x["score"] += w
        x["sources"].add(src)
        if a.get("genres"):
            x["genres"] = a["genres"]

    def add_album(al, saved):
        if not al or not al.get("artists") or al.get("album_type") == "compilation":
            return
        k = akey(al["artists"][0]["name"], al["name"])
        year = int(al["release_date"][:4]) if (al.get("release_date") or "")[:4].isdigit() else None
        x = albums.setdefault(k, {"artist": al["artists"][0]["name"], "title": al["name"], "year": year,
                                  "spotify_id": al.get("id"), "saved": False})
        x["saved"] |= saved

    def section(name, fn):
        nonlocal ok
        try:
            fn()
            ok += 1
            log(f"spotify {name} ok")
        except Exception as e:  # noqa: BLE001
            log(f"spotify {name} failed:", e)

    def top_artists():
        for rng, w in (("short_term", 3.0), ("medium_term", 2.0), ("long_term", 2.0)):
            for i, a in enumerate(spotify_pages("/me/top/artists", {"time_range": rng, "limit": 50}, 100)):
                add_artist(a, w * (1 - i / 150), "top-" + rng.split("_")[0])

    def top_tracks():
        for rng in ("short_term", "medium_term", "long_term"):
            for t in spotify_pages("/me/top/tracks", {"time_range": rng, "limit": 50}, 100):
                add_artist(t["artists"][0], 0.4, "top-track")
                add_album(t.get("album"), False)

    def saved_albums():
        for it in spotify_pages("/me/albums", {"limit": 50}, 3000):
            add_album(it["album"], True)
            for a in it["album"]["artists"][:1]:
                add_artist(a, 1.0, "saved-album")

    def saved_tracks():
        for it in spotify_pages("/me/tracks", {"limit": 50}, 5000):
            add_artist(it["track"]["artists"][0], 0.15, "saved-track")
            add_album(it["track"].get("album"), False)

    def followed():
        for a in spotify_pages("/me/following", {"type": "artist", "limit": 50}, 2000, key="artists"):
            add_artist(a, 1.0, "followed")

    def recent():
        for it in spotify_pages("/me/player/recently-played", {"limit": 50}, 50):
            add_artist(it["track"]["artists"][0], 0.2, "recent")

    for name, fn in [("top artists", top_artists), ("top tracks", top_tracks), ("saved albums", saved_albums),
                     ("saved tracks", saved_tracks), ("followed", followed), ("recently played", recent)]:
        section(name, fn)
    if not ok or not artists:
        raise RuntimeError("Spotify sync got nothing (session expired? run `music_picks.py login`)")
    with db() as c, c.cursor() as cur:
        cur.execute("update music_artists set score = 0")
        cur.executemany(
            "insert into music_artists(spotify_id, name, genres, score, sources) values (%s,%s,%s,%s,%s) "
            "on conflict (spotify_id) do update set name=excluded.name, "
            "genres=coalesce(nullif(excluded.genres, '{}'), music_artists.genres), score=excluded.score, "
            "sources=excluded.sources, updated_at=now()",
            [(i, a["name"], a["genres"], round(a["score"], 3), sorted(a["sources"])) for i, a in artists.items()])
        cur.executemany(
            "insert into music_albums(key, artist, title, year, spotify_id, saved) values (%s,%s,%s,%s,%s,%s) "
            "on conflict (key) do update set saved = music_albums.saved or excluded.saved, updated_at=now()",
            [(k, a["artist"], a["title"], a["year"], a["spotify_id"], a["saved"]) for k, a in albums.items()])
    log(f"spotify sync: {len(artists)} artists, {len(albums)} albums ({sum(a['saved'] for a in albums.values())} saved)")


def load_taste():
    with db() as c:
        artists = [{"id": r[0], "name": r[1], "genres": r[2] or [], "score": r[3], "sources": r[4] or []}
                   for r in c.execute("select spotify_id, name, genres, score, sources from music_artists "
                                      "order by score desc")]
        albums = {r[0]: {"artist": r[1], "title": r[2], "year": r[3], "saved": r[4]}
                  for r in c.execute("select key, artist, title, year, saved from music_albums")}
    return artists, albums


def load_history():
    with db() as c:
        return [{"mbid": r[0], "artist": r[1], "title": r[2], "year": r[3]}
                for r in c.execute("select mbid, artist, title, year from music_sent order by sent_at")]


# ---------------------------------------------------------------- Claude
class ClaudeLimit(RuntimeError):
    """Subscription usage limit hit; worth retrying later."""


def run_claude(prompt, system=None):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    out = subprocess.run(
        [os.path.expanduser("~/.local/bin/claude"), "-p", prompt, "--output-format", "json",
         "--model", CFG["CLAUDE_MODEL"], "--tools", "", "--strict-mcp-config", "--no-session-persistence",
         *(["--append-system-prompt", system] if system else [])],
        capture_output=True, text=True, timeout=1200, env=env, cwd=STATE)
    try:
        meta = json.loads(out.stdout)
    except ValueError:
        meta = {}
    err = str(meta.get("result", "")).lower() if meta.get("is_error") else ""
    if "authenticat" in err or "login" in err or meta.get("api_error_status") in (401, 403):
        raise RuntimeError(f"claude auth failed (run `claude auth login`): {meta.get('result')}")
    if meta.get("api_error_status") == 429 or "usage limit" in err or "rate limit" in err:
        raise ClaudeLimit(meta.get("result", "usage limit"))
    if out.returncode != 0 or meta.get("is_error") or "result" not in meta:
        raise RuntimeError(f"claude failed: {out.stderr[-2000:]} {out.stdout[-2000:]}")
    return json.loads(re.search(r"\[.*\]", meta["result"], re.S).group(0))


# ---------------------------------------------------------------- Lidarr
def cover_url(a):
    for i in a.get("images", []):
        if i.get("coverType") == "cover" and i.get("remoteUrl"):
            return i["remoteUrl"]
    return a.get("remoteCover") or ""


def lookup_album(artist, title, year=None):
    res = lidarr("GET", "/album/lookup?term=" + urllib.parse.quote(f"{artist} {title}")) or []
    good = []
    for a in res:
        if norm(a["artist"]["artistName"]) != norm(artist):
            continue
        t, want = norm(a["title"]), norm(title)
        if not t or not (t == want or t.startswith(want) or want.startswith(t)):
            continue
        y = int((a.get("releaseDate") or "0")[:4] or 0)
        if year and y and abs(y - int(year)) > 2:
            continue
        good.append(a)
    good.sort(key=lambda a: (a.get("albumType") not in ("Album", "EP"), norm(a["title"]) != norm(title),
                             -((a.get("ratings") or {}).get("votes") or 0)))
    return good[0] if good else None


def lookup_mbid(mbid):
    res = lidarr("GET", "/album/lookup?term=" + urllib.parse.quote("lidarr:" + mbid)) or []
    return next((a for a in res if a["foreignAlbumId"] == mbid), res[0] if res else None)


def lidarr_album(mbid):
    r = lidarr("GET", f"/album?foreignAlbumId={mbid}") or []
    return r[0] if r else None


def add_to_lidarr(mbid):
    """Add the album (artist unmonitored for anything else) and search. Soularr then tries Soulseek,
    Lidarr tries torrents via Prowlarr."""
    al = lidarr_album(mbid)
    if al:
        if not al["monitored"]:
            lidarr("PUT", "/album/monitor", {"albumIds": [al["id"]], "monitored": True})
        lidarr("POST", "/command", {"name": "AlbumSearch", "albumIds": [al["id"]]})
        return al
    a = lookup_mbid(mbid)
    if not a:
        raise RuntimeError(f"album {mbid} not found in metadata")
    a.update(monitored=True, addOptions={"searchForNewAlbum": True})
    existing = lidarr("GET", f"/artist?mbId={a['artist']['foreignArtistId']}") or []
    if existing:
        a["artist"], a["artistId"] = existing[0], existing[0]["id"]
    else:
        a["artist"].update(qualityProfileId=int(CFG["LIDARR_PROFILE"]), metadataProfileId=int(CFG["LIDARR_METADATA"]),
                           rootFolderPath="/media/music", monitored=True, monitorNewItems="none",
                           addOptions={"monitor": "none", "searchForMissingAlbums": False})
    return lidarr("POST", "/album", a)


def album_tracks(a):
    rel = next((r for r in a.get("releases") or [] if r.get("monitored")), None) or (a.get("releases") or [{}])[0]
    return rel.get("trackCount") or 0


# ---------------------------------------------------------------- links
def sign(mbid, exp):
    return hmac.new(CFG["LINK_SECRET"].encode(), f"{mbid}:{exp}".encode(), hashlib.sha256).hexdigest()[:24]


def download_link(mbid):
    exp = int(time.time()) + 60 * 86400
    return f"{CFG['PUBLIC_URL'].rstrip('/')}/d/{mbid}/{exp}/{sign(mbid, exp)}"


def verify(mbid, exp, sig):
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sign(mbid, exp), sig)


# ---------------------------------------------------------------- email
E = html.escape
ACC = "#22c55e"


def fmt_duration(ms):
    m = round((ms or 0) / 60000)
    return f"{m} min" if m else ""


def meta_line(p):
    return " · ".join(x for x in [str(p["year"] or ""), p.get("type") if p.get("type") not in ("Album", None) else "",
                                   f"{p['tracks']} tracks" if p.get("tracks") else "", fmt_duration(p.get("duration")),
                                   ", ".join(p["genres"])] if x)


def render_email(picks, date):
    cards = []
    for p in picks:
        cover = (f'<img src="{E(p["cover"])}" width="140" alt="{E(p["title"])} cover" '
                 f'style="display:block;width:140px;max-width:140px;height:auto;border-radius:6px;border:0">'
                 if p["cover"] else "")
        known = (f'<span style="color:#78716c;font-weight:400;text-transform:none;letter-spacing:0"> · artist you know</span>'
                 if p.get("known_artist") else "")
        mb = f"https://musicbrainz.org/release-group/{p['mbid']}"
        cards.append(f"""
<tr><td style="padding:0 0 16px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#1c1917;border-radius:10px">
  <tr>
   <td valign="top" width="140" style="padding:16px 0 16px 16px;width:140px">{cover}</td>
   <td valign="top" style="padding:16px">
    <div style="font-size:11px;color:{ACC};text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:4px">{E(p.get('facet', ''))}{known}</div>
    <div style="font-size:20px;line-height:1.25;font-weight:700;color:#fafaf9">{E(p['title'])}</div>
    <div style="font-size:15px;color:#d6d3d1;margin-top:2px">{E(p['artist'])}</div>
    <div style="font-size:12px;color:#78716c;margin-top:6px;text-transform:uppercase;letter-spacing:.04em">{E(meta_line(p))}</div>
    <div style="font-size:14px;line-height:1.5;color:#e7e5e4;margin-top:10px">{E(p['why'])}</div>
    <div style="margin-top:14px">
     <a href="{E(p['spotify'])}" style="display:inline-block;background:{ACC};color:#052e16;text-decoration:none;font-weight:700;font-size:14px;padding:9px 16px;border-radius:6px">▶ Spotify</a>
     &nbsp;<a href="{E(download_link(p['mbid']))}" style="display:inline-block;border:1px solid #44403c;color:#e7e5e4;text-decoration:none;font-size:14px;padding:8px 14px;border-radius:6px">⬇ Download</a>
     &nbsp;&nbsp;<a href="{mb}" style="color:#a8a29e;font-size:13px">MusicBrainz</a>
    </div>
   </td>
  </tr>
 </table>
</td></tr>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark"></head>
<body style="margin:0;padding:0;background:#0c0a09">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0c0a09">
 <tr><td align="center" style="padding:24px 12px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:620px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
   <tr><td style="padding:0 4px 18px">
    <div style="font-size:12px;color:{ACC};letter-spacing:.12em;text-transform:uppercase;font-weight:700">Music Picks</div>
    <div style="font-size:24px;color:#fafaf9;font-weight:700;margin-top:4px">{len(picks)} albums for today</div>
    <div style="font-size:13px;color:#78716c;margin-top:4px">{date:%A, %d %B %Y} · one from each side of your Spotify</div>
   </td></tr>
   {''.join(cards)}
   <tr><td style="padding:8px 4px;font-size:12px;color:#57534e">“Download” adds the album to Lidarr: Soulseek first, torrents as a fallback.</td></tr>
  </table>
 </td></tr>
</table></body></html>"""


def render_text(picks):
    out = []
    for p in picks:
        out += [f"{p['artist']} — {p['title']} ({p['year']})", p["why"], "Spotify: " + p["spotify"],
                "Download: " + download_link(p["mbid"]), ""]
    return "\n".join(out)


def send_email(subject, html_body, text_body):
    rcpts = [r.strip() for r in CFG.get("RECIPIENTS", "").split(",") if r.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    name, addr = parseaddr(CFG["MAIL_FROM"])
    msg["From"] = formataddr((CFG.get("MAIL_NAME") or name, addr))
    msg["To"] = ", ".join(rcpts)
    msg["Message-ID"] = make_msgid(domain=CFG["MAIL_FROM"].rsplit("@", 1)[-1].strip(">"))
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(CFG["SMTP_HOST"], int(CFG["SMTP_PORT"]), timeout=60) as s:
        s.login(CFG["SMTP_USER"], CFG["SMTP_PASSWORD"].replace(" ", ""))
        s.send_message(msg)
    log("email sent to", rcpts)
    return rcpts


def write_metrics(ok):
    """node_exporter textfile metrics for Grafana alerts (METRICS_FILE; unset = off)."""
    path = os.environ.get("METRICS_FILE") or CFG.get("METRICS_FILE")
    if not path:
        return
    path = pathlib.Path(path)
    last = 0
    try:
        m = re.search(r"^music_picks_last_success_timestamp_seconds (\S+)$", path.read_text(), re.M)
        last = float(m.group(1)) if m else 0
    except OSError:
        pass
    now = time.time()
    if ok:
        last = now
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# HELP music_picks_last_success_timestamp_seconds Last successful daily email.\n"
                   "# TYPE music_picks_last_success_timestamp_seconds gauge\n"
                   f"music_picks_last_success_timestamp_seconds {last:.0f}\n"
                   "# HELP music_picks_last_run_ok 1 if the last daily run sent the email, else 0.\n"
                   "# TYPE music_picks_last_run_ok gauge\n"
                   f"music_picks_last_run_ok {1 if ok else 0}\n"
                   f"music_picks_last_run_timestamp_seconds {now:.0f}\n")
    tmp.chmod(0o644)
    tmp.replace(path)


def cmd_pick(dry_run=False):
    sync_spotify()
    artists, albums = load_taste()
    history = load_history()
    log(f"artists: {len(artists)}, known albums: {len(albums)}, sent before: {len(history)}")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import selection
    picks = selection.choose(artists, albums, history)
    if not picks:
        raise SystemExit("no picks produced")
    for p in picks:
        p["spotify"] = spotify_album_url(p["artist"], p["title"])
    today = dt.date.today()
    body = render_email(picks, today)
    out = STATE / "last_email.html"
    out.write_text(body)
    out.chmod(0o600)
    subject = f"{len(picks)} albums today: " + " · ".join(f"{p['artist']} — {p['title']}" for p in picks[:3])
    log(subject)
    if dry_run or not CFG.get("SMTP_PASSWORD"):
        log("dry run / no SMTP_PASSWORD: saved to", out)
        return
    rcpts = send_email(subject, body, render_text(picks))
    with db() as c, c.cursor() as cur:
        cur.executemany(
            "insert into music_sent(batch, mbid, artist, title, year, facet, why, genres, known_artist, recipients) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(today, p["mbid"], p["artist"], p["title"], p["year"], p.get("facet"), p["why"], p["genres"],
              p.get("known_artist"), rcpts) for p in picks])


def cmd_pick_with_retry(dry_run=False, tries=17, wait_min=30):
    for i in range(tries):
        try:
            return cmd_pick(dry_run)
        except Exception as e:  # selection.py imports this module separately -> match by class name
            if type(e).__name__ != "ClaudeLimit" or i == tries - 1:
                raise
            log(f"Claude usage limit ({e}); retrying in {wait_min} min ({i + 1}/{tries - 1})")
            time.sleep(wait_min * 60)


# ---------------------------------------------------------------- web
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Music Picks</title>{refresh}
<style>
:root{{--bg:#0c0a09;--card:#1c1917;--fg:#fafaf9;--mut:#a8a29e;--acc:#22c55e}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:520px;margin:0 auto;padding:24px 16px}}
.card{{background:var(--card);border-radius:12px;padding:16px;display:flex;gap:16px;flex-wrap:wrap}}
img{{width:140px;border-radius:8px;flex:none;align-self:flex-start}}
h1{{font-size:20px;margin:0}} .a{{color:#d6d3d1}} .m{{color:var(--mut);font-size:14px}}
button{{margin-top:14px;background:var(--acc);color:#052e16;border:0;border-radius:8px;padding:12px 18px;font-weight:700;font-size:16px;cursor:pointer}}
.s{{margin-top:14px;font-weight:600}} .bar{{height:8px;background:#292524;border-radius:4px;margin-top:8px;overflow:hidden}}
.bar i{{display:block;height:100%;background:var(--acc)}}
</style></head><body><main><div class="card">{cover}<div style="flex:1;min-width:200px">
<h1>{title}</h1><div class="a">{artist}</div><div class="m">{meta}</div>{body}</div></div></main></body></html>"""


def slskd_progress(title):
    """Bytes done / total for slskd transfers whose folder name contains the album title."""
    if not CFG["SLSKD_KEY"]:
        return None
    try:
        done = total = 0
        want = norm(title)
        for user in slskd("/transfers/downloads") or []:
            for d in user.get("directories", []):
                if want and want in norm(d.get("directory", "").replace("\\", "/").rsplit("/", 1)[-1]):
                    for f in d.get("files", []):
                        total += f.get("size") or 0
                        done += f.get("bytesTransferred") or 0
        return (done, total) if total else None
    except Exception:  # noqa: BLE001
        return None


def album_status(al):
    st = al.get("statistics") or {}
    have, total = st.get("trackFileCount") or 0, st.get("trackCount") or st.get("totalTrackCount") or 0
    if total and have >= total:
        return f'<div class="s">✓ Downloaded — in the library ({(st.get("sizeOnDisk") or 0) / 1e6:.0f} MB)</div>', False
    bars = []
    q = lidarr("GET", f"/queue/details?albumIds={al['id']}") or []
    for r in q[:1]:
        pct = 100 * (1 - (r.get("sizeleft") or 0) / r["size"]) if r.get("size") else 0
        bars.append(f'<div class="s">Torrent: {pct:.0f}%</div><div class="m">{E(r.get("title", ""))}</div>'
                    f'<div class="bar"><i style="width:{pct:.0f}%"></i></div>')
    sp = slskd_progress(al["title"])
    if sp:
        pct = 100 * sp[0] / sp[1]
        bars.append(f'<div class="s">Soulseek: {pct:.0f}%</div><div class="bar"><i style="width:{pct:.0f}%"></i></div>')
    if bars:
        return "".join(bars) + (f'<div class="m">{have}/{total} tracks imported</div>' if have else ""), True
    if have:
        return f'<div class="s">{have}/{total} tracks imported, looking for the rest…</div>', True
    added = (al.get("artist") or {}).get("added") or ""
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(added.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        age = 0
    if age < 1800:
        return ('<div class="s">Searching Soulseek and torrent trackers…</div>'
                '<div class="m">Soulseek is checked every 5 minutes; this page refreshes by itself.</div>'), True
    return ('<div class="s">Not found yet.</div>'
            '<div class="m">Soularr keeps retrying Soulseek and Lidarr keeps watching the trackers.</div>'), False


class Handler(BaseHTTPRequestHandler):
    server_version = "music-picks"

    def log_message(self, fmt, *args):
        log("web", self.address_string(), fmt % args)

    def reply(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def parse(self):
        m = re.fullmatch(r"/d/([0-9a-f-]{36})/(\d+)/([0-9a-f]{24})", urllib.parse.urlparse(self.path).path)
        if not m or not verify(*m.groups()):
            self.reply(404, "<h1>Link expired or invalid</h1>")
            return None
        return m.group(1)

    def page(self, a, body, refresh=False):
        meta = meta_line({"year": (a.get("releaseDate") or "")[:4], "type": a.get("albumType"),
                          "tracks": album_tracks(a), "duration": a.get("duration"), "genres": (a.get("genres") or [])[:3]})
        cover = f'<img src="{E(cover_url(a))}" alt="">' if cover_url(a) else ""
        self.reply(200, PAGE.format(refresh='<meta http-equiv="refresh" content="20">' if refresh else "",
                                    cover=cover, title=E(a["title"]), artist=E((a.get("artist") or {}).get("artistName", "")),
                                    meta=E(meta), body=body))

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, "ok", "text/plain")
        mbid = self.parse()
        if mbid is None:
            return
        try:
            al = lidarr_album(mbid)
            if al and al.get("monitored"):
                body, refresh = album_status(al)
                return self.page(al, body, refresh)
            a = al or lookup_mbid(mbid)
            if not a:
                return self.reply(404, "<h1>Album not found</h1>")
            self.page(a, '<form method="post"><button type="submit">⬇ Download</button></form>'
                         '<div class="m" style="margin-top:8px">Soulseek first, torrents as a fallback; MP3 V0/320 or FLAC.</div>')
        except Exception as e:  # noqa: BLE001
            log("web error", e)
            self.reply(502, "<h1>Lidarr is unavailable</h1>")

    def do_POST(self):
        mbid = self.parse()
        if mbid is None:
            return
        try:
            al = lidarr_album(mbid)
            if not (al and al.get("monitored")):
                al = add_to_lidarr(mbid)
                log("added to lidarr:", (al.get("artist") or {}).get("artistName"), "—", al.get("title"))
                event(mbid, "download", title=al.get("title"))
            self.reply(303, "", headers={"Location": self.path})
        except Exception as e:  # noqa: BLE001
            log("web error", e)
            self.reply(502, "<h1>Could not add the album to Lidarr</h1>")


def cmd_serve():
    port = int(CFG["WEB_PORT"])
    log("listening on 127.0.0.1:%d" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        cmd_serve()
    elif cmd == "login":
        os.execv(str(VENV_PY), [str(VENV_PY), "-I", str(ROOT / "app" / "spotify_browser.py"), "login"])
    elif cmd == "sync":
        sync_spotify()
    elif cmd == "pick":
        dry = "--dry-run" in sys.argv
        try:
            cmd_pick_with_retry(dry_run=dry)
        except BaseException:
            if not dry:
                write_metrics(ok=False)
            raise
        if not dry:
            write_metrics(ok=True)
    else:
        print(__doc__)
