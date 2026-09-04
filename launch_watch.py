"""
launch_watch.py — Fermi lanseringsbevakning (moln)

Samlar KARDINALA lanseringssignaler kring release. Kör INGEN modell.
Nyckel är app_id genomgående. Etiketter är beskrivande, aldrig join-nyckel.

Idempotent och tidsstyrd internt: kan anropas hur ofta som helst.
Modulen avgör själv per titel om det är dags att skriva en ny observation.

Endpoints (alla utan API-nyckel):
  CCU     ISteamUserStats/GetNumberOfCurrentPlayers
  Reviews store.steampowered.com/appreviews (query_summary)
  Pris    store.steampowered.com/api/appdetails (price_overview)
"""

import json
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

DB_PATH = "/var/data/fermi_gts.db"
UA = "FermiResearch/1.0 (launch_watch)"
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------- watchlist

# release_utc = faktisk release-tidpunkt i UTC (Steam unlock), inte kalenderdatum.
# aktiv=False stänger av en titel utan att radera historiken.
LAUNCH_WATCH = [
    {
        "app_id": None,          # TODO: TF3 Steam app_id
        "label": "TF3",
        "release_utc": "2026-09-29T17:00:00Z",   # TODO: bekräfta unlock-tid
        "aktiv": True,
    },
]


# ---------------------------------------------------------------- fasschema

# (namn, från_dagar_relativt_release, till_dagar, intervall_sekunder)
PHASES = [
    ("pre_far",     -90.0,  -14.0,  86400),   # 1/dag  — wishlist-uppbyggnad
    ("pre_near",    -14.0,   -1.0,  21600),   # 4/dag
    ("launch",       -1.0,    3.0,    900),   # var 15:e min — peak-fångst
    ("post_early",    3.0,   14.0,   3600),   # 1/tim
    ("post_tail",    14.0,   90.0,  21600),   # 4/dag
]


def phase_for(now, release):
    """Returnerar (fasnamn, intervall_sek) eller (None, None) utanför fönstret."""
    d = (now - release).total_seconds() / 86400.0
    for name, lo, hi, interval in PHASES:
        if lo <= d < hi:
            return name, interval
    return None, None


# ---------------------------------------------------------------- schema

DDL = """
CREATE TABLE IF NOT EXISTS launch_obs (
    app_id       INTEGER NOT NULL,
    ts_utc       TEXT    NOT NULL,
    phase        TEXT    NOT NULL,
    ccu          INTEGER,
    rev_total    INTEGER,
    rev_pos      INTEGER,
    rev_neg      INTEGER,
    price_cents  INTEGER,
    disc_pct     INTEGER,
    src_ok       TEXT,
    PRIMARY KEY (app_id, ts_utc)
);
CREATE INDEX IF NOT EXISTS ix_launch_obs_app_ts ON launch_obs (app_id, ts_utc);
"""


def ensure_schema(conn):
    conn.executescript(DDL)
    conn.commit()


# ---------------------------------------------------------------- hämtning

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_ccu(app_id):
    """Samtidiga spelare just nu. None vid fel."""
    url = ("https://api.steampowered.com/ISteamUserStats/"
           f"GetNumberOfCurrentPlayers/v1/?appid={app_id}")
    d = _get_json(url)
    resp = d.get("response") or {}
    if resp.get("result") != 1:
        return None
    return resp.get("player_count")


def fetch_reviews(app_id):
    """Kumulativa recensionstal. Rå källa — inga splice-breaks som rev_cnt_max."""
    url = (f"https://store.steampowered.com/appreviews/{app_id}"
           "?json=1&num_per_page=0&language=all&purchase_type=all"
           "&filter=all&review_type=all")
    d = _get_json(url)
    if d.get("success") != 1:
        return None
    q = d.get("query_summary") or {}
    return {
        "total": q.get("total_reviews"),
        "pos": q.get("total_positive"),
        "neg": q.get("total_negative"),
    }


def fetch_price(app_id, cc="us"):
    """Pris i cent + rabatt i procent. USD-bas, i linje med modellen."""
    url = ("https://store.steampowered.com/api/appdetails"
           f"?appids={app_id}&cc={cc}&filters=price_overview")
    d = _get_json(url)
    node = (d or {}).get(str(app_id)) or {}
    if not node.get("success"):
        return None
    po = (node.get("data") or {}).get("price_overview")
    if not po:
        return {"price_cents": 0, "disc_pct": 0}   # free/unreleased
    return {
        "price_cents": po.get("final"),
        "disc_pct": po.get("discount_percent"),
    }


# ---------------------------------------------------------------- körning

