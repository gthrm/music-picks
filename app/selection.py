"""Diversity-first album selection (same approach as movie-picks):

1. never repeat: code filters saved/known albums, already sent, already in Lidarr
2. taste facets are built from Spotify artists ONCE (rebuilt every 21 days or when the top artists shift)
   and stored in Postgres; each day code picks random anchor artists per facet, a rotating lens and persona
3. verbalized sampling: 6 tail candidates per facet with probabilities (<0.10); code samples
4. one album per taste direction per day; few albums by artists already in their Spotify; one artist /
   decade / genre can't dominate
"""
import datetime as dt
import random

import music_picks as mp

FACET_MAX_AGE_DAYS = 21
MIN_FACETS, MAX_FACETS = 4, 12   # taste directions; every day one album per direction

LENSES = [
    "made before 1985", "released in the last 3 years", "from outside the UK and US", "a debut album",
    "an instrumental record", "sung in a language other than English", "the 1990s", "the 2000s",
    "under 40 minutes", "from a small independent label", "Latin American, African or Asian music",
    "something warmer or lighter than their usual", "a cult record that never charted",
    "European (non-UK) scenes", "from a regional scene or city they probably don't know",
]
PERSONAS = [
    "You are a record-store clerk with deep, eclectic, unpretentious taste across every genre.",
    "You are a music journalist who champions overlooked albums over the usual canon.",
    "You are a crate-digging DJ who knows obscure records from every continent.",
    "You are a college-radio music director who loves connecting scenes and eras.",
    "You are a label A&R veteran who knows the deep catalogues behind famous artists' influences.",
]


def as_year(v):
    """Claude's or Lidarr's year as an int, 0 when it isn't one."""
    try:
        y = int(str(v).strip()[:4])
        return y if 1000 <= y <= 2999 else 0
    except (TypeError, ValueError):
        return 0


def release_year(a):
    return as_year((a.get("releaseDate") or "")[:4])


def ensure_tables(c):
    c.execute("""create table if not exists music_facets (
        id serial primary key, name text not null, artists text[] not null,
        top_signature text, created_at timestamptz default now())""")
    c.execute("""create table if not exists music_candidates (
        id bigserial primary key, batch date not null, facet text, lens text, artist text, title text,
        year int, mbid text, probability real, picked boolean default false, reason text)""")


def describe(a):
    g = ", ".join(a["genres"][:3])
    return f"{a['name']}" + (f" [{g}]" if g else "")


def get_facets(artists):
    top = [a for a in artists if a["score"] > 0][:150]
    signature = sorted(a["name"] for a in top[:40])
    with mp.db() as c:
        ensure_tables(c)
        row = c.execute("select created_at, top_signature from music_facets order by created_at desc limit 1").fetchone()
        if row:
            old = set((row[1] or "").split("\x1f"))
            fresh = (dt.datetime.now(dt.timezone.utc) - row[0]).days < FACET_MAX_AGE_DAYS
            if fresh and signature and len(old & set(signature)) >= 0.7 * len(signature):
                return [{"name": r[0], "artists": r[1]}
                        for r in c.execute("select name, artists from music_facets where created_at=%s", (row[0],))]
    listing = "\n".join(describe(a) for a in top)
    facets = mp.run_claude(f"""Here are the artists one person listens to most on Spotify (strongest first;
no genre tags are given, infer them from the artists):
{listing}

Split these artists into ALL the distinct taste directions they actually have: {MIN_FACETS}-{MAX_FACETS} facets,
as many as are genuinely different (don't merge unrelated styles, don't split one style in two), as
different from each other as possible
(genre, era, region, mood, energy). Every artist goes into exactly one facet; each facet needs at least 3 artists.
Facet names: 2-4 words, concrete (e.g. "Dusty 70s Soul", "Nordic Ambient Techno").
Respond with ONLY JSON: [{{"facet": "name", "artists": ["Artist", ...]}}, ...]""")
    bad = [f for f in facets if not (isinstance(f, dict) and isinstance(f.get("facet"), str) and f["facet"].strip()
                                     and isinstance(f.get("artists"), list) and f["artists"])]
    if bad or not facets:
        raise RuntimeError(f"claude returned malformed facets: {str(bad or facets)[:300]}")
    now = dt.datetime.now(dt.timezone.utc)
    with mp.db() as c, c.transaction():   # all or nothing: a partial set would be cached for 3 weeks
        for f in facets:
            c.execute("insert into music_facets(name, artists, top_signature, created_at) values (%s,%s,%s,%s)",
                      (f["facet"], [str(x) for x in f["artists"]], "\x1f".join(signature), now))
    mp.log(f"facets regenerated: {[f['facet'] for f in facets]}")
    return [{"name": f["facet"], "artists": f["artists"]} for f in facets]


