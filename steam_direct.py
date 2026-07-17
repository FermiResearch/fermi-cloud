"""
STEAM DIRECT (sekundar) — Steams Global Top Sellers, ofiltrerad, direkt fran
store.steampowered.com/search/results/ (params: filter=globaltopsellers, hwtype=0,
ndl=1, inga taggfilter). BEKRAFTAD 2026-07-17 att matcha gaminganalytics/modellkallan
(topp: CS2, Steam Machine, Palworld, Marvel Rivals, PUBG, Apex, Dota...).

Paginerar till 3000 titlar. Var 4:e timme -> forfinar dygnets min/max-position.
Skriver till PARALLELL tabell steam_gts_daily. Ror ej gaminganalytics-primaren.
Plain HTML, ingen protobuf, ingen webblasare, ingen access_token.
"""

import re
import json
import time
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, datetime, timezone

import ingest  # get_conn, init_db, ingest_log

BASE = "https://store.steampowered.com/search/results/"
MAX_TITLES = int(os.environ.get("STEAM_MAX_TITLES", "3000"))
PER_PAGE = 100
PAUSE = 1.2

PARAMS = {"query": "", "filter": "globaltopsellers", "hwtype": "0",
          "ndl": "1", "infinite": "1", "count": str(PER_PAGE)}

CORE = {
    "394360": "HOI4", "281990": "Stellaris", "1158310": "CK3", "949230": "CS2",
    "255710": "CS1", "236850": "EU4", "3450310": "EU5", "529340": "Vic3",
    "1669000": "AoW4", "532790": "BL2",
}

_ANCHOR = re.compile(
    r'<a\b(?=[^>]*class="[^"]*search_result_row)(?=[^>]*data-ds-appid="([0-9,]+)")[^>]*>')


def fetch_page(start):
    p = dict(PARAMS); p["start"] = str(start)
    url = BASE + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FermiResearch/1.0)"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1)); continue
            raise
    raise RuntimeError(f"429 vid start={start}")


def parse_rows(html, start):
    """[(position, appid, disc%), ...] i ordning. Rabatt bunden till radens segment."""
    out = []
    matches = list(_ANCHOR.finditer(html))
    for i, m in enumerate(matches):
        aid = m.group(1)
        if "," in aid:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        seg = html[m.end():end]
        dm = re.search(r'-(\d+)%', seg)
        disc = int(dm.group(1)) if dm else 0
        out.append((start + i + 1, aid, disc))
    return out


def scrape(max_titles=MAX_TITLES):
    ranking = []; start = 0; total = None
    while start < max_titles:
        data = fetch_page(start)
        if total is None:
            total = int(data.get("total_count", 0) or 0)
        page = parse_rows(data.get("results_html", "") or "", start)
        if not page:
            break
        ranking.extend(page)
        start += PER_PAGE
        if total and start >= total:
            break
        time.sleep(PAUSE)
    return ranking, total


def init_steam_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS steam_gts_daily (
        dzien TEXT, app_id TEXT, min_pos INTEGER, max_pos INTEGER,
        disc_max REAL, readings INTEGER, last_ts TEXT,
        PRIMARY KEY (dzien, app_id))""")
    con.commit()


def run_direct():
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).isoformat()
    con = ingest.get_conn(); ingest.init_db(con); init_steam_table(con)
    try:
        ranking, total = scrape()
    except Exception as e:
        con.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                    (ts, today, "steam_direct", 0, 0, 0, str(e)[:300]))
        con.commit(); con.close()
        return {"ok": False, "error": str(e)}
    added = updated = 0
    for pos, aid, disc in ranking:
        disc = float(disc)
        row = con.execute("SELECT min_pos,max_pos,disc_max,readings FROM steam_gts_daily "
                          "WHERE dzien=? AND app_id=?", (today, aid)).fetchone()
        if row is None:
            con.execute("INSERT INTO steam_gts_daily VALUES (?,?,?,?,?,?,?)",
                        (today, aid, pos, pos, disc, 1, ts))
            added += 1
        else:
            con.execute("UPDATE steam_gts_daily SET min_pos=?,max_pos=?,disc_max=?,readings=?,last_ts=? "
                        "WHERE dzien=? AND app_id=?",
                        (min(row[0], pos), max(row[1], pos), max(row[2], disc), row[3] + 1, ts, today, aid))
            updated += 1
    con.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                (ts, today, "steam_direct", added, updated, 1, f"{len(ranking)} titlar (av {total})"))
    con.commit(); con.close()
    return {"ok": True, "titlar": len(ranking), "total": total, "added": added, "updated": updated}


if __name__ == "__main__":
    print(json.dumps(run_direct(), ensure_ascii=False, indent=2))
