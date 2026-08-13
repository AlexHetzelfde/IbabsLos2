#!/usr/bin/env python3
"""
Haalt vertrektijden op van EBS-haltes via drgl.nl, dedupliceert ritten
op journey-ID (zodat een lijn die op meerdere haltes stopt niet dubbel
geteld wordt), en detecteert uitval / oorzaken / storingsmeldingen.

Haltes:
    De Vlinder                            NL:S:37223552
    Gedempte Gracht                       NL:S:37223860
    Station Zaandam (Provincialeweg)      NL:S:37220130
    Purmerend, Busstation Tramplein       NL:S:37400100
    Purmerend, Station Overwhere          NL:S:37402090
    Edam, Busstation                      NL:S:38491011
    Monnickendam, Swaensborch             NL:S:37392150
    Assendelft/Krommenie-Assendelft (bus) NL:S:37320410
    Zaanse Schans                         NL:S:37221160
    Purmerend, Korenstraat                NL:S:37400500
    Purmerend, Neckerstraat               NL:S:37403640
    Amsterdam, Busstation Station Noord   NL:S:30001305
    (de laatste 6 zijn toegevoegd op 12-08-2026 — data per lijn/halte van
    vóór die datum dekt dus alleen wat via de eerste 3 haltes zichtbaar was)

Resultaat:
    data/ebs_uitval.json              — alleen geannuleerde ritten VAN VANDAAG
    data/ebs_totaal_teller.json       — totaal unieke ritten VAN VANDAAG
    data/ebs_percentage_historie.json — 1 samengevatte regel per afgesloten dag:
                                         totaal, cancelled, pct, PLUS een
                                         compacte uitsplitsing per lijn,
                                         oorzaak, halte en dagdeel (per_lijn,
                                         per_oorzaak, per_halte, per_dagdeel).
                                         Dit blijft klein: de grootte groeit
                                         met het aantal DISTINCTE lijnen/
                                         oorzaken/haltes/dagdelen (vrijwel
                                         constant), niet met het aantal
                                         ritten. Losse ritten worden dus NIET
                                         voor altijd bewaard, maar de
                                         belangrijkste dimensies wel — zodat
                                         het dashboard cumulatief kan blijven
                                         tonen "per lijn", "per oorzaak" etc.
                                         over de hele meetperiode, i.p.v.
                                         alleen over vandaag.
    data/ebs_meldingen.json           — NIEUW: de storingsmeldingen die EBS
                                         zelf bovenaan de departureboard
                                         toont (bv. "Door technische
                                         storingen kunnen ritten op lijn 67
                                         uitvallen", met een start/eind-
                                         tijdvenster). Deze laag is los van
                                         de per-rit-uitval en werd voorheen
                                         genegeerd. Blijft historisch bewaard
                                         (niet dagelijks weggegooid, want
                                         het volume is klein).

NIEUW: naast per_lijn (aantal UITGEVALLEN ritten per lijn) wordt nu ook
totaal_per_lijn bijgehouden — het totaal aantal ritten (uitgevallen én
gereden) per lijn. Zonder dat kon er geen uitvalpercentage per lijn
berekend worden, alleen een absoluut aantal. Dit geldt zowel voor de
live teller van vandaag als voor gearchiveerde dagen. LET OP: dagen van
vóór deze wijziging hebben geen totaal_per_lijn — de frontend moet dat
gewoon overslaan voor die dagen, niet crashen of foutief 0% tonen.

Zodra een nieuwe dag begint, wordt de vorige dag automatisch samengevat naar
ebs_percentage_historie.json (inclusief de uitsplitsingen hierboven) en
verdwijnen de losse ritten uit ebs_uitval.json en ebs_totaal_teller.json.
Zo groeien die twee bestanden nooit onbeperkt door. LET OP: eenmaal
gearchiveerde dagen worden nooit opnieuw gegenereerd, ook niet als die dag
onvolledig gevangen was (bv. door een gemiste scrape-run) — er is geen
correctiemechanisme achteraf.

Elke rit is een uniek record op (journey_id + datum). Eén fysieke rit die
op meerdere van de haltes stopt, wordt samengevoegd tot één record
met een lijst van haltebezoeken (elk met eigen tijd/status).

Status per rit (op rit-niveau, "ergste" status wint over de haltes heen):
    cancelled — geannuleerd op minstens één halte (dit is de ENIGE status
                die als "uitval" telt in de aggregaten)
    gereden   — normaal gereden (op tijd, vertraagd, al vertrokken, of
                voortijdig beëindigd zonder cancel-status — dat laatste
                wordt nog wel vastgelegd in "terminus_alert" als context,
                maar telt niet meer mee als aparte "verkort"-categorie)

Elke rit legt ook, waar beschikbaar, "product_label" (bv. "MeerPlus") en
"drukte_omschrijving"/"drukte_icoon" (de bezettingsindicatie die drgl.nl
per vertrek toont) vast.

Gebruik:
    python3 scrape_ebs.py

Draai dit elke 15 minuten (bijv. via cron-jobs.org die een GitHub Actions
workflow_dispatch triggert) zodat ritten die kort van tevoren worden
geannuleerd niet gemist worden voordat ze van het bord verdwijnen. LET OP:
dit hangt volledig af van die externe cron-service — er is geen ingebouwde
GitHub Actions 'schedule:'-trigger als vangnet.
"""