def ask(facets, artists, albums, history, rng, per_facet=6):
    by_name = {mp.norm(a["name"]): a for a in artists}
    saved_by_artist = {}
    for al in albums.values():
        if al["saved"]:
            saved_by_artist.setdefault(mp.norm(al["artist"]), []).append(al["title"])
    lenses = rng.sample(LENSES, k=min(len(LENSES), len(facets)))
    in_facets, blocks = set(), []
    for f, lens in zip(facets, lenses):
        in_facets |= {mp.norm(x) for x in f["artists"]}
        anchors = rng.sample(f["artists"], k=min(len(f["artists"]), rng.randint(4, 7)))
        desc = []
        for x in anchors:
            a = by_name.get(mp.norm(x))
            s = describe(a) if a else x
            fav = saved_by_artist.get(mp.norm(x))
            desc.append(s + (f" (saved: {rng.choice(fav)})" if fav else ""))
        f["lens"] = lens
        blocks.append(f'- Facet "{f["name"]}". Anchors: {"; ".join(desc)}. Today\'s lens: {lens}.')
    rng.shuffle(blocks)
    recent = [a["name"] for a in artists if "top-month" in a["sources"] and mp.norm(a["name"]) not in in_facets][:15]
    known = [a["name"] for a in artists[:250]]
    sent = [f"{h['artist']} — {h['title']}" for h in history[-300:]]
    prompt = f"""Recommend ALBUMS for one person, facet by facet, based on their Spotify listening (genres are not given: infer them from the artists). For each facet the
anchors are artists they love; the lens is a HARD constraint for that facet today.
{chr(10).join(blocks)}

{('On heavy rotation lately (strong current signal, use as extra anchors): ' + '; '.join(recent)) if recent else ''}

Artists already in their library (prefer artists NOT on this list; at most one candidate per facet may be by them,
and then only a lesser-known album of theirs): {'; '.join(known)}

Never suggest these, already recommended before: {'; '.join(sent) or 'none yet'}

For EACH facet give {per_facet} candidates sampled from the TAILS of the distribution: every candidate's
probability (how typical a pick it would be for this facet) must be below 0.10. No "greatest albums of all time"
canon. Only real, released studio albums or EPs (no compilations, live albums or singles) with exact artist
names, exact album titles and original release years, as listed on MusicBrainz.

Respond with ONLY JSON:
[{{"facet": "facet name", "candidates": [{{"artist": "Artist", "title": "Album title", "year": 1999,
   "country": "Country", "probability": 0.05, "why": "..."}}]}}]

Rules for "why" (max 30 words, plain English, no hype): one concrete sentence on what the record sounds like
(instruments, production, mood, scene); optionally "Close to <anchor>" naming ONE anchor artist and the shared
quality. Never claim what they felt. Only state facts you are certain about."""
    return mp.run_claude(prompt, system=rng.choice(PERSONAS))


