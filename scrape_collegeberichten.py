#!/usr/bin/env python3
"""
Haalt raadsinformatiebrieven en kennisgevingen op uit iBabs Zaanstad,
downloadt de bijbehorende PDFs, extraheert de tekst, en detecteert
checkwaardige claims via twee lagen:

  1. CODE — altijd actief, regex-patronen op geldbedragen, percentages,
             beloftetaal, datumdeadlines, jaartalvergelijkingen,
             vastgoedwaarden en vage beweringen. Elke code-claim krijgt
             ook een niet-AI kruischeck: een deterministische vergelijking
             met stemmingen, moties, aanbestedingen en eerdere
             collegebrieven van dezelfde portefeuillehouder (zie
             kruischeck_claim()) — geen taalmodel, alleen tekst- en
             bedragvergelijking.
  2. AI   — optioneel, Gemini 1.5 Flash voor diepere claimanalyse

Claims krijgen een "bron"-veld: "code" of "ai".
Resultaat wordt opgeslagen in data/collegeberichten.json

Gebruik:
    python3 scrape_collegeberichten.py

Vereiste omgevingsvariabelen:
    GEMINI_API_KEY  — Gemini API key (optioneel, alleen voor AI-laag)

Optionele omgevingsvariabelen:
    SCRAPE_VANAF    — datum YYYY-MM-DD (standaard: afgelopen 7 dagen)
"""

import json
import re
import time
import sys
import os
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from datetime import datetime, timedelta

# ── CONFIGURATIE ──────────────────────────────────────────────────────────────
BASE_URL       = "https://zaanstad.bestuurlijkeinformatie.nl"
LIJST_PAGE_URL = f"{BASE_URL}/Reports/Details/8ea04074-52e6-4284-bd1a-66e378b40ec1"
LIJST_DATA_URL = f"{BASE_URL}/Reports/GetReportData/8ea04074-52e6-4284-bd1a-66e378b40ec1"
PAGE_SIZE      = 100
OUTPUT         = "data/collegeberichten.json"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent"
)

RELEVANTE_TYPEN = {"Raadsinformatiebrief", "Kennisgeving"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE_URL,
}

COLUMNS = [
    ("title",                      False),
    ("datumbericht",               True),
    ("portefeuillehouderselectie", True),
    ("typeselectie",               True),
    ("afhandelingselectie",        True),
    ("registrationdate",           True),
]

# ── PRIORITEITSSCORES ─────────────────────────────────────────────────────────
PRIO_SCORE = {"HOOG": 75, "MIDDEL": 45, "LAAG": 20}

# ── RETRY-HELPER ──────────────────────────────────────────────────────────────
def open_met_retry(opener, req, timeout=30, retries=3, wachttijden=(2, 5, 10)):
    """
    Voert opener.open(req) uit met retries bij tijdelijke netwerkfouten
    (timeouts, 5xx-serverfouten, connectieproblemen). Geeft de response
    terug bij succes, of raised de laatste fout na alle pogingen.
    """
    laatste_fout = None
    for poging in range(1, retries + 1):
        try:
            return opener.open(req, timeout=timeout)
        except Exception as e:
            laatste_fout = e
            if poging < retries:
                wacht = wachttijden[min(poging - 1, len(wachttijden) - 1)]
                print(f"(poging {poging}/{retries} mislukt: {e} — {wacht}s wachten)", end=" ", flush=True)
                time.sleep(wacht)
    raise laatste_fout


def urlopen_met_retry(req, timeout=30, retries=3, wachttijden=(2, 5, 10)):
    """
    Zelfde als open_met_retry, maar voor kale urllib.request.urlopen-calls
    (dus zonder cookiejar-opener) — gebruikt bij de Gemini-call.
    """
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


