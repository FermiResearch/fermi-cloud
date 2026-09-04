"""
REVIEWS (MAU-underlag) — hamtar dagligt totalt antal recensioner + betyg for de
bolag vi tacker, via Steams offentliga appreviews-API (INGEN cookie, ingen nyckel).
Underlag for review-velocity och MAU-berakningar. Skriver till reviews_daily.

Watchlist = PDX (grundspel + legacy) + Coffee Stain-ankare, verifierade app-id:n
ur FERMI_DATA_INDEX.csv. Lagg till fler genom att fylla pa WATCHLIST.
"""

import time
import json
import urllib.request
import urllib.parse
from datetime import date, datetime, timezone

import ingest  # get_conn

APPREVIEWS = "https://store.steampowered.com/appreviews/{aid}?json=1&language=all&purchase_type={pt}&num_per_page=0"

WATCHLIST = {
    # PDX grundspel
    "394360": "HOI4", "1158310": "CK3", "529340": "Vic3", "236850": "EU4",
    "3450310": "EU5", "1669000": "AoW4", "255710": "CS1", "949230": "CS2",
    "281990": "Stellaris", "532790": "BL2", "859580": "Imperator: Rome",
    # PDX legacy (verifierade appid)
    "25800": "Victoria II", "203770": "CK2", "637090": "BATTLETECH",
    "362960": "Tyranny", "238370": "Magicka 2", "604540": "Empire of Sin",
    "684450": "Surviving the Aftermath", "1324130": "Stranded: Alien Dawn",
    "1622900": "Star Trek: Infinite", "1408610": "Lamplighters League",
    "234650": "Shadowrun Returns", "300550": "Shadowrun: Dragonfall",
    "233450": "Prison Architect", "601510": "Academia", "1283400": "Across the Obelisk",
    # Urban Games / TF-serien (TF3 lanseras 29 sep 2026)
    "3493540": "Transport Fever 3", "1066780": "Transport Fever 2",
    "446800": "Transport Fever", "304730": "Train Fever",
    # Coffee Stain (OOS-ankare)
    "526870": "Satisfactory", "892970": "Valheim", "548430": "Deep Rock Galactic",
    "265930": "Goat Simulator", "91600": "Sanctum", "210770": "Sanctum 2",
    "598550": "Huntdown", "1167630": "Teardown", "867210": "Songs of Conquest",
}

PAUSE = 1.0


def fetch_summary(aid, purchase_type="all"):
    """purchase_type="all" = alla recensioner. "steam" = enbart Steam-kop.
    Skillnaden ar key-aktiveringar (GOG/Epic/retail). Aterbrukas av launch_watch."""
    url = APPREVIEWS.format(aid=aid, pt=purchase_type)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FermiResearch/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("success") != 1:
        return None
    return data.get("query_summary", {})


def init_reviews_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS reviews_daily (
        dzien TEXT, app_id TEXT, name TEXT,
        total_reviews INTEGER, total_positive INTEGER, total_negative INTEGER,
        review_score INTEGER, review_score_desc TEXT, last_ts TEXT,
        PRIMARY KEY (dzien, app_id))""")
    con.commit()


def run_reviews():
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).isoformat()
    con = ingest.get_conn(); ingest.init_db(con); init_reviews_table(con)
    added = updated = failed = 0
    for aid, name in WATCHLIST.items():
        try:
            qs = fetch_summary(aid)
        except Exception:
            qs = None
        if not qs:
            failed += 1
            time.sleep(PAUSE)
            continue
        vals = (today, aid, name,
                qs.get("total_reviews"), qs.get("total_positive"),
                qs.get("total_negative"), qs.get("review_score"),
                qs.get("review_score_desc"), ts)
        exists = con.execute("SELECT 1 FROM reviews_daily WHERE dzien=? AND app_id=?",
                             (today, aid)).fetchone()
        con.execute("INSERT OR REPLACE INTO reviews_daily VALUES (?,?,?,?,?,?,?,?,?)", vals)
        if exists: updated += 1
        else: added += 1
        time.sleep(PAUSE)
    con.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                (ts, today, "reviews", added, updated, 1,
                 f"{added+updated}/{len(WATCHLIST)} titlar ({failed} fel)"))
    con.commit(); con.close()
    return {"ok": True, "added": added, "updated": updated, "failed": failed,
            "watchlist": len(WATCHLIST)}


if __name__ == "__main__":
    print(json.dumps(run_reviews(), ensure_ascii=False, indent=2))