def choose(artists, albums, history):
    n = int(mp.CFG.get("PICKS_PER_DAY") or 0)   # 0 = one album for every taste direction
    rng = random.Random()
    today = dt.date.today()
    known_artists = {mp.norm(a["name"]) for a in artists}
    sent_mbids = {h["mbid"] for h in history}
    sent_keys = {mp.akey(h["artist"], h["title"]) for h in history}
    in_lidarr = {a["foreignAlbumId"] for a in mp.lidarr("GET", "/album") or [] if a.get("monitored")}

    facets = get_facets(artists)
    if n:   # optional cap (PICKS_PER_DAY): least recently used directions first
        last_used = {h["facet"]: i for i, h in enumerate(history) if h.get("facet")}
        rng.shuffle(facets)
        facets.sort(key=lambda f: last_used.get(f["name"], -1))
        facets = facets[:n]
    n = len(facets)
    max_known = max(1, n // 4)
    cap = max(2, (n + 2) // 3)   # at most this many picks from one decade / one first genre
    mp.log(f"facets today ({n}): {[f['name'] for f in facets]}")
    answer = ask(facets, artists, albums, history, rng)
    lens_of = {f["name"]: f.get("lens", "") for f in facets}
    picks, logrows = [], []

    def resolve(cand):
        artist, title = cand.get("artist", ""), cand.get("title", "")
        k = mp.akey(artist, title)
        if k in albums:
            return None, "in spotify library"
        if k in sent_keys:
            return None, "already sent"
        if any(mp.norm(p["artist"]) == mp.norm(artist) for p in picks):
            return None, "same artist"
        try:
            a = mp.lookup_album(artist, title, cand.get("year"))
        except Exception as e:  # noqa: BLE001
            return None, f"lookup error {e}"
        if not a:
            return None, "not found"
        secondary = {x if isinstance(x, str) else x.get("name") for x in a.get("secondaryTypes") or []}
        if a.get("albumType") not in ("Album", "EP") or secondary - {"Studio"}:
            return None, f"type {a.get('albumType')} {a.get('secondaryTypes')}"
        if mp.akey(a["artist"]["artistName"], a["title"]) in albums:
            return None, "in spotify library"
        mbid = a["foreignAlbumId"]
        if mbid in sent_mbids:
            return None, "already sent"
        if mbid in in_lidarr:
            return None, "in lidarr"
        if any(p["mbid"] == mbid for p in picks):
            return None, "duplicate"
        return a, "ok"

    def fits(a):
        dec = (release_year(a) or 0) // 10
        g = mp.norm((a.get("genres") or ["?"])[0])
        known = mp.norm(a["artist"]["artistName"]) in known_artists
        return (sum(as_year(p["year"]) // 10 == dec for p in picks if as_year(p["year"])) < cap
                and sum(mp.norm((p["genres"] or ["?"])[0]) == g for p in picks) < cap
                and not (known and sum(p["known_artist"] for p in picks) >= max_known))

    groups = [g for g in answer if g.get("candidates")]
    rng.shuffle(groups)
    for g in groups:
        if len(picks) >= n:
            break
        cands = list(g["candidates"])
        w = [max(float(x.get("probability") or 0.05), 0.005) for x in cands]
        order = []
        while cands:   # sample order from the verbalized distribution
            i = rng.choices(range(len(cands)), weights=w)[0]
            order.append(cands.pop(i))
            w.pop(i)
        resolved = []
        for cand in order:
            a, why = resolve(cand)
            logrows.append([today, g.get("facet"), lens_of.get(g.get("facet"), ""), cand.get("artist"), cand.get("title"),
                            cand.get("year"), a and a["foreignAlbumId"], cand.get("probability"), False, why])
            if a:
                resolved.append((a, cand, len(logrows) - 1))
                if fits(a):
                    break   # enough: lookups are slow, take the first that fits
        choice = next((r for r in resolved if fits(r[0])), None)
        if not choice and resolved:   # nothing fits the diversity caps: take a new artist if there is one
            choice = next((r for r in resolved if mp.norm(r[0]["artist"]["artistName"]) not in known_artists), None)
        if not choice:
            continue
        a, cand, li = choice
        logrows[li][8] = True
        picks.append({"mbid": a["foreignAlbumId"], "artist": a["artist"]["artistName"], "title": a["title"],
                      "year": release_year(a) or as_year(cand.get("year")) or None,
                      "type": a.get("albumType"), "facet": g.get("facet", ""), "why": cand.get("why", ""),
                      "genres": [x for x in (a.get("genres") or [])][:3], "cover": mp.cover_url(a),
                      "tracks": mp.album_tracks(a), "duration": a.get("duration") or 0,
                      "country": cand.get("country", ""),
                      "known_artist": mp.norm(a["artist"]["artistName"]) in known_artists})
    try:
        with mp.db() as c, c.cursor() as cur:
            cur.executemany("insert into music_candidates(batch, facet, lens, artist, title, year, mbid, probability, "
                            "picked, reason) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", logrows)
    except Exception as e:  # noqa: BLE001 — logging candidates must never block the email
        mp.log("candidates log failed:", e)
    mp.log(f"candidates: {len(logrows)}, picked: {len(picks)}, known artists: {sum(p['known_artist'] for p in picks)}")
    return picks
