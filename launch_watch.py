"""
launch_watch.py — Fermi lanseringsbevakning (moln)

Samlar KARDINALA lanseringssignaler kring release. Kor INGEN modell.
Nyckel ar app_id genomgaende. Etiketter ar beskrivande, aldrig join-nyckel.

ATERANVANDER befintliga hamtare — ingen dubblerad HTTP-logik:
  reviews.fetch_summary()      recensioner (Valve appreviews)
  ingest._get("steam/st_wishl") wishlist-rank (gaminganalytics st_wishl)
  ingest.run_wishlists()        skriver hela topp-1000 till wishlist_daily

EGNA hamtare (finns inte i stacken sedan tidigare, alla Valve direkt):
  CCU        ISteamUserStats/GetNumberOfCurrentPlayers
  Key-andel  appreviews med purchase_type=steam
  Pris       appdetails (BADE initial och final)
  Sprak      appreviews per sprakkod
  Achieve    ISteamUserStats/GetGlobalAchievementPercentagesForApp
  Meta       appdetails (dlc-lista, regionala priser)

Idempotent och tidsstyrd internt: kan anropas hur ofta som helst.
"""

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone

import ingest
import reviews

UA = "Mozilla/5.0 (compatible; FermiResearch/1.0)"
HTTP_TIMEOUT = 25


# ---------------------------------------------------------------- watchlist

LAUNCH_WATCH = [
    {
        "app_id": 3493540,
        "label": "TransportFever3",
        "release_utc": "2026-09-29T13:00:00Z",
        "aktiv": True,
        "note": "Unlock-tid ANTAGEN (15:00 CEST). Launch-fasen spanner T-1d till "
                "T+3d sa nagra timmars fel spelar ingen roll for peak-fangsten.",
    },
    {
        "app_id": 1066780,
        "label": "TransportFever2",
        "release_utc": "2026-09-29T13:00:00Z",
        "aktiv": True,
        "note": "REFERENSANKARE, ej lansering. Speglar TF3:s fonster for halo "
                "och kannibalisering. Satt aktiv=False om overflodigt.",
    },
]

# Sprak for recensionsmixen. TF-serien ar tungt tysktalande.
LANGS = ["english", "german", "french", "russian", "schinese",
         "spanish", "polish", "japanese", "koreana", "brazilian"]

# Regionala prismarknader (cc-koder).
REGIONS = ["us", "de", "gb", "pl", "br"]

# Langsam kadens per typ, i sekunder.
SLOW_KINDS = {"langs": 21600, "ach": 86400, "meta": 86400}


# ---------------------------------------------------------------- fasschema

PHASES = [
    ("pre_far",     -90.0,  -14.0,  86400),
    ("pre_near",    -14.0,   -1.0,  21600),
    ("launch",       -1.0,    3.0,    900),
    ("post_early",    3.0,   14.0,   3600),
    ("post_tail",    14.0,   90.0,  21600),
]


def phase_for(now, release):
    d = (now - release).total_seconds() / 86400.0
    for name, lo, hi, interval in PHASES:
        if lo <= d < hi:
            return name, interval
    return None, None


# ---------------------------------------------------------------- schema

DDL = """
CREATE TABLE IF NOT EXISTS launch_obs (
    app_id        INTEGER NOT NULL,
    ts_utc        TEXT    NOT NULL,
    phase         TEXT    NOT NULL,
    ccu           INTEGER,
    rev_total     INTEGER,
    rev_pos       INTEGER,
    rev_neg       INTEGER,
    rev_score     INTEGER,
    rev_steamonly INTEGER,
    wl_pos        INTEGER,
    price_init    INTEGER,
    price_final   INTEGER,
    disc_pct      INTEGER,
    src_ok        TEXT,
    PRIMARY KEY (app_id, ts_utc)
);
CREATE INDEX IF NOT EXISTS ix_launch_obs_app_ts ON launch_obs (app_id, ts_utc);

CREATE TABLE IF NOT EXISTS launch_slow (
    app_id   INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    ts_utc   TEXT    NOT NULL,
    payload  TEXT,
    PRIMARY KEY (app_id, kind, ts_utc)
);
"""


def ensure_schema(conn):
    conn.executescript(DDL)
    conn.commit()


# ---------------------------------------------------------------- hamtare

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_ccu(app_id):
    """Samtidiga spelare just nu. Valve direkt, ingen nyckel."""
    d = _get_json("https://api.steampowered.com/ISteamUserStats/"
                  f"GetNumberOfCurrentPlayers/v1/?appid={app_id}")
    resp = d.get("response") or {}
    return resp.get("player_count") if resp.get("result") == 1 else None


def fetch_reviews_steamonly(app_id):
    """Recensioner ENBART fran Steam-kop. Skillnaden mot reviews.fetch_summary
    (som kor purchase_type=all) ar key-aktiveringar — dvs GOG/Epic/retail.
    Med TF3:s multi-store-lansering ar det ett direkt matt pa Steam-andelen."""
    d = _get_json(f"https://store.steampowered.com/appreviews/{app_id}"
                  "?json=1&language=all&purchase_type=steam&num_per_page=0")
    if d.get("success") != 1:
        return None
    return (d.get("query_summary") or {}).get("total_reviews")


