#!/usr/bin/env python3
"""
"Scout"-script voor de Tweede Kamer.

Doel: bepalen op welke dag in de komende week de plenaire stemmingen
daadwerkelijk plaatsvinden. Normaal is dat dinsdagmiddag, maar door reces,
een verschoven sluiting van het debat, of een andere planningswijziging kan
dat afwijken. Dit script checkt het van tevoren (bedoeld om op maandag te
draaien) in plaats van blind "dinsdag" aan te nemen — vergelijkbaar met het
rollend-venster-probleem dat we bij de Zaanstad-scrapers hebben gefixt: beter
even vooraf controleren dan achteraf ontdekken dat de aanname niet klopte.

Bron: de officiële, sleutelvrije OData-API van de Tweede Kamer
(gegevensmagazijn.tweedekamer.nl/OData/v4/2.0). Geen API-key, geen
registratie nodig — expliciet gepubliceerd als open data voor dit soort
gebruik.

LET OP — NOG NIET LIVE GETEST: de veldnamen hieronder (Datum, Onderwerp,
Soort, Verwijderd, Id, Nummer, Status) zijn gebaseerd op de officiële
documentatie en een voorbeeldquery uit een bestaande open-source library,
niet op een eigen live proefquery — ik kon deze API vanaf mijn eigen omgeving
niet bereiken (robots.txt / geen netwerktoegang). Vandaar de uitgebreide
diagnostische print van de ruwe respons hieronder: bekijk die bij de eerste
run goed, en meld het als er iets niet klopt met de veldnamen.

Schrijft naar data/tweedekamer_scout.json:
    {
      "gevonden": true/false,
      "datum": "YYYY-MM-DD" of null,
      "activiteit_id": "..." of null,
      "onderwerp": "..." of null,
      "gecontroleerd_op": "YYYY-MM-DDTHH:MM:SS"
    }

Gebruik:
    python3 scrape_tweedekamer_scout.py
"""

import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

BASE_URL = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0"
OUTPUT   = "data/tweedekamer_scout.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ── RETRY-HELPER ──────────────────────────────────────────────────────────────
# Zelfde bewezen patroon als in de Zaanstad-scrapers (scrape_moties.py e.a.).
def open_met_retry(req, timeout=30, retries=3, wachttijden=(2, 5, 10)):
    laatste_fout = None
    for poging in range(1, retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            laatste_fout = e
            if poging < retries:
                wacht = wachttijden[min(poging - 1, len(wachttijden) - 1)]
                print(f"(poging {poging}/{retries} mislukt: {e} — {wacht}s wachten)", end=" ", flush=True)
                time.sleep(wacht)
    raise laatste_fout


def zoek_stemmingen_activiteit(vanaf, tot):
    """
    Vraagt de Activiteit-entiteit op met Soort = 'Stemmingen' binnen
    [vanaf, tot). Geeft de ruwe 'value'-lijst uit de OData-respons terug.

    Filterlogica gebaseerd op de officiële documentatie (opendata.tweedekamer.nl
    /documentatie/odata-api) en een reëel voorbeeld uit de open-source
    'gegevensmagazijn' Rust-library, die exact dit soort $filter-syntax op
    Activiteit/Besluit gebruikt (Soort eq '...', Datum ge/lt ...).
    """
    filter_expr = (
        "Soort eq 'Stemmingen' and Verwijderd eq false "
        f"and Datum ge {vanaf.strftime('%Y-%m-%d')}T00:00:00 "
        f"and Datum lt {tot.strftime('%Y-%m-%d')}T00:00:00"
    )
    params = {
        "$filter":  filter_expr,
        "$orderby": "Datum asc",
    }
    url = f"{BASE_URL}/Activiteit?{urllib.parse.urlencode(params)}"
    print(f"Query: {url}")
    req = urllib.request.Request(url, headers=HEADERS)
    with open_met_retry(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("value", [])


def main():
    vandaag = datetime.now()
    vanaf   = vandaag
    tot     = vandaag + timedelta(days=7)

    print(f"Zoeken naar 'Stemmingen'-activiteit tussen "
          f"{vanaf.strftime('%Y-%m-%d')} en {tot.strftime('%Y-%m-%d')}...")

    try:
        resultaten = zoek_stemmingen_activiteit(vanaf, tot)
    except Exception as e:
        print(f"FOUT bij ophalen: {e}")
        resultaten = None

    scout_resultaat = {
        "gevonden":         False,
        "datum":            None,
        "activiteit_id":    None,
        "onderwerp":        None,
        "gecontroleerd_op": vandaag.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if resultaten is None:
        scout_resultaat["fout"] = "Kon de Tweede Kamer-API niet bereiken — zie logs hierboven"
        print("⚠ Geen resultaat kunnen ophalen — data/tweedekamer_scout.json markeert dit als fout, geen gevonden datum.")

    elif not resultaten:
        print("Geen 'Stemmingen'-activiteit gevonden in de komende week — waarschijnlijk reces of een lege agenda.")
        print("Als dit onverwacht is (bv. midden in een normale zittingsweek), controleer dan of de")
        print("veldnamen 'Soort'/'Datum'/'Verwijderd' nog kloppen — zie de query hierboven.")

    else:
        # NIEUW — diagnostische print van de eerste ruwe respons. Dit is de
        # allereerste keer dat deze query daadwerkelijk tegen de live API
        # draait; als een veldnaam toch niet blijkt te kloppen (bv. heet het
        # datumveld ergens anders), is dat hier meteen zichtbaar in de logs,
        # in plaats van dat het script stil verkeerde data wegschrijft.
        print()
        print("─── Ruwe eerste treffer (controleer of de veldnamen kloppen) ───")
        print(json.dumps(resultaten[0], indent=2, ensure_ascii=False)[:1000])
        print("─────────────────────────────────────────────────────────────")
        print()

        eerste     = resultaten[0]
        ruwe_datum = eerste.get("Datum") or ""
        datum_only = ruwe_datum[:10] if ruwe_datum else None

        if not datum_only:
            print("⚠ Geen 'Datum'-veld gevonden in het resultaat — zie de ruwe respons hierboven")
            print("  om te ontdekken hoe het datumveld écht heet, en pas dit script daarop aan.")

        scout_resultaat.update({
            "gevonden":      True,
            "datum":         datum_only,
            "activiteit_id": eerste.get("Id"),
            "onderwerp":     eerste.get("Onderwerp"),
        })
        print(f"Stemmingen gevonden op: {datum_only} — {eerste.get('Onderwerp', '(geen onderwerp-veld gevonden)')}")

        if len(resultaten) > 1:
            print(f"(let op: {len(resultaten)} treffers in totaal binnen het venster — "
                  f"alleen de eerste/vroegste is gebruikt; controleer of dat klopt met wat je verwacht)")

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(scout_resultaat, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Weggeschreven naar {OUTPUT}")


if __name__ == "__main__":
    main()
