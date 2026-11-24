# music-picks

Daily album recommendations built from your Spotify taste, picked by Claude
(via the Claude Code CLI on a subscription), delivered by email with a
**▶ Spotify** link and a one-click **Download** button that sends the album to Lidarr.
Sibling of [movie-picks](https://github.com/gthrm/movie-picks): same flow, music instead of films.

## How it works

1. **Sync taste** (`sync`, also at the start of every `pick`): no Spotify developer app (those need
   Premium since 2026). A headless Chromium logged in to open.spotify.com once
   (`app/spotify_browser.py`, profile in `state/browser`) hands over the web player's own access
   token, and the Web API is called with it: top artists
   (4 weeks / 6 months / all time), top tracks, saved albums, saved tracks, followed
   artists and recent plays go into Postgres (`music_artists`, `music_albums`).
2. **Pick** (`app/selection.py`), diversity-first:
   - top artists are split once into distinct taste *facets* (rebuilt every 3 weeks
     or when the top artists shift);
   - every day each facet gets random anchor artists, a rotating lens (era, region,
     debut, instrumental, …) and a curator persona;
   - Claude proposes long-tail albums with probabilities, and the code samples them;
   - each candidate is resolved on MusicBrainz via Lidarr. Saved/known albums, albums
     already sent or already in Lidarr, compilations and live albums are dropped;
   - at most 1 album a day by an artist already in your Spotify.
3. **Email**: cover, why-you'd-like-it, a Spotify link and a signed download link (HMAC, 60 days).
4. **Download page** (`serve`): *Download* adds the album to Lidarr (artist is not
   monitored for anything else). **Soularr** feeds it to **slskd** (Soulseek) every
   5 minutes, and Lidarr also searches torrents through Prowlarr → qBittorrent.
   The page shows Soulseek/torrent progress. Quality: MP3 V0/256/320, AAC 256+ or lossless.

## Layout

```
app/music_picks.py      CLI: login | sync | pick | serve; Spotify, Lidarr, email, web handler
app/spotify_browser.py  headless Chromium (Playwright): one-time login, web-player token
app/selection.py        facets, prompt, sampling and filters
setup/configure_arr.py  one-time Lidarr/Prowlarr/Soularr setup
docker-compose.yml      Lidarr, slskd, Soularr (on the shared "media" network)
deploy/                 systemd units and timer
```

## Setup

1. `cp .env.example .env` and fill it in.
2. Playwright for the Spotify browser:
   ```sh
   python3 -m venv .venv && .venv/bin/pip install playwright && .venv/bin/playwright install chromium
   ```
3. Containers and config:
   ```sh
   docker compose up -d
   grep -o '<ApiKey>[^<]*' lidarr/config.xml | cut -c9- > state/lidarr.key
   python3 setup/configure_arr.py   # profiles, root folder, qBittorrent, Prowlarr app, soularr/config.ini
   docker compose restart soularr
   ```
   qBittorrent must let Lidarr in: add `172.31.77.13/32` to *Options → Web UI →
   Bypass authentication for clients in whitelisted IP subnets*, then rerun the setup script.
4. Spotify login (once): `python3 app/music_picks.py login` starts Chromium with remote debugging
   on 127.0.0.1:9222. Tunnel it (`ssh -L 9222:127.0.0.1:9222 …`), open `chrome://inspect`, add
   `localhost:9222`, *inspect* the Spotify page and log in there.
5. Web handler and the daily timer:
   ```sh
   sudo cp deploy/* /etc/systemd/system/ && sudo systemctl daemon-reload
   sudo systemctl enable --now music-picks-web.service
   python3 app/music_picks.py pick --dry-run   # writes state/last_email.html
   sudo systemctl enable --now music-picks.timer
   ```