# ── CODE-GEBASEERDE CLAIMDETECTIE ─────────────────────────────────────────────
#
# Elk patroon is een tuple van:
#   (regex, prioriteit, verificatietip)
#
# De detectie werkt per zin: als een zin matcht op een patroon
# wordt die zin als claim opgeslagen. Eén match per zin volstaat.
#
CODE_PATRONEN = [
    # Geldbedragen met €-teken — FIX: eist een cijfer aan het eind, zodat een
    # zin-afsluitende punt ("€500.") niet wordt meegepakt in het bedrag.
    (
        r'€\s*\d(?:[\d.,]*\d)?(?:\s*(?:miljoen|miljard|mln|mld|duizend|k))?',
        "HOOG",
        "Controleer bedrag via gemeentebegroting, raadsstuk of jaarverslag"
    ),
    # Geldbedragen zonder €-teken maar met "euro" voluit geschreven —
    # NIEUW: dit gat bestond nog niet. "2 miljoen euro" werd eerder gemist.
    (
        r'\b\d+(?:[\d.,]*\d)?\s*(?:euro|miljoen\s+euro|miljard\s+euro)\b',
        "HOOG",
        "Controleer bedrag via gemeentebegroting, raadsstuk of jaarverslag"
    ),
    # Percentages — FIX: "procent" voluit geschreven werd eerder gemist,
    # alleen het %-teken werd herkend.
    (
        r'\b\d+(?:[,.]\d+)?\s*(?:%|procent)\b',
        "HOOG",
        "Controleer percentage via bron, onderzoeksrapport of CBS-data"
    ),
    # Concrete aantallen mensen/woningen
    (
        r'\b\d+\s*(?:woningen|appartementen|inwoners|huishoudens|bewoners|'
        r'vluchtelingen|statushouders|leerlingen|medewerkers|fte|arbeidsplaatsen)',
        "HOOG",
        "Controleer aantal via CBS, gemeentelijke rapportage of aanbieder"
    ),
    # Concrete aantallen — ALGEMEEN, NIEUW. Het patroon hierboven werkt alleen
    # voor een vaste lijst zelfstandige naamwoorden en miste daardoor elk
    # ander telbaar ding dat het college claimt te hebben gerealiseerd of
    # geplaatst ("40 laadpalen", "15 speeltuinen", "8 handhavers"). Dit vangt
    # elke "wij/we/het college hebben/heeft ... [getal] [naamwoord]"-claim,
    # ongeacht het naamwoord zelf — precies het soort zelf-gerapporteerde
    # college-cijfer waar het om gaat.
    # De negative lookahead sluit maandnamen uit, zodat een datum als
    # "3 juni" niet als aantalclaim wordt gelezen.
    (
        r'(?=.*\b(?:wij|we|het college)\s+(?:hebben|heeft)\b)'
        r'(?=.*\b\d+(?:\.\d+)?\s+'
        r'(?!januari|februari|maart|april|mei|juni|juli|augustus|'
        r'september|oktober|november|december)[a-zà-ÿ]{4,}\b)',
        "HOOG",
        "Controleer dit concrete aantal via CBS, gemeentelijke rapportage of de betrokken uitvoerder"
    ),
    # Concrete aantallen incidenten/meldingen
    (
        r'\b\d+\s*(?:meldingen|klachten|incidenten|overtredingen|'
        r'aanvragen|bezwaren|vergunningen)',
        "MIDDEL",
        "Controleer via jaarrapportage handhaving of gemeentelijke registratie"
    ),
    # Datumdeadlines en tijdskaders
    (
        r'(?:voor|eind|in|per|uiterlijk)\s+'
        r'(?:20\d\d|dit jaar|volgend jaar|begin \d{4}|medio \d{4}|Q[1-4]\s*20\d\d)',
        "MIDDEL",
        "Controleer deadline via eerder raadsstuk, motie of collegebrief"
    ),
    # Expliciete beloftes en toezeggingen
    (
        r'(?:zal worden|gaan we|wordt gerealiseerd|is toegezegd|'
        r'hebben wij toegezegd|wordt opgeleverd|zullen wij|'
        r'nemen wij|doen wij|streven wij|is onze inzet)',
        "MIDDEL",
        "Controleer toezegging via eerdere collegebrieven, moties of raadsvragen"
    ),
    # Vergelijkingen met eerdere periodes
    (
        r'(?:ten opzichte van|vergeleken met|meer dan vorig jaar|'
        r'minder dan vorig jaar|stijging van|daling van|'
        r'toegenomen met|afgenomen met|hoger dan|lager dan)',
        "MIDDEL",
        "Controleer vergelijking via jaarrapportage, CBS of vorige collegebrief"
    ),
    # Wetsartikelen en beleidsreferenties
    (
        r'(?:artikel\s+\d+[a-z]?|wet\s+[A-Z][a-z]+|'
        r'besluit\s+[A-Z][a-z]+|verordening\s+[a-z])',
        "LAAG",
        "Controleer wetsartikel of beleidsdocument via overheid.nl of gemeentearchief"
    ),
    # Vage grote hoeveelheden zonder concreet getal — NIEUW. Dit is precies
    # het soort taalgebruik dat onderbouwing ontwijkt: "honderden meldingen"
    # klinkt concreet maar bevat geen controleerbaar getal.
    (
        r'\b(?:tientallen|honderden|duizenden|miljoenen)\b',
        "LAAG",
        "Vage hoeveelheid zonder concreet getal — vraag om het exacte aantal"
    ),
    # Vage superlatieven zonder onderbouwing
    (
        r'\b(?:structureel|aanzienlijk|fors|significant|'
        r'sterk gestegen|sterk gedaald|substantieel|'
        r'groot aantal|veel meer|veel minder|hoog risico)\b',
        "LAAG",
        "Vage bewering zonder getal of bron — vraag om kwantificering"
    ),
    # Jaar-op-jaar vergelijkingen met een expliciet jaartal — NIEUW.
    # Dit is scherper dan het algemene "vergeleken met vorig jaar"-patroon
    # hierboven, omdat het jaartal zelf te verifiëren is (bv. "in 2023 was
    # dit nog 40%").
    (
        r'\b(?:in|sinds)\s+20\d\d\s+(?:was|waren|bedroeg|bedroegen|lag|lagen)\b',
        "MIDDEL",
        "Controleer het genoemde jaartal via het jaarverslag of de begroting van dat jaar"
    ),
    # Vastgoed- en taxatiewaarden — NIEUW. Komt vaak voor bij woningsluitingen,
    # aankoop/verkoop van gemeentelijk vastgoed en grondexploitaties, en werd
    # eerder alleen gevangen als er toevallig ook een €-teken bij stond.
    (
        r'\b(?:woz-waarde|getaxeerd(?:e|\s+op)?|marktwaarde|residuele\s+waarde|'
        r'boekwaarde)\b',
        "MIDDEL",
        "Controleer waardering via taxatierapport of WOZ-gegevens"
    ),
    # Relatieve termijnen — NIEUW. "binnen 6 weken" is net zo goed een
    # controleerbare toezegging als een harde einddatum, maar werd door het
    # bestaande datumdeadline-patroon (dat op jaartallen/kwartalen let)
    # gemist.
    (
        r'\bbinnen\s+\d+\s*(?:dag|dagen|week|weken|maand|maanden|jaar|jaren)\b',
        "MIDDEL",
        "Controleer of deze termijn is gehaald via een latere collegebrief of raadsvraag"
    ),
    # Looptijd/contractduur — NIEUW. Vaak relevant bij aanbestedingen en
    # samenwerkingsovereenkomsten; een genoemde looptijd is direct te
    # vergelijken met de aanbestedingsdata die het dashboard al heeft.
    (
        r'\b(?:looptijd|contractperiode|contractduur)\s+van\s+\d+\s*'
        r'(?:jaar|jaren|maand|maanden)\b',
        "MIDDEL",
        "Vergelijk de genoemde looptijd met de onderliggende aanbesteding of overeenkomst"
    ),
    # Vermenigvuldigingsfactoren — NIEUW. "verdubbeld", "drie keer zoveel" is
    # net zo'n harde, verifieerbare claim als een percentage, maar wordt door
    # geen van de bestaande patronen gevangen omdat er geen cijfer/€/% in de
    # zin zelf hoeft te staan.
    (
        r'\b(?:verdubbeld(?:e)?|verdrievoudigd(?:e)?|verviervoudigd(?:e)?|'
        r'gehalveerd(?:e)?|\d+\s*keer\s+zo\s*(?:veel|groot|hoog|laag)|'
        r'een\s+factor\s+\d+)\b',
        "MIDDEL",
        "Controleer de genoemde toe- of afname via de onderliggende cijfers"
    ),
    # Toezeggingen gekoppeld aan een met naam genoemde bestuurder — NIEUW.
    # Dit maakt een toezegging persoonlijk herleidbaar, wat 'm nieuwswaardiger
    # maakt dan een anonieme "het college zal".
    (
        r'\bwethouder\s+[A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+)?\s+'
        r'(?:heeft|zal|gaat|is van plan|zegt toe|heeft toegezegd)\b',
        "MIDDEL",
        "Controleer deze persoonlijke toezegging via het raadsverslag of latere correspondentie"
    ),
    # Vage tijdsaanduidingen — NIEUW, aanvullend op de bestaande harde
    # datumdeadlines. "Op korte termijn" en "de komende periode" klinken als
    # een toezegging maar zijn niet aan een controleerbare datum te knopen.
    (
        r'\b(?:op korte termijn|op afzienbare termijn|zo spoedig mogelijk|'
        r'de komende periode|de komende tijd|binnenkort|op termijn)\b',
        "LAAG",
        "Vage tijdsaanduiding zonder harde datum — vraag om een concrete termijn"
    ),
    # Ongefundeerde positieve zelfevaluatie — NIEUW. Colleges rapporteren
    # zelden negatief over eigen beleid; een kwalitatief oordeel zonder cijfer
    # is daarom extra de moeite van het navragen waard.
    (
        r'\b(?:succesvol verlopen|goed verlopen|naar tevredenheid|'
        r'positief ontvangen|positief geëvalueerd|breed gedragen)\b',
        "LAAG",
        "Zelfevaluatie zonder onderliggend cijfer — vraag naar de meetbare uitkomst"
    ),
]


