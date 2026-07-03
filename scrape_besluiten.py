#!/usr/bin/env python3
"""
scrape_besluiten.py
====================
Vervangt scrape_rss.py.

Houdt twee dingen bij, allebei vanaf 1 juli 2026:

  1. CAMERATOEZICHT — via de brede RSS-feed van officielebekendmakingen.nl
     (subject-filter "Openbare orde en veiligheid | Organisatie en beleid").
     Per treffer wordt de losse besluit-pagina gefetched om de machine-
     leesbare metavelden te lezen (OVERHEIDop.startdatum / einddatum) en het
     "Camera: <naam>"-label in de body, dat als unieke sleutel dient voor
     verlengingen.
     Output:
       data/cameras_actief.json       — camera's die nu (nog) actief zijn
       data/cameras_geschiedenis.json — camera's waarvan de periode voorbij is

  2. WONINGSLUITINGEN — via HTML-scraping van deorkaan.nl/tag/woningsluiting/
     (alleen pagina 1 — de nieuwste). Zaanstad publiceert dit namelijk niet
     via officielebekendmakingen.nl.
     Output:
       data/woningsluitingen.json

Dwangsommen worden niet meer bijgehouden.

LET OP over de woningsluitingen-scraper:
De Orkaan biedt geen RSS/API aan voor deze tag. De parser in
parse_orkaan_pagina() is gebouwd op de daadwerkelijke live HTML van
deorkaan.nl/tag/woningsluiting/ (juli 2026): elk artikel staat in een
<div class="mb-6 ..."> blok, de titel-link omhult de <h2> (dus <a href="..."><h2>Titel</h2></a>,
niet andersom), en de publicatiedatum staat in een <span class="...text-xs">
na een <img ... alt="date">, niet in een <time>-tag.

Gebruik:
    python3 scrape_besluiten.py
"""

import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Vaste startdatum voor deze nieuwe tracking — alles hiervoor wordt genegeerd.
# Override met env var SCRAPE_VANAF (formaat YYYY-MM-DD) indien nodig.
STANDAARD_VANAF = "2026-07-01"

CAMERA_RSS_URL = (
    "https://zoek.officielebekendmakingen.nl/rss"
    "?q=(c.product-area==%22officielepublicaties%22)"
    "and((((w.organisatietype==%22gemeente%22)"
    "and((dt.creator==%22Zaanstad%22)"
    "or(dt.creator==%22gemeente%20Zaanstad%22)))))"
    "and(((w.publicatienaam==%22Tractatenblad%22))"
    "or((w.publicatienaam==%22Staatsblad%22))"
    "or((w.publicatienaam==%22Staatscourant%22))"
    "or((w.publicatienaam==%22Gemeenteblad%22))"
    "or((w.publicatienaam==%22Provinciaal%20blad%22))"
    "or((w.publicatienaam==%22Waterschapsblad%22))"
    "or((w.publicatienaam==%22Blad%20gemeenschappelijke%20regeling%22)))"
    "%20AND%20dt.subject==%22Openbare%20orde%20en%20veiligheid%20"
    "|%20Organisatie%20en%20beleid%22"
)

ORKAAN_WONINGSLUITING_URL = "https://www.deorkaan.nl/tag/woningsluiting/"

OUT_CAMERAS_ACTIEF      = "data/cameras_actief.json"
OUT_CAMERAS_GESCHIEDENIS = "data/cameras_geschiedenis.json"
OUT_WONINGSLUITINGEN    = "data/woningsluitingen.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Straat-suffix regex, hergebruikt uit de oude scraper (voor adres-extractie
# bij woningsluitingen, t.b.v. de wijk-weergave in het dashboard).
ADRES_REGEX = re.compile(
    r"([A-Z][a-zA-Z'.\- ]*?(?:straat|weg|laan|singel|kade|gracht|plein|dijk|"
    r"pad|baan|steeg|hof|plantsoen|werf|oord|meen|donk|akker|brink|erf|"
    r"hofje|park|zoom|burg|hoek)\s+\d+[a-zA-Z]?)"
)

MAAND_MAP = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10,
    "november": 11, "december": 12,
}

DUUR_WOORDEN = {
    "één": 1, "een": 1, "twee": 2, "drie": 3, "vier": 4, "vijf": 5,
    "zes": 6, "zeven": 7, "acht": 8, "negen": 9, "tien": 10, "elf": 11,
    "twaalf": 12,
}


def grens_datum():
    return os.environ.get("SCRAPE_VANAF", "").strip() or STANDAARD_VANAF


def http_get(url, retries=3, wait=4):
    """Simpele GET met retries. Geeft bytes terug of raised na laatste poging."""
    req = urllib.request.Request(url, headers=HEADERS)
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


