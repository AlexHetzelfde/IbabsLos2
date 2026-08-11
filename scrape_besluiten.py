#!/usr/bin/env python3
"""
scrape_besluiten.py
====================
Vervangt scrape_rss.py.

Houdt twee dingen bij, allebei vanaf 1 juli 2026:

  1. CAMERATOEZICHT — via de brede RSS-feed van officielebekendmakingen.nl.
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

LET OP over de camera-RSS-feed (FIX augustus 2026):
De query filterde eerder op dt.subject=="Openbare orde en veiligheid |
Organisatie en beleid". Die taxonomie-waarde bestaat niet (meer) zo op de
server, en de literal "|" in de querystring was bovendien niet URL-encoded
(had %7C moeten zijn) — de combinatie deed de SRU/CQL-parser van
officielebekendmakingen.nl stikken en gaf structureel HTTP 500. De query is
vervangen door een fulltext-filter (cql.textAndIndexes=="camera") gecombineerd
met dt.creator=="Zaanstad". Dit is ook inhoudelijk robuuster: minder
afhankelijk van een wankele taxonomie-waarde, dichter bij "documenten die
over camera's gaan". Let op twee gedragsveranderingen hierdoor:
  - Het organisatietype==gemeente-filter is vervallen; er wordt alleen nog
    gefilterd op dt.creator=="Zaanstad". Als een andere organisatie ooit
    "Zaanstad" als creator gebruikt over een niet-cameratoezicht-onderwerp,
    komt die nu ook door de RSS-feed heen.
  - cql.textAndIndexes=="camera" matcht op elk woord met "camera" erin (ook
    "camerabeveiliging" e.d., of het woord in een heel andere context).
    Daarom blijft de "cameratoezicht" in de description-filter in
    verwerk_cameratoezicht() staan als extra check — niet weghalen.

LET OP over de eerdere titel/description-bug (juli 2026, blijft relevant):
De <title>-tag van elk RSS-item bevat NIET de beschrijvende tekst, alleen
het publicatienummer, bijv. "gmb-2026-364272 : Zaanstad". De beschrijvende
tekst ("Aanwijzingsbesluit tijdelijk cameratoezicht Elzenstraat...") staat
in de <description>-tag. De filter kijkt naar de description i.p.v. de titel.

NIEUW (sinds juli 2026): elke camera-periode en elke woningsluiting krijgt
een "plaats"-veld (Zaandam/Wormerveer/Assendelft/etc.), en camera-periodes
krijgen daarnaast "reden_categorieen" en "reden_samenvatting" — geclassificeerd
uit de "Overwegende dat"-tekst van het besluit, die eerder wel werd opgehaald
maar nergens werd gebruikt. Zie ZAANSTAD_PLAATSEN en REDEN_KEYWORDS.

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

# FIX augustus 2026: dt.subject-taxonomiefilter (met niet-ge-encodede "|")
# vervangen door een fulltext-filter op "camera" + creator-filter op
# "Zaanstad". Zie toelichting bovenaan het bestand.
CAMERA_RSS_URL = (
    "https://zoek.officielebekendmakingen.nl/rss"
    "?q=(c.product-area==%22officielepublicaties%22)"
    "and(((w.publicatienaam==%22Tractatenblad%22))"
    "or((w.publicatienaam==%22Staatsblad%22))"
    "or((w.publicatienaam==%22Staatscourant%22))"
    "or((w.publicatienaam==%22Gemeenteblad%22))"
    "or((w.publicatienaam==%22Provinciaal%20blad%22))"
    "or((w.publicatienaam==%22Waterschapsblad%22))"
    "or((w.publicatienaam==%22Blad%20gemeenschappelijke%20regeling%22)))"
    "and(cql.textAndIndexes=%22camera%22)"
    "%20AND%20dt.creator==%22Zaanstad%22"
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

# Plaatsnamen binnen de gemeente Zaanstad — gebruikt om een los "plaats"-veld
# te vullen bij zowel camera's als woningsluitingen, i.p.v. de gebroken
# WIJK_MAP-fallback in de frontend die alleen het eerste woord van het adres
# pakte. Volgorde doet ertoe: "Koog aan de Zaan" moet vóór "Zaandam" gecheckt
# worden e.d., maar met een woordgrens-regex is dat hier niet nodig — elke
# plaatsnaam is uniek genoeg om los te matchen.
ZAANSTAD_PLAATSEN = [
    "Zaandam", "Wormerveer", "Assendelft", "Koog aan de Zaan",
    "Krommenie", "Westzaan", "Zaandijk", "Wormer",
]

# Keyword-classificatie voor de aanleiding van een cameratoezicht-besluit,
# gebaseerd op de "Overwegende dat"-tekst die tot nu toe werd opgehaald en
# vervolgens genegeerd. Zelfde stijl als ONDERWERP_KEYWORDS in app.js: een
# term kan in meerdere categorieën voorkomen, dat is bewust — de tekst gaat
# vaak over meerdere dingen tegelijk (bv. wapen + geweld).
REDEN_KEYWORDS = {
    "Wapens/schietincident": ["vuurwapen", "wapen", "wapens", "schietincident", "beschoten", "kogelgaten", "geschoten"],
    "Steekincident":         ["steekincident", "gestoken", "mes"],
    "Drugs":                 ["drugs", "verdovende middelen", "drugshandel", "drugsoverlast"],
    "Overlast":              ["overlast", "wanordelijkheden", "onrust"],
    "Geweld/bedreiging":     ["geweld", "bedreiging", "mishandeling"],
    "Brand/explosie":        ["brand", "explosie", "explosief"],
    "Ondermijning":          ["ondermijning", "criminele activiteit", "criminaliteit"],
}


def extract_plaats(tekst):
    """Zoekt de eerste bekende Zaanstad-plaatsnaam in de tekst (titel of
    camera-label). None als er geen match is."""
    if not tekst:
        return None
    for plaats in ZAANSTAD_PLAATSEN:
        if re.search(rf"(?i)\b{re.escape(plaats)}\b", tekst):
            return plaats
    return None


def extract_overwegende_blok(platte_tekst):
    """Isoleert het 'Overwegende dat: ...'-blok uit de platte besluittekst,
    dat de feitelijke aanleiding bevat (incident, reden). Valt terug op de
    volledige tekst als de markers niet gevonden worden — beter een breder
    zoekgebied dan niets classificeren."""
    m = re.search(
        r"(?i)overwegende\s+dat[:\s]*(.*?)(?:gelet\s+op\b|besluit\s*:)",
        platte_tekst, re.S
    )
    return m.group(1) if m else platte_tekst


def classificeer_reden(overwegende_blok):
    """Geeft een lijst van gematchte reden-categorieën terug (kan leeg zijn
    als geen enkele keyword matcht — dat is een geldige uitkomst, niet elk
    besluit hoeft in een bekende categorie te vallen)."""
    tekst_lower = overwegende_blok.lower()
    return [
        categorie for categorie, keywords in REDEN_KEYWORDS.items()
        if any(kw in tekst_lower for kw in keywords)
    ]


def extract_reden_samenvatting(overwegende_blok, categorieen):
    """Pakt de eerste zin/bullet uit het overwegende-blok die een van de
    gematchte keywords bevat, als korte journalistieke duiding. Truncate
    op 300 tekens — dit is een aanwijzing voor de journalist, geen
    volledige juridische tekst (die blijft via de link beschikbaar)."""
    if not categorieen:
        return None
    alle_keywords = [kw for cat in categorieen for kw in REDEN_KEYWORDS[cat]]
    # Splits ruwweg op bullets/zinnen.
    fragmenten = re.split(r"[•\n]|(?<=[.;])\s+(?=[A-Z])", overwegende_blok)
    for fragment in fragmenten:
        fragment_schoon = re.sub(r"\s+", " ", fragment).strip()
        if not fragment_schoon:
            continue
        if any(kw in fragment_schoon.lower() for kw in alle_keywords):
            return fragment_schoon[:300] + ("…" if len(fragment_schoon) > 300 else "")
    return None


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
    """Haalt de RSS-feed op en geeft per item titel, beschrijving, link en
    publicatiedatum terug.

    LET OP: de <title>-tag bevat alleen het publicatienummer (bijv.
    "gmb-2026-364272 : Zaanstad"), NIET de beschrijvende tekst. Die staat in
    <description> (bijv. "Aanwijzingsbesluit tijdelijk cameratoezicht
    Elzenstraat in Wormerveer"). We halen dus expliciet ook de description
    op, en filteren daar verderop in verwerk_cameratoezicht() op.
    """
    print("Camera-RSS ophalen...")
    data = http_get(CAMERA_RSS_URL)
    root = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        beschrijving = (item.findtext("description") or "").strip()
        link  = (item.findtext("link") or "").strip()
        datum = parse_rss_datum(item.findtext("pubDate") or "")
        items.append({
            "titel": titel,
            "beschrijving": beschrijving,
            "link": link,
            "pubdate": datum,
        })
    if not items:
        # Atom-fallback, zelfde patroon als de oude scraper.
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.iter(f"{ns}entry"):
            titel = (entry.findtext(f"{ns}title") or "").strip()
            beschrijving = (
                entry.findtext(f"{ns}summary") or
                entry.findtext(f"{ns}content") or ""
            ).strip()
            link_el = entry.find(f"{ns}link")
            link = link_el.get("href", "") if link_el is not None else ""
            datum = parse_rss_datum(
                entry.findtext(f"{ns}published") or
                entry.findtext(f"{ns}updated") or ""
            )
            items.append({
                "titel": titel,
                "beschrijving": beschrijving,
                "link": link,
                "pubdate": datum,
            })
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
            r"(?i)aanwijzing(s)?besluit\s+(extra\s+)?(verlenging\s+)?tijdelijk\s+cameratoezicht\s*",
            "", titel_meta
        )
        schoon = re.sub(r"(?i)\s+(in|te)\s+Zaandam.*$", "", schoon).strip()
        camera_label = schoon or titel_meta or link

    is_verlenging = bool(re.search(r"(?i)verlenging", titel_meta))

    if not start or not eind:
        print(f"  ⚠ geen bruikbare start/einddatum gevonden voor: {titel_meta} ({link})")
        return None

    # Plaats + reden-classificatie. De platte body-tekst werd al opgehaald
    # (html_text) maar tot nu toe alleen gebruikt voor het "Camera:"-label —
    # de rest ("Overwegende dat...") werd weggegooid.
    plaats = extract_plaats(titel_meta) or extract_plaats(camera_label)

    platte_tekst = re.sub(r"<[^>]+>", "\n", html_text)
    overwegende_blok = extract_overwegende_blok(platte_tekst)
    reden_categorieen = classificeer_reden(overwegende_blok)
    reden_samenvatting = extract_reden_samenvatting(overwegende_blok, reden_categorieen)

    return {
        "camera": camera_label,
        "titel": titel_meta,
        "link": link,
        "start": start,
        "eind": eind,
        "is_verlenging": is_verlenging,
        "plaats": plaats,
        "reden_categorieen": reden_categorieen,
        "reden_samenvatting": reden_samenvatting,
    }


def normaliseer_label(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def vind_matchende_sleutel(nieuwe_sleutel, actief_map):
    """Zoekt een bestaande camera-sleutel die vermoedelijk dezelfde locatie is
    als nieuwe_sleutel, ook als de gemeente het net anders heeft geformuleerd
    (bijv. "Elzenstraat" vs. "Elzenstraat in Wormerveer" — zelfde straat,
    twee besluiten met een net iets ander "Camera:"-label).

    Matcht op exacte gelijkheid eerst, anders op deelstring-bevat-relatie in
    beide richtingen. Een minimale lengte van 6 tekens voorkomt dat korte,
    generieke labels elkaar per ongeluk aantrekken.
    """
    if nieuwe_sleutel in actief_map:
        return nieuwe_sleutel
    for bestaande_sleutel in actief_map:
        if len(nieuwe_sleutel) < 6 or len(bestaande_sleutel) < 6:
            continue
        if nieuwe_sleutel in bestaande_sleutel or bestaande_sleutel in nieuwe_sleutel:
            return bestaande_sleutel
    return None


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
    # Filter op de beschrijving, niet op de titel — zie toelichting bij
    # fetch_camera_rss(). De <title>-tag bevat alleen het publicatienummer
    # en matcht daardoor nooit op "cameratoezicht". Deze check blijft ook
    # met de nieuwe fulltext-query nodig, want cql.textAndIndexes=="camera"
    # matcht breder dan alleen cameratoezicht-besluiten.
    camera_items = [
        it for it in feed_items
        if "cameratoezicht" in it["beschrijving"].lower()
        and it["link"] not in verwerkte_links
        and (it["pubdate"] or "9999-99-99") >= grens
    ]
    print(f"  {len(camera_items)} nieuwe cameratoezicht-items te verwerken")

    actief_map = {normaliseer_label(c["camera"]): c for c in actief}

    for item in camera_items:
        print(f"  → {item['beschrijving']}")
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
            "reden_categorieen": besluit["reden_categorieen"],
            "reden_samenvatting": besluit["reden_samenvatting"],
        }

        match_sleutel = vind_matchende_sleutel(sleutel, actief_map)
        if match_sleutel:
            entry = actief_map[match_sleutel]
            entry["periodes"].append(periode)
            entry["eind"] = max(entry["eind"], besluit["eind"])
            entry["start"] = min(entry["start"], besluit["start"])
            if besluit["is_verlenging"]:
                entry["keer_verlengd"] = entry.get("keer_verlengd", 0) + 1
            # Bewaar alternatieve labelvarianten zodat je kunt zien dat dit
            # een fuzzy-match was en niet een letterlijk identiek label —
            # handig als je dit ooit moet controleren.
            if besluit["camera"] != entry["camera"]:
                entry.setdefault("labels", [entry["camera"]])
                if besluit["camera"] not in entry["labels"]:
                    entry["labels"].append(besluit["camera"])
            # plaats kan bij de eerste periode nog ontbroken hebben — vul 'm
            # alsnog in als een latere periode het wel oplevert.
            if not entry.get("plaats") and besluit["plaats"]:
                entry["plaats"] = besluit["plaats"]
            print(f"    ↻ verlenging/update: {besluit['camera']} → gekoppeld aan '{entry['camera']}', nu tot {entry['eind']}")
        else:
            nieuw = {
                "camera": besluit["camera"],
                "plaats": besluit["plaats"],
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

        # Plaats-veld, zelfde ZAANSTAD_PLAATSEN-lijst als bij camera's.
        # Vervangt de kapotte WIJK_MAP-fallback in de frontend die alleen
        # het eerste woord van het adres pakte (vaak gewoon de straatnaam,
        # niet een wijk of plaats).
        plaats = extract_plaats(volledige_tekst)

        bestaand[art["link"]] = {
            "titel": art["titel"],
            "link": art["link"],
            "datum": art["datum"],
            "excerpt": art["excerpt"],
            "adres": adres,
            "plaats": plaats,
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
