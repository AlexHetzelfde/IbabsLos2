#!/usr/bin/env python3
"""
scrape_aanbestedingen.py
=========================
Haalt EU-aanbestedingspublicaties (TED) van/over gemeente Zaanstad op via de
officiële, anonieme TED Search API (geen key nodig):

    POST https://api.ted.europa.eu/v3/notices/search

en bouwt daaruit één tijdlijn per aanbesteding: de "Mededinging" (oproep tot
inschrijving) en de latere "Resultaat"-publicatie (gunning of intrekking)
van dezelfde aanbesteding worden gekoppeld via de "Identificatiecode van de
procedure" — een UUID die bij beide publicaties gelijk blijft.

BELANGRIJK — onzekerheid over exacte veldnamen:
De TED API v3 retourneert velden onder eForms-veldnamen (bijv.
"publication-number", "buyer-name"). Een aantal veldnamen in dit script
(vooral: procedure-ID, CPV-code, geraamde/gegunde waarde, winnaar) zijn
gebaseerd op de meest waarschijnlijke conventie, maar niet 1-op-1 bevestigd
tegen een live voorbeeldresponse. Om die reden:
  1. Wordt PER PUBLICATIE de volledige ruwe response bewaard onder
     "ruwe_velden", naast de "opgeschoonde" velden. Als een veldnaam-gok
     fout blijkt, is de data nog steeds terug te vinden en te repareren
     zonder opnieuw te hoeven scrapen.
  2. Print het script bij de EERSTE run een overzicht van alle sleutels die
     daadwerkelijk in de response zitten, zodat je dat in de GitHub Actions-
     log kunt controleren tegen FIELDS hieronder.

Koppeling Mededinging ↔ Resultaat:
Eén record per procedure_id (of, als die ontbreekt, per publicatienummer
als losstaand record). Elk record heeft een lijst "publicaties" met alle
bijbehorende TED-publicaties (Mededinging, Resultaat, Vooraankondiging),
en een afgeleide "status": "actief lopend" / "gegund" / "ingetrokken" /
"onbekend", bepaald op basis van de laatst binnengekomen publicatie.

Output:
    data/aanbestedingen.json

Gebruik:
    python3 scrape_aanbestedingen.py

Environment:
    SCRAPE_VANAF (optioneel, formaat YYYYMMDD) — publicatiedatum-ondergrens.
    Zonder deze env var wordt STANDAARD_VANAF gebruikt (zie CONFIG hieronder).
    Dit script gebruikt bewust NIET de gedeelde SCRAPE_VANAF-tracker van
    scrape.yml (net als scrape_besluiten.py) — dedup gebeurt via het
    publicatienummer, dus een te ruim gekozen venster levert geen dubbele
    records op, alleen wat extra (overgeslagen) API-calls.
"""

import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

# Startdatum als er geen tracker/env var is — ruim gekozen zodat de eerste
# run een goede historische basis pakt. Formaat: YYYYMMDD (TED-conventie).
STANDAARD_VANAF = "20250101"

OUTPUT = "data/aanbestedingen.json"

# NUTS-code voor de Zaanstreek (regio-filter) + naam-filter op de koper,
# zoals in het plan beschreven. Beide worden gecombineerd met OR: een
# publicatie hoeft niet aan allebei te voldoen, want "buyer-name" kan soms
# net anders geschreven zijn dan verwacht, en "place-of-performance" vangt
# dan mogelijk publicaties op die buyer-name mist (en andersom).
TED_QUERY_TEMPLATE = (
    '(buyer-name~"Zaanstad" OR place-of-performance IN (NL325)) '
    'AND PD>={vanaf} SORT BY publication-date DESC'
)

# Best-gok eForms-veldnamen (kebab-case-conventie van de BT-veldlabels).
# Zie de docstring hierboven: dit is niet 100% bevestigd, vandaar dat de
# ruwe response ALTIJD apart bewaard wordt.
FIELDS = [
    "publication-number",
    "notice-title",
    "notice-type",
    "buyer-name",
    "publication-date",
    "deadline-receipt-tender-date-lot",
    "classification-cpv",
    "estimated-value-lot",
    "estimated-value-cpv",
    "tender-value-lot",
    "winner-name",
    "procedure-identifier",
    "notice-identifier",
]

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

PAGE_LIMIT = 100

# Keyword-classificatie voor het "type aankondiging" — TED's eigen
# "notice-type"-veld gebruikt codes (bv. "cn-standard", "can-standard") die
# we niet blind willen vertrouwen zonder bevestiging; we vallen daarom ook
# terug op de titel als extra check.
MEDEDINGING_KEYWORDS = ["mededinging", "aankondiging van een opdracht", "cn-standard", "aanbesteding"]
RESULTAAT_KEYWORDS   = ["gunning", "resultaat", "can-standard", "aankondiging van een gegunde opdracht"]
INTREKKING_KEYWORDS  = ["ingetrokken", "annulering", "geannuleerd", "geen gunning", "procedure ingetrokken"]


