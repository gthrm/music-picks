"""One-time (idempotent) configuration of Lidarr, Prowlarr and Soularr for music-picks.

Needs state/lidarr.key, state/slskd.key and the Prowlarr key (PROWLARR_KEY_FILE,
default ../movie-picks/state/prowlarr.key). Writes state/lidarr_profile_id,
state/lidarr_metadata_id and soularr/config.ini.
"""
import json
import os
import pathlib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
LK = (STATE / "lidarr.key").read_text().strip()
SK = (STATE / "slskd.key").read_text().strip()
PK_FILE = pathlib.Path(os.environ.get("PROWLARR_KEY_FILE", ROOT.parent / "movie-picks" / "state" / "prowlarr.key"))
L = "http://127.0.0.1:8686/api/v1"
P = "http://127.0.0.1:9696/api/v1"
LIDARR_IP, SLSKD_IP, QBIT_IP, PROWLARR_IP = "172.31.77.13", "172.31.77.14", "172.31.77.5", "172.31.77.11"
GOOD = {"MP3-VBR-V0", "MP3-256", "MP3-320", "AAC-256", "AAC-320", "AAC-VBR", "OGG Vorbis Q8", "OGG Vorbis Q9",
        "OGG Vorbis Q10", "FLAC", "FLAC 24bit", "ALAC", "ALAC 24bit"}


def call(base, key, method, path, body=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: {e.code} {e.read()[:800].decode(errors='replace')}")


def lidarr(m, p, b=None):
    return call(L, LK, m, p, b)


def fill(schema, values):
    for f in schema["fields"]:
        if f["name"] in values:
            f["value"] = values[f["name"]]
    return schema


# --- quality profile "Good": MP3 V0/256+, AAC 256+, lossless; no upgrades ---
profs = lidarr("GET", "/qualityprofile")
prof = next((p for p in profs if p["name"] == "Good"), None)
if not prof:
    prof = json.loads(json.dumps(next(p for p in profs if p["name"] == "Any")))
    prof.pop("id")
    prof["name"] = "Good"
    for it in prof["items"]:
        for q in it.get("items") or [it]:
            q["allowed"] = q["quality"]["name"] in GOOD
        if it.get("items"):
            it["allowed"] = any(q["allowed"] for q in it["items"])
    prof["upgradeAllowed"] = False
    for it in prof["items"]:   # cutoff = whatever item (group or quality) holds FLAC
        if any(q["quality"]["name"] == "FLAC" for q in it.get("items") or [it]):
            prof["cutoff"] = it["id"] if it.get("items") else it["quality"]["id"]
    prof = lidarr("POST", "/qualityprofile", prof)
(STATE / "lidarr_profile_id").write_text(str(prof["id"]))
print("quality profile ok", prof["id"])

# --- metadata profile "Picks": albums + EPs, studio, official ---
metas = lidarr("GET", "/metadataprofile")
meta = next((m for m in metas if m["name"] == "Picks"), None)
if not meta:
    meta = json.loads(json.dumps(next(m for m in metas if m["name"] == "Standard")))
    meta.pop("id")
    meta["name"] = "Picks"
    for t in meta["primaryAlbumTypes"]:
        t["allowed"] = t["albumType"]["name"] in ("Album", "EP")
    meta = lidarr("POST", "/metadataprofile", meta)
(STATE / "lidarr_metadata_id").write_text(str(meta["id"]))
print("metadata profile ok", meta["id"])

# --- root folder ---
if not any(r["path"] == "/media/music" for r in lidarr("GET", "/rootfolder")):
    lidarr("POST", "/rootfolder", {"name": "Music", "path": "/media/music", "defaultQualityProfileId": prof["id"],
                                   "defaultMetadataProfileId": meta["id"], "defaultMonitorOption": "none",
                                   "defaultNewItemMonitorOption": "none", "defaultTags": []})
print("root folder ok")

# --- qBittorrent (torrent route, in parallel with Soulseek) ---
if not any(c["name"] == "qBittorrent" for c in lidarr("GET", "/downloadclient")):
    s = next(x for x in lidarr("GET", "/downloadclient/schema") if x["implementation"] == "QBittorrent")
    fill(s, {"host": QBIT_IP, "port": 8080, "musicCategory": "lidarr"})
    s.update(name="qBittorrent", enable=True, priority=1)
    try:
        lidarr("POST", "/downloadclient", s)
        print("download client ok")
    except SystemExit as e:
        print(f"download client SKIPPED (add {LIDARR_IP}/32 to qBittorrent's auth-bypass whitelist, rerun):", e)
else:
    print("download client ok")

# --- Prowlarr: Lidarr application (syncs the same public indexers) ---
if PK_FILE.exists():
    PK = PK_FILE.read_text().strip()
    if not any(a["name"] == "Lidarr" for a in call(P, PK, "GET", "/applications")):
        s = next(x for x in call(P, PK, "GET", "/applications/schema") if x["implementation"] == "Lidarr")
        fill(s, {"prowlarrUrl": f"http://{PROWLARR_IP}:9696", "baseUrl": f"http://{LIDARR_IP}:8686", "apiKey": LK})
        s.update(name="Lidarr", syncLevel="fullSync")
        call(P, PK, "POST", "/applications", s)
    print("prowlarr app ok")
else:
    print("no prowlarr key at", PK_FILE, "- skipping")

# --- Soularr config: wanted albums from Lidarr -> Soulseek via slskd ---
cfg = ROOT / "soularr" / "config.ini"
cfg.write_text(f"""[Lidarr]
api_key = {LK}
host_url = http://{LIDARR_IP}:8686
download_dir = /media/slskd
disable_sync = False

[Slskd]
api_key = {SK}
host_url = http://{SLSKD_IP}:5030
url_base = /
download_dir = /media/slskd
delete_searches = False
stalled_timeout = 3600
remote_queue_timeout = 600

[Release Settings]
use_most_common_tracknum = True
allow_multi_disc = True
accepted_countries = Europe,Japan,United Kingdom,United States,[Worldwide],Australia,Canada,Germany,France
skip_region_check = True
accepted_formats = CD,Digital Media,Vinyl

[Search Settings]
search_timeout = 5000
maximum_peer_queue = 50
minimum_peer_upload_speed = 0
minimum_filename_match_ratio = 0.8
allowed_filetypes = flac 24/192,flac 16/44.1,flac,mp3 320,mp3
ignored_users =
search_for_tracks = True
album_prepend_artist = True
track_prepend_artist = True
search_type = incrementing_page
number_of_albums_to_grab = 10
remove_wanted_on_failure = False
title_blacklist =
search_blacklist =
search_source = missing

[Download Settings]
download_filtering = True
use_extension_whitelist = False
extensions_whitelist = lrc,nfo,txt

[Logging]
level = INFO
log_to_file = False
""")
cfg.chmod(0o600)
print("soularr config ok")
