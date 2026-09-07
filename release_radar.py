"""
release_radar.py — Fermi releasebevakning, hela Steam

Hittar och rangordnar kommande releaser som ar finansiellt relevanta, och
signalerar nar nagot andras. Kor INGEN modell.

BYGGER PA BEFINTLIG DATA. Ingen ny hamtning, inga nya rate-limits.
ingest.run_wishlists() skriver redan topp 1000 mest onskade till
wishlist_daily varje dygn, med namn, pris, releasedatum, utvecklare och
utgivare. Radarn laser den tabellen.

Att ligga i topp 1000 mest onskade ar i sig ett storleksfilter — en titel
som inte gor det kommer inte flytta nagon kvartalsrapport.

Signaler som genereras:
  NY            titel syns i topp-listan for forsta gangen
  KLATTRAR      positionen forbattras kraftigt over kort tid
  FALLER        positionen forsamras kraftigt
  NARA          release inom kort och positionen ar stark
  DATUM         releasedatum har andrats
  SLAPPT        titeln har passerat sitt releasedatum
"""

import sqlite3
from datetime import datetime, timezone, date, timedelta

import ingest

# ---------------------------------------------------------------- trosklar

# Storleksklasser efter basta uppnadda onskelisteplacering.
# Grovt kalibrerade: TF3 lag pa 88 och ar en fullprisrelease fran en
# noterad utgivare — det ar mitten av KLASS_B.
KLASS = [
    (0,   25,  "A", "Mycket stor"),
    (25,  120, "B", "Stor"),
    (120, 350, "C", "Medel"),
    (350, 10**9, "D", "Liten"),
]

NARA_DAGAR = 45        # release inom sa manga dagar ger NARA-signal
NARA_POS = 300         # ...om positionen ar battre an detta
RORELSE_POS = 40       # positionsforandring som kravs for KLATTRAR/FALLER
RORELSE_DAGAR = 14     # ...matt over sa manga dagar
MIN_PRIS = 9.99        # titlar under detta ar sallan finansiellt relevanta

# Datumplatshallare i kallan. wishlist_daily satter 31 dec for titlar
# utan kant releasedatum — behandla som OKANT, aldrig som ett datum.
PLATSHALLARE = ("2026-12-31", "2027-12-31", "2025-12-31", "")


DDL = """
CREATE TABLE IF NOT EXISTS radar_titel (
    app_id        TEXT PRIMARY KEY,
    namn          TEXT,
    utgivare      TEXT,
    utvecklare    TEXT,
    release_date  TEXT,
    datum_kant    INTEGER,
    pris          REAL,
    forst_sedd    TEXT,
    sist_sedd     TEXT,
    basta_pos     INTEGER,
    senaste_pos   INTEGER,
    klass         TEXT,
    slappt        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS radar_signal (
    ts_utc   TEXT NOT NULL,
    app_id   TEXT NOT NULL,
    typ      TEXT NOT NULL,
    namn     TEXT,
    klass    TEXT,
    pos      INTEGER,
    dagar    INTEGER,
    text     TEXT,
    PRIMARY KEY (ts_utc, app_id, typ)
);
CREATE INDEX IF NOT EXISTS ix_radar_signal_ts ON radar_signal (ts_utc DESC);
"""


def ensure_schema(conn):
    conn.executescript(DDL)
    conn.commit()


def klassa(pos):
    if pos is None:
        return "D"
    for lo, hi, kod, _ in KLASS:
        if lo <= pos < hi:
            return kod
    return "D"


def klassnamn(kod):
    for _, _, k, namn in KLASS:
        if k == kod:
            return namn
    return "Okand"