def grens_datum():
    return os.environ.get("SCRAPE_VANAF", "").strip() or STANDAARD_VANAF


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ted_search(query, page=1, retries=3, wait=4):
    """POST naar de TED Search API. Geeft de geparste JSON-response terug,
    of raised na alle retries."""
    body = json.dumps({
        "query": query,
        "fields": FIELDS,
        "limit": PAGE_LIMIT,
        "scope": "ALL",
        "checkQuerySyntax": False,
        "paginationMode": "ITERATION",
        "page": page,
    }).encode("utf-8")

    laatste_fout = None
    for poging in range(1, retries + 1):
        try:
            req = urllib.request.Request(TED_SEARCH_URL, data=body, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            fout_body = e.read().decode("utf-8", errors="replace")[:500]
            laatste_fout = f"HTTP {e.code}: {fout_body}"
            print(f"  poging {poging} mislukt: {laatste_fout}")
        except Exception as e:
            laatste_fout = str(e)
            print(f"  poging {poging} mislukt: {e}")
        if poging < retries:
            time.sleep(wait)
    raise RuntimeError(f"TED-zoekopdracht mislukt na {retries} pogingen: {laatste_fout}")


def fetch_alle_notices(vanaf):
    """Pagineert door alle resultaten van de TED-query."""
    query = TED_QUERY_TEMPLATE.format(vanaf=vanaf)
    print(f"TED-query: {query}")

    alle_notices = []
    page = 1
    while True:
        data = ted_search(query, page=page)
        notices = data.get("notices") or data.get("results") or []
        if page == 1:
            totaal = data.get("totalNoticeCount") or data.get("total") or "onbekend"
            print(f"  totaal beschikbaar volgens API: {totaal}")
            if notices:
                print(f"  sleutels in eerste resultaat (ter controle van FIELDS hierboven):")
                print(f"    {sorted(notices[0].keys())}")
        if not notices:
            break
        alle_notices.extend(notices)
        print(f"  pagina {page}: {len(notices)} resultaten (totaal tot nu toe: {len(alle_notices)})")
        if len(notices) < PAGE_LIMIT:
            break
        page += 1
        time.sleep(1)  # vriendelijk zijn voor de API — er geldt een fair-usage-limiet

    return alle_notices


def veld(notice, *kandidaten):
    """Probeert meerdere mogelijke veldnamen (voor het geval de eForms-
    conventie net anders blijkt dan verwacht) en geeft de eerste gevonden
    waarde terug. TED-velden komen vaak als lijst (meertalig / meerdere
    lots) — pakt dan het eerste element."""
    for k in kandidaten:
        if k in notice and notice[k] not in (None, "", []):
            waarde = notice[k]
            if isinstance(waarde, list):
                return waarde[0] if waarde else None
            return waarde
    return None


def bepaal_type_aankondiging(notice, titel):
    notice_type_raw = str(veld(notice, "notice-type") or "").lower()
    titel_lower = (titel or "").lower()
    tekst = notice_type_raw + " " + titel_lower

    if any(kw in tekst for kw in INTREKKING_KEYWORDS):
        return "Resultaat", "ingetrokken"
    if any(kw in tekst for kw in RESULTAAT_KEYWORDS):
        return "Resultaat", "gegund"
    if any(kw in tekst for kw in MEDEDINGING_KEYWORDS):
        return "Mededinging", "actief lopend"
    return "Onbekend", "onbekend"


def parse_notice(notice):
    titel = veld(notice, "notice-title")
    publicatienummer = veld(notice, "publication-number", "notice-identifier")
    procedure_id = veld(notice, "procedure-identifier")
    datum = veld(notice, "publication-date")
    deadline = veld(notice, "deadline-receipt-tender-date-lot")
    cpv = veld(notice, "classification-cpv")
    geraamde_waarde = veld(notice, "estimated-value-lot", "estimated-value-cpv")
    gegunde_waarde = veld(notice, "tender-value-lot")
    winnaar = veld(notice, "winner-name")
    koper = veld(notice, "buyer-name")

    type_aankondiging, status_indicatie = bepaal_type_aankondiging(notice, titel)

    return {
        "publicatienummer": publicatienummer,
        "procedure_id": procedure_id,
        "titel": titel,
        "koper": koper,
        "type_aankondiging": type_aankondiging,
        "status_indicatie": status_indicatie,
        "datum_bekendmaking": datum,
        "sluitingsdatum": deadline,
        "cpv_code": cpv,
        "geraamde_waarde": geraamde_waarde,
        "gegunde_waarde": gegunde_waarde,
        "winnaar": winnaar,
        "link": f"https://ted.europa.eu/nl/notice/-/detail/{publicatienummer}" if publicatienummer else None,
        "ruwe_velden": notice,  # NIET weggooien — vangnet voor foute veldnaam-gokken
    }


def groepeer_per_aanbesteding(publicaties):
    """Groepeert losse TED-publicaties tot één record per aanbesteding, via
    procedure_id. Publicaties zonder procedure_id worden als losstaand
    record behandeld (met publicatienummer als sleutel) — beter een niet-
    gekoppelde losse aanbesteding tonen dan 'm laten verdwijnen omdat de
    koppeling niet lukte."""
    per_procedure = {}
    losstaand = []

    for pub in publicaties:
        sleutel = pub.get("procedure_id")
        if sleutel:
            if sleutel not in per_procedure:
                per_procedure[sleutel] = {
                    "procedure_id": sleutel,
                    "titel": pub["titel"],
                    "koper": pub["koper"],
                    "cpv_code": pub["cpv_code"],
                    "publicaties": [],
                }
            per_procedure[sleutel]["publicaties"].append(pub)
        else:
            losstaand.append({
                "procedure_id": None,
                "titel": pub["titel"],
                "koper": pub["koper"],
                "cpv_code": pub["cpv_code"],
                "publicaties": [pub],
            })

    aanbestedingen = list(per_procedure.values()) + losstaand

    for a in aanbestedingen:
        a["publicaties"].sort(key=lambda p: p.get("datum_bekendmaking") or "")
        laatste = a["publicaties"][-1]
        a["status"] = laatste["status_indicatie"]
        a["laatste_update"] = laatste["datum_bekendmaking"]
        a["geraamde_waarde"] = next(
            (p["geraamde_waarde"] for p in a["publicaties"] if p["geraamde_waarde"]), None
        )
        a["gegunde_waarde"] = next(
            (p["gegunde_waarde"] for p in reversed(a["publicaties"]) if p["gegunde_waarde"]), None
        )
        a["winnaar"] = next(
            (p["winnaar"] for p in reversed(a["publicaties"]) if p["winnaar"]), None
        )
        a["sluitingsdatum"] = next(
            (p["sluitingsdatum"] for p in a["publicaties"] if p["sluitingsdatum"]), None
        )

    return aanbestedingen


def main():
    vanaf = grens_datum()
    print(f"Aanbestedingen ophalen vanaf publicatiedatum: {vanaf}")

    try:
        notices = fetch_alle_notices(vanaf)
    except Exception as e:
        print(f"✗ Ophalen mislukt: {e}")
        return

    print(f"\n{len(notices)} publicaties opgehaald van TED")

    publicaties = [parse_notice(n) for n in notices]

    zonder_procedure_id = sum(1 for p in publicaties if not p["procedure_id"])
    if zonder_procedure_id:
        print(f"  ⚠ {zonder_procedure_id} publicatie(s) zonder procedure_id — "
              f"waarschijnlijk klopt de veldnaam 'procedure-identifier' niet. "
              f"Check 'ruwe_velden' in de output om de juiste sleutel te vinden.")

    nieuwe_aanbestedingen = groepeer_per_aanbesteding(publicaties)

    # Samenvoegen met bestaande data — dedup op publicatienummer, zodat een
    # (bewust) ruim gekozen SCRAPE_VANAF geen dubbele publicaties oplevert.
    bestaand = load_json(OUTPUT, [])
    bestaande_per_procedure = {a["procedure_id"]: a for a in bestaand if a.get("procedure_id")}
    bekende_publicatienummers = {
        p["publicatienummer"] for a in bestaand for p in a.get("publicaties", [])
        if p.get("publicatienummer")
    }

    nieuw_count = 0
    for nieuwe_a in nieuwe_aanbestedingen:
        nieuwe_pubs = [
            p for p in nieuwe_a["publicaties"]
            if p["publicatienummer"] not in bekende_publicatienummers
        ]
        if not nieuwe_pubs:
            continue
        nieuw_count += len(nieuwe_pubs)

        procedure_id = nieuwe_a.get("procedure_id")
        if procedure_id and procedure_id in bestaande_per_procedure:
            bestaande_a = bestaande_per_procedure[procedure_id]
            bestaande_a["publicaties"].extend(nieuwe_pubs)
            bestaande_a["publicaties"].sort(key=lambda p: p.get("datum_bekendmaking") or "")
            laatste = bestaande_a["publicaties"][-1]
            bestaande_a["status"] = laatste["status_indicatie"]
            bestaande_a["laatste_update"] = laatste["datum_bekendmaking"]
            bestaande_a["gegunde_waarde"] = next(
                (p["gegunde_waarde"] for p in reversed(bestaande_a["publicaties"]) if p["gegunde_waarde"]),
                bestaande_a.get("gegunde_waarde"),
            )
            bestaande_a["winnaar"] = next(
                (p["winnaar"] for p in reversed(bestaande_a["publicaties"]) if p["winnaar"]),
                bestaande_a.get("winnaar"),
            )
            print(f"  ↻ update: {bestaande_a['titel'][:70]} → status {bestaande_a['status']}")
        else:
            bestaand.append(nieuwe_a)
            if procedure_id:
                bestaande_per_procedure[procedure_id] = nieuwe_a
            print(f"  + nieuw: {nieuwe_a['titel'][:70] if nieuwe_a['titel'] else '(geen titel)'}")

        for p in nieuwe_pubs:
            bekende_publicatienummers.add(p["publicatienummer"])

    save_json(OUTPUT, bestaand)
    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw_count} nieuwe publicaties · {len(bestaand)} aanbestedingen totaal in JSON")


if __name__ == "__main__":
    main()
