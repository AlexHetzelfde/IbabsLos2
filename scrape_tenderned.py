#!/usr/bin/env python3
"""
scrape_tenderned.py
====================
Haalt aanbestedingspublicaties van gemeente Zaanstad op via TenderNed's
publieke, anonieme JSON-webservice (TNS) — GEEN account nodig, in
tegenstelling tot TenderNed's "echte" XML-API (die momenteel een wachtlijst
van maanden heeft voor nieuwe aanvragen).

WAAROM DIT SCRIPT NAAST scrape_aanbestedingen.py BESTAAT:
scrape_aanbestedingen.py haalt alleen bij TED op (de Europese
aankondigingendienst). TED bevat uitsluitend publicaties boven de
EU-drempel (~€221.000 voor diensten/leveringen). TenderNed is de bron
waar Zaanstad zelf op publiceert, en niet alles daarvan wordt doorgezet
naar TED — met name nationale en net-onder-drempel procedures blijven
TenderNed-only. Dit script vult dus een structureel gat, geen duplicaat
van scrape_aanbestedingen.py.

BRON VAN DE QUERY-PARAMETERS:
Dit is GEEN gedocumenteerde, gegarandeerde API — het is de interne
webservice die tenderned.nl/aankondigingen/overzicht zelf aanroept vanuit
de browser (bevestigd via de Network-tab: filter op "Aanbestedende dienst"
levert exact de twee aanroepen hieronder op). TenderNed's eigen docs
waarschuwen expliciet: "deze Webservice kan zonder voorafgaande
kennisgeving gewijzigd worden." Vandaar de volledige 'ruwe_velden' als
vangnet (zelfde patroon als scrape_aanbestedingen.py) en een duidelijke
foutmelding als het schema verandert, in plaats van een stille lege lijst.

Twee endpoints:
  1. GET /aanbestedendediensten?search=<naam>  — zoekt de interne
     organisatie-ID op naam. Nodig omdat filteren op naam zelf NIET werkt
     (geprobeerd en genegeerd door de server) — het moet via dit ID.
  2. GET /publicaties?...&aanbestedendeDienstId=<id> — de gefilterde lijst.

Wat dit script NIET doet:
Aankondiging en gunning van dezelfde procedure worden NIET aan elkaar
gekoppeld (zoals scrape_aanbestedingen.py wel doet via procedure-identifier
voor TED). Het 'kenmerk'-veld in de TenderNed-respons lijkt een interne
referentie die procedures zou kunnen koppelen, maar dat is niet geverifieerd
tegen een bevestigd aankondiging/gunning-paar. Elke publicatie is hier een
eigen record. Zodra er een paar dagen echte Zaanstad-data binnen zijn,
controleer of twee publicaties over hetzelfde onderwerp hetzelfde 'kenmerk'
delen — zo ja, is een koppeling zoals bij TED een logische volgende stap.

Output:
    data/tenderned.json

Gebruik:
    python3 scrape_tenderned.py

Environment:
    AANBESTEDENDE_DIENST (optioneel) — naam om op te zoeken, standaard "Zaanstad".
"""

import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2"
ZOEK_URL = f"{BASE}/aanbestedendediensten"
PUBLICATIES_URL = f"{BASE}/publicaties"

OUTPUT = "data/tenderned.json"
PAGE_SIZE = 50

# Harde noodstop — zelfde beveiliging als MAX_PAGINAS in scrape_aanbestedingen.py.
MAX_PAGINAS = 30

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}


def _get_json(url, retries=3, wachttijden=(2, 5, 10)):
    laatste_fout = None
    for poging in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            laatste_fout = e
            if poging < retries:
                wacht = wachttijden[min(poging - 1, len(wachttijden) - 1)]
                print(f"(poging {poging}/{retries} mislukt: {e} — {wacht}s wachten)", end=" ", flush=True)
                time.sleep(wacht)
    raise laatste_fout


def zoek_aanbestedende_dienst(naam):
    """
    Zoekt de interne organisatie-ID op via de TNS-zoek-endpoint. Geeft
    (id, gevonden_naam) terug, of (None, None) als er niets matcht.
    Bij meerdere treffers: voorkeur voor een naam die exact "gemeente
    <naam>" is (case-insensitief); anders de eerste treffer, met een
    waarschuwing, zodat een verkeerde match nooit stilzwijgend gebeurt.
    """
    url = f"{ZOEK_URL}?{urllib.parse.urlencode({'page': 0, 'size': 10, 'search': naam})}"
    data = _get_json(url)
    kandidaten = data.get("content") or data.get("aanbestedendeDiensten") or []
    if not kandidaten:
        return None, None

    exact = [
        k for k in kandidaten
        if (k.get("naam") or k.get("aanbestedendeDienstNaam") or "").strip().lower()
        == f"gemeente {naam}".strip().lower()
    ]
    gekozen = exact[0] if exact else kandidaten[0]
    gekozen_naam = gekozen.get("naam") or gekozen.get("aanbestedendeDienstNaam")
    gekozen_id = gekozen.get("id") or gekozen.get("aanbestedendeDienstId")

    if not exact and len(kandidaten) > 1:
        print(f"  ⚠ meerdere treffers voor '{naam}', geen exacte match — "
              f"gekozen: '{gekozen_naam}' (controleer dit handmatig in data/tenderned.json)")

    return gekozen_id, gekozen_naam