def fetch_price(app_id, cc="us"):
    """BADE listpris (initial) och faktiskt pris (final). Utan initial vet vi
    bara vad spelet kostade, inte vad det borde ha kostat."""
    d = _get_json("https://store.steampowered.com/api/appdetails"
                  f"?appids={app_id}&cc={cc}&filters=price_overview")
    node = (d or {}).get(str(app_id)) or {}
    if not node.get("success"):
        return None
    po = (node.get("data") or {}).get("price_overview")
    if not po:
        return {"init": 0, "final": 0, "disc": 0}
    return {"init": po.get("initial"), "final": po.get("final"),
            "disc": po.get("discount_percent")}


def fetch_langs(app_id):
    """Recensionsmix per sprak. Regional mix styr bade Boxleiter-talet och
    intakt per enhet."""
    out = {}
    for lang in LANGS:
        try:
            d = _get_json(f"https://store.steampowered.com/appreviews/{app_id}"
                          f"?json=1&language={lang}&purchase_type=all&num_per_page=0")
            if d.get("success") == 1:
                out[lang] = (d.get("query_summary") or {}).get("total_reviews")
        except Exception:
            continue
    return out


def fetch_achievements(app_id):
    """Global achievement-procent. Ger nagot forst efter release."""
    d = _get_json("https://api.steampowered.com/ISteamUserStats/"
                  f"GetGlobalAchievementPercentagesForApp/v2/?gameid={app_id}")
    ach = ((d or {}).get("achievementpercentages") or {}).get("achievements")
    if not ach:
        return None
    return {a.get("name"): a.get("percent") for a in ach[:40]}


def fetch_meta(app_id):
    """DLC-lista (fangar Deluxe-uppgraderingen) + regionala priser."""
    out = {"dlc": None, "regions": {}}
    try:
        d = _get_json("https://store.steampowered.com/api/appdetails"
                      f"?appids={app_id}&cc=us")
        node = (d or {}).get(str(app_id)) or {}
        if node.get("success"):
            out["dlc"] = (node.get("data") or {}).get("dlc")
    except Exception:
        pass
    for cc in REGIONS:
        try:
            p = fetch_price(app_id, cc=cc)
            if p:
                out["regions"][cc] = p
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- wishlist

def fetch_wishlist_ranks(app_ids):
    """Ett anrop mot st_wishl ger hela topp-1000. Vi plockar ut vara app_id.
    Gaminganalytics ar sekundarkalla, men Valve publicerar ingen wishlist-rank
    i oppet API — och listan ar redan i drift och korsvaliderad."""
    want = {str(a) for a in app_ids}
    rows = ingest._get("steam/st_wishl", {})
    out = {}
    for r in rows:
        aid = str(r.get("app_id", "")).strip()
        if aid in want:
            try:
                out[int(aid)] = int(float(r.get("position")))
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------- korning

def _last_ts(conn, app_id):
    row = conn.execute("SELECT MAX(ts_utc) FROM launch_obs WHERE app_id=?",
                       (app_id,)).fetchone()
    return datetime.fromisoformat(row[0].replace("Z", "+00:00")) if row and row[0] else None


def _last_slow_ts(conn, app_id, kind):
    row = conn.execute("SELECT MAX(ts_utc) FROM launch_slow WHERE app_id=? AND kind=?",
                       (app_id, kind)).fetchone()
    return datetime.fromisoformat(row[0].replace("Z", "+00:00")) if row and row[0] else None


def _due(prev, interval, now):
    return prev is None or (now - prev).total_seconds() >= interval * 0.9


def observe(app_id, wl_pos=None):
    out = {k: None for k in ("ccu", "rev_total", "rev_pos", "rev_neg", "rev_score",
                             "rev_steamonly", "price_init", "price_final", "disc_pct")}
    out["wl_pos"] = wl_pos
    ok = ["wl"] if wl_pos is not None else []

    try:
        out["ccu"] = fetch_ccu(app_id)
        if out["ccu"] is not None:
            ok.append("ccu")
    except Exception:
        pass

    try:
        qs = reviews.fetch_summary(str(app_id))   # aterbruk, ingen egen hamtare
        if qs:
            out["rev_total"] = qs.get("total_reviews")
            out["rev_pos"] = qs.get("total_positive")
            out["rev_neg"] = qs.get("total_negative")
            out["rev_score"] = qs.get("review_score")
            ok.append("rev")
    except Exception:
        pass

    try:
        out["rev_steamonly"] = fetch_reviews_steamonly(app_id)
        if out["rev_steamonly"] is not None:
            ok.append("revsteam")
    except Exception:
        pass

    try:
        p = fetch_price(app_id)
        if p:
            out["price_init"], out["price_final"], out["disc_pct"] = (
                p["init"], p["final"], p["disc"])
            ok.append("price")
    except Exception:
        pass

    out["src_ok"] = ",".join(ok) if ok else "none"
    return out