import hashlib
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
MELDINGEN_BESTAND = "data/ebs_meldingen.json"

HALTES = [
    {"id": "NL:S:37223552", "naam": "De Vlinder"},
    {"id": "NL:S:37223860", "naam": "Gedempte Gracht"},
    {"id": "NL:S:37220130", "naam": "Station Zaandam (Provincialeweg)"},
    {"id": "NL:S:37400100", "naam": "Purmerend, Busstation Tramplein"},
    # Dekt lijnen die niet bij Tramplein stoppen, o.a. 101, 102, 276,
    # 306 (R-net) en 413 — bevestigd via de live departureboard van drgl.nl
    # op 12-08-2026.
    {"id": "NL:S:37402090", "naam": "Purmerend, Station Overwhere (Churchilllaan)"},
    {"id": "NL:S:38491011", "naam": "Edam, Busstation"},
    {"id": "NL:S:37392150", "naam": "Monnickendam, Swaensborch"},
    {"id": "NL:S:37320410", "naam": "Assendelft/Krommenie-Assendelft, Rangeerder"},
    {"id": "NL:S:37221160", "naam": "Zaanse Schans"},
    # NIEUW: dekt lijnen 104, 272 en 307 — die eindigen in Purmer-Noord
    # (Korenstraat) en raken geen van de bovenstaande Purmerend-haltes.
    {"id": "NL:S:37400500", "naam": "Purmerend, Korenstraat"},
    # NIEUW: dekt lijn 308 (eindigt in Weidevenne) en buurtbus 416
    # (stopt hier onderweg, bevestigd via haltedata).
    {"id": "NL:S:37403640", "naam": "Purmerend, Neckerstraat"},
    # NIEUW: dekt lijn 119 (Amsterdam Noord – Landsmeer), die helemaal
    # niet in Purmerend komt en dus geen van de andere haltes raakt.
    {"id": "NL:S:30001305", "naam": "Amsterdam, Busstation Station Noord"},
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
        if not journey_id.startswith("EBS:"):
            continue  # niet-EBS vervoerder (bijv. Connexxion) — overslaan
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
        product_label = None
        drukte_icoon = None
        drukte_omschrijving = None
        if cat_div:
            cat_tekst = cat_div.get_text(" ", strip=True)
            delen = re.split(r"[•\u2022]|&bull;", cat_tekst)
            categorie = delen[0].strip() if delen and delen[0].strip() else None
            # NIEUW: het sub-label na de bullet (bv. "MeerPlus") werd eerder
            # wel gelezen maar daarna weggegooid — nu bewaren we het.
            if len(delen) > 1 and delen[1].strip():
                product_label = delen[1].strip()
            # NIEUW: de drukte-/bezettingsindicatie (icoon + alt-tekst, bv.
            # "Zitplaatsen beschikbaar") stond al in dezelfde div maar werd
            # nooit gelezen — get_text() negeert <img>, dus apart opvragen.
            drukte_img = cat_div.select_one("img")
            if drukte_img:
                drukte_omschrijving = drukte_img.get("alt")
                src = drukte_img.get("src", "")
                drukte_icoon = src.rsplit("/", 1)[-1] if src else None

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

        # "Verkort" (voortijdig beëindigd, "Terminates at ...") is GEEN
        # aparte statuscategorie meer — telt niet mee als uitval. We bewaren
        # het signaal wel als terminus_alert, puur als context bij een rit
        # die verder gewoon als "gereden" telt (tenzij ook echt cancelled).
        is_verkort = bool(notice_alert and notice_alert.lower().startswith("terminates"))
        status = "cancelled" if is_cancelled else "gereden"

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
            "product_label":  product_label,
            "drukte_icoon":   drukte_icoon,
            "drukte_omschrijving": drukte_omschrijving,
            "terminus_alert": notice_alert if is_verkort else None,
            "oorzaak_raw":    cause_raw,
            "oorzaak_categorieen": parse_oorzaak_categorieen(cause_raw),
            "advies":         advice,
        })

    return resultaten