# ── LAAG 0: INTERNE REKENKUNDIGE CONSISTENTIE ─────────────────────────────────
#
# Deze check heeft geen enkele externe data nodig — hij rekent puur na of een
# "gestegen/gedaald van X naar Y"-formulering wel klopt met de twee genoemde
# getallen. Dat vangt tikfouten en slordige formuleringen die met platte
# regex-detectie (die alleen op het BESTAAN van een getal let) nooit zouden
# opvallen.
_RICHTING_WOORDEN = {
    "gestegen":   "op", "toegenomen": "op", "gegroeid":  "op", "opgelopen": "op",
    "gedaald":    "neer", "afgenomen": "neer", "gekrompen": "neer", "gezakt": "neer",
}

def check_richting_tegenstrijdigheid(zin):
    """
    Vindt claims van de vorm "gestegen van X naar Y" en controleert of de
    richting (stijging/daling) daadwerkelijk klopt met de twee genoemde
    getallen. Geeft een kant-en-klare kruischeck-tekst terug (beginnend met
    "Afwijkend:", zodat de bestaande kleurcodering in de UI 'm automatisch
    als weersproken toont), of None als er niets te controleren valt of de
    richting wél klopt.
    """
    patroon = (
        r'\b(' + '|'.join(_RICHTING_WOORDEN) + r')\s+van\s+'
        r'(\d+(?:[.,]\d+)?)\s*(?:%|procent)?\s+naar\s+'
        r'(\d+(?:[.,]\d+)?)\s*(?:%|procent)?'
    )
    m = re.search(patroon, zin, re.IGNORECASE)
    if not m:
        return None
    woord     = m.group(1).lower()
    richting  = _RICHTING_WOORDEN[woord]
    try:
        van   = float(m.group(2).replace(',', '.'))
        naar  = float(m.group(3).replace(',', '.'))
    except ValueError:
        return None
    if richting == "op" and naar <= van:
        return (f"Afwijkend: '{woord}' duidt op een toename, maar {van:g} naar {naar:g} "
                f"is geen stijging — controleer of dit een tikfout of onjuiste formulering is")
    if richting == "neer" and naar >= van:
        return (f"Afwijkend: '{woord}' duidt op een afname, maar {van:g} naar {naar:g} "
                f"is geen daling — controleer of dit een tikfout of onjuiste formulering is")
    return None


def detecteer_code_claims(tekst, context=None):
    """
    Detecteert checkwaardige claims via regex-patronen.
    Werkt altijd, ook zonder Gemini API key.
    Geeft maximaal 10 claims terug met bron='code'.

    'context' (optioneel) schakelt de niet-AI kruischeck in — zie
    kruischeck_claim() hieronder. Zonder context blijft 'kruischeck' None,
    exact zoals voorheen.
    """
    if not tekst:
        return []

    # Splits op zinsgrenzen
    zinnen = re.split(r'(?<=[.!?])\s+|\n', tekst)

    claims = []
    gezien = set()  # voorkom duplicaten

    for zin in zinnen:
        zin = zin.strip()
        # Te kort of te lang om zinvol te zijn
        if len(zin) < 25 or len(zin) > 500:
            continue

        # Laag 0: interne rekenkundige tegenstrijdigheid — gaat vóór de
        # gewone patronen, want dit is een hard feitelijk probleem in de zin
        # zelf, geen "vraag dit na"-signaal. Werkt zonder context/externe data.
        tegenstrijdigheid = check_richting_tegenstrijdigheid(zin)
        if tegenstrijdigheid:
            sleutel = zin[:50].lower()
            if sleutel not in gezien:
                gezien.add(sleutel)
                claims.append({
                    "claim":      zin[:250],
                    "verificatie": "Controleer of dit een tikfout of onjuiste formulering in de brief is",
                    "prioriteit": "HOOG",
                    "score":      PRIO_SCORE["HOOG"],
                    "bron":       "code",
                    "kruischeck": tegenstrijdigheid,
                })
                if len(claims) >= 10:
                    break
            continue

        for patroon, prioriteit, verificatie in CODE_PATRONEN:
            if re.search(patroon, zin, re.IGNORECASE):
                # Dedupliceer op basis van eerste 50 tekens
                sleutel = zin[:50].lower()
                if sleutel in gezien:
                    break
                gezien.add(sleutel)

                claims.append({
                    "claim":      zin[:250],
                    "verificatie": verificatie,
                    "prioriteit": prioriteit,
                    "score":      PRIO_SCORE[prioriteit],
                    "bron":       "code",
                    "kruischeck": kruischeck_claim(zin, context) if context else None,
                })
                break  # één match per zin is genoeg

        if len(claims) >= 10:
            break

    # Sorteer: HOOG eerst
    volgorde = {"HOOG": 0, "MIDDEL": 1, "LAAG": 2}
    claims.sort(key=lambda c: volgorde.get(c["prioriteit"], 9))

    return claims


# ── NIET-AI KRUISCHECK ─────────────────────────────────────────────────────────
#
# Vergelijkt een gedetecteerde claim deterministisch (geen AI, geen taalmodel)
# met data die de dashboard al zelf verzamelt: aanbestedingen (bedragen),
# stemmingen (raadsbesluiten), moties, en eerder verwerkte collegebrieven van
# dezelfde portefeuillehouder. Puur op tekst-overlap en getallen — dus 100%
# reproduceerbaar en uit te leggen.