def parse_iso_datum(s):
    """YYYY-MM-DD -> zelfde string, gevalideerd. None als ongeldig/leeg."""
    if not s:
        return None
    try:
        datetime.strptime(s.strip(), "%Y-%m-%d")
        return s.strip()
    except ValueError:
        return None


def parse_rss_datum(s):
    if not s:
        return None
    formaten = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ]
    for fmt in formaten:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_dutch_datum(tekst):
    """'5 december 2024' -> '2024-12-05'. None als niet te parsen."""
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", tekst.lower())
    if not m:
        return None
    dag, maand_naam, jaar = m.groups()
    maand = MAAND_MAP.get(maand_naam)
    if not maand:
        return None
    try:
        return datetime(int(jaar), maand, int(dag)).strftime("%Y-%m-%d")
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CAMERATOEZICHT
# ─────────────────────────────────────────────────────────────────────────────

def fetch_camera_rss():
    print("Camera-RSS ophalen...")
    data = http_get(CAMERA_RSS_URL)
    root = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        datum = parse_rss_datum(item.findtext("pubDate") or "")
        items.append({"titel": titel, "link": link, "pubdate": datum})
    if not items:
        # Atom-fallback, zelfde patroon als de oude scraper.
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            titel = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            datum = parse_rss_datum(
                entry.findtext("{http://www.w3.org/2005/Atom}published") or
                entry.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            )
            items.append({"titel": titel, "link": link, "pubdate": datum})
    print(f"  {len(items)} items in feed")
    return items


def parse_meta_tags(html_text):
    """Leest alle <meta name=... content=...> tags in, onafhankelijk van
    attribuut-volgorde. Geeft een dict {name: content} terug."""
    metas = {}
    for m in re.finditer(r"<meta\s+([^>]+?)/?>", html_text, re.I):
        attrs = dict(re.findall(r'([\w:.\-]+)\s*=\s*"([^"]*)"', m.group(1)))
        naam = attrs.get("name") or attrs.get("property")
        inhoud = attrs.get("content")
        if naam and inhoud is not None:
            metas[naam] = inhoud
    return metas


def extract_camera_label(html_text):
    """Zoekt het 'Camera: <naam>'-label in de body van de besluit-pagina.
    Probeert eerst binnen een enkel tag-element, dan als losse tekst."""
    m = re.search(r"<p[^>]*>\s*Camera\s*:\s*([^<]+?)\s*</p>", html_text, re.I)
    if m:
        return m.group(1).strip()
    # Fallback: strip tags en zoek in platte tekst.
    platte_tekst = re.sub(r"<[^>]+>", "\n", html_text)
    m = re.search(r"Camera\s*:\s*([^\n]+)", platte_tekst, re.I)
    if m:
        return m.group(1).strip()
    return None


def fetch_camera_besluit(link):
    """Haalt startdatum, einddatum, camera-label en of het een verlenging is
    op van de individuele besluit-pagina."""
    try:
        html_bytes = http_get(link, retries=2, wait=3)
    except Exception as e:
        print(f"  kon besluit-pagina niet ophalen ({link}): {e}")
        return None
    html_text = html_bytes.decode("utf-8", errors="replace")

    metas = parse_meta_tags(html_text)
    start = parse_iso_datum(metas.get("OVERHEIDop.startdatum"))
    eind  = parse_iso_datum(metas.get("OVERHEIDop.einddatum"))
    titel_meta = metas.get("DC.title") or ""

    camera_label = extract_camera_label(html_text)

    if not camera_label:
        # Zonder label kunnen we niet matchen op verlengingen — val terug
        # op de titel zonder "Aanwijzingsbesluit (verlenging) tijdelijk
        # cameratoezicht" / "in/te <plaats>" als benadering.
        schoon = re.sub(
            r"(?i)aanwijzing(s)?besluit\s+(verlenging\s+)?tijdelijk\s+cameratoezicht\s*",
            "", titel_meta
        )
        schoon = re.sub(r"(?i)\s+(in|te)\s+Zaandam.*$", "", schoon).strip()
        camera_label = schoon or titel_meta or link

    is_verlenging = bool(re.search(r"(?i)verlenging", titel_meta))

    if not start or not eind:
        print(f"  ⚠ geen bruikbare start/einddatum gevonden voor: {titel_meta} ({link})")
        return None

    return {
        "camera": camera_label,
        "titel": titel_meta,
        "link": link,
        "start": start,
        "eind": eind,
        "is_verlenging": is_verlenging,
    }