def _dag(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _kant_datum(s):
    """Skiljer ett riktigt releasedatum fran kallans platshallare."""
    if not s or str(s)[:10] in PLATSHALLARE:
        return None
    return _dag(s)


# ---------------------------------------------------------------- korning

def run_radar(db_path=None, now=None):
    """Laser wishlist_daily, uppdaterar radar_titel och skapar signaler.
    Idempotent: kan koras hur ofta som helst, signaler dubbleras inte."""
    now = now or datetime.now(timezone.utc)
    idag = now.date()
    ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    conn = sqlite3.connect(db_path or ingest.DB_PATH, timeout=30)
    ensure_schema(conn)

    # Senaste dygnets rader ur onskelistan
    senaste_dag = conn.execute(
        "SELECT MAX(dzien) FROM wishlist_daily").fetchone()[0]
    if not senaste_dag:
        conn.close()
        return {"status": "ingen wishlist-data"}

    rader = conn.execute(
        "SELECT app_id, name, min_pos, price, release_date, developers, publishers "
        "FROM wishlist_daily WHERE dzien = ?", (senaste_dag,)).fetchall()

    signaler = []
    nya = uppdaterade = 0

    for app_id, namn, pos, pris, rel, dev, pub in rader:
        if pos is None or pos <= 0:
            continue
        if pris is not None and pris > 0 and pris < MIN_PRIS:
            continue

        rd = _kant_datum(rel)
        datum_kant = 1 if rd else 0
        dagar_kvar = (rd - idag).days if rd else None
        kl = klassa(pos)

        gammal = conn.execute(
            "SELECT basta_pos, senaste_pos, release_date, klass, slappt "
            "FROM radar_titel WHERE app_id = ?", (app_id,)).fetchone()

        if gammal is None:
            conn.execute(
                "INSERT INTO radar_titel (app_id, namn, utgivare, utvecklare, "
                "release_date, datum_kant, pris, forst_sedd, sist_sedd, "
                "basta_pos, senaste_pos, klass, slappt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (app_id, namn, pub, dev, rel, datum_kant, pris,
                 senaste_dag, senaste_dag, pos, pos, kl))
            nya += 1
            if kl in ("A", "B", "C"):
                signaler.append((ts, app_id, "NY", namn, kl, pos, dagar_kvar,
                    f"Ny i toppen pa plats {pos}. Klass {kl}, {klassnamn(kl)}."))
        else:
            g_basta, g_senaste, g_rel, g_klass, g_slappt = gammal
            basta = min(g_basta, pos) if g_basta else pos
            conn.execute(
                "UPDATE radar_titel SET namn=?, utgivare=?, utvecklare=?, "
                "release_date=?, datum_kant=?, pris=?, sist_sedd=?, basta_pos=?, "
                "senaste_pos=?, klass=? WHERE app_id=?",
                (namn, pub, dev, rel, datum_kant, pris, senaste_dag, basta,
                 pos, klassa(basta), app_id))
            uppdaterade += 1

            if g_rel and rel and str(g_rel)[:10] != str(rel)[:10]:
                signaler.append((ts, app_id, "DATUM", namn, klassa(basta), pos,
                    dagar_kvar,
                    f"Releasedatum andrat fran {str(g_rel)[:10]} till {str(rel)[:10]}."))

            # rorelse matt mot positionen for RORELSE_DAGAR sedan
            fore = conn.execute(
                "SELECT min_pos FROM wishlist_daily WHERE app_id=? AND dzien<=? "
                "ORDER BY dzien DESC LIMIT 1",
                (app_id, (_dag(senaste_dag) - timedelta(days=RORELSE_DAGAR)).isoformat())
            ).fetchone()
            if fore and fore[0]:
                diff = fore[0] - pos          # positivt = klattrat
                if diff >= RORELSE_POS:
                    signaler.append((ts, app_id, "KLATTRAR", namn, klassa(basta),
                        pos, dagar_kvar,
                        f"Klattrat {diff} platser pa {RORELSE_DAGAR} dagar, "
                        f"fran {fore[0]} till {pos}."))
                elif -diff >= RORELSE_POS:
                    signaler.append((ts, app_id, "FALLER", namn, klassa(basta),
                        pos, dagar_kvar,
                        f"Fallit {-diff} platser pa {RORELSE_DAGAR} dagar, "
                        f"fran {fore[0]} till {pos}."))

            if not g_slappt and rd and dagar_kvar is not None and dagar_kvar < 0:
                conn.execute("UPDATE radar_titel SET slappt=1 WHERE app_id=?",
                             (app_id,))
                signaler.append((ts, app_id, "SLAPPT", namn, klassa(basta), pos,
                    dagar_kvar, f"Slapptes {rd.isoformat()}. Basta position {basta}."))

        if rd and dagar_kvar is not None and 0 <= dagar_kvar <= NARA_DAGAR \
                and pos <= NARA_POS:
            signaler.append((ts, app_id, "NARA", namn, kl, pos, dagar_kvar,
                f"Slapps om {dagar_kvar} dagar. Position {pos}, klass {kl}."))

    for s in signaler:
        conn.execute("INSERT OR IGNORE INTO radar_signal "
                     "(ts_utc, app_id, typ, namn, klass, pos, dagar, text) "
                     "VALUES (?,?,?,?,?,?,?,?)", s)
    conn.commit()
    conn.close()

    return {"dzien": senaste_dag, "granskade": len(rader), "nya": nya,
            "uppdaterade": uppdaterade, "signaler": len(signaler)}


# ---------------------------------------------------------------- lasning

def senaste_signaler(db_path=None, dagar=7, klasser=("A", "B", "C")):
    """Signaler fran de senaste dygnen, viktigast forst."""
    conn = sqlite3.connect(db_path or ingest.DB_PATH, timeout=30)
    ensure_schema(conn)
    grans = (datetime.now(timezone.utc) - timedelta(days=dagar)) \
        .isoformat().replace("+00:00", "Z")
    p = ",".join("?" * len(klasser))
    rader = conn.execute(
        f"SELECT ts_utc, app_id, typ, namn, klass, pos, dagar, text "
        f"FROM radar_signal WHERE ts_utc >= ? AND klass IN ({p}) "
        f"ORDER BY klass, ts_utc DESC", [grans, *klasser]).fetchall()
    conn.close()
    return [{"ts": r[0], "app_id": r[1], "typ": r[2], "namn": r[3],
             "klass": r[4], "pos": r[5], "dagar_till_release": r[6],
             "text": r[7]} for r in rader]


def kommande(db_path=None, dagar=90, klasser=("A", "B", "C")):
    """Bevakningslista: kommande releaser inom fonstret, starkast forst."""
    conn = sqlite3.connect(db_path or ingest.DB_PATH, timeout=30)
    ensure_schema(conn)
    p = ",".join("?" * len(klasser))
    rader = conn.execute(
        f"SELECT app_id, namn, utgivare, release_date, pris, basta_pos, "
        f"senaste_pos, klass FROM radar_titel "
        f"WHERE slappt = 0 AND datum_kant = 1 AND klass IN ({p}) "
        f"ORDER BY basta_pos", list(klasser)).fetchall()
    conn.close()
    idag = date.today()
    ut = []
    for app_id, namn, pub, rel, pris, basta, senaste, kl in rader:
        rd = _kant_datum(rel)
        if not rd:
            continue
        kvar = (rd - idag).days
        if kvar < 0 or kvar > dagar:
            continue
        ut.append({"app_id": app_id, "namn": namn, "utgivare": pub,
                   "release": rd.isoformat(), "dagar_kvar": kvar,
                   "pris": pris, "basta_pos": basta, "senaste_pos": senaste,
                   "klass": kl, "klassnamn": klassnamn(kl)})
    return ut


if __name__ == "__main__":
    import json
    print(json.dumps(run_radar(), indent=2, ensure_ascii=False))