_STOPWOORDEN = {
    "college", "gemeente", "zaanstad", "wordt", "worden", "hebben", "heeft",
    "wij", "deze", "onze", "voor", "over", "naar", "vanaf", "tegen", "binnen",
    "raad", "brief", "kennisgeving", "informeren", "informatie", "verzoek",
    # NIEUW: generieke bestuurs-/procedurewoorden die in vrijwel elk
    # raadsstuk voorkomen en daardoor géén signaal zijn dat twee documenten
    # over hetzelfde ONDERWERP gaan — ontdekt doordat "wijziging" + het
    # toevallig gedeelde "Waterland" (uit twee heel verschillende namen,
    # "Zaanstreek-Waterland" vs "Twiske-Waterland") een valse match gaf.
    "wijziging", "wijzigen", "vaststellen", "vaststelling", "verordening",
    "besluit", "voorstel", "regeling", "toelichting", "advies", "betreft",
    "onderwerp", "portefeuille", "portefeuillehouder", "leden", "geachte",
    # NIEUW: documenttype-labels — deze staan per definitie in vrijwel elke
    # titel binnen hun eigen categorie (elke motie heet "Motie ...") en zijn
    # dus geen bruikbaar onderwerp-signaal. Ontdekt doordat twee compleet
    # ongerelateerde moties via het woord "Motie" zelf als eigennaam matchten.
    "motie", "moties", "amendement", "amendementen", "vreemd",
}

def _belangrijke_woorden(tekst):
    """Geeft de betekenisvolle woorden (>=5 tekens, geen stopwoord) uit tekst."""
    woorden = re.findall(r"[a-zA-ZÀ-ÿ]{5,}", (tekst or "").lower())
    return {w for w in woorden if w not in _STOPWOORDEN}


def _geeft_gedeelde_eigennaam(a, b):
    """
    NIEUW: True als a en b een betekenisvol woord delen dat in de brontekst
    ook daadwerkelijk met een hoofdletter voorkomt (in minstens één van de
    twee) — dat wijst op een eigennaam (straat, plaats, projectnaam) in
    plaats van toevallig gedeeld jargon, en is daarom ook met maar één
    treffer al een betrouwbaar signaal. Ontdekt doordat een brief over
    "Veiligheidsmaatregelen Zuiddijk" niet aan de woningsluiting op diezelfde
    Zuiddijk werd gekoppeld: één gedeeld woord haalde de gewone drempel van
    twee niet, terwijl het wél de exacte, specifieke straatnaam was.

    Sluit woorden uit die ALLEEN voorkomen als tweede helft van een
    koppelteken-naam (bv. "Waterland" in "Zaanstreek-Waterland" versus
    "Twiske-Waterland") — dat is het fragment van een langere, per geval
    verschillende naam, geen zelfstandige eigennaam-match. Zonder deze
    uitzondering gaf "Zaanstreek-Waterland" een valse match met het
    volledig ongerelateerde "Twiske-Waterland".
    """
    gedeeld = _belangrijke_woorden(a) & _belangrijke_woorden(b)
    if not gedeeld:
        return False
    volledige_tekst = f"{a or ''} {b or ''}"
    los_hoofdletter = re.findall(r'(?<!-)\b([A-ZÀ-Þ][a-zà-ÿ]{4,})\b', volledige_tekst)
    hoofdletter_woorden = {w.lower() for w in los_hoofdletter}
    return bool(gedeeld & hoofdletter_woorden)


def _woord_overlap(a, b, minimum=2):
    """
    True als twee teksten minstens 'minimum' betekenisvolle woorden delen,
    óf als ze samen al aan één gedeelde eigennaam genoeg hebben (zie
    _geeft_gedeelde_eigennaam) — dat laatste is vaak specifieker dan twee
    toevallig gedeelde gewone woorden.
    """
    gedeeld = _belangrijke_woorden(a) & _belangrijke_woorden(b)
    if len(gedeeld) >= minimum:
        return True
    return _geeft_gedeelde_eigennaam(a, b)


def _parse_bedrag(tekst):
    """
    Zet een Nederlandstalig bedrag ('€ 2,5 miljoen', '450.000 euro') om naar
    een float in euro's. Geeft None als er geen bedrag in de tekst staat.
    Twee vormen, precies zoals de twee €-patronen in CODE_PATRONEN: met
    €-teken (het woord "euro" hoeft er dan niet bij te staan), of met het
    woord "euro" voluit (dan hoeft er geen €-teken te staan).
    """
    m = re.search(
        r'€\s*(\d(?:[\d.,]*\d)?)\s*(miljoen|miljard|mln|mld|duizend|k)?',
        tekst, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'\b(\d(?:[\d.,]*\d)?)\s*(miljoen\s+euro|miljard\s+euro|euro)\b',
            tekst, re.IGNORECASE
        )
    if not m:
        return None
    ruw = m.group(1)
    eenheid = (m.group(2) or "").lower().replace(" euro", "").strip()
    if eenheid == "euro":
        eenheid = ""
    # NL-notatie: punt = duizendtal-scheiding, komma = decimaal
    getal_str = ruw.replace(".", "").replace(",", ".")
    try:
        getal = float(getal_str)
    except ValueError:
        return None
    vermenigvuldiger = {
        "miljoen": 1_000_000, "mln": 1_000_000,
        "miljard": 1_000_000_000, "mld": 1_000_000_000,
        "duizend": 1_000, "k": 1_000,
    }.get(eenheid, 1)
    return getal * vermenigvuldiger


def _dagen_verschil(datum_a, datum_b):
    try:
        da = datetime.strptime(datum_a[:10], "%Y-%m-%d")
        db = datetime.strptime(datum_b[:10], "%Y-%m-%d")
        return abs((da - db).days)
    except (ValueError, TypeError):
        return None