def normaliseer_label(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def verwerk_cameratoezicht():
    print("\n=== CAMERATOEZICHT ===")
    grens = grens_datum()
    print(f"Alleen vanaf: {grens}")

    actief = load_json(OUT_CAMERAS_ACTIEF, [])
    geschiedenis = load_json(OUT_CAMERAS_GESCHIEDENIS, [])

    verwerkte_links = set()
    for c in actief + geschiedenis:
        for p in c.get("periodes", []):
            if p.get("link"):
                verwerkte_links.add(p["link"])

    feed_items = fetch_camera_rss()
    camera_items = [
        it for it in feed_items
        if "cameratoezicht" in it["titel"].lower()
        and it["link"] not in verwerkte_links
        and (it["pubdate"] or "9999-99-99") >= grens
    ]
    print(f"  {len(camera_items)} nieuwe cameratoezicht-items te verwerken")

    actief_map = {normaliseer_label(c["camera"]): c for c in actief}

    for item in camera_items:
        print(f"  → {item['titel']}")
        besluit = fetch_camera_besluit(item["link"])
        time.sleep(1)  # vriendelijk zijn voor de server
        if not besluit:
            continue
        if besluit["start"] < grens and besluit["eind"] < grens:
            continue  # volledig voor de grensdatum, negeren

        sleutel = normaliseer_label(besluit["camera"])
        periode = {
            "start": besluit["start"],
            "eind": besluit["eind"],
            "titel": besluit["titel"],
            "link": besluit["link"],
        }

        if sleutel in actief_map:
            entry = actief_map[sleutel]
            entry["periodes"].append(periode)
            entry["eind"] = max(entry["eind"], besluit["eind"])
            entry["start"] = min(entry["start"], besluit["start"])
            if besluit["is_verlenging"]:
                entry["keer_verlengd"] = entry.get("keer_verlengd", 0) + 1
            print(f"    ↻ verlenging/update: {besluit['camera']} → nu tot {entry['eind']}")
        else:
            nieuw = {
                "camera": besluit["camera"],
                "start": besluit["start"],
                "eind": besluit["eind"],
                "keer_verlengd": 0,
                "periodes": [periode],
            }
            actief.append(nieuw)
            actief_map[sleutel] = nieuw
            print(f"    + nieuw: {besluit['camera']} ({besluit['start']} t/m {besluit['eind']})")

    # Verlopen camera's verhuizen naar geschiedenis.
    vandaag = datetime.now().strftime("%Y-%m-%d")
    nog_actief = []
    for c in actief:
        if c["eind"] < vandaag:
            print(f"  ✗ verlopen, naar geschiedenis: {c['camera']} (was t/m {c['eind']})")
            geschiedenis.append(c)
        else:
            nog_actief.append(c)

    save_json(OUT_CAMERAS_ACTIEF, sorted(nog_actief, key=lambda x: x["eind"]))
    save_json(OUT_CAMERAS_GESCHIEDENIS, sorted(geschiedenis, key=lambda x: x["eind"], reverse=True))
    print(f"  klaar — {len(nog_actief)} actief, {len(geschiedenis)} in geschiedenis")


# ─────────────────────────────────────────────────────────────────────────────
# WONINGSLUITINGEN (De Orkaan)
# ─────────────────────────────────────────────────────────────────────────────

def extract_duur_maanden(tekst):
    """Zoekt '(drie|zes|...) maanden' of '(3|6) maanden' in de tekst."""
    m = re.search(r"(\d+)\s+maand", tekst.lower())
    if m:
        return int(m.group(1))
    for woord, getal in DUUR_WOORDEN.items():
        if re.search(rf"\b{woord}\s+maand", tekst.lower()):
            return getal
    return None


def parse_orkaan_pagina(html_text):
    """Parseert de artikel-blokken op de De Orkaan tag-pagina.

    Echte structuur (bevestigd op live HTML, juli 2026):

        <div class="mb-6 pb-0 border-b border-gray-300">
          <div class="sm:flex">
            <div class="mr-6 block-item ...">
              <a href="ARTIKEL_URL" class="overview-item">
                <img ... class="post-thumb" ...>
              </a>
            </div>
            <div class="flex-1">
              <a class="text-lg font-bold leading-tight" href="ARTIKEL_URL">
                <h2 class="overview-post-title mb-1">TITEL</h2>
              </a>
              EXCERPT-TEKST...
              <div class="mt-4"><a href="ARTIKEL_URL" class="button">Lees meer</a></div>
            </div>
          </div>
          <div class="sm:mt-1.5 meta ...">
            ...
            <div class="... flex items-center">
              <img ... alt="date"/>
              <span class="pt-0.5 text-xs">
                  5 mei 2026            </span>
            </div>
          </div>
        </div>

    Twee dingen die eerder faalden en nu gefixt zijn:
      1. De titel-link OMHULT de <h2> (<a href="..."><h2>Titel</h2></a>),
         niet andersom zoals eerder aangenomen.
      2. De datum staat niet in een <time>-tag maar in een <span> die volgt
         op een <img alt="date">.
    """
    artikelen = []

    # Elk artikel zit in een eigen <div class="mb-6 ...">-blok. Splits daarop.
    blokken = re.split(r'(?=<div class="mb-6\b)', html_text)
    if len(blokken) <= 1:
        # Fallback voor het geval de class-naam wijzigt: val terug op de
        # titel-link zelf als bloksplitser.
        blokken = re.split(
            r'(?=<a class="text-lg font-bold leading-tight")', html_text
        )

    for blok in blokken:
        # Titel-link omhult de <h2>: <a ... href="...">\s*<h2 ...>Titel</h2>\s*</a>
        m_link = re.search(
            r'<a[^>]+href="([^"]+)"[^>]*>\s*<h2[^>]*>\s*([^<]+?)\s*</h2>\s*</a>',
            blok, re.I,
        )
        if not m_link:
            continue
        link, titel = m_link.group(1).strip(), m_link.group(2).strip()

        # Datum: <img ... alt="date"/> gevolgd door <span ...>5 mei 2026</span>
        m_datum = re.search(
            r'alt="date"[^>]*/?>\s*<span[^>]*>\s*([^<]+?)\s*</span>',
            blok, re.I,
        )
        if m_datum:
            datum = parse_dutch_datum(m_datum.group(1))
        else:
            # Fallback: zoek een losse "D maand JJJJ"-patroon in het hele blok.
            datum = parse_dutch_datum(re.sub(r"<[^>]+>", " ", blok))

        # Excerpt: platte tekst tussen het einde van de titel-<a> en de
        # "Lees meer"-knop.
        m_excerpt = re.search(
            r'</h2>\s*</a>(.*?)(?:<div class="mt-4">|<a[^>]*>\s*Lees meer|$)',
            blok, re.I | re.S,
        )
        excerpt_html = m_excerpt.group(1) if m_excerpt else blok
        excerpt = re.sub(r"<[^>]+>", " ", excerpt_html)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
        if len(excerpt) > 400:
            excerpt = excerpt[:400] + "…"

        if not link or not titel:
            continue
        artikelen.append({
            "titel": titel,
            "link": link,
            "datum": datum,
            "excerpt": excerpt,
        })

    return artikelen


def verwerk_woningsluitingen():
    print("\n=== WONINGSLUITINGEN (De Orkaan) ===")
    grens = grens_datum()
    print(f"Alleen vanaf: {grens}")

    bestaand = {b["link"]: b for b in load_json(OUT_WONINGSLUITINGEN, [])}

    print("Pagina 1 ophalen...")
    try:
        html_bytes = http_get(ORKAAN_WONINGSLUITING_URL)
    except Exception as e:
        print(f"  ✗ kon De Orkaan niet ophalen: {e}")
        return
    html_text = html_bytes.decode("utf-8", errors="replace")

    artikelen = parse_orkaan_pagina(html_text)
    print(f"  {len(artikelen)} artikelen gevonden op pagina 1")
    if not artikelen:
        print("  ⚠ 0 artikelen — de selectors in parse_orkaan_pagina() matchen "
              "niet meer met de site. Check de live HTML en pas ze aan.")

    nieuw = 0
    for art in artikelen:
        if art["link"] in bestaand:
            continue
        if art["datum"] and art["datum"] < grens:
            continue

        volledige_tekst = art["titel"] + " " + art["excerpt"]
        duur_maanden = extract_duur_maanden(volledige_tekst)
        eind_datum = None
        if duur_maanden and art["datum"]:
            start_dt = datetime.strptime(art["datum"], "%Y-%m-%d")
            # Benadering: 1 maand ≈ 30 dagen. Prima voor journalistieke
            # oriëntatie, geen juridisch bindende einddatum.
            eind_datum = (start_dt + timedelta(days=30 * duur_maanden)).strftime("%Y-%m-%d")

        adres = None
        m_adres = ADRES_REGEX.search(volledige_tekst)
        if m_adres:
            adres = m_adres.group(1)

        bestaand[art["link"]] = {
            "titel": art["titel"],
            "link": art["link"],
            "datum": art["datum"],
            "excerpt": art["excerpt"],
            "adres": adres,
            "duur_maanden": duur_maanden,
            "eind_datum": eind_datum,
            "eind_datum_type": "geschat_uit_artikeltekst" if eind_datum else None,
            "bron": "De Orkaan",
        }
        nieuw += 1

    resultaat = sorted(bestaand.values(), key=lambda x: x.get("datum") or "", reverse=True)
    save_json(OUT_WONINGSLUITINGEN, resultaat)
    print(f"  klaar — {nieuw} nieuwe, {len(resultaat)} totaal in JSON")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    verwerk_cameratoezicht()
    verwerk_woningsluitingen()


if __name__ == "__main__":
    main()
