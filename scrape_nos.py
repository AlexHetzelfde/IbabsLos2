#!/usr/bin/env python3
"""
scrape_nos.py
=============
Haalt de algemene NOS-nieuws-RSS-feed op en beoordeelt per artikel of het
landelijke nieuws een mogelijke lokale invalshoek heeft voor Zaanstad/de
Zaanstreek.

Werkwijze (hybride, om Gemini-kosten te beperken):
  1. Elk artikel wordt eerst langs een gratis keyword-filter gehaald
     (NOS_KEYWORDS) — brede beleids-/maatschappijthema's die vaak een
     lokale doorvertaling hebben (wonen, energie, zorg, onderwijs, OV,
     veiligheid, etc.).
  2. Alleen artikelen die door de keyword-filter komen, worden aan Gemini
     voorgelegd met de vraag: heeft dit een concrete Zaanstad-invalshoek,
     en zo ja welke?
  3. Artikelen die de keyword-filter niet passeren, krijgen geen AI-call
     en worden gemarkeerd als "niet gecheckt" (niet als "geen invalshoek"
     — dat onderscheid is belangrijk, want de keyword-filter is een grove
     voorselectie, geen garantie dat er niks relevants tussen zit).

COPYRIGHT: de <description> van NOS-RSS-items bevat de volledige
artikeltekst. We bewaren daarvan alleen een korte, niet-woordelijke
excerpt (eerste ~250 tekens, zelfde aanpak als bij woningsluitingen in
scrape_besluiten.py) plus Gemini's eigen samenvatting/toelichting — nooit
de volledige tekst. Het dashboard linkt door naar het originele NOS-artikel.

Output:
    data/nos_lokaal.json

Gebruik:
    python3 scrape_nos.py
Environment:
    GEMINI_API_KEY (verplicht voor de AI-stap; zonder key wordt alleen de
    keyword-filter toegepast en blijft heeft_lokale_hoek op None staan)
"""

import json
import os
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime

NOS_RSS_URL = "https://feeds.nos.nl/nosnieuwsalgemeen"
OUTPUT = "data/nos_lokaal.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Grove, gratis voorselectie — brede thema's die vaak een lokale
# doorvertaalslag mogelijk maken. Bewust ruim: liever een paar irrelevante
# artikelen die Gemini afwijst, dan relevante artikelen die nooit bekeken
# worden. Geen poging tot volledigheid, dit is een filter, geen classificatie.
NOS_KEYWORDS = [
    "gemeente", "gemeenten", "woning", "wonen", "huur", "woningmarkt",
    "energie", "gas", "stroom", "warmtenet", "zonnepanelen", "windmolen",
    "klimaat", "stikstof", "milieu",
    "zorg", "ziekenhuis", "huisarts", "ggd", "jeugdzorg",
    "onderwijs", "school", "scholen", "leraren", "leraar",
    "politie", "criminaliteit", "ondermijning", "drugs", "wapen", "schietpartij", "steekpartij",
    "ov", "trein", "bus", "spoor", "ns ", "prorail",
    "asiel", "vluchteling", "azc", "statushouder",
    "cao", "werkgever", "werknemer", "uitkering", "bijstand", "armoede",
    "subsidie", "kabinet", "wet", "wetsvoorstel", "regeling",
    "vuurwerk", "brand", "explosie",
    "verkiezing", "referendum", "raad",
]


def http_get(url, retries=3, wait=4, headers=None):
    req = urllib.request.Request(url, headers=headers or HEADERS)
    laatste_fout = None
    for poging in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            laatste_fout = e
            print(f"  poging {poging} mislukt: {e}")
            if poging < retries:
                time.sleep(wait)
    raise RuntimeError(f"Alle pogingen mislukt voor {url}: {laatste_fout}")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_rss_datum(s):
    if not s:
        return None
    formaten = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ]
    for fmt in formaten:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return s.strip()


def strip_html(tekst):
    schoon = re.sub(r"<[^>]+>", " ", tekst or "")
    schoon = re.sub(r"\s+", " ", schoon).strip()
    return schoon


def fetch_nos_rss():
    print("NOS-RSS ophalen...")
    data = http_get(NOS_RSS_URL)
    root = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        beschrijving_ruw = item.findtext("description") or ""
        guid = (item.findtext("guid") or link or "").strip()
        pubdate = parse_rss_datum(item.findtext("pubDate") or "")
        items.append({
            "guid": guid,
            "titel": titel,
            "link": link,
            "beschrijving_ruw": beschrijving_ruw,
            "datum": pubdate,
        })
    print(f"  {len(items)} items in feed")
    return items