def _interne_brief_tegenstrijdigheid(claim_tekst, context):
    """
    NIEUW: doorzoekt de rest van DEZELFDE brief op een andere zin over
    kennelijk hetzelfde onderwerp met een ander bedrag. Dit vangt het geval
    waarin een brief zelf op twee plekken een verschillend cijfer noemt —
    de meest rechtstreekse vorm van interne inconsistentie, en iets waar de
    bestaande lagen (die allemaal buiten de brief zelf kijken) niets over
    zeggen.
    """
    volledige_tekst = context.get("volledige_tekst") or ""
    if not volledige_tekst:
        return None
    bedrag = _parse_bedrag(claim_tekst)
    if bedrag is None:
        return None
    for andere_zin in re.split(r'(?<=[.!?])\s+|\n', volledige_tekst):
        andere_zin = andere_zin.strip()
        if not andere_zin or len(andere_zin) < 25 or andere_zin == claim_tekst.strip():
            continue
        if not _woord_overlap(claim_tekst, andere_zin, minimum=3):
            continue
        ander_bedrag = _parse_bedrag(andere_zin)
        if ander_bedrag is None or ander_bedrag == 0 or ander_bedrag == bedrag:
            continue
        afwijking = abs(bedrag - ander_bedrag) / max(ander_bedrag, bedrag)
        if afwijking >= 0.10:
            return (f"Afwijkend: dezelfde brief noemt elders een ander bedrag over "
                    f"kennelijk hetzelfde onderwerp: \"{andere_zin[:90]}\"")
    return None


def _ebs_gemiddeld_pct_rond_datum(ebs_data, datum_str, dagen=30):
    """
    Gemiddeld dagelijks EBS-uitvalpercentage in de 'dagen' dagen tot en met
    datum_str, berekend uit de eigen (al gescrapete) EBS-geschiedenis.
    """
    try:
        d_eind = datetime.strptime(datum_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    d_start = d_eind - timedelta(days=dagen)
    totaal = cancelled = 0
    for d, v in (ebs_data or {}).items():
        try:
            dd = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        if d_start <= dd <= d_eind:
            totaal += v.get("totaal", 0) or 0
            cancelled += v.get("cancelled", 0) or 0
    if totaal == 0:
        return None
    return round(cancelled / totaal * 100, 1)


def _ebs_kruischeck(claim_tekst, context):
    """
    NIEUW: als een claim over EBS/busvervoer gaat én een percentage bevat,
    vergelijk dat met het daadwerkelijk gemeten uitvalpercentage uit de
    EBS-tab van dit dashboard, in de 30 dagen voorafgaand aan de brief. Dit
    is de enige laag die een claim toetst aan data die het dashboard zelf
    continu en onafhankelijk van het college verzamelt.
    """
    tekst_lower = claim_tekst.lower()
    if not any(w in tekst_lower for w in ("ebs", "buslijn", "busvervoer", "streekvervoer")):
        return None
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:%|procent)', claim_tekst)
    if not m:
        return None
    try:
        genoemd_pct = float(m.group(1).replace(',', '.'))
    except ValueError:
        return None
    werkelijk_pct = _ebs_gemiddeld_pct_rond_datum(
        context.get("ebs_percentage_historie"), context.get("datum") or ""
    )
    if werkelijk_pct is None:
        return None
    afwijking = abs(genoemd_pct - werkelijk_pct)
    if afwijking <= 1.0:
        return f"Bevestigd: komt overeen met het gemeten EBS-uitvalpercentage rond deze periode ({werkelijk_pct}%)"
    if afwijking >= 3.0:
        return (f"Afwijkend: het gemeten EBS-uitvalpercentage in de 30 dagen rond deze brief "
                f"was {werkelijk_pct}%, niet {genoemd_pct:g}% — controleer om welke periode/lijn het gaat")
    return None


def _sluiting_kruischeck(zoekbasis, context):
    """
    NIEUW: koppelt een claim aan een bekende woningsluiting of camera-inzet
    op basis van adres/naam-overlap. Blijft, net als de stemmingen/moties-
    laag, bewust bij "gerelateerd gevonden" — de vrije tekst rond een
    sluitingsduur is te wisselend van formulering om daar veilig een
    bevestigd/afwijkend-oordeel op te baseren.
    """
    for w in context.get("woningsluitingen", []):
        titel_w = w.get("titel", "")
        if not titel_w or not _woord_overlap(zoekbasis, titel_w):
            continue
        duur = w.get("duur_maanden")
        eind = w.get("eind_datum")
        if duur:
            return f"Gerelateerde woningsluiting gevonden: '{titel_w[:60]}' — geregistreerde duur: {duur} maanden"
        if eind:
            return f"Gerelateerde woningsluiting gevonden: '{titel_w[:60]}' — einddatum: {eind}"

    for c in context.get("cameras", []):
        naam = c.get("camera", "")
        if not naam or not _woord_overlap(zoekbasis, naam):
            continue
        eind = c.get("eind")
        if eind:
            return f"Gerelateerd cameratoezicht gevonden: '{naam}' — beëindigd op {eind}"
        return f"Gerelateerd cameratoezicht gevonden: '{naam}' — nog actief volgens de eigen data"

    return None


