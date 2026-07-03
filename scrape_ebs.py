#!/usr/bin/env python3
"""
Haalt vertrektijden op van drie EBS-haltes via drgl.nl, dedupliceert ritten
op journey-ID (zodat een lijn die op meerdere haltes stopt niet dubbel
geteld wordt), en detecteert uitval / verkorte ritten / oorzaken.

Haltes:
    De Vlinder           NL:S:37223552
    Gedempte Gracht       NL:S:37223860
    Station Zaandam       NL:S:37220130   (alleen Provincialeweg)

Resultaat:
    data/ebs_uitval.json              — alleen geannuleerde ritten VAN VANDAAG
    data/ebs_totaal_teller.json       — totaal unieke ritten VAN VANDAAG
    data/ebs_percentage_historie.json — 1 samengevatte regel per afgesloten dag
                                         (totaal, cancelled, pct) — groeit met
                                         maar ~365 regels per jaar, blijft klein

Zodra een nieuwe dag begint, wordt de vorige dag automatisch samengevat naar
ebs_percentage_historie.json en verdwijnen de losse ritten uit ebs_uitval.json
en ebs_totaal_teller.json. Zo groeien die twee bestanden nooit onbeperkt door.

Elke rit is een uniek record op (journey_id + datum). Eén fysieke rit die
op meerdere van de drie haltes stopt, wordt samengevoegd tot één record
met een lijst van haltebezoeken (elk met eigen tijd/status).

Status per rit (op rit-niveau, "ergste" status wint over de haltes heen):
    cancelled   — geannuleerd op minstens één halte
    verkort     — voortijdig beëindigd ("Terminates at ...")
    gereden     — normaal gereden (op tijd, vertraagd, of al vertrokken)

Gebruik:
    python3 scrape_ebs.py

Draai dit elke 15 minuten (bijv. via cron-jobs.org die een GitHub Actions
workflow_dispatch triggert) zodat ritten die kort van tevoren worden
geannuleerd niet gemist worden voordat ze van het bord verdwijnen.
"""

import json
import os
import re
import time
import urllib.request
import http.cookiejar
from datetime import datetime

try:
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit(
        "BeautifulSoup ontbreekt. Installeer met:\n"
        "  pip install beautifulsoup4 --break-system-packages"
    )

# ── CONFIGURATIE ──────────────────────────────────────────────────────────────
BASE_URL = "https://drgl.nl"
OUTPUT           = "data/ebs_uitval.json"
TELLER_BESTAND   = "data/ebs_totaal_teller.json"
HISTORIE_BESTAND = "data/ebs_percentage_historie.json"

