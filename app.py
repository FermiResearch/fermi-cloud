"""
FERMI CLOUD APP — alltid-pa tjanst pa Render.
  * schemalagd daglig hamtning (APScheduler) -> skriver till SQLite pa disken
  * /export/pdx  -> JSON med PDX-titlarnas dagsrader (Claude hamtar via denna, token-skyddad)
  * /health      -> senaste lyckade hamtning per kalla (upptack tysta luckor)

Secrets (FERMI_API_KEY, EXPORT_TOKEN) las fran miljon — committas aldrig.
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import ingest
import steam_direct
import reviews
import launch_watch

EXPORT_TOKEN = os.environ.get("EXPORT_TOKEN", "")
SCRAPE_HOUR_UTC = int(os.environ.get("SCRAPE_HOUR_UTC", "6"))  # ~08 svensk sommartid

# PDX Core-9 + naraliggande basspel (app_id -> etikett)
PDX = {
    "394360": "HOI4", "281990": "Stellaris", "1158310": "CK3", "949230": "CS2",
    "255710": "CS1", "236850": "EU4", "3450310": "EU5", "529340": "Vic3",
    "1669000": "AoW4", "532790": "BL2", "233450": "Prison Architect",
    "859580": "Imperator", "1283400": "Across the Obelisk",
}

scheduler = BackgroundScheduler(timezone="UTC")


def _db_path():
    """Faktisk sokvag till SQLite-filen, hamtad fran ingest sa launch_watch
    garanterat skriver till SAMMA disk-DB och inte en egen fil."""
    con = ingest.get_conn()
    try:
        return con.execute("PRAGMA database_list").fetchone()[2]
    finally:
        con.close()


def _daily_job():
    try:
        ingest.run_all()
    except Exception as e:
        print("SCHED FEL:", e)


def _steam_job():
    try:
        steam_direct.run_direct()
    except Exception as e:
        print("STEAM SCHED FEL:", e)


def _reviews_job():
    try:
        reviews.run_reviews()
    except Exception as e:
        print("REVIEWS SCHED FEL:", e)


def _launch_job():
    """Lanseringsbevakning. Idempotent och tidsstyrd internt — modulen avgor
    sjalv per titel om det ar dags. Utanfor lanseringsfonster ar det en no-op."""
    try:
        launch_watch.run_launch_watch(db_path=_db_path())
    except Exception as e:
        print("LAUNCH SCHED FEL:", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    con = ingest.get_conn(); ingest.init_db(con); steam_direct.init_steam_table(con); reviews.init_reviews_table(con); launch_watch.ensure_schema(con); con.close()
    # seedar tomt? kor en hamtning direkt sa DB inte startar tom
    try:
        if not ingest.last_ingest().get("gts_daily"):
            ingest.run_all()
    except Exception as e:
        print("STARTUP-hamtning hoppad:", e)
    scheduler.add_job(_daily_job, CronTrigger(hour=SCRAPE_HOUR_UTC, minute=15),
                      id="daily", replace_existing=True)
    # direkt-Steam Core-10 var 4:e timme (oberoende sekundar)
    scheduler.add_job(_steam_job, CronTrigger(hour="*/4", minute=30),
                      id="steam_direct", replace_existing=True)
    # review/MAU-underlag dagligen (cookie-fritt)
    scheduler.add_job(_reviews_job, CronTrigger(hour=SCRAPE_HOUR_UTC, minute=45),
                      id="reviews", replace_existing=True)
    # lanseringsbevakning: tick var 5:e minut, modulen filtrerar sjalv
    scheduler.add_job(_launch_job, CronTrigger(minute="*/5"),
                      id="launch_watch", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Fermi Cloud Ingest", lifespan=lifespan)


def _auth(token: str):
    if not EXPORT_TOKEN or token != EXPORT_TOKEN:
        raise HTTPException(status_code=401, detail="ogiltig token")


def _launch_health():
    """Antal observationer per bevakad titel senaste dygnet — upptacker
    tyst lanseringsbevakning pa samma satt som last_ingest for ovriga kallor."""
    try:
        con = ingest.get_conn()
        rows = con.execute(
            "SELECT app_id, COUNT(*), MAX(ts_utc) FROM launch_obs "
            "WHERE ts_utc >= datetime('now','-1 day') GROUP BY app_id").fetchall()
        con.close()
        watch = [{"app_id": t.get("app_id"), "label": t.get("label"),
                  "release_utc": t.get("release_utc"), "aktiv": t.get("aktiv")}
                 for t in launch_watch.LAUNCH_WATCH]
        return {"watchlist": watch,
                "obs_24h": [{"app_id": r[0], "n": r[1], "senast": r[2]} for r in rows]}
    except Exception as e:
        return {"fel": str(e)}


@app.get("/health")
def health():
    return {"status": "ok", "last_ingest": ingest.last_ingest(),
            "scrape_hour_utc": SCRAPE_HOUR_UTC,
            "launch": _launch_health()}


@app.get("/export/pdx")
def export_pdx(token: str = Query(...),
               frm: str = Query(None, alias="from"),
               to: str = Query(None)):
    """PDX-titlarnas dagsrader som JSON. Filtrera med ?from=YYYY-MM-DD&to=YYYY-MM-DD."""
    _auth(token)
    con = ingest.get_conn(); ingest.init_db(con)
    ids = ",".join("?" for _ in PDX)
    q = (f"SELECT dzien, app_id, name, min_pos, max_pos, disc_max, price, rev_cnt "
         f"FROM gts_daily WHERE app_id IN ({ids})")
    args = list(PDX.keys())
    if frm:
        q += " AND dzien >= ?"; args.append(frm)
    if to:
        q += " AND dzien <= ?"; args.append(to)
    q += " ORDER BY app_id, dzien"
    rows = con.execute(q, args).fetchall()
    con.close()
    out = [{"dzien": r[0], "app_id": r[1], "label": PDX.get(r[1], r[2]),
            "name": r[2], "min_pos": r[3], "max_pos": r[4], "disc_max": r[5],
            "price": r[6], "rev_cnt": r[7]} for r in rows]
    return JSONResponse({"count": len(out), "titles": list(PDX.values()), "rows": out})


@app.get("/export/launch")
def export_launch(token: str = Query(...), app_id: int = Query(None)):
    """Dagsaggregat av lanseringsbevakningen: peak CCU, kumulativa recensioner,
    dagligt recensionsdelta och rabattspann. Ren lasning."""
    _auth(token)
    rows = launch_watch.daily_rollup(db_path=_db_path(), app_id=app_id)
    return JSONResponse({"count": len(rows), "rows": rows})


@app.get("/run")
def manual_run(token: str = Query(...)):
    """Manuell hamtning pa begaran (token-skyddad)."""
    _auth(token)
    return ingest.run_all()


@app.get("/run/steam")
def manual_steam(token: str = Query(...)):
    """Manuell direkt-Steam-korning (token-skyddad)."""
    _auth(token)
    return steam_direct.run_direct()


@app.get("/run/reviews")
def manual_reviews(token: str = Query(...)):
    """Manuell review/MAU-hamtning (token-skyddad)."""
    _auth(token)
    return reviews.run_reviews()


@app.get("/run/launch")
def manual_launch(token: str = Query(...)):
    """Manuell lanseringsbevakningskorning (token-skyddad)."""
    _auth(token)
    return launch_watch.run_launch_watch(db_path=_db_path())


def _disc_mult(d):
    d = 0.0 if d is None else float(d)
    return 1.0 if d <= 0 else 1.5 if d < 30 else 2.0 if d <= 49 else 2.5 if d <= 69 else 3.5


@app.get("/crossval")
def crossval(token: str = Query(...)):
    """Korsvaliderar gaminganalytics (gts_daily) vs direkt-Steam (steam_gts_daily)
    pa overlappande app_id+dzien. WPV-ratio nara 1,0 = kallorna ekvivalenta."""
    _auth(token)
    con = ingest.get_conn()
    rows = con.execute("""
        SELECT g.app_id, g.dzien,
               g.min_pos, g.max_pos, g.disc_max,
               s.min_pos, s.max_pos, s.disc_max
        FROM gts_daily g JOIN steam_gts_daily s
          ON g.app_id = s.app_id AND g.dzien = s.dzien
        ORDER BY g.dzien, g.app_id""").fetchall()
    con.close()
    out = []
    for r in rows:
        ga_mid = (r[2] + r[3]) / 2.0
        st_mid = (r[5] + r[6]) / 2.0
        ga_wpv = (1 / ga_mid ** 0.65) * _disc_mult(r[4])
        st_wpv = (1 / st_mid ** 0.65) * _disc_mult(r[7])
        out.append({"app_id": r[0], "label": PDX.get(r[0], r[0]), "dzien": r[1],
                    "ga_midpos": round(ga_mid, 1), "steam_midpos": round(st_mid, 1),
                    "wpv_ratio": round(st_wpv / ga_wpv, 3) if ga_wpv else None})
    ratios = [x["wpv_ratio"] for x in out if x["wpv_ratio"]]
    med = sorted(ratios)[len(ratios) // 2] if ratios else None
    return {"overlap_days": len(out),
            "median_wpv_ratio": med,
            "verdict": ("ekvivalenta (0,95-1,05)" if med and 0.95 <= med <= 1.05
                        else "avviker - kalibrera innan byte" if med else "for lite overlapp"),
            "rows": out}