def last_obs_ts(conn, app_id):
    row = conn.execute(
        "SELECT MAX(ts_utc) FROM launch_obs WHERE app_id = ?", (app_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0].replace("Z", "+00:00"))


def due(conn, app_id, interval_sek, now):
    prev = last_obs_ts(conn, app_id)
    if prev is None:
        return True
    return (now - prev).total_seconds() >= interval_sek * 0.9


def observe(app_id):
    """Hämtar alla källor. Delfel tolereras och loggas i src_ok."""
    out = {"ccu": None, "rev_total": None, "rev_pos": None, "rev_neg": None,
           "price_cents": None, "disc_pct": None}
    ok = []

    try:
        out["ccu"] = fetch_ccu(app_id)
        if out["ccu"] is not None:
            ok.append("ccu")
    except Exception:
        pass

    try:
        r = fetch_reviews(app_id)
        if r:
            out["rev_total"], out["rev_pos"], out["rev_neg"] = (
                r["total"], r["pos"], r["neg"])
            ok.append("rev")
    except Exception:
        pass

    try:
        p = fetch_price(app_id)
        if p:
            out["price_cents"], out["disc_pct"] = p["price_cents"], p["disc_pct"]
            ok.append("price")
    except Exception:
        pass

    out["src_ok"] = ",".join(ok) if ok else "none"
    return out


def run_launch_watch(db_path=DB_PATH, now=None):
    """Anropas av /run/launch. Returnerar sammanfattning för loggning."""
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)

    result = []
    for t in LAUNCH_WATCH:
        app_id = t.get("app_id")
        if not t.get("aktiv") or not app_id:
            continue

        release = datetime.fromisoformat(t["release_utc"].replace("Z", "+00:00"))
        phase, interval = phase_for(now, release)
        if phase is None:
            result.append({"app_id": app_id, "status": "utanfor_fonster"})
            continue

        if not due(conn, app_id, interval, now):
            result.append({"app_id": app_id, "status": "ej_dags", "phase": phase})
            continue

        obs = observe(app_id)
        if obs["src_ok"] == "none":
            result.append({"app_id": app_id, "status": "alla_kallor_fel",
                           "phase": phase})
            continue

        ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT OR REPLACE INTO launch_obs "
            "(app_id, ts_utc, phase, ccu, rev_total, rev_pos, rev_neg, "
            " price_cents, disc_pct, src_ok) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (app_id, ts, phase, obs["ccu"], obs["rev_total"], obs["rev_pos"],
             obs["rev_neg"], obs["price_cents"], obs["disc_pct"], obs["src_ok"]),
        )
        conn.commit()
        result.append({"app_id": app_id, "status": "skrivet", "phase": phase,
                       "ts": ts, "ccu": obs["ccu"], "rev": obs["rev_total"],
                       "src": obs["src_ok"]})

    conn.close()
    return {"now": now.isoformat(), "titlar": result}


# ---------------------------------------------------------------- rollup

def daily_rollup(db_path=DB_PATH, app_id=None):
    """
    Dagsaggregat för senare modellbruk. Ren läsning, skriver inget.
    peak_ccu = max inom dygnet (undersampling underskattar peak systematiskt —
    därför 15-min-kadens i launch-fasen).
    rev_delta = kumulativ recensionsdiff mot föregående dygn.
    """
    conn = sqlite3.connect(db_path)
    q = """
    SELECT app_id,
           substr(ts_utc, 1, 10)          AS dag,
           MAX(ccu)                       AS peak_ccu,
           MAX(rev_total)                 AS rev_kum,
           MIN(disc_pct)                  AS disc_min,
           MAX(disc_pct)                  AS disc_max,
           COUNT(*)                       AS n_obs
    FROM launch_obs
    {where}
    GROUP BY app_id, dag
    ORDER BY app_id, dag
    """.format(where="WHERE app_id = ?" if app_id else "")
    rows = conn.execute(q, (app_id,) if app_id else ()).fetchall()
    conn.close()

    out, prev = [], {}
    for aid, dag, peak, rev_kum, dmin, dmax, n in rows:
        delta = None
        if rev_kum is not None and prev.get(aid) is not None:
            delta = rev_kum - prev[aid]
        if rev_kum is not None:
            prev[aid] = rev_kum
        out.append({"app_id": aid, "dag": dag, "peak_ccu": peak,
                    "rev_kum": rev_kum, "rev_delta": delta,
                    "disc_min": dmin, "disc_max": dmax, "n_obs": n})
    return out


if __name__ == "__main__":
    print(json.dumps(run_launch_watch(), indent=2, ensure_ascii=False))