def fetch_publicaties(dienst_id, bekende_ids):
    """
    Pagineert door /publicaties, gefilterd op aanbestedendeDienstId.
    Early-stop zodra een hele pagina al bekende publicatieId's bevat —
    zelfde patroon als de early-stop pagination in scrape_moties.py en
    scrape_collegeberichten.py (de lijst komt nieuwste-eerst binnen).
    """
    alle = []
    for pagina in range(MAX_PAGINAS):
        params = {
            "page": pagina,
            "size": PAGE_SIZE,
            "aanbestedendeDienstId": dienst_id,
        }
        url = f"{PUBLICATIES_URL}?{urllib.parse.urlencode(params)}"
        data = _get_json(url)
        content = data.get("content") or []

        if pagina == 0:
            totaal = data.get("totalElements", "onbekend")
            print(f"  totaal beschikbaar voor deze dienst: {totaal}")
            if not content:
                break

        if not content:
            break

        alle.extend(content)

        nieuw_op_pagina = sum(
            1 for p in content if str(p.get("publicatieId")) not in bekende_ids
        )
        if nieuw_op_pagina == 0 and pagina > 0:
            print(f"  (pagina {pagina} volledig al bekend — paginering gestopt)")
            break

        if data.get("last", True):
            break

        time.sleep(0.3)

    return alle


def parse_publicatie(p):
    return {
        "publicatie_id":       str(p.get("publicatieId")) if p.get("publicatieId") else None,
        "kenmerk":             p.get("kenmerk"),  # NIET geverifieerd als koppel-sleutel, zie docstring
        "titel":               p.get("aanbestedingNaam"),
        "opdrachtgever":       p.get("opdrachtgeverNaam"),
        "type_publicatie":     (p.get("typePublicatie") or {}).get("omschrijving"),
        "type_publicatie_code":(p.get("typePublicatie") or {}).get("code"),
        "publicatiecode":      (p.get("publicatiecode") or {}).get("omschrijving"),
        "procedure":           (p.get("procedure") or {}).get("omschrijving"),
        "type_opdracht":       (p.get("typeOpdracht") or {}).get("omschrijving"),
        "datum_publicatie":    p.get("publicatieDatum"),
        "sluitingsdatum":      p.get("sluitingsDatum"),
        "europees":            p.get("europees"),
        "digitaal":            p.get("digitaal"),
        "omschrijving":        p.get("opdrachtBeschrijving"),
        "link":                (p.get("link") or {}).get("href")
                                or (f"https://www.tenderned.nl/aankondigingen/overzicht/{p.get('publicatieId')}"
                                    if p.get("publicatieId") else None),
        "ruwe_velden":         p,  # vangnet — zie docstring
    }


def load_existing():
    if not os.path.exists(OUTPUT):
        return []
    with open(OUTPUT, encoding="utf-8") as f:
        return json.load(f)


def main():
    naam = os.environ.get("AANBESTEDENDE_DIENST", "Zaanstad").strip()
    print(f"Aanbestedende dienst opzoeken: '{naam}'...", end=" ", flush=True)
    try:
        dienst_id, gevonden_naam = zoek_aanbestedende_dienst(naam)
    except Exception as e:
        print(f"\n✗ Opzoeken mislukt: {e}")
        return

    if not dienst_id:
        print(f"\n✗ Geen aanbestedende dienst gevonden voor '{naam}' — "
              f"controleer of TenderNed's zoek-endpoint van vorm is veranderd.")
        return
    print(f"OK — '{gevonden_naam}' (id: {dienst_id})")

    bestaand = load_existing()
    bekend_per_id = {p["publicatie_id"]: p for p in bestaand if p.get("publicatie_id")}

    try:
        publicaties = fetch_publicaties(dienst_id, set(bekend_per_id.keys()))
    except Exception as e:
        print(f"✗ Ophalen publicaties mislukt: {e}")
        return

    nieuw = 0
    for ruw in publicaties:
        record = parse_publicatie(ruw)
        pid = record["publicatie_id"]
        if not pid:
            continue
        if pid not in bekend_per_id:
            nieuw += 1
            print(f"  + {record['datum_publicatie']} — {(record['titel'] or '')[:65]} "
                  f"({record['type_publicatie']})")
        bekend_per_id[pid] = record  # ook bestaande bijwerken (bv. sluitingsdatum kan wijzigen)

    resultaat = sorted(bekend_per_id.values(), key=lambda p: p.get("datum_publicatie") or "", reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw} nieuwe publicaties · {len(resultaat)} totaal in JSON")


if __name__ == "__main__":
    main()
