"""
FERMI CLOUD INGEST — hamtar dagliga Steam-rankningar fran gaminganalytics.info
och skriver till SQLite (fermi_gts.db) pa den bestandiga disken.

Bygger PA Antons beprovade fermi_ingest.py — samma endpoint, samma apikey-request,
samma min/max-upsert. Skillnad: nyckel + db-sokvag las fran miljovariabler (secrets
committas ALDRIG), och funktionerna ar importerbara sa schemalaggaren kan kalla dem.
Ingen cookie kravs (officiella apikey-API:t).
"""

import os
import json
import urllib.request
import urllib.parse
import sqlite3
from datetime import date, datetime, timezone

API_ROOT = "https://gaminganalytics.info/api"
API_KEY = os.environ.get("FERMI_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/var/data/fermi_gts.db")


def get_conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


# ---- beprovad request (oforandrad logik fran fermi_ingest.py) ----
def _get(path, extra_params):
    if not API_KEY:
        raise RuntimeError("FERMI_API_KEY saknas i miljon")
    params = {"apikey": API_KEY, "result": "json"}
    params.update(extra_params)
    url = f"{API_ROOT}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if not isinstance(data, list):
        raise ValueError(f"Ovantat svar fran {path}: {type(data)}")
    return data


def _num(v):
    try:
        return float(v) if v not in (None, "", "string") else 0
    except (ValueError, TypeError):
        return 0


def _devpub(r):
    dev = " | ".join(str(r.get(f"developers_{i}", "")) for i in (1, 2, 3)).strip(" |")
    pub = " | ".join(str(r.get(f"publishers_{i}", "")) for i in (1, 2, 3)).strip(" |")
    return dev, pub


def init_db(con):
    con.execute("""CREATE TABLE IF NOT EXISTS gts_daily (
        dzien TEXT, app_id TEXT, name TEXT, min_pos INTEGER, max_pos INTEGER,
        disc_max REAL, price REAL, rev_cnt INTEGER, rev_perc INTEGER,
        developers TEXT, publishers TEXT, PRIMARY KEY (dzien, app_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS wishlist_daily (
        dzien TEXT, app_id TEXT, name TEXT, min_pos INTEGER, max_pos INTEGER,
        price REAL, rev_cnt INTEGER, rev_perc INTEGER, release_date TEXT,
        developers TEXT, publishers TEXT, PRIMARY KEY (dzien, app_id))""")
    # driftlogg for att upptacka tysta luckor
    con.execute("""CREATE TABLE IF NOT EXISTS ingest_log (
        ts TEXT, dzien TEXT, source TEXT, added INTEGER,
        updated INTEGER, ok INTEGER, note TEXT, PRIMARY KEY (ts, source))""")
    con.commit()


def upsert_minmax(con, table, rows, key_cols, key_vals_fn, extra_fn, disc_field=None):
    added = updated = 0
    for r in rows:
        app_id = str(r.get("app_id", "")).strip()
        if not app_id:
            continue
        pos = _num(r.get("position"))
        keyvals = key_vals_fn(app_id, r)
        where = " AND ".join(f"{c}=?" for c in key_cols)
        sel = "min_pos, max_pos" + (", disc_max" if disc_field else "")
        existing = con.execute(f"SELECT {sel} FROM {table} WHERE {where}", keyvals).fetchone()
        cols, vals = extra_fn(r)
        if existing is None:
            allcols = list(key_cols) + ["min_pos", "max_pos"] + cols
            allvals = list(keyvals) + [pos, pos] + vals
            ph = ",".join("?" * len(allcols))
            con.execute(f"INSERT INTO {table} ({','.join(allcols)}) VALUES ({ph})", allvals)
            added += 1
        else:
            new_min = min(existing[0], pos)
            new_max = max(existing[1], pos)
            setcols = ["min_pos=?", "max_pos=?"] + [f"{c}=?" for c in cols]
            setvals = [new_min, new_max] + vals
            if disc_field:
                idx = cols.index(disc_field)
                setvals[2 + idx] = max(existing[2], vals[idx])
            con.execute(f"UPDATE {table} SET {','.join(setcols)} WHERE {where}",
                        setvals + list(keyvals))
            updated += 1
    con.commit()
    return added, updated


def run_topsellers(con, today):
    rows = _get("steam/st_gts", {})
    def extra(r):
        dev, pub = _devpub(r)
        return (["name", "disc_max", "price", "rev_cnt", "rev_perc", "developers", "publishers"],
                [r.get("name", ""), _num(r.get("disc")), _num(r.get("price")),
                 _num(r.get("rev_cnt")), _num(r.get("rev_perc")), dev, pub])
    return upsert_minmax(con, "gts_daily", rows, ["dzien", "app_id"],
                         lambda a, r: (today, a), extra, disc_field="disc_max")


def run_wishlists(con, today):
    rows = _get("steam/st_wishl", {})
    def extra(r):
        dev, pub = _devpub(r)
        return (["name", "price", "rev_cnt", "rev_perc", "release_date", "developers", "publishers"],
                [r.get("name", ""), _num(r.get("price")), _num(r.get("rev_cnt")),
                 _num(r.get("rev_perc")), r.get("release_date_parsed", ""), dev, pub])
    return upsert_minmax(con, "wishlist_daily", rows, ["dzien", "app_id"],
                         lambda a, r: (today, a), extra)


def run_all():
    """Ett dygns hamtning. Returnerar sammanfattning; loggar varje kalla."""
    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).isoformat()
    con = get_conn()
    init_db(con)
    summary = {"dzien": today, "sources": {}}
    for name, fn in [("gts_daily", run_topsellers), ("wishlist_daily", run_wishlists)]:
        try:
            a, u = fn(con, today)
            summary["sources"][name] = {"ok": True, "added": a, "updated": u}
            con.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                        (ts, today, name, a, u, 1, ""))
        except Exception as e:
            summary["sources"][name] = {"ok": False, "error": str(e)}
            con.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                        (ts, today, name, 0, 0, 0, str(e)[:300]))
    con.commit()
    con.close()
    return summary


def last_ingest():
    """Senaste lyckade skrivning per kalla — for /health och luckdetektering."""
    con = get_conn()
    init_db(con)
    out = {}
    for src in ("gts_daily", "wishlist_daily", "steam_direct"):
        row = con.execute(
            "SELECT dzien, ts FROM ingest_log WHERE source=? AND ok=1 ORDER BY ts DESC LIMIT 1",
            (src,)).fetchone()
        out[src] = {"last_day": row[0], "last_ts": row[1]} if row else None
    con.close()
    return out


if __name__ == "__main__":
    print(json.dumps(run_all(), ensure_ascii=False, indent=2))