# ── STORINGSMELDINGEN (bovenaan de departureboard) ────────────────────────────
_MELDING_DATUM_RE = lambda label: re.compile(
    rf"{label}:\s*(\d{{2}})-(\d{{2}})-(\d{{4}})\s+(\d{{2}}):(\d{{2}})"
)
_LIJN_MELDING_RE = re.compile(
    r"(?:lijn(?:en)?|bus(?:sen)?)\s+([0-9]{1,3}(?:\s*(?:,|en|&amp;|&)\s*[0-9]{1,3})*)",
    re.IGNORECASE,
)


def _parse_meldingdatum(tekst, label):
    m = _MELDING_DATUM_RE(label).search(tekst)
    if not m:
        return None
    dag, maand, jaar, uur, minuut = m.groups()
    return f"{jaar}-{maand}-{dag}T{uur}:{minuut}:00"


def _extract_mogelijke_lijnen(tekst):
    """
    Best-effort: pikt lijnnummers op die direct na 'lijn(en)'/'bus(sen)' in de
    meldingstekst staan. Geen gegarandeerd volledige of correcte lijst —
    vrije tekst is niet waterdicht te parsen — dus altijd als hint tonen,
    nooit als harde data.
    """
    gevonden = set()
    for m in _LIJN_MELDING_RE.finditer(tekst):
        for getal in re.findall(r"\d{1,3}", m.group(1)):
            gevonden.add(getal)
    return sorted(gevonden, key=lambda x: int(x))


def _melding_id(titel, starttijd, eindtijd):
    ruw = f"{titel}|{starttijd}|{eindtijd}"
    return hashlib.sha1(ruw.encode("utf-8")).hexdigest()[:12]