HALTES = [
    {"id": "NL:S:37223552", "naam": "De Vlinder"},
    {"id": "NL:S:37223860", "naam": "Gedempte Gracht"},
    {"id": "NL:S:37220130", "naam": "Station Zaandam (Provincialeweg)"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

OORZAAK_CATEGORIEEN = [
    "Personeel", "Logistiek", "Verkeer", "Stremming/Omleiding",
    "Materieel", "Weersomstandigheden", "Overig",
]


# ── DAGDEEL ────────────────────────────────────────────────────────────────────
def bepaal_dagdeel(tijd_str):
    """tijd_str = 'HH:MM' (geplande tijd, zonder vertraging/marker)."""
    try:
        uur = int(tijd_str.split(":")[0])
    except Exception:
        return "onbekend"
    if 7 <= uur < 9:
        return "ochtendspits"
    if 9 <= uur < 16:
        return "dal"
    if 16 <= uur < 19:
        return "avondspits"
    if 19 <= uur < 24:
        return "avond"
    return "nacht"


# ── PARSING ────────────────────────────────────────────────────────────────────
def parse_geplande_tijd(tijd_tekst):
    """
    'ott-departure-time' tekst kan zijn: '16:34', '16:11 +8', '16:21 -1',
    '16:35 ?'. We willen alleen de geplande tijd (HH:MM) en de vertraging.
    """
    tijd_tekst = re.sub(r"\s+", " ", tijd_tekst).strip()
    m = re.match(r"(\d{1,2}:\d{2})\s*([+-]\d+)?", tijd_tekst)
    if not m:
        return None, None
    tijd = m.group(1)
    vertraging = int(m.group(2)) if m.group(2) else 0
    return tijd, vertraging


def parse_oorzaak_categorieen(cause_tekst):
    """Splitst 'Cause: Personeel, Logistiek' in losse categorieën."""
    if not cause_tekst:
        return []
    cause_tekst = re.sub(r"^Cause:\s*", "", cause_tekst).strip()
    return [c.strip() for c in cause_tekst.split(",") if c.strip()]


def parse_halte_html(html, halte_id, halte_naam):
    """
    Parseert de departureboard-HTML van één halte.
    Retourneert lijst van dicts, één per vertrek.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("div.list-group > a.list-group-item")

    resultaten = []
    for item in items:
        href = item.get("href", "")
        m = re.match(r"/journey/([^/]+)/(\d{8})/", href)
        if not m:
            continue  # dit is de alert/mededeling-regel bovenaan, geen vertrek
        journey_id, datum_ruw = m.group(1), m.group(2)
        datum = f"{datum_ruw[0:4]}-{datum_ruw[4:6]}-{datum_ruw[6:8]}"

        tijd_div = item.select_one(".ott-departure-time")
        if not tijd_div:
            continue
        tijd_classes = tijd_div.get("class", [])
        tijd_tekst   = tijd_div.get_text(" ", strip=True)
        geplande_tijd, vertraging = parse_geplande_tijd(tijd_tekst)
        if not geplande_tijd:
            continue

        is_cancelled = (
            "ott-tripstatus-cancel" in tijd_classes
            or "ott-departure-cancel" in tijd_classes
        )
        is_vertrokken = item.select_one(".ott-departed") is not None
        is_realtime   = tijd_div.select_one("img.realtime-indication") is not None
        is_onbekend   = "ott-tripstatus-unknown" in tijd_classes

        platform_div = item.select_one(".ott-platform")
        platform = platform_div.get_text(strip=True) if platform_div else None

        linecode_div = item.select_one(".ott-linecode")
        lijn      = linecode_div.get_text(strip=True) if linecode_div else None
        style_attr = linecode_div.get("style", "") if linecode_div else ""
        kleur_m   = re.search(r"background\s*:\s*(#[0-9a-fA-F]{3,6})", style_attr)
        lijnkleur = kleur_m.group(1) if kleur_m else None

        dest_div = item.select_one(".ott-destination")
        bestemming = dest_div.get_text(strip=True) if dest_div else None

        cat_div  = item.select_one(".ott-productcategory")
        categorie = None
        if cat_div:
            cat_tekst = cat_div.get_text(" ", strip=True)
            categorie = cat_tekst.split("•")[0].split("\u2022")[0].split("&bull;")[0].strip()

        # Notices: alert (Cancelled / Terminates at ...), Cause, Advice
        notice_alert = None
        cause_raw    = None
        advice       = None
        for span in item.select("span.notice"):
            tekst = span.get_text(" ", strip=True)
            if "notice-alert" in (span.get("class") or []):
                notice_alert = tekst
            elif tekst.startswith("Cause:"):
                cause_raw = tekst
            elif tekst.startswith("Advice:"):
                advice = tekst

        is_verkort = bool(notice_alert and notice_alert.lower().startswith("terminates"))

        if is_cancelled:
            status = "cancelled"
        elif is_verkort:
            status = "verkort"
        else:
            status = "gereden"

        resultaten.append({
            "journey_id":     journey_id,
            "datum":          datum,
            "halte_id":       halte_id,
            "halte_naam":     halte_naam,
            "geplande_tijd":  geplande_tijd,
            "vertraging_min": vertraging,
            "status":         status,
            "vertrokken":     is_vertrokken,
            "realtime":       is_realtime,
            "onbekend":       is_onbekend,
            "platform":       platform,
            "lijn":           lijn,
            "lijnkleur":      lijnkleur,
            "bestemming":     bestemming,
            "categorie":      categorie,
            "terminus_alert": notice_alert if is_verkort else None,
            "oorzaak_raw":    cause_raw,
            "oorzaak_categorieen": parse_oorzaak_categorieen(cause_raw),
            "advies":         advice,
        })

    return resultaten


def fetch_halte(opener, halte_id):
    url = f"{BASE_URL}/stop/{halte_id}"
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── SAMENVOEGEN OVER HALTES ───────────────────────────────────────────────────
STATUS_PRIORITEIT = {"cancelled": 2, "verkort": 1, "gereden": 0}


def combineer_ritten(alle_vertrekken):
    """
    Groepeert vertrekken per (journey_id, datum) — dat is dezelfde fysieke
    rit, ongeacht via welke halte we hem zagen. Voorkomt dat bv. lijn 395
    dubbel geteld wordt omdat hij op meerdere van onze haltes stopt.
    """
    ritten = {}
    for v in alle_vertrekken:
        key = (v["journey_id"], v["datum"])
        if key not in ritten:
            ritten[key] = {
                "id":          f"{v['journey_id']}_{v['datum']}",
                "journey_id":  v["journey_id"],
                "datum":       v["datum"],
                "lijn":        v["lijn"],
                "lijnkleur":   v["lijnkleur"],
                "categorie":   v["categorie"],
                "status":      v["status"],
                "oorzaak_categorieen": list(v["oorzaak_categorieen"]),
                "oorzaak_raw": v["oorzaak_raw"],
                "terminus_alert": v["terminus_alert"],
                "advies":      v["advies"],
                "haltes":      [],
            }
        rit = ritten[key]

        # Status: ergste status over alle haltebezoeken wint
        if STATUS_PRIORITEIT[v["status"]] > STATUS_PRIORITEIT[rit["status"]]:
            rit["status"] = v["status"]
        if v["oorzaak_raw"] and not rit["oorzaak_raw"]:
            rit["oorzaak_raw"] = v["oorzaak_raw"]
            rit["oorzaak_categorieen"] = list(v["oorzaak_categorieen"])
        if v["terminus_alert"] and not rit["terminus_alert"]:
            rit["terminus_alert"] = v["terminus_alert"]
        if v["advies"] and not rit["advies"]:
            rit["advies"] = v["advies"]

        rit["haltes"].append({
            "halte_id":       v["halte_id"],
            "halte_naam":     v["halte_naam"],
            "geplande_tijd":  v["geplande_tijd"],
            "vertraging_min": v["vertraging_min"],
            "status":         v["status"],
            "vertrokken":     v["vertrokken"],
            "realtime":       v["realtime"],
            "platform":       v["platform"],
            "bestemming":     v["bestemming"],
        })

    # Referentietijd voor dagdeel-indeling = vroegste geplande tijd over de haltes
    for rit in ritten.values():
        tijden = sorted(h["geplande_tijd"] for h in rit["haltes"])
        rit["eerste_tijd"] = tijden[0] if tijden else None
        rit["dagdeel"]     = bepaal_dagdeel(rit["eerste_tijd"]) if rit["eerste_tijd"] else "onbekend"
        rit["haltes"].sort(key=lambda h: h["geplande_tijd"])

    return ritten


# ── BESTAANDE DATA ────────────────────────────────────────────────────────────
def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {r["id"]: r for r in data}


# ── TELLER VOOR TOTAAL RITTEN ─────────────────────────────────────────────────
def laad_teller():
    if not os.path.exists(TELLER_BESTAND):
        return {}
    with open(TELLER_BESTAND, encoding="utf-8") as f:
        return json.load(f)


def bewaar_teller(teller):
    os.makedirs("data", exist_ok=True)
    with open(TELLER_BESTAND, "w", encoding="utf-8") as f:
        json.dump(teller, f, ensure_ascii=False, indent=2)


# ── HISTORIE (1 samengevatte regel per afgesloten dag) ────────────────────────
def laad_historie():
    if not os.path.exists(HISTORIE_BESTAND):
        return {}
    with open(HISTORIE_BESTAND, encoding="utf-8") as f:
        return json.load(f)


def bewaar_historie(historie):
    os.makedirs("data", exist_ok=True)
    with open(HISTORIE_BESTAND, "w", encoding="utf-8") as f:
        json.dump(historie, f, ensure_ascii=False, indent=2)


def archiveer_oude_dagen(vandaag, teller, bestaand_ruw):
    """
    Zet elke dag die niet 'vandaag' is om in één samengevatte regel in
    ebs_percentage_historie.json, en verwijdert die dag daarna uit de
    teller. De aanroeper is verantwoordelijk voor het filteren van
    bestaand_ruw (ebs_uitval.json) op alleen 'vandaag' ná deze aanroep.
    """
    oude_datums = {d for d in teller if d != vandaag}
    oude_datums |= {r["datum"] for r in bestaand_ruw.values() if r["datum"] != vandaag}
    if not oude_datums:
        return teller

    historie = laad_historie()
    gewijzigd = False
    for oude_datum in oude_datums:
        if oude_datum in historie:
            continue
        totaal    = teller.get(oude_datum, {}).get("totaal", 0)
        cancelled = sum(1 for r in bestaand_ruw.values() if r["datum"] == oude_datum)
        pct = round(cancelled / totaal * 100, 1) if totaal else 0
        historie[oude_datum] = {"totaal": totaal, "cancelled": cancelled, "pct": pct}
        gewijzigd = True

    if gewijzigd:
        bewaar_historie(historie)
        print(f"  Historie bijgewerkt met {len(oude_datums)} afgesloten dag(en)")

    for oude_datum in oude_datums:
        teller.pop(oude_datum, None)

    return teller


# ── HOOFDPROGRAMMA ────────────────────────────────────────────────────────────
def main():
    nu = datetime.now()
    vandaag = nu.strftime("%Y-%m-%d")
    print(f"EBS-uitval scrape gestart om {nu.strftime('%Y-%m-%d %H:%M:%S')}")

    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    alle_vertrekken = []
    for halte in HALTES:
        print(f"  {halte['naam']} ({halte['id']})...", end=" ", flush=True)
        try:
            html = fetch_halte(opener, halte["id"])
            vertrekken = parse_halte_html(html, halte["id"], halte["naam"])
            alle_vertrekken.extend(vertrekken)
            n_cancel = sum(1 for v in vertrekken if v["status"] == "cancelled")
            print(f"OK — {len(vertrekken)} vertrekken, {n_cancel} cancelled")
        except Exception as e:
            print(f"MISLUKT ({e})")
        time.sleep(0.5)

    if not alle_vertrekken:
        print("Geen vertrekken opgehaald — stoppen zonder wijzigingen.")
        return

    # Combineer alle vertrekken tot unieke ritten (ongeacht status)
    nieuwe_ritten = combineer_ritten(alle_vertrekken)

    # ── OUDE DAGEN ARCHIVEREN & OPRUIMEN ──────────────────
    # Alles wat nu nog in ebs_uitval.json / ebs_totaal_teller.json staat en
    # niet van vandaag is, wordt samengevat naar de historie en daarna
    # weggegooid, zodat beide bestanden nooit onbeperkt groeien.
    teller       = laad_teller()
    bestaand_ruw = load_existing()
    teller       = archiveer_oude_dagen(vandaag, teller, bestaand_ruw)

    if vandaag not in teller:
        teller[vandaag] = {"totaal": 0, "journeys": []}

    # Alle journey_id's van vandaag (alleen unieke, dat zijn ze al in 'nieuwe_ritten')
    ids_vandaag = {rit["journey_id"] for rit in nieuwe_ritten.values() if rit["datum"] == vandaag}
    bestaande_ids = set(teller[vandaag]["journeys"])
    nieuwe_ids = ids_vandaag - bestaande_ids

    if nieuwe_ids:
        teller[vandaag]["totaal"] += len(nieuwe_ids)
        teller[vandaag]["journeys"].extend(nieuwe_ids)
        print(f"  Teller: +{len(nieuwe_ids)} unieke ritten vandaag → totaal {teller[vandaag]['totaal']}")

    bewaar_teller(teller)

    # ── ALLEEN CANCELLED VAN VANDAAG OPSLAAN ──────────────
    bestaand = {
        rid: r for rid, r in bestaand_ruw.items()
        if r.get("status") == "cancelled" and r.get("datum") == vandaag
    }

    nieuw_count = 0
    update_count = 0
    hersteld_count = 0
    for rid, rit in nieuwe_ritten.items():
        if rit["datum"] != vandaag:
            continue

        if rit["status"] == "cancelled":
            rit["bijgewerkt"] = nu.strftime("%Y-%m-%d %H:%M:%S")
            if rid in bestaand:
                rit["eerst_gezien"] = bestaand[rid].get("eerst_gezien", rit["bijgewerkt"])
                update_count += 1
            else:
                rit["eerst_gezien"] = rit["bijgewerkt"]
                nieuw_count += 1
            bestaand[rid] = rit
        else:
            # Rit stond nog op het bord maar is niet (meer) cancelled — als hij
            # eerder wél als cancelled was opgeslagen, is de annulering
            # kennelijk teruggedraaid. Verwijder 'm dan uit de uitval-lijst.
            if rid in bestaand:
                del bestaand[rid]
                hersteld_count += 1

    resultaat = sorted(
        bestaand.values(),
        key=lambda r: (r.get("datum") or "", r.get("eerste_tijd") or ""),
        reverse=True,
    )

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw_count} nieuwe geannuleerde ritten · {update_count} bijgewerkt")
    if hersteld_count:
        print(f"  {hersteld_count} eerder geannuleerde rit(ten) bleken hersteld en zijn verwijderd")
    print(f"  {len(resultaat)} geannuleerde ritten vandaag ({vandaag}) in JSON")


if __name__ == "__main__":
    main()
