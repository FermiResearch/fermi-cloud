# FERMI CLOUD — deploy-guide (klick for klick)

Ingen terminal, inga kommandon. Bara webblasaren.

# STEG 1 — Lagg filerna i ett GitHub-repo

1. Ga till github.com, klicka gron knapp **"New"** (nytt repo).
2. Namn: `fermi-cloud`. Valj **Private**. Klicka "Create repository".
3. Pa nasta sida: klicka **"uploading an existing file"** (bla lank i texten).
4. Dra in ALLA filer fran zippen (app.py, ingest.py, requirements.txt, render.yaml, DEPLOY.md, .gitignore). Klicka **"Commit changes"**.

# STEG 2 — Res tjansten i Render (Blueprint)

1. I Render: klicka **"New +"** uppe till hoger → **"Blueprint"**.
2. Valj repot `fermi-cloud`. Render laser `render.yaml` sjalv och visar tjansten `fermi-ingest`.
3. Klicka **"Apply"**. Render borjar bygga (nagra minuter).

# STEG 3 — Satt den hemliga nyckeln

Render fragar efter `FERMI_API_KEY` (den ar markt hemlig, committas aldrig).
1. Oppna tjansten `fermi-ingest` → flik **"Environment"**.
2. Vid `FERMI_API_KEY`, klistra in: `18593824908`. Spara.
3. Tjansten deployar om automatiskt. Vanta tills status blir **"Live"**.

# STEG 4 — Hamta din export-lank at Claude

1. Fortfarande i **"Environment"**: hitta `EXPORT_TOKEN` (Render har genererat en slumptoken). Kopiera vardet.
2. Din tjanst-URL star hogst upp, typ `https://fermi-ingest.onrender.com`.
3. Klistra in DENNA rad till Claude i chatten (byt ut token):

   `https://fermi-ingest.onrender.com/export/pdx?token=DIN_TOKEN`

Da kan Claude hamta farsk PDX-data nar du vill ha en analys — precis som Y/Y-korningen, utan att du kor nagot.

# STEG 5 — Kolla att det lever

Oppna i webblasaren: `https://fermi-ingest.onrender.com/health`
Ska visa `"status": "ok"` och ett farskt `last_ingest`-datum.

---

## Vad tjansten gor

- Hamtar Steam Top Sellers + Wishlists **varje dygn 06:15 UTC** automatiskt (ingen dator hos dig behovs).
- Skriver till `fermi_gts.db` pa en bestandig disk i Frankfurt (EU).
- `/health` visar senaste lyckade hamtning — sa tysta luckor syns direkt.
- `/run?token=...` kor en hamtning pa begaran om du nagon gang vill trigga manuellt.

## VIKTIGT — historik maste seedas separat

Moln-DB:n startar **tom** och fyller pa fran den dag den gar live. For Y/Y bakat i tiden
(t.ex. 2025 vs 2026) behover vi ladda in din befintliga historik EN gang. Nasta steg:
ladda upp `pdx_export`-zippen sa lagger Claude till en engangs-seedning. Da funkar Y/Y
i molnet direkt; annars kan molnet forst jamfora nar det samlat tva perioder sjalv.

## Kostnad
Web-tjanst (Starter, alltid pa) ~7 USD/man + disk ~0,25 USD/GB. Totalt ~80 SEK/man.

---

---

## Steam-direkt sekundar (bekraftad 2026-07-17)

- `steam_direct.py` skrapar Steams Global Top Sellers direkt (store.steampowered.com/search/results,
  filter=globaltopsellers, hwtype=0, ndl=1 - inga filter), **3000 titlar**, oberoende av gaminganalytics.
- BEKRAFTAD att matcha modellkallan: topp = CS2, Steam Machine, Palworld, Marvel Rivals, PUBG, Apex...
  och alla Core-titlar fangas pa djupet (CS2 #126 ... BL2 #1821).
- Kors **var 4:e timme** -> forfinar dygnets min/max i tabell `steam_gts_daily`. Gaminganalytics-primaren ror inte.
- `/crossval?token=...` jamfor kallorna (WPV-ratio). Byte till primar forst nar den reproducerar kand
  traffsakerhet - aldrig mitt i kvartal.
- Plain HTML, ingen protobuf, ingen webblasare, ingen access_token. Rate-limit-snallt (1,2 s/sida, backoff pa 429).
- Manuell koning: `/run/steam?token=...`  |  Valfri env: `STEAM_MAX_TITLES` (default 3000).

---

## Review/MAU sekundar (2026-07-17)

- `reviews.py` hamtar dagligt totalt recensionsantal + betyg for bevakade titlar (PDX grundspel+legacy + Coffee Stain-ankare, ~35 st) via Steams appreviews-API. INGEN cookie, ingen nyckel.
- Tabell `reviews_daily`. Kors dagligen (SCRAPE_HOUR_UTC:45). Underlag for review-velocity och MAU.
- Manuell koning: `/run/reviews?token=...`  |  Lagg till fler titlar i WATCHLIST i reviews.py.