def matcht_keywords(titel, beschrijving):
    tekst = (titel + " " + beschrijving).lower()
    gevonden = [kw for kw in NOS_KEYWORDS if kw in tekst]
    return gevonden


def gemini_beoordeel_lokale_hoek(titel, excerpt, api_key):
    """Vraagt Gemini of dit landelijke artikel een concrete Zaanstad-
    invalshoek heeft. Geeft (heeft_lokale_hoek: bool|None, toelichting: str|None)
    terug — None als de call mislukt, zodat de scraper niet crasht op een
    tijdelijke API-fout."""
    prompt = f"""Je bent een redactionele assistent voor een lokale journalist in Zaanstad
(Zaandam, Wormerveer, Assendelft, Krommenie, Koog aan de Zaan, Westzaan, Zaandijk, Wormer).

Hieronder staat een landelijk NOS-nieuwsbericht. Beoordeel of dit bericht een
CONCRETE lokale invalshoek voor Zaanstad kan hebben — niet "dit raakt heel
Nederland dus ook Zaanstad" (te vaag), maar een specifieke, uitvoerbare
journalistieke vervolgvraag. Bijvoorbeeld: een landelijke regeling die de
gemeente moet uitvoeren, een landelijk probleem waarvan bekend is dat het in
Zaanstad ook speelt, of een aanleiding om een lokale bestuurder of instantie
om reactie te vragen.

Titel: {titel}
Samenvatting: {excerpt}

Antwoord ALLEEN met JSON, geen markdown:
{{"lokale_hoek": true of false, "toelichting": "in maximaal 2 zinnen, in eigen woorden, concreet"}}"""

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"    ⚠ Gemini-call mislukt: {e}")
        return None, None

    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print(f"    ⚠ onverwacht Gemini-antwoord: {data}")
        return None, None

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        print(f"    ⚠ geen JSON in Gemini-antwoord: {raw[:200]}")
        return None, None
    try:
        parsed = json.loads(match.group(0))
        return bool(parsed.get("lokale_hoek")), parsed.get("toelichting")
    except json.JSONDecodeError:
        print(f"    ⚠ kon Gemini-JSON niet parsen: {raw[:200]}")
        return None, None


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        print("✓ Gemini API key gevonden — AI-laag actief")
    else:
        print("⚠ Geen GEMINI_API_KEY — alleen keyword-filter wordt toegepast, geen AI-beoordeling")

    bestaand = load_json(OUTPUT, [])
    bekende_guids = {a["guid"] for a in bestaand}

    feed_items = fetch_nos_rss()
    nieuw = 0
    gecheckt_door_ai = 0

    for item in feed_items:
        if item["guid"] in bekende_guids:
            continue

        beschrijving_plat = strip_html(item["beschrijving_ruw"])
        excerpt = beschrijving_plat[:250] + ("…" if len(beschrijving_plat) > 250 else "")

        gevonden_keywords = matcht_keywords(item["titel"], beschrijving_plat)

        record = {
            "guid": item["guid"],
            "titel": item["titel"],
            "link": item["link"],
            "datum": item["datum"],
            "excerpt": excerpt,
            "matched_keywords": gevonden_keywords,
            "heeft_lokale_hoek": None,   # None = niet (kunnen) beoordelen
            "lokale_hoek_toelichting": None,
            "ai_gecheckt": False,
        }

        if gevonden_keywords and api_key:
            print(f"  → AI-check: {item['titel'][:70]}")
            heeft_hoek, toelichting = gemini_beoordeel_lokale_hoek(
                item["titel"], excerpt, api_key
            )
            record["heeft_lokale_hoek"] = heeft_hoek
            record["lokale_hoek_toelichting"] = toelichting
            record["ai_gecheckt"] = heeft_hoek is not None
            if heeft_hoek is not None:
                gecheckt_door_ai += 1
            time.sleep(1)  # vriendelijk zijn voor de API

        bestaand.append(record)
        bekende_guids.add(item["guid"])
        nieuw += 1

    resultaat = sorted(bestaand, key=lambda x: x.get("datum") or "", reverse=True)
    # Vensterbeperking: RSS-feeds bevatten sowieso alleen recente items, maar
    # voor de zekerheid houden we het bestand ook zelf begrensd tot de
    # laatste 500 artikelen, zodat het nooit ongelimiteerd groeit.
    resultaat = resultaat[:500]

    save_json(OUTPUT, resultaat)
    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw} nieuwe artikelen · {gecheckt_door_ai} door AI beoordeeld")
    print(f"  {len(resultaat)} totaal in JSON")


if __name__ == "__main__":
    main()