def parse_meldingen_html(html, halte_naam):
    """
    Parseert de storingsmeldingen/aankondigingen bovenaan de departureboard
    — een aparte informatielaag t.o.v. individuele ritstatussen (bv. "Door
    technische storingen kunnen ritten op lijn 67 uitvallen", geldig van/tot
    een expliciet tijdvenster). Deze items zijn te herkennen aan de
    'button'-class en hebben geen /journey/-link — voorheen werden ze puur
    op basis daarvan overgeslagen (zie parse_halte_html), nu lezen we ze
    apart uit.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("div.list-group > a.list-group-item.button")

    resultaten = []
    for item in items:
        titel_el = item.select_one("b")
        titel = titel_el.get_text(" ", strip=True) if titel_el else None

        # Los kopietje van dit item om <b> (titel, staat er al apart bij) en
        # .collapse (de Starttijd/Eindtijd-regel, staat er ook al apart bij)
        # uit te knippen — anders zit "bericht" straks dubbelop met de titel
        # én vervuild met de periode-tekst.
        item_copy = BeautifulSoup(str(item), "html.parser")
        for sub in item_copy.select("b, .collapse, i.material-icons"):
            sub.decompose()
        bericht = item_copy.get_text(" ", strip=True)
        if not bericht:
            bericht = titel

        periode_el = item.select_one(".collapse p")
        periode_tekst = periode_el.get_text(" ", strip=True) if periode_el else ""
        starttijd = _parse_meldingdatum(periode_tekst, "Starttijd")
        eindtijd  = _parse_meldingdatum(periode_tekst, "Eindtijd")

        resultaten.append({
            "id":              _melding_id(titel, starttijd, eindtijd),
            "titel":           titel,
            "bericht":         bericht,
            "starttijd":       starttijd,
            "eindtijd":        eindtijd,
            "mogelijk_lijnen": _extract_mogelijke_lijnen(bericht),
            "halte_naam":      halte_naam,
        })
    return resultaten


def fetch_halte(opener, halte_id):
    url = f"{BASE_URL}/stop/{halte_id}"
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── SAMENVOEGEN OVER HALTES ───────────────────────────────────────────────────
STATUS_PRIORITEIT = {"cancelled": 1, "gereden": 0}


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
                "product_label": v["product_label"],
                "drukte_icoon": v["drukte_icoon"],
                "drukte_omschrijving": v["drukte_omschrijving"],
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
        if v["drukte_omschrijving"] and not rit["drukte_omschrijving"]:
            rit["drukte_omschrijving"] = v["drukte_omschrijving"]
            rit["drukte_icoon"] = v["drukte_icoon"]

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


# ── MELDINGEN (storingsmeldingen, los van individuele ritten) ─────────────────
def laad_meldingen():
    """Retourneert een dict {id: melding} zodat we op id kunnen mergen
    (dezelfde melding kan op meerdere haltes gezien worden)."""
    if not os.path.exists(MELDINGEN_BESTAND):
        return {}
    with open(MELDINGEN_BESTAND, encoding="utf-8") as f:
        try:
            return {m["id"]: m for m in json.load(f)}
        except (json.JSONDecodeError, KeyError, TypeError):
            return {}


def bewaar_meldingen(meldingen):
    os.makedirs("data", exist_ok=True)
    lijst = sorted(meldingen.values(), key=lambda m: m.get("starttijd") or "", reverse=True)
    with open(MELDINGEN_BESTAND, "w", encoding="utf-8") as f:
        json.dump(lijst, f, ensure_ascii=False, indent=2)


# ── NIEUW: COMPACTE PER-DAG AGGREGATEN (per lijn/oorzaak/halte/dagdeel) ───────
def _tel_enkelvoudig(ritten, sleutel_fn):
    """Telt ritten op een sleutel die per rit precies 1 waarde oplevert
    (bijv. lijn, halte, dagdeel). None-waarden worden overgeslagen."""
    teller = {}
    for r in ritten:
        sleutel = sleutel_fn(r)
        if sleutel is None:
            continue
        teller[sleutel] = teller.get(sleutel, 0) + 1
    return teller


def _tel_meervoudig(ritten, lijst_fn):
    """Telt ritten op een sleutel die per rit meerdere waarden kan opleveren
    (bijv. oorzaak_categorieen: een rit kan meerdere oorzaken hebben)."""
    teller = {}
    for r in ritten:
        for sleutel in lijst_fn(r):
            teller[sleutel] = teller.get(sleutel, 0) + 1
    return teller


def bouw_dag_aggregaat(oude_datum, teller, bestaand_ruw):
    """
    Bouwt een compact aggregaat voor één afgesloten dag: totalen plus een
    uitsplitsing per lijn/oorzaak/halte/dagdeel. Dit bevat GEEN losse
    ritten meer (geen bestemming, geen exacte tijden, geen journey_id) —
    alleen tellingen. De grootte van dit aggregaat groeit met het aantal
    DISTINCTE lijnen/oorzaken/haltes/dagdelen, wat in de praktijk vrijwel
    constant is, ongeacht hoeveel ritten er die dag waren.
    """
    ritten_die_dag = [r for r in bestaand_ruw.values() if r["datum"] == oude_datum]
    uitgevallen = [r for r in ritten_die_dag if r["status"] == "cancelled"]

    totaal    = teller.get(oude_datum, {}).get("totaal", 0)
    cancelled = len(uitgevallen)

    return {
        "totaal":      totaal,
        "cancelled":   cancelled,
        "pct":         round(cancelled / totaal * 100, 1) if totaal else 0,
        "per_lijn":    _tel_enkelvoudig(uitgevallen, lambda r: r["lijn"]),
        # NIEUW: totaal aantal ritten (uitgevallen + gereden) per lijn, nodig
        # om een uitvalPERCENTAGE per lijn te kunnen berekenen — per_lijn
        # hierboven telt alleen de uitval, niet de noemer.
        "totaal_per_lijn": teller.get(oude_datum, {}).get("totaal_per_lijn", {}),
        "per_oorzaak": _tel_meervoudig(uitgevallen, lambda r: r["oorzaak_categorieen"] or []),
        "per_halte":   _tel_enkelvoudig(uitgevallen, lambda r: (r["haltes"] or [{}])[0].get("halte_naam")),
        "per_dagdeel": _tel_enkelvoudig(uitgevallen, lambda r: r["dagdeel"]),
    }


def archiveer_oude_dagen(vandaag, teller, bestaand_ruw):
    """
    Zet elke dag die niet 'vandaag' is om in één samengevat aggregaat in
    ebs_percentage_historie.json (totalen + per_lijn/oorzaak/halte/dagdeel),
    en verwijdert die dag daarna uit de teller. De aanroeper is
    verantwoordelijk voor het filteren van bestaand_ruw (ebs_uitval.json)
    op alleen 'vandaag' ná deze aanroep — dat is het enige moment waarop
    de losse ritten van de oude dag nog beschikbaar zijn om te aggregeren,
    dus dit MOET gebeuren vóórdat ze elders worden weggegooid.
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
        historie[oude_datum] = bouw_dag_aggregaat(oude_datum, teller, bestaand_ruw)
        gewijzigd = True

    if gewijzigd:
        bewaar_historie(historie)
        print(f"  Historie bijgewerkt met {len(oude_datums)} afgesloten dag(en) (incl. per_lijn/oorzaak/halte/dagdeel)")

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
    alle_meldingen = []
    for halte in HALTES:
        print(f"  {halte['naam']} ({halte['id']})...", end=" ", flush=True)
        try:
            html = fetch_halte(opener, halte["id"])
            vertrekken = parse_halte_html(html, halte["id"], halte["naam"])
            alle_vertrekken.extend(vertrekken)
            alle_meldingen.extend(parse_meldingen_html(html, halte["naam"]))
            n_cancel = sum(1 for v in vertrekken if v["status"] == "cancelled")
            print(f"OK — {len(vertrekken)} vertrekken, {n_cancel} cancelled")
        except Exception as e:
            print(f"MISLUKT ({e})")
        time.sleep(0.5)

    if not alle_vertrekken:
        print("Geen vertrekken opgehaald — stoppen zonder wijzigingen.")
        return

    # ── MELDINGEN SAMENVOEGEN & OPSLAAN ───────────────────
    # Dezelfde melding kan op meerdere haltes voorkomen (identieke id, want
    # die is afgeleid van titel+start+eind) — mergen op id, haltes_gezien
    # groeit dan vanzelf i.p.v. dat de melding dupliceert.
    meldingen = laad_meldingen()
    nieuwe_meldingen_count = 0
    for m in alle_meldingen:
        bestaand_m = meldingen.get(m["id"])
        if bestaand_m:
            haltes_gezien = set(bestaand_m.get("haltes_gezien", [bestaand_m.get("halte_naam")]))
            haltes_gezien.add(m["halte_naam"])
            bestaand_m["haltes_gezien"] = sorted(h for h in haltes_gezien if h)
            bestaand_m["laatst_gezien"] = nu.strftime("%Y-%m-%d %H:%M:%S")
            bestaand_m.pop("halte_naam", None)
        else:
            m["haltes_gezien"] = [m.pop("halte_naam")]
            m["eerst_gezien"] = nu.strftime("%Y-%m-%d %H:%M:%S")
            m["laatst_gezien"] = m["eerst_gezien"]
            meldingen[m["id"]] = m
            nieuwe_meldingen_count += 1
    if alle_meldingen:
        bewaar_meldingen(meldingen)
        if nieuwe_meldingen_count:
            print(f"  Meldingen: {nieuwe_meldingen_count} nieuwe storingsmelding(en) opgeslagen ({len(meldingen)} totaal)")

    # Combineer alle vertrekken tot unieke ritten (ongeacht status)
    nieuwe_ritten = combineer_ritten(alle_vertrekken)

    # ── OUDE DAGEN ARCHIVEREN & OPRUIMEN ──────────────────
    # Alles wat nu nog in ebs_uitval.json / ebs_totaal_teller.json staat en
    # niet van vandaag is, wordt samengevat naar de historie (incl. de
    # per_lijn/oorzaak/halte/dagdeel-uitsplitsing) en daarna weggegooid,
    # zodat beide bestanden nooit onbeperkt groeien.
    teller       = laad_teller()
    bestaand_ruw = load_existing()
    teller       = archiveer_oude_dagen(vandaag, teller, bestaand_ruw)

    if vandaag not in teller:
        teller[vandaag] = {"totaal": 0, "journeys": [], "totaal_per_lijn": {}}
    teller[vandaag].setdefault("totaal_per_lijn", {})

    # Alle journey_id's van vandaag (alleen unieke, dat zijn ze al in 'nieuwe_ritten')
    ids_vandaag = {rit["journey_id"] for rit in nieuwe_ritten.values() if rit["datum"] == vandaag}
    bestaande_ids = set(teller[vandaag]["journeys"])
    nieuwe_ids = ids_vandaag - bestaande_ids

    # NIEUW: lijn-lookup zodat we per nieuwe unieke rit ook totaal_per_lijn
    # kunnen bijhouden — nodig voor een uitvalpercentage per lijn (zie
    # bouw_dag_aggregaat hierboven voor dezelfde logica bij afgesloten dagen).
    journey_naar_lijn = {
        rit["journey_id"]: rit["lijn"]
        for rit in nieuwe_ritten.values()
        if rit["datum"] == vandaag
    }

    if nieuwe_ids:
        for jid in nieuwe_ids:
            lijn = journey_naar_lijn.get(jid)
            if lijn:
                teller[vandaag]["totaal_per_lijn"][lijn] = teller[vandaag]["totaal_per_lijn"].get(lijn, 0) + 1
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