def kruischeck_claim(claim_tekst, context):
    """
    Probeert een claim te bevestigen of tegen te spreken met de dashboard-eigen
    data. Geeft een korte Nederlandse toelichting terug, of None als er geen
    relevante match is gevonden (dan blijft het aan een lezer om het na te
    trekken — we verzinnen geen kruischeck die er niet is).
    """
    brief_titel  = context.get("titel", "") or ""
    brief_datum  = context.get("datum") or ""
    brief_ph     = context.get("portefeuillehouder", "") or ""
    zoekbasis    = f"{brief_titel} {claim_tekst}"
    bedrag       = _parse_bedrag(claim_tekst)

    # 1) NIEUW — tegenstrijdigheid binnen dezelfde brief. Gaat als eerste,
    # want een brief die zichzelf tegenspreekt is de sterkste, meest
    # rechtstreekse bevinding die deze functie kan doen.
    intern = _interne_brief_tegenstrijdigheid(claim_tekst, context)
    if intern:
        return intern

    # 2) NIEUW — EBS-kruischeck tegen de eigen, onafhankelijk gemeten data.
    ebs_resultaat = _ebs_kruischeck(claim_tekst, context)
    if ebs_resultaat:
        return ebs_resultaat

    # 3) Bedrag vergelijken met aanbestedingen (geraamde/gegunde waarde)
    if bedrag is not None:
        for proc in context.get("aanbestedingen", []):
            titel_a = proc.get("titel", "")
            if not _woord_overlap(zoekbasis, titel_a):
                continue
            for pub in proc.get("publicaties", []):
                for veld in ("gegunde_waarde", "geraamde_waarde"):
                    waarde = pub.get(veld)
                    if waarde is None:
                        continue
                    try:
                        waarde = float(waarde)
                    except (TypeError, ValueError):
                        continue
                    if waarde == 0:
                        continue
                    afwijking = abs(bedrag - waarde) / waarde
                    if afwijking <= 0.10:
                        return (f"Bevestigd: komt overeen met {veld.replace('_', ' ')} "
                                f"in aanbesteding '{titel_a[:60]}'")
                    if afwijking >= 0.30:
                        waarde_nl = f"{waarde:,.0f}".replace(",", ".")
                        return (f"Afwijkend: aanbesteding '{titel_a[:60]}' noemt "
                                f"€{waarde_nl} ({veld.replace('_', ' ')}) i.p.v. "
                                f"het hier genoemde bedrag — controleer welk bedrag klopt")

    # 4) Onderwerp + periode vergelijken met stemmingen (raadsbesluiten)
    for stem in context.get("stemmingen", []):
        titel_s = stem.get("titel", "")
        datum_s = stem.get("datum", "")
        if not _woord_overlap(zoekbasis, titel_s):
            continue
        verschil = _dagen_verschil(brief_datum, datum_s) if brief_datum and datum_s else None
        if verschil is not None and verschil > 400:
            continue
        uitslag = stem.get("uitslag_tekst") or stem.get("uitslag") or "nog geen uitslag bekend"
        return f"Gerelateerd raadsbesluit gevonden: '{titel_s[:60]}' — uitslag: {uitslag}"

    # 5) Onderwerp + periode vergelijken met moties
    for motie in context.get("moties", []):
        titel_m = motie.get("titel", "") or motie.get("onderwerp", "")
        datum_m = motie.get("datum", "")
        if not titel_m or not _woord_overlap(zoekbasis, titel_m):
            continue
        verschil = _dagen_verschil(brief_datum, datum_m) if brief_datum and datum_m else None
        if verschil is not None and verschil > 400:
            continue
        uitslag = motie.get("uitslag") or motie.get("status") or "status onbekend"
        return f"Gerelateerde motie gevonden: '{titel_m[:60]}' — {uitslag}"

    # 6) NIEUW — gerelateerde woningsluiting of camera-inzet
    sluiting_resultaat = _sluiting_kruischeck(zoekbasis, context)
    if sluiting_resultaat:
        return sluiting_resultaat

    # 7) Tegenstrijdigheid met een eerdere brief van dezelfde portefeuillehouder
    if brief_ph:
        for eerdere in context.get("eerdere_brieven", {}).values():
            if eerdere.get("portefeuillehouder") != brief_ph:
                continue
            if eerdere.get("titel") == brief_titel:
                continue  # zelfde brief (kan bij nabewerking voorkomen)
            for c in (eerdere.get("claims") or []):
                oude_claim = c.get("claim", "")
                if not _woord_overlap(claim_tekst, oude_claim, minimum=3):
                    continue
                oud_bedrag = _parse_bedrag(oude_claim)
                if bedrag is not None and oud_bedrag is not None and oud_bedrag != 0:
                    afwijking = abs(bedrag - oud_bedrag) / oud_bedrag
                    if afwijking >= 0.10:
                        return (f"Mogelijk tegenstrijdig met eerdere brief "
                                f"'{eerdere.get('titel', '')[:60]}' ({eerdere.get('datum', '')}): "
                                f"daar werd een ander bedrag genoemd over hetzelfde onderwerp")

    return None