def _slow_pass(conn, app_id, now, ts):
    """Langsamma signaler, egen kadens per typ."""
    done = []
    for kind, interval in SLOW_KINDS.items():
        if not _due(_last_slow_ts(conn, app_id, kind), interval, now):
            continue
        try:
            if kind == "langs":
                payload = fetch_langs(app_id)
            elif kind == "ach":
                payload = fetch_achievements(app_id)
            else:
                payload = fetch_meta(app_id)
        except Exception:
            payload = None
        if payload:
            conn.execute("INSERT OR REPLACE INTO launch_slow VALUES (?,?,?,?)",
                         (app_id, kind, ts, json.dumps(payload, ensure_ascii=False)))
            done.append(kind)
    return done


def run_launch_watch(db_path=None, now=None):
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path or ingest.DB_PATH, timeout=30)
    ensure_schema(conn)

    aktiva = []
    for t in LAUNCH_WATCH:
        aid = t.get("app_id")
        if not t.get("aktiv") or not aid:
            continue
        rel = datetime.fromisoformat(t["release_utc"].replace("Z", "+00:00"))
        phase, interval = phase_for(now, rel)
        if phase and _due(_last_ts(conn, aid), interval, now):
            aktiva.append((aid, phase))

    if not aktiva:
        conn.close()
        return {"now": now.isoformat(), "status": "inget_att_gora"}

    # ETT wishlist-anrop delas av alla titlar i passet
    wl = {}
    try:
        wl = fetch_wishlist_ranks([a for a, _ in aktiva])
    except Exception as e:
        print("WISHLIST FEL:", e)

    ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    result = []
    for aid, phase in aktiva:
        o = observe(aid, wl_pos=wl.get(aid))
        if o["src_ok"] == "none":
            result.append({"app_id": aid, "status": "alla_kallor_fel"})
            continue
        conn.execute(
            "INSERT OR REPLACE INTO launch_obs (app_id, ts_utc, phase, ccu, "
            "rev_total, rev_pos, rev_neg, rev_score, rev_steamonly, wl_pos, "
            "price_init, price_final, disc_pct, src_ok) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, ts, phase, o["ccu"], o["rev_total"], o["rev_pos"], o["rev_neg"],
             o["rev_score"], o["rev_steamonly"], o["wl_pos"], o["price_init"],
             o["price_final"], o["disc_pct"], o["src_ok"]))
        slow = _slow_pass(conn, aid, now, ts)
        conn.commit()
        result.append({"app_id": aid, "status": "skrivet", "phase": phase,
                       "ccu": o["ccu"], "rev": o["rev_total"],
                       "rev_steam": o["rev_steamonly"], "wl_pos": o["wl_pos"],
                       "src": o["src_ok"], "slow": slow})

    conn.close()
    return {"now": now.isoformat(), "ts": ts, "titlar": result}


# ---------------------------------------------------------------- lasning

def wishlist_history(db_path=None, app_id=None):
    """Vad som REDAN finns i wishlist_daily for en titel. Molnet har skrivit
    sedan 17 juli 2026 — kolla detta INNAN nagon backfill byggs."""
    conn = sqlite3.connect(db_path or ingest.DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT dzien, min_pos, max_pos, price, rev_cnt, release_date "
        "FROM wishlist_daily WHERE app_id=? ORDER BY dzien", (str(app_id),)).fetchall()
    conn.close()
    return [{"dzien": r[0], "min_pos": r[1], "max_pos": r[2], "price": r[3],
             "rev_cnt": r[4], "release_date": r[5]} for r in rows]


def daily_rollup(db_path=None, app_id=None):
    """Dagsaggregat. peak_ccu = max inom dygnet; rev_delta = dagligt tillskott.
    key_andel = 1 - steam-kop / alla recensioner, dvs GOG/Epic/retail-nycklar."""
    conn = sqlite3.connect(db_path or ingest.DB_PATH, timeout=30)
    q = """
    SELECT app_id, substr(ts_utc,1,10) AS dag,
           MAX(ccu), MAX(rev_total), MAX(rev_steamonly),
           MIN(wl_pos), MAX(disc_pct), MAX(price_init), MIN(price_final), COUNT(*)
    FROM launch_obs {where}
    GROUP BY app_id, dag ORDER BY app_id, dag
    """.format(where="WHERE app_id = ?" if app_id else "")
    rows = conn.execute(q, (app_id,) if app_id else ()).fetchall()
    conn.close()

    out, prev = [], {}
    for aid, dag, ccu, rev, revs, wl, disc, pini, pfin, n in rows:
        delta = rev - prev[aid] if rev is not None and prev.get(aid) is not None else None
        if rev is not None:
            prev[aid] = rev
        key_andel = None
        if rev and revs is not None and rev > 0:
            key_andel = round(1 - revs / rev, 4)
        out.append({"app_id": aid, "dag": dag, "peak_ccu": ccu, "rev_kum": rev,
                    "rev_delta": delta, "rev_steamonly": revs,
                    "key_andel": key_andel, "wl_bast_pos": wl,
                    "disc_max": disc, "price_init": pini, "price_final": pfin,
                    "n_obs": n})
    return out


if __name__ == "__main__":
    print(json.dumps(run_launch_watch(), indent=2, ensure_ascii=False))