def laad_referentiedata():
    """Laadt de datasets die de kruischeck gebruikt. Ontbrekend bestand → lege lijst/dict."""
    referenties = {}
    for naam, pad in (
        ("stemmingen", "data/stemmingen.json"),
        ("moties", "data/moties.json"),
        ("aanbestedingen", "data/aanbestedingen.json"),
        ("woningsluitingen", "data/woningsluitingen.json"),
    ):
        try:
            with open(pad, encoding="utf-8") as f:
                referenties[naam] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            referenties[naam] = []

    # Camera's: actief + geschiedenis samengevoegd, want voor de kruischeck
    # maakt het niet uit of een camera nog aanstaat of al is uitgeschakeld.
    cameras = []
    for pad in ("data/cameras_actief.json", "data/cameras_geschiedenis.json"):
        try:
            with open(pad, encoding="utf-8") as f:
                cameras.extend(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    referenties["cameras"] = cameras

    # EBS-percentagehistorie: een dict (datum -> cijfers), geen lijst.
    try:
        with open("data/ebs_percentage_historie.json", encoding="utf-8") as f:
            referenties["ebs_percentage_historie"] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        referenties["ebs_percentage_historie"] = {}

    return referenties


# ── AI-GEBASEERDE CLAIMANALYSE ────────────────────────────────────────────────
def analyseer_ai_claims(tekst, titel, portefeuillehouder, api_key):
    if not api_key or not tekst:
        return []

    tekst_kort = tekst[:8000]

    prompt = f"""Je bent een factcheck-assistent voor een journalist die collegebrieven van de gemeente Zaanstad analyseert.

Document: "{titel}"
Portefeuillehouder: {portefeuillehouder or "onbekend"}

Analyseer de onderstaande tekst en identificeer alle feitelijke claims die verifieerbaar zijn.
Denk aan: getallen, percentages, datums, tijdlijnen, beloftes van het college, budgetten, aantallen woningen of inwoners, vergelijkingen met eerdere jaren, statusupdates op moties of eerdere beloftes.

Geef voor elke claim:
- De exacte claim (kort en precies, max 200 tekens)
- Hoe een journalist dit kan controleren (welke bron, welk document)
- Prioriteit: HOOG / MIDDEL / LAAG
- Score: 0-100 (hoe checkwaardig)

Maximaal 8 claims, HOOG eerst.

Antwoord ALLEEN met een JSON-array, geen markdown, geen uitleg:
[{{"claim":"...","verificatie":"...","prioriteit":"HOOG","score":85}}]

Tekst:
{tekst_kort}"""

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}
    }).encode("utf-8")

    url = f"{GEMINI_URL}?key={api_key}"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urlopen_met_retry(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw   = data["candidates"][0]["content"]["parts"][0]["text"]
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            return []
        ai_claims = json.loads(match.group(0))
        for c in ai_claims:
            c["bron"]       = "ai"
            c["kruischeck"] = c.get("kruischeck") or None
        return ai_claims
    except Exception as e:
        print(f"  (Gemini mislukt na retries: {e})")
        return []


# ── CLAIMS SAMENVOEGEN ────────────────────────────────────────────────────────
def combineer_claims(code_claims, ai_claims):
    """
    Voegt code- en AI-claims samen.
    Verwijdert code-claims die al door AI zijn gevonden
    (op basis van overlap in de eerste 40 tekens).
    AI-claims gaan voor omdat ze rijker zijn.
    """
    if not ai_claims:
        return code_claims

    ai_teksten = {c["claim"][:40].lower() for c in ai_claims}

    unieke_code = [
        c for c in code_claims
        if c["claim"][:40].lower() not in ai_teksten
    ]

    gecombineerd = ai_claims + unieke_code

    # Sorteer: HOOG eerst, dan score
    volgorde = {"HOOG": 0, "MIDDEL": 1, "LAAG": 2}
    gecombineerd.sort(key=lambda c: (
        volgorde.get(c.get("prioriteit", "LAAG"), 9),
        -(c.get("score") or 0)
    ))

    return gecombineerd[:12]  # max 12 gecombineerde claims


# ── HELPERS ───────────────────────────────────────────────────────────────────
def parse_datum(s):
    if not s:
        return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def build_lijst_body(start, draw):
    params = [("draw", str(draw))]
    for i, (name, has_pipe) in enumerate(COLUMNS):
        params += [
            (f"columns[{i}][data]",          name),
            (f"columns[{i}][name]",          name),
            (f"columns[{i}][searchable]",    "true"),
            (f"columns[{i}][orderable]",     "true"),
            (f"columns[{i}][search][value]", "|" if has_pipe else ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    params += [
        ("order[0][column]", "5"),
        ("order[0][dir]",    "desc"),
        ("order[0][name]",   "registrationdate"),
        ("start",            str(start)),
        ("length",           str(PAGE_SIZE)),
        ("search[value]",    ""),
        ("search[regex]",    "false"),
    ]
    return urllib.parse.urlencode(params).encode("utf-8")


# ── PDF OPHALEN & TEKST EXTRAHEREN ────────────────────────────────────────────
def haal_document_id(opener, item_id):
    url = f"{BASE_URL}/Reports/Item/{item_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with open_met_retry(opener, req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"(detailpagina mislukt na retries: {e})")
        return None

    m = re.search(
        r"/Reports/Document/" + re.escape(item_id) +
        r"\?documentId=([a-f0-9\-]{36})", html
    )
    if m:
        return m.group(1)
    m = re.search(r"documentId=([a-f0-9\-]{36})", html)
    return m.group(1) if m else None


def download_pdf(opener, item_id, document_id):
    url = f"{BASE_URL}/Document/View/{document_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":              "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language":         "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer":                 f"{BASE_URL}/Reports/Item//{item_id}",
            "sec-ch-ua":               '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile":        "?0",
            "sec-ch-ua-platform":      '"macOS"',
            "sec-fetch-dest":          "document",
            "sec-fetch-mode":          "navigate",
            "sec-fetch-site":          "same-origin",
            "sec-fetch-user":          "?1",
            "upgrade-insecure-requests": "1",
        }
    )
    try:
        with open_met_retry(opener, req, timeout=30) as resp:
            data = resp.read()
            if data[:4] != b'%PDF':
                print(f"(geen PDF ontvangen, eerste bytes: {data[:20]})")
                return None
            return data
    except Exception as e:
        print(f"(PDF download mislukt na retries: {e})")
        return None


def extraheer_pdf_tekst(pdf_bytes):
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        delen = []
        for pagina in reader.pages:
            tekst = pagina.extract_text()
            if tekst:
                delen.append(tekst)
        return "\n".join(delen).strip()
    except ImportError:
        print("  ⚠ pypdf niet geïnstalleerd — pip install pypdf --break-system-packages")
        return None
    except Exception as e:
        print(f"  (PDF-tekst extractie mislukt: {e})")
        return None


# ── BESTAANDE DATA ────────────────────────────────────────────────────────────
def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {b["id"]: b for b in data}


# ── HOOFDPROGRAMMA ────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        print("✓ Gemini API key gevonden — AI-laag actief")
    else:
        print("⚠  Geen GEMINI_API_KEY — alleen code-gebaseerde claimdetectie")

    # Datumbereik: minimaal 30 dagen terug, ÁLTIJD — ongeacht wat SCRAPE_VANAF
    # (gevoed door de scrape-tracker) doorgeeft. Zelfde reden als bij
    # scrape_moties.py en scrape_stemmingen.py. De bestaande skip-check
    # verderop (`"claims" in bestaand[item_id]`) zorgt dat dit vrijwel niets
    # extra kost voor brieven die al verwerkt zijn.
    vandaag            = datetime.now()
    vanaf_env          = os.environ.get("SCRAPE_VANAF", "").strip()
    dertig_dagen_terug = (vandaag - timedelta(days=30)).strftime("%Y-%m-%d")
    grens              = min(vanaf_env, dertig_dagen_terug) if vanaf_env else dertig_dagen_terug
    print(f"Collegeberichten vanaf: {grens}")

    # Sessie
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    print("Sessie ophalen...", end=" ", flush=True)
    try:
        open_met_retry(
            opener,
            urllib.request.Request(LIJST_PAGE_URL, headers={"User-Agent": HEADERS["User-Agent"]}),
            timeout=15,
        )
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e}) — doorgaan zonder sessie")

    # Eerste pagina
    lijst_headers = {**HEADERS, "Referer": LIJST_PAGE_URL}
    print("Lijst ophalen...", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            LIJST_DATA_URL, data=build_lijst_body(0, 1), headers=lijst_headers
        )
        with open_met_retry(opener, req, timeout=30) as resp:
            first = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"\nFout bij ophalen lijst: {e}")
        sys.exit(1)

    total    = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    print(f"OK — {total} items totaal")

    # Resterende pagina's — met early-stop: de lijst komt aflopend gesorteerd
    # binnen op registrationdate (zie order[0]), dus zodra een hele pagina
    # ouder is dan grens hoeven we niet verder te pagineren. Dit scheelt hier
    # het meest: bij 4690 items werden voorheen alle ~47 pagina's opgehaald
    # ook als alleen de eerste paar pagina's relevant waren.
    draw, start = 2, PAGE_SIZE
    while start < total:
        req = urllib.request.Request(
            LIJST_DATA_URL, data=build_lijst_body(start, draw), headers=lijst_headers
        )
        try:
            with open_met_retry(opener, req, timeout=30) as resp:
                page = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"\nFout bij ophalen pagina (start={start}): {e} — stoppen met pagineren")
            break

        rows = page.get("data", [])
        all_rows.extend(rows)

        if rows and all(
            (parse_datum(r.get("registrationdate")) or "9999-99-99") < grens
            for r in rows
        ):
            print(f"  (pagina bij start={start} volledig ouder dan {grens} — paginering gestopt)")
            break

        draw += 1; start += PAGE_SIZE
        time.sleep(0.3)

    # Filteren
    relevante_rows = [
        r for r in all_rows
        if r.get("typeselectie", "") in RELEVANTE_TYPEN
        and (parse_datum(r.get("datumbericht")) or "") >= grens
    ]
    print(f"{len(relevante_rows)} relevante brieven vanaf {grens}")

    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} brieven")

    # Referentiedata voor de niet-AI kruischeck (één keer laden, niet per brief)
    referenties = laad_referentiedata()
    print(
        f"Kruischeck-data geladen: {len(referenties['stemmingen'])} stemmingen, "
        f"{len(referenties['moties'])} moties, "
        f"{len(referenties['aanbestedingen'])} aanbestedingen, "
        f"{len(referenties['woningsluitingen'])} woningsluitingen, "
        f"{len(referenties['cameras'])} camera's, "
        f"{len(referenties['ebs_percentage_historie'])} dagen EBS-historie"
    )

    # Per brief verwerken
    print("Brieven verwerken...")
    verwerkt = 0
    code_totaal = 0
    ai_totaal   = 0

    for i, row in enumerate(relevante_rows):
        item_id = row.get("DT_RowId")
        titel   = row.get("title", "").strip()
        datum   = parse_datum(row.get("datumbericht"))
        type_   = row.get("typeselectie", "")
        ph_raw  = row.get("portefeuillehouderselectie", "") or ""
        # FIX: was ", ".join(...) — maar Nederlandse bestuurlijke naam-
        # notatie ("Laan, van der H.") bevat zelf al een komma. Bij meerdere
        # portefeuillehouders op één brief was dan niet meer te onderscheiden
        # welke komma een naam scheidt en welke twee personen scheidt — één
        # persoon werd daardoor in het dashboard als twee mensen geteld.
        # " | " kan niet in een naam voorkomen, dus ondubbelzinnig.
        ph      = " | ".join([p.strip() for p in ph_raw.split("\r\n") if p.strip()])

        print(f"  [{i+1}/{len(relevante_rows)}] {datum} — {titel[:55]}", end=" ", flush=True)

        # Overslaan als al verwerkt.
        # FIX: was `bestaand[item_id].get("claims")` — een lege lijst ([])
        # is falsy in Python, dus brieven zonder claims werden ELKE run
        # opnieuw volledig herverwerkt (PDF opnieuw downloaden, tekst
        # opnieuw extraheren, en bij een Gemini-key elke run opnieuw een
        # AI-call voor dezelfde brief). Nu checken we op AANWEZIGHEID van
        # de key, niet op de waarheid van de inhoud — "verwerkt met 0
        # claims" telt nu ook terecht als klaar.
        if item_id in bestaand and "claims" in bestaand[item_id]:
            print("→ al verwerkt, overgeslagen")
            continue

        # DocumentId ophalen
        doc_id = haal_document_id(opener, item_id)
        if not doc_id:
            print("→ geen documentId gevonden")
            bestaand[item_id] = {
                "id": item_id, "titel": titel, "type": type_,
                "datum": datum, "portefeuillehouder": ph,
                "url": f"{BASE_URL}/Reports/Item/{item_id}",
                "tekst": None, "claims": [],
                "bijgewerkt": vandaag.strftime("%Y-%m-%d"),
            }
            time.sleep(0.4)
            continue

        # PDF downloaden
        time.sleep(0.3)
        pdf_bytes = download_pdf(opener, item_id, doc_id)
        if not pdf_bytes:
            print("→ PDF niet beschikbaar")
            bestaand[item_id] = {
                "id": item_id, "titel": titel, "type": type_,
                "datum": datum, "portefeuillehouder": ph,
                "url": f"{BASE_URL}/Reports/Item/{item_id}",
                "pdf_url": f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}",
                "tekst": None, "claims": [],
                "bijgewerkt": vandaag.strftime("%Y-%m-%d"),
            }
            time.sleep(0.4)
            continue

        # Tekst extraheren
        tekst = extraheer_pdf_tekst(pdf_bytes)

        # Laag 1: code-gebaseerde claims (altijd), incl. niet-AI kruischeck
        # tegen stemmingen/moties/aanbestedingen/eerdere brieven
        claim_context = {
            "titel": titel, "datum": datum, "portefeuillehouder": ph,
            "stemmingen": referenties["stemmingen"],
            "moties": referenties["moties"],
            "aanbestedingen": referenties["aanbestedingen"],
            "woningsluitingen": referenties["woningsluitingen"],
            "cameras": referenties["cameras"],
            "ebs_percentage_historie": referenties["ebs_percentage_historie"],
            "eerdere_brieven": bestaand,
            "volledige_tekst": tekst,
        }
        code_claims = detecteer_code_claims(tekst, claim_context) if tekst else []

        # Laag 2: AI-claims (optioneel)
        ai_claims = []
        if tekst and api_key:
            ai_claims = analyseer_ai_claims(tekst, titel, ph, api_key)
            time.sleep(0.3)

        # Combineren
        alle_claims = combineer_claims(code_claims, ai_claims)

        code_totaal += len(code_claims)
        ai_totaal   += len(ai_claims)

        tekst_info = f"{len(tekst)} tekens" if tekst else "geen tekst"
        print(
            f"→ {tekst_info} · "
            f"{len(code_claims)} code-claims · "
            f"{len(ai_claims)} AI-claims · "
            f"{len(alle_claims)} totaal"
        )

        bestaand[item_id] = {
            "id":                item_id,
            "titel":             titel,
            "type":              type_,
            "datum":             datum,
            "portefeuillehouder": ph,
            "url":               f"{BASE_URL}/Reports/Item/{item_id}",
            "pdf_url":           f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}",
            "tekst":             tekst[:5000] if tekst else None,
            "claims":            alle_claims,
            "bijgewerkt":        vandaag.strftime("%Y-%m-%d"),
        }
        verwerkt += 1
        time.sleep(0.5)

    # Opslaan
    resultaat = sorted(
        bestaand.values(),
        key=lambda x: x.get("datum") or "",
        reverse=True,
    )
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    totaal_claims = sum(len(b.get("claims") or []) for b in resultaat)
    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {verwerkt} brieven nieuw verwerkt")
    print(f"  {len(resultaat)} totaal in JSON")
    print(f"  {code_totaal} code-claims gedetecteerd")
    print(f"  {ai_totaal} AI-claims gedetecteerd")
    print(f"  {totaal_claims} claims totaal in JSON")


if __name__ == "__main__":
    main()
