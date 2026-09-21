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
  2. AI   — optioneel, Gemini voor diepere claimanalyse. Het model komt uit
             een fallback-keten (zie GEMINI_MODEL_VOORKEUR): valt een model
             weg, dan pakt het script vanzelf het volgende. De volledige
             brief wordt in brokken geanalyseerd, elke AI-claim moet
             aantoonbaar in de brontekst staan, en krijgt dezelfde
             deterministische kruischeck als een code-claim.

Claims krijgen een "bron"-veld: "code" of "ai".
Per brief wordt "ai_gedaan" bijgehouden: True (AI-laag afgerond), False (nog
te doen — wordt bij een volgende run met een werkende key opnieuw geprobeerd)
of None (niet van toepassing, bv. een PDF zonder tekstlaag).
Resultaat wordt opgeslagen in data/collegeberichten.json

Gebruik:
    python3 scrape_collegeberichten.py

Vereiste omgevingsvariabelen:
    GEMINI_API_KEY  — Gemini API key (optioneel, alleen voor AI-laag)

Optionele omgevingsvariabelen:
    SCRAPE_VANAF        — datum YYYY-MM-DD (er wordt altijd minimaal 30 dagen
                          teruggekeken)
    GEMINI_MODEL        — één model dat vóór de standaardketen wordt geprobeerd
    GEMINI_MODELS       — komma-gescheiden lijst die de standaardketen vervangt
    AI_HERVERWERK_MAX   — max. aantal reeds verwerkte brieven per run waarvan
                          de ontbrekende AI-laag wordt ingehaald (standaard 20)
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

# ── GEMINI-CONFIGURATIE ───────────────────────────────────────────────────────
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Voorkeursvolgorde, nieuwste eerst. Het script vraagt bij de start aan Google
# welke van deze modellen jouw key daadwerkelijk kan gebruiken (ListModels) en
# slaat de rest over. Valt er tijdens de run een model weg (404) of is de
# limiet bereikt (429), dan gaat het naar het volgende. Staat er geen enkel
# voorkeursmodel meer in de lijst van Google, dan kiest het script zelf de
# nieuwste beschikbare "flash"-variant.
# Overschrijven kan met GEMINI_MODELS="a,b,c" of GEMINI_MODEL="a".
GEMINI_MODEL_VOORKEUR = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",   # draait ook in scrape_nos.py
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
]

AI_CHUNK_TEKENS   = 7000   # tekens per Gemini-call; de hele brief wordt in brokken aangeboden
AI_MAX_CHUNKS     = 6      # max. brokken per brief (6 x 7000 = ~42.000 tekens)
AI_MAX_TOKENS     = 8192   # ruim genoeg, ook voor modellen die "denktokens" meetellen
MAX_CODE_CLAIMS   = 10     # max. code-claims per brief (ná sortering op prioriteit)
MAX_CLAIMS_TOTAAL = 12     # max. claims per brief na combineren
MAX_POGINGEN      = 4      # zo vaak wordt een ontbrekende AI-laag opnieuw geprobeerd

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
_MAANDEN_RE = "januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december"
_VERLEDEN_JAREN = "|".join(str(y) for y in range(1990, datetime.now().year))

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
    # De negative lookaheads sluiten maandnamen en tijdseenheden uit ("3 juni",
    # "6 weken" — die hebben eigen patronen) én jaartallen: voorheen werd
    # "Het college heeft in 2026 besloten" gelezen als een aantal ("2026
    # besloten"). De woordgrens (\b) in de uitsluiting voorkomt dat "12 meisjes"
    # wegvalt vanwege "mei".
    (
        r'(?=.*\b(?:wij|we|het college)\s+(?:hebben|heeft)\b)'
        r'(?=.*\b(?!(?:19|20)\d\d\b)\d+(?:\.\d+)?\s+'
        r'(?!(?:januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|jaar|jaren|maand|maanden|week|weken|dag|dagen|uur|uren|'
        r'procent)\b)[a-zà-ÿ]{4,}\b)',
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
    # Datumdeadlines en tijdskaders — FIX: het losse voorzetsel "in" is uit het
    # harde patroon gehaald. Elk "in 2026" in een brief uit 2026 telde voorheen
    # als deadline. "in 2027" telt nu alleen nog als de zin ook vooruitkijkt
    # (tweede patroon, met een werkwoord van plannen/verwachten/opleveren).
    (
        r'\b(?:voor|eind|uiterlijk|per|begin|medio|tegen)\s+'
        r'(?:\d{1,2}\s+)?(?:(?:januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december)\s+)?'
        r'(?:20\d\d|dit jaar|volgend jaar|Q[1-4]\s*20\d\d)\b',
        "MIDDEL",
        "Controleer deadline via eerder raadsstuk, motie of collegebrief"
    ),
    (
        r'(?=.*\bin\s+20\d\d\b)'
        r'(?=.*\b(?:wordt|worden|zal|zullen|gaat|gaan|verwacht|verwachten|streven|streeft|'
        r'opgeleverd|gerealiseerd|afgerond|gereed|start|starten|planning|gepland)\b)',
        "MIDDEL",
        "Controleer deadline via eerder raadsstuk, motie of collegebrief"
    ),
    # Verleden jaartal + concreet aantal in dezelfde zin ("in 2022 … 42 vrijwilligers").
    # Het getal mag geen jaartal zijn, geen dag in een datum ("8 september") en
    # geen onderdeel van 08-09-2025 of 1.500.
    (
        r'(?=.*\b(?:in|sinds|na|uit)\s+(?:' + _VERLEDEN_JAREN + r')\b)'
        r'(?=.*(?<![\w.,/-])(?!(?:19|20)\d\d\b)\d+(?:[.,]\d+)?(?![\d/-]|[.,]\d)'
        r'(?!\s*(?:' + _MAANDEN_RE + r')\b))',
        "MIDDEL",
        "Controleer het genoemde aantal en jaartal via jaarverslag, CBS of de bron zelf"
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
    # Wetsartikelen en beleidsreferenties — FIX: alle patronen worden met
    # re.IGNORECASE gezocht, waardoor [A-Z] ook kleine letters matchte en
    # "de wet is", "besluit tot" en "verordening en" allemaal als
    # wetsverwijzing telden. (?-i:...) zet hoofdlettergevoeligheid lokaal weer
    # aan: alleen echte eigennamen (Gemeentewet, Wet maatschappelijke
    # ondersteuning, Algemene Plaatselijke Verordening) matchen nog.
    (
        r'(?:\bartikel\s+\d+[a-z]?\b'
        r'|(?-i:\b[A-Z][a-z]+wet\b)'
        r'|(?-i:\bWet\s+[A-Za-z]{3,})'
        r'|(?-i:\bBesluit\s+(?:omgevingsrecht|bouwwerken leefomgeving|begroting en verantwoording)\b)'
        r'|(?-i:\b(?:APV|Algemene Plaatselijke Verordening|[A-Z][a-z]+verordening)\b))',
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
        r'\b[Ww]ethouder\s+'
        r'(?-i:[A-Z][a-zà-ÿ]+(?:\s+(?:van|de|der|den|ter|te|het)){0,2}(?:\s+[A-Z][a-zà-ÿ]+)?)\s+'
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
    # Historische bewering met een verleden jaartal — bewust als laatste patroon.
    (
        r'\b(?:in|sinds|na|uit)\s+(?:' + _VERLEDEN_JAREN + r')\b',
        "LAAG",
        "Historische bewering met jaartal — controleer feit en jaartal via raadsarchief, jaarverslag of uitspraak"
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
    Geeft maximaal MAX_CODE_CLAIMS claims terug met bron='code'.

    FIX: eerst worden ALLE kandidaat-zinnen verzameld, dan pas gesorteerd
    (harde interne fouten → HOOG → MIDDEL → LAAG, binnen een niveau in
    documentvolgorde) en pas daarna afgekapt. Voorheen brak de lus na de
    eerste 10 treffers in documentvolgorde af en werd er pas daarná
    gesorteerd — een HOOG-claim laat in de brief verdween dan achter tien
    vage LAAG-zinnen aan het begin.

    'context' (optioneel) schakelt de niet-AI kruischeck in — zie
    kruischeck_claim(). De kruischeck wordt alleen berekend voor de claims
    die de cut halen (goedkoper, en het is O(zinnen) per claim).
    """
    if not tekst:
        return []

    zinnen = re.split(r'(?<=[.!?])\s+|\n', tekst)
    volgorde = {"HOOG": 0, "MIDDEL": 1, "LAAG": 2}
    kandidaten = []      # (sorteersleutel, claim-dict, volledige zin)
    gezien = set()       # dedupliceer op de eerste 50 tekens

    for volgnr, zin in enumerate(zinnen):
        zin = zin.strip()

        # Te kort of te lang om zinvol te zijn
        if len(zin) < 25 or len(zin) > 500:
            continue

        sleutel = zin[:50].lower()
        if sleutel in gezien:
            continue

        # Laag 0: interne rekenkundige tegenstrijdigheid — een hard feitelijk
        # probleem in de zin zelf, geen "vraag dit na"-signaal. Gaat altijd
        # vooraan, werkt zonder context/externe data.
        tegenstrijdigheid = check_richting_tegenstrijdigheid(zin)
        if tegenstrijdigheid:
            gezien.add(sleutel)
            kandidaten.append(((0, 0, volgnr), {
                "claim": zin[:250],
                "verificatie": "Controleer of dit een tikfout of onjuiste formulering in de brief is",
                "prioriteit": "HOOG",
                "score": PRIO_SCORE["HOOG"],
                "bron": "code",
                "kruischeck": tegenstrijdigheid,
            }, zin))
            continue

        for patroon, prioriteit, verificatie in CODE_PATRONEN:
            if re.search(patroon, zin, re.IGNORECASE):
                gezien.add(sleutel)
                kandidaten.append(((1, volgorde[prioriteit], volgnr), {
                    "claim": zin[:250],
                    "verificatie": verificatie,
                    "prioriteit": prioriteit,
                    "score": PRIO_SCORE[prioriteit],
                    "bron": "code",
                    "kruischeck": None,
                }, zin))
                break  # één match per zin is genoeg

    kandidaten.sort(key=lambda k: k[0])

    claims = []
    for _sleutel, claim, zin in kandidaten[:MAX_CODE_CLAIMS]:
        if claim["kruischeck"] is None and context:
            claim["kruischeck"] = kruischeck_claim(zin, context)
        claims.append(claim)
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


_RE_BEDRAG_TEKEN = re.compile(
    r'€\s*(\d(?:[\d.,]*\d)?)(?:\s*(miljoen|miljard|mln|mld|duizend|k)\b)?',
    re.IGNORECASE,
)
_RE_BEDRAG_WOORD = re.compile(
    r'\b(\d(?:[\d.,]*\d)?)\s*(miljoen\s+euro|miljard\s+euro|euro)\b',
    re.IGNORECASE,
)
_BEDRAG_FACTOR = {
    "miljoen": 1_000_000, "mln": 1_000_000,
    "miljard": 1_000_000_000, "mld": 1_000_000_000,
    "duizend": 1_000, "k": 1_000,
}


def _bedrag_waarde(ruw, eenheid):
    """
    Zet ('2,5', 'miljoen') om naar euro's. NL-notatie: punt = duizendtal-
    scheiding, komma = decimaal. Uitzondering: staat er een eenheid als
    "miljoen" bij en heeft het getal precies één punt met 1-2 cijfers erna
    ("€ 2.5 miljoen"), dan is het punt een decimaalteken — anders werd dat
    25 miljoen in plaats van 2,5 miljoen.
    """
    eenheid = (eenheid or "").lower().replace(" euro", "").strip()
    if eenheid == "euro":
        eenheid = ""
    if eenheid in _BEDRAG_FACTOR and re.fullmatch(r'\d{1,3}\.\d{1,2}', ruw):
        getal_str = ruw
    else:
        getal_str = ruw.replace(".", "").replace(",", ".")
    try:
        getal = float(getal_str)
    except ValueError:
        return None
    return getal * _BEDRAG_FACTOR.get(eenheid, 1)


def _parse_bedragen(tekst):
    """
    Alle bedragen in een tekst als floats in euro's, in volgorde van
    voorkomen. Twee vormen, precies zoals de twee €-patronen in
    CODE_PATRONEN: met €-teken, of met het woord "euro" voluit.

    FIX: de eenheid "k" eist nu een woordgrens — "€ 10 keer" werd voorheen
    gelezen als 10.000. En er worden nu ALLE bedragen teruggegeven, zodat
    de kruischeck weet wanneer een zin er meerdere noemt (en dan geen
    oordeel velt op basis van alleen het eerste).
    """
    tekst = tekst or ""
    gevonden, spans = [], []
    for m in _RE_BEDRAG_TEKEN.finditer(tekst):
        w = _bedrag_waarde(m.group(1), m.group(2))
        if w is not None:
            gevonden.append((m.start(), w))
            spans.append(m.span())
    for m in _RE_BEDRAG_WOORD.finditer(tekst):
        if any(m.start() < e and s < m.end() for s, e in spans):
            continue  # zit al in een €-match ("€ 2 miljoen euro")
        w = _bedrag_waarde(m.group(1), m.group(2))
        if w is not None:
            gevonden.append((m.start(), w))
    gevonden.sort()
    return [w for _, w in gevonden]


def _parse_bedrag(tekst):
    """
    Eerste bedrag in de tekst als float in euro's, of None. Bewaard voor
    compatibiliteit; de kruischeck gebruikt _parse_bedragen().
    """
    bedragen = _parse_bedragen(tekst)
    return bedragen[0] if bedragen else None


def _enkel_bedrag(tekst):
    """Het bedrag als de tekst er PRECIES één noemt, anders None (dan is een vergelijking dubbelzinnig)."""
    bedragen = _parse_bedragen(tekst)
    return bedragen[0] if len(bedragen) == 1 else None


def _dagen_verschil(datum_a, datum_b):
    try:
        da = datetime.strptime(datum_a[:10], "%Y-%m-%d")
        db = datetime.strptime(datum_b[:10], "%Y-%m-%d")
        return abs((da - db).days)
    except (ValueError, TypeError):
        return None


def _interne_brief_tegenstrijdigheid(claim_tekst, context):
    """
    Doorzoekt de rest van DEZELFDE brief op een andere zin over kennelijk
    hetzelfde onderwerp met een ander bedrag. Dit vangt het geval waarin een
    brief zelf op twee plekken een verschillend cijfer noemt.

    Alleen wanneer beide zinnen precies één bedrag noemen: bij enumeraties
    ("€ 2 mln voor A en € 3 mln voor B") is een vergelijking dubbelzinnig en
    zou het een vals alarm geven.
    """
    volledige_tekst = context.get("volledige_tekst") or ""
    if not volledige_tekst:
        return None

    bedrag = _enkel_bedrag(claim_tekst)
    if bedrag is None:
        return None

    for andere_zin in re.split(r'(?<=[.!?])\s+|\n', volledige_tekst):
        andere_zin = andere_zin.strip()
        if not andere_zin or len(andere_zin) < 25 or andere_zin == claim_tekst.strip():
            continue
        if not _woord_overlap(claim_tekst, andere_zin, minimum=3):
            continue
        ander_bedrag = _enkel_bedrag(andere_zin)
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


def _gedeelde_woorden(a, b):
    """Aantal betekenisvolle woorden dat twee teksten delen (zonder eigennaam-shortcut)."""
    return len(_belangrijke_woorden(a) & _belangrijke_woorden(b))


def kruischeck_claim(claim_tekst, context):
    """
    Probeert een claim te bevestigen of tegen te spreken met de dashboard-eigen
    data. Geeft een korte Nederlandse toelichting terug, of None als er geen
    relevante match is gevonden (dan blijft het aan een lezer om het na te
    trekken — we verzinnen geen kruischeck die er niet is).

    FIX — claim-niveau versus titel-niveau. Voorheen werd overal op
    "brieftitel + claim" gematcht. Daardoor koppelde één woord uit de titel
    élk bedrag in de brief aan dezelfde aanbesteding, en gaf een ongerelateerd
    bedrag (bv. een subsidie) een vals "Afwijkend:". Nu geldt:
      • een OORDEEL (Bevestigd / Afwijkend) mag alleen op basis van overlap
        met de claimzin zélf — bij aanbestedingen minstens twee gedeelde
        betekenisvolle woorden;
      • een GERELATEERD-verwijzing (stemming, motie, sluiting, camera) mag
        ook via de brieftitel, maar wordt dan als zodanig gelabeld;
      • bij een zin met meerdere bedragen wordt nooit "Afwijkend" gemeld.
    """
    brief_titel = context.get("titel", "") or ""
    brief_datum = context.get("datum") or ""
    brief_ph = context.get("portefeuillehouder", "") or ""

    bedragen = _parse_bedragen(claim_tekst)
    bedrag = bedragen[0] if len(bedragen) == 1 else None

    # 1) Tegenstrijdigheid binnen dezelfde brief — de sterkste, meest
    #    rechtstreekse bevinding die deze functie kan doen.
    intern = _interne_brief_tegenstrijdigheid(claim_tekst, context)
    if intern:
        return intern

    # 2) EBS-kruischeck tegen de eigen, onafhankelijk gemeten data.
    ebs_resultaat = _ebs_kruischeck(claim_tekst, context)
    if ebs_resultaat:
        return ebs_resultaat

    # 3) Bedrag vergelijken met aanbestedingen (geraamde/gegunde waarde).
    #    Alleen bij overlap met de claimzin zelf (>= 2 gedeelde woorden).
    if bedragen:
        for proc in context.get("aanbestedingen", []):
            titel_a = proc.get("titel", "")
            if not titel_a or _gedeelde_woorden(claim_tekst, titel_a) < 2:
                continue

            waarden = []
            for pub in proc.get("publicaties", []):
                for veld in ("gegunde_waarde", "geraamde_waarde"):
                    w = pub.get(veld)
                    if w is None:
                        continue
                    try:
                        w = float(w)
                    except (TypeError, ValueError):
                        continue
                    if w == 0:
                        continue
                    waarden.append((veld, w))
            if not waarden:
                continue

            # Bevestigd zodra één genoemd bedrag bij één van de waarden past.
            for b in bedragen:
                for veld, w in waarden:
                    if abs(b - w) / w <= 0.10:
                        return (f"Bevestigd: komt overeen met {veld.replace('_', ' ')} "
                                f"in aanbesteding '{titel_a[:60]}'")

            # Afwijkend alleen bij één ondubbelzinnig bedrag dat van ÁLLE
            # bekende waarden ver afligt.
            if bedrag is not None and all(abs(bedrag - w) / w >= 0.30 for _, w in waarden):
                veld, w = next(((v, x) for v, x in waarden if v == "gegunde_waarde"), waarden[0])
                waarde_nl = f"{w:,.0f}".replace(",", ".")
                return (f"Afwijkend: aanbesteding '{titel_a[:60]}' noemt "
                        f"€{waarde_nl} ({veld.replace('_', ' ')}) i.p.v. "
                        f"het hier genoemde bedrag — controleer welk bedrag klopt")

    # 4-6) Gerelateerde stemming / motie / woningsluiting / camera.
    #      Eerst op de claimzin zelf; pas daarna (gelabeld) via de brieftitel.
    for niveau in ("claim", "titel"):
        if niveau == "claim":
            basis, label = claim_tekst, ""
        else:
            basis = f"{brief_titel} {claim_tekst}"
            label = " (gekoppeld via de brieftitel, niet via deze claimzin)"

        for stem in context.get("stemmingen", []):
            titel_s = stem.get("titel", "")
            datum_s = stem.get("datum", "")
            if not _woord_overlap(basis, titel_s):
                continue
            verschil = _dagen_verschil(brief_datum, datum_s) if brief_datum and datum_s else None
            if verschil is not None and verschil > 400:
                continue
            uitslag = stem.get("uitslag_tekst") or stem.get("uitslag") or "nog geen uitslag bekend"
            return f"Gerelateerd raadsbesluit gevonden: '{titel_s[:60]}' — uitslag: {uitslag}{label}"

        for motie in context.get("moties", []):
            titel_m = motie.get("titel", "") or motie.get("onderwerp", "")
            datum_m = motie.get("datum", "")
            if not titel_m or not _woord_overlap(basis, titel_m):
                continue
            verschil = _dagen_verschil(brief_datum, datum_m) if brief_datum and datum_m else None
            if verschil is not None and verschil > 400:
                continue
            uitslag = motie.get("uitslag") or motie.get("status") or "status onbekend"
            return f"Gerelateerde motie gevonden: '{titel_m[:60]}' — {uitslag}{label}"

        sluiting_resultaat = _sluiting_kruischeck(basis, context)
        if sluiting_resultaat:
            return sluiting_resultaat + label

    # 7) Tegenstrijdigheid met een eerdere brief van dezelfde portefeuillehouder
    #    (vereist >= 3 gedeelde woorden met de eerdere claim, dus claim-niveau).
    if brief_ph and bedrag is not None:
        for eerdere in context.get("eerdere_brieven", {}).values():
            if eerdere.get("portefeuillehouder") != brief_ph:
                continue
            if eerdere.get("titel") == brief_titel:
                continue  # zelfde brief (kan bij nabewerking voorkomen)
            for c in (eerdere.get("claims") or []):
                oude_claim = c.get("claim", "")
                if not _woord_overlap(claim_tekst, oude_claim, minimum=3):
                    continue
                oud_bedrag = _enkel_bedrag(oude_claim)
                if oud_bedrag is not None and oud_bedrag != 0:
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


# ── GEMINI-CLIENT MET MODEL-FALLBACK ──────────────────────────────────────────
#
# Foutsoorten (GeminiFout.soort) en wat het script ermee doet:
#   sleutel   — key ongeldig/verlopen (400 API_KEY_INVALID, 401): meteen stoppen,
#               geen enkel model gaat dan werken → AI-laag uit voor de hele run.
#   model     — 404 / "model niet ondersteund": model bestaat niet (meer) →
#               volgende model in de keten, dit model niet meer proberen.
#   toegang   — 403: dit model is voor deze key niet toegestaan → idem.
#   verzoek   — overige 400: request geweigerd → volgende model.
#   limiet    — 429: quota op → korte herkansing, dan volgende model.
#   tijdelijk — 5xx / timeout / netwerk → retry met wachttijd, dan volgende model.
#   inhoud    — leeg of geblokkeerd antwoord → volgende model.

class GeminiFout(Exception):
    def __init__(self, soort, bericht):
        super().__init__(bericht)
        self.soort = soort


_GEM = {
    "keten": [],               # geordende modelnamen (gevuld door init_gemini)
    "actief": None,            # laatst succesvolle model — wordt als eerste geprobeerd
    "dood": set(),             # modellen die in deze run permanent faalden
    "mislukt_achtereen": 0,    # brieven achter elkaar waarvoor geen model werkte
    "uitgeschakeld": None,     # reden (str) als de AI-laag voor de rest van de run uit is
}


def _http_fout_tekst(e):
    """Leesbare foutmelding uit een HTTPError-body (Google stuurt JSON met error.message)."""
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        return e.reason if isinstance(getattr(e, "reason", None), str) else ""
    try:
        return str(json.loads(body)["error"]["message"])[:300]
    except Exception:
        return body[:300]


def _classificeer_http_fout(code, tekst):
    t = (tekst or "").lower()
    if code == 401 or "api key not valid" in t or "api_key_invalid" in t or "api key expired" in t:
        return "sleutel"
    if code == 404:
        return "model"
    if code == 400 and any(k in t for k in (
        "is not found", "not supported for generatecontent", "unknown model", "model not found"
    )):
        return "model"
    if code == 403:
        return "toegang"
    if code == 429:
        return "limiet"
    if code >= 500 or code == 408:
        return "tijdelijk"
    return "verzoek"


def _gemini_tekst(data):
    """Haalt de antwoordtekst uit een generateContent-response (denk-delen worden genegeerd)."""
    kandidaten = data.get("candidates") or []
    if not kandidaten:
        reden = (data.get("promptFeedback") or {}).get("blockReason") or "geen candidates"
        raise GeminiFout("inhoud", f"geen antwoord ({reden})")
    kand = kandidaten[0]
    delen = (kand.get("content") or {}).get("parts") or []
    tekst = "".join(p.get("text", "") for p in delen if not p.get("thought"))
    if not tekst.strip():
        raise GeminiFout("inhoud", f"leeg antwoord (finishReason={kand.get('finishReason')})")
    return tekst


def _gemini_call(model, prompt, api_key, json_modus=True):
    """
    Eén generateContent-call naar één model, met retries alléén voor tijdelijke
    fouten (5xx, timeouts, netwerk, 429). Permanente fouten (404, 403, 400, 401)
    worden direct doorgegeven — die worden door een retry niet beter.
    De key gaat in een header, niet in de URL, zodat hij nooit in een
    foutmelding of log terechtkomt.
    """
    generatie = {"temperature": 0.2, "maxOutputTokens": AI_MAX_TOKENS}
    if json_modus:
        generatie["responseMimeType"] = "application/json"
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generatie,
    }).encode("utf-8")
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    wachttijden = (2, 5, 10)

    for poging in range(1, 4):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return _gemini_tekst(data)
        except urllib.error.HTTPError as e:
            tekst = _http_fout_tekst(e)
            soort = _classificeer_http_fout(e.code, tekst)
            fout = GeminiFout(soort, f"HTTP {e.code}: {tekst}")
            if soort == "verzoek" and json_modus:
                # Sommige modellen weigeren responseMimeType: één keer zonder proberen.
                return _gemini_call(model, prompt, api_key, json_modus=False)
            max_pogingen = 2 if soort == "limiet" else 3
            if soort not in ("tijdelijk", "limiet") or poging >= max_pogingen:
                raise fout
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            fout = GeminiFout("tijdelijk", f"netwerk: {e}")
            if poging >= 3:
                raise fout
        except ValueError as e:      # kapotte JSON in de response
            raise GeminiFout("inhoud", f"onleesbare response: {e}")
        wacht = wachttijden[poging - 1]
        print(f"(poging {poging} mislukt: {str(fout)[:80]} — {wacht}s wachten)", end=" ", flush=True)
        time.sleep(wacht)
    raise fout


def gemini_genereer(prompt, api_key):
    """
    Probeert de modelketen af tot er één antwoordt. Geeft (tekst, modelnaam).
    Raised GeminiFout als geen enkel model werkt, of meteen bij een key-fout.
    """
    keten = _GEM["keten"]
    actief = _GEM["actief"]
    volgorde = ([actief] if actief in keten else []) + [m for m in keten if m != actief]
    volgorde = [m for m in volgorde if m not in _GEM["dood"]]
    if not volgorde:
        raise GeminiFout("model", "geen bruikbaar model meer over in de modelketen")

    laatste = None
    for model in volgorde:
        try:
            tekst = _gemini_call(model, prompt, api_key)
            _GEM["actief"] = model
            return tekst, model
        except GeminiFout as e:
            laatste = e
            print(f"({model}: {e.soort} — {str(e)[:100]})", end=" ", flush=True)
            if e.soort == "sleutel":
                raise
            if e.soort in ("model", "toegang", "verzoek"):
                _GEM["dood"].add(model)
    raise laatste


def _gewenste_modellen():
    lijst = os.environ.get("GEMINI_MODELS", "").strip()
    modellen = [m.strip() for m in lijst.split(",") if m.strip()] if lijst else list(GEMINI_MODEL_VOORKEUR)
    enkel = os.environ.get("GEMINI_MODEL", "").strip()
    if enkel:
        modellen = [enkel] + [m for m in modellen if m != enkel]
    return [m.replace("models/", "") for m in modellen]


def _lijst_beschikbare_modellen(api_key):
    """
    Vraagt Google welke modellen deze key kan gebruiken voor generateContent.
    Geeft een set met namen, of None als de lijst niet op te halen is.
    Raised GeminiFout('sleutel') als de key zelf wordt geweigerd.
    """
    modellen, token = set(), None
    for _ in range(5):
        url = f"{GEMINI_BASE}/models?pageSize=1000"
        if token:
            url += f"&pageToken={urllib.parse.quote(token)}"
        req = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            tekst = _http_fout_tekst(e)
            if _classificeer_http_fout(e.code, tekst) == "sleutel":
                raise GeminiFout("sleutel", f"HTTP {e.code}: {tekst}")
            return None
        except Exception:
            return None
        for m in data.get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                modellen.add((m.get("name") or "").split("/", 1)[-1])
        token = data.get("nextPageToken")
        if not token:
            break
    return modellen or None


def _auto_kandidaten(beschikbaar):
    """
    Noodoptie: geen enkel voorkeursmodel meer beschikbaar → kies zelf de
    nieuwste gewone "gemini-X-flash" (niet-preview, niet-lite eerst).
    Image-, tts-, live- en embedding-varianten vallen vanzelf af door de
    strikte naamvorm.
    """
    kandidaten = []
    for naam in beschikbaar:
        m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?(-preview[\w.-]*)?", naam)
        if not m:
            continue
        kandidaten.append(((bool(m.group(3)), bool(m.group(2)), -float(m.group(1))), naam))
    kandidaten.sort()
    return [naam for _, naam in kandidaten]


def init_gemini(api_key):
    """
    Bouwt de modelketen op basis van voorkeurslijst ∩ wat Google jouw key
    toont. Geeft True als de AI-laag bruikbaar lijkt, anders False (met reden
    in _GEM['uitgeschakeld']).
    """
    gewenst = _gewenste_modellen()
    try:
        beschikbaar = _lijst_beschikbare_modellen(api_key)
    except GeminiFout as e:
        _GEM["uitgeschakeld"] = f"API-key geweigerd door Google ({str(e)[:120]})"
        print(f"✗ {_GEM['uitgeschakeld']}")
        return False

    if beschikbaar is None:
        keten = list(gewenst)
        print("⚠ Modellijst van Google niet op te halen — voorkeurslijst wordt blind geprobeerd")
    else:
        keten = [m for m in gewenst if m in beschikbaar]
        weg = [m for m in gewenst if m not in beschikbaar]
        if weg:
            print(f"  (niet beschikbaar voor deze key: {', '.join(weg)})")
        for m in _auto_kandidaten(beschikbaar):
            if len(keten) >= 4:
                break
            if m not in keten:
                keten.append(m)

    if not keten:
        _GEM["uitgeschakeld"] = "geen enkel bruikbaar Gemini-model gevonden voor deze key"
        print(f"✗ {_GEM['uitgeschakeld']}")
        return False

    _GEM["keten"] = keten
    print(f"✓ AI-modelketen: {' → '.join(keten[:4])}{' → …' if len(keten) > 4 else ''}")
    return True


# ── AI-GEBASEERDE CLAIMANALYSE ────────────────────────────────────────────────

def _norm_sterk(t):
    """Kleine letters, alleen letters/cijfers, spaties samengevoegd — voor tekstvergelijking."""
    t = (t or "").lower().replace("\u00ad", "")
    t = re.sub(r"[^0-9a-zà-ÿ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _woorden_set(t):
    return set(re.findall(r"[a-zà-ÿ]{4,}", _norm_sterk(t)))


def _getallen(t):
    """Alle getallen in een tekst, zonder scheidingstekens (zodat '2,5' en '2.5' gelijk zijn)."""
    return {re.sub(r"[.,]", "", g) for g in re.findall(r"\d+(?:[.,]\d+)*", t or "")}


def _splits_in_brokken(tekst, max_tekens=AI_CHUNK_TEKENS, max_brokken=AI_MAX_CHUNKS):
    """
    Knipt de volledige brieftekst op zinsgrenzen in brokken van hooguit
    max_tekens. Geeft (brokken, afgekapt). Voorheen kreeg Gemini alleen de
    eerste 8000 tekens; van een brief van 12.000 tekens bleef een derde
    onbekeken.
    """
    zinnen = []
    for z in re.split(r'(?<=[.!?])\s+|\n', tekst or ""):
        z = z.strip()
        while len(z) > max_tekens:           # pathologisch lange "zin": hard knippen
            zinnen.append(z[:max_tekens])
            z = z[max_tekens:]
        if z:
            zinnen.append(z)

    brokken, huidig, lengte = [], [], 0
    for z in zinnen:
        if huidig and lengte + len(z) + 1 > max_tekens:
            brokken.append("\n".join(huidig))
            huidig, lengte = [], 0
        huidig.append(z)
        lengte += len(z) + 1
    if huidig:
        brokken.append("\n".join(huidig))
    return brokken[:max_brokken], len(brokken) > max_brokken


def _ai_prompt(brok, titel, portefeuillehouder, deel, van):
    deel_info = f"\nDit is deel {deel} van {van} van het document." if van > 1 else ""
    return f"""Je bent een factcheck-assistent voor een journalist die collegebrieven van de gemeente Zaanstad analyseert.

Document: "{titel}"
Portefeuillehouder: {portefeuillehouder or "onbekend"}{deel_info}

Analyseer de onderstaande tekst en identificeer feitelijke claims die verifieerbaar zijn.
Denk aan: getallen, percentages, datums, tijdlijnen, beloftes van het college, budgetten, aantallen woningen of inwoners, vergelijkingen met eerdere jaren, statusupdates op moties of eerdere beloftes.

Regels:
- Kopieer elke claim WOORDELIJK uit de tekst (één zin of zinsdeel, max 200 tekens). Herformuleer of vat niets samen en verzin niets.
- Neem alleen claims op die in de tekst hieronder staan.
- Geef per claim: hoe een journalist dit kan controleren (welke bron, welk document), prioriteit HOOG / MIDDEL / LAAG, en een score 0-100 (hoe checkwaardig).
- Maximaal 8 claims, HOOG eerst. Zijn er geen verifieerbare claims, antwoord dan met [].

Antwoord ALLEEN met een JSON-array, geen markdown, geen uitleg:
[{{"claim":"...","verificatie":"...","prioriteit":"HOOG","score":85}}]

Tekst:
{brok}"""


def _parse_ai_json(raw):
    """
    JSON-array uit een modelantwoord. Tolerant: haalt markdown-hekjes weg,
    zoekt de array in omringende tekst, en redt losse objecten uit een
    afgekapte response (finishReason=MAX_TOKENS).
    """
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    data = None
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\[[\s\S]*\]", raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except ValueError:
                data = None
        if data is None:
            data = []
            for obj in re.findall(r"\{[^{}]*\}", raw):
                try:
                    data.append(json.loads(obj))
                except ValueError:
                    pass
    if isinstance(data, dict):
        data = data.get("claims") or []
    return [c for c in data if isinstance(c, dict) and str(c.get("claim") or "").strip()]


def _normaliseer_ai_claim(c):
    claim = str(c.get("claim") or "").strip()
    if not claim:
        return None
    prio = str(c.get("prioriteit") or "MIDDEL").strip().upper()
    if prio not in PRIO_SCORE:
        prio = "MIDDEL"
    try:
        score = int(round(float(c.get("score"))))
    except (TypeError, ValueError):
        score = PRIO_SCORE[prio]
    return {
        "claim": claim[:400],
        "verificatie": str(c.get("verificatie") or "Controleer via het onderliggende raadsstuk of de bron").strip()[:300],
        "prioriteit": prio,
        "score": max(0, min(100, score)),
        "bron": "ai",
        "kruischeck": None,
    }


def _verifieer_ai_claims(claims, tekst):
    """
    Een taalmodel kan claims verzinnen of parafraseren. Voor een journalist is
    dat onbruikbaar, dus elke AI-claim moet aantoonbaar in de brontekst staan:
      • "exact": de claim komt (na normalisatie van hoofdletters/leestekens)
        letterlijk in de brief voor;
      • "zin": de claim is een parafrase van één zin (of twee aangrenzende
        zinnen) uit de brief — alle getallen komen daarin voor en >= 60% van
        de betekenisvolle woorden. De claim wordt dan vervangen door de
        originele brieftekst; de formulering van het model blijft bewaard
        in 'ai_formulering'.
    Claims die niet terug te vinden zijn worden verworpen.
    Geeft (goedgekeurde claims, aantal verworpen).
    """
    tekst_n = _norm_sterk(tekst)
    zinnen = [z.strip() for z in re.split(r'(?<=[.!?])\s+|\n', tekst or "") if z.strip()]
    zin_data = [(z, _woorden_set(z), _getallen(z)) for z in zinnen]
    paren = [
        (zinnen[i] + " " + zinnen[i + 1],
         zin_data[i][1] | zin_data[i + 1][1],
         zin_data[i][2] | zin_data[i + 1][2])
        for i in range(len(zinnen) - 1)
    ]

    goed, verworpen = [], 0
    for c in claims:
        claim = c["claim"]
        cn = _norm_sterk(claim)
        if len(cn) >= 15 and cn in tekst_n:
            c["claim"] = claim[:250]
            c["brontekst_check"] = "exact"
            goed.append(c)
            continue

        cw, cg = _woorden_set(claim), _getallen(claim)
        beste, beste_score = None, 0.0
        if len(cw) >= 3:
            for z, zw, zg in zin_data + paren:
                if not cg <= zg:
                    continue
                score = len(cw & zw) / len(cw)
                # bij gelijke score de KORTSTE tekst kiezen (een losse zin boven een zinnenpaar)
                if score > beste_score or (
                    beste is not None and score == beste_score and len(z) < len(beste)
                ):
                    beste, beste_score = z, score
        if beste is not None and beste_score >= 0.6:
            c["ai_formulering"] = claim[:250]
            c["claim"] = beste[:250]
            c["brontekst_check"] = "zin"
            goed.append(c)
        else:
            verworpen += 1
    return goed, verworpen


def _zelfde_claim(a, b):
    """True als twee claimteksten kennelijk dezelfde bewering zijn (ook bij parafrase)."""
    na, nb = _norm_sterk(a), _norm_sterk(b)
    if not na or not nb:
        return False
    if na[:40] == nb[:40]:
        return True
    if len(na) >= 20 and len(nb) >= 20 and (na in nb or nb in na):
        return True
    wa, wb = _woorden_set(a), _woorden_set(b)
    if len(wa) >= 3 and len(wb) >= 3 and _getallen(a) == _getallen(b):
        return len(wa & wb) / min(len(wa), len(wb)) >= 0.7
    return False


def analyseer_ai_claims_status(tekst, titel, portefeuillehouder, api_key, context=None):
    """
    Volledige AI-laag voor één brief. Geeft (claims, gelukt, info):
      claims — geverifieerde AI-claims, elk mét deterministische kruischeck
               (als 'context' is meegegeven);
      gelukt — True als alle brokken door een model zijn geanalyseerd; bij False
               wordt de brief bij een volgende run opnieuw geprobeerd;
      info   — dict met model, verworpen, afgekapt, fout.
    """
    info = {"model": None, "verworpen": 0, "afgekapt": False, "fout": None}
    if not api_key:
        return [], False, info
    if not tekst:
        return [], True, info
    if _GEM["uitgeschakeld"]:
        info["fout"] = _GEM["uitgeschakeld"]
        return [], False, info

    brokken, info["afgekapt"] = _splits_in_brokken(tekst)
    ruw, gelukt = [], True
    for i, brok in enumerate(brokken, 1):
        try:
            antwoord, model = gemini_genereer(
                _ai_prompt(brok, titel, portefeuillehouder, i, len(brokken)), api_key
            )
        except GeminiFout as e:
            info["fout"] = str(e)[:200]
            gelukt = False
            if e.soort == "sleutel":
                _GEM["uitgeschakeld"] = f"API-key geweigerd door Google ({str(e)[:120]})"
            break
        info["model"] = model
        ruw.extend(_parse_ai_json(antwoord))
        if i < len(brokken):
            time.sleep(0.5)

    if gelukt:
        _GEM["mislukt_achtereen"] = 0
    else:
        _GEM["mislukt_achtereen"] += 1
        if _GEM["mislukt_achtereen"] >= 3 and not _GEM["uitgeschakeld"]:
            _GEM["uitgeschakeld"] = (
                f"3 brieven achter elkaar zonder werkend model (laatste fout: {info['fout']})"
            )

    genormaliseerd = [c for c in (_normaliseer_ai_claim(r) for r in ruw) if c]
    genormaliseerd.sort(key=lambda c: (-PRIO_SCORE[c["prioriteit"]], -c["score"]))
    uniek = []
    for c in genormaliseerd:
        if not any(_zelfde_claim(c["claim"], u["claim"]) for u in uniek):
            uniek.append(c)

    claims, verworpen = _verifieer_ai_claims(uniek, tekst)
    info["verworpen"] = verworpen
    claims = claims[:MAX_CLAIMS_TOTAAL]

    # Dezelfde deterministische kruischeck als bij code-claims. Voorheen
    # kregen AI-claims altijd kruischeck=None.
    if context:
        for c in claims:
            c["kruischeck"] = kruischeck_claim(c["claim"], context)
    return claims, gelukt, info


def analyseer_ai_claims(tekst, titel, portefeuillehouder, api_key):
    """Compatibele wrapper: geeft alleen de claims (zie analyseer_ai_claims_status)."""
    claims, _gelukt, _info = analyseer_ai_claims_status(tekst, titel, portefeuillehouder, api_key)
    return claims


# ── CLAIMS SAMENVOEGEN ────────────────────────────────────────────────────────
def combineer_claims(code_claims, ai_claims):
    """
    Voegt code- en AI-claims samen. AI-claims gaan voor omdat ze rijker zijn,
    maar:
      • duplicaten worden herkend op inhoud (parafrase, gelijke getallen,
        containment) en niet alleen op de eerste 40 tekens;
      • een deterministisch oordeel van de code-claim (kruischeck) gaat nooit
        verloren: het wordt overgenomen door de AI-claim als die er zelf
        geen heeft, en een "Afwijkend:"-oordeel tilt de claim naar HOOG;
      • bij het afkappen op MAX_CLAIMS_TOTAAL staan claims met een
        "Afwijkend:"-oordeel vooraan, dan HOOG > MIDDEL > LAAG.
    """
    if not ai_claims:
        return code_claims

    unieke_code = []
    for c in code_claims:
        dubbel = next((a for a in ai_claims if _zelfde_claim(c["claim"], a["claim"])), None)
        if dubbel is None:
            unieke_code.append(c)
            continue
        if c.get("kruischeck") and not dubbel.get("kruischeck"):
            dubbel["kruischeck"] = c["kruischeck"]
        if (dubbel.get("kruischeck") or "").startswith("Afwijkend") and dubbel.get("prioriteit") != "HOOG":
            dubbel["prioriteit"] = "HOOG"
            dubbel["score"] = max(dubbel.get("score") or 0, PRIO_SCORE["HOOG"])

    volgorde = {"HOOG": 0, "MIDDEL": 1, "LAAG": 2}
    gecombineerd = ai_claims + unieke_code
    gecombineerd.sort(key=lambda c: (
        0 if (c.get("kruischeck") or "").startswith("Afwijkend") else 1,
        volgorde.get(c.get("prioriteit", "LAAG"), 9),
        -(c.get("score") or 0),
    ))
    return gecombineerd[:MAX_CLAIMS_TOTAAL]


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
# ── AI-STATUS PER BRIEF ───────────────────────────────────────────────────────

def _ai_status(brief):
    """
    True  = AI-laag voor deze brief afgerond
    False = nog te doen (mislukt, of nooit gedraaid)
    None  = niet van toepassing (bv. PDF zonder tekstlaag)

    Brieven die vóór het 'ai_gedaan'-veld zijn weggeschreven hebben dat veld
    niet. Die tellen als afgerond zodra er minstens één AI-claim in staat;
    anders is niet te bewijzen dat de AI-laag ooit gedraaid heeft en wordt
    hij (begrensd) ingehaald.
    """
    if "ai_gedaan" in brief:
        return brief["ai_gedaan"]
    if any(c.get("bron") == "ai" for c in (brief.get("claims") or [])):
        return True
    return False


def _behoud_bij_mislukking(basis, vorig, ai_aan, pogingen_prev, **extra):
    """
    Record voor een brief waarvan PDF of tekst niet op te halen was. Bestond de
    brief al met claims, dan blijven die behouden (een tijdelijke PDF-fout mag
    eerder goed werk niet wissen); alleen het pogingen-veld gaat omhoog.
    """
    if vorig and "claims" in vorig:
        rec = dict(vorig)
        rec["ai_gedaan"] = _ai_status(vorig)
    else:
        rec = {**basis, "tekst": None, "claims": [], "ai_gedaan": False}
        rec.update(extra)
    if rec.get("ai_gedaan") is False and ai_aan:
        rec["ai_pogingen"] = pogingen_prev + 1
    return rec


# ── HOOFDPROGRAMMA ────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        print("✓ Gemini API key gevonden — AI-laag wordt geïnitialiseerd")
        if not init_gemini(api_key):
            print("⚠  AI-laag uit voor deze run — alleen code-gebaseerde claimdetectie")
    else:
        print("⚠  Geen GEMINI_API_KEY — alleen code-gebaseerde claimdetectie")

    try:
        herverwerk_max = int(os.environ.get("AI_HERVERWERK_MAX", "20"))
    except ValueError:
        herverwerk_max = 20

    # Datumbereik: minimaal 30 dagen terug, ÁLTIJD — ongeacht wat SCRAPE_VANAF
    # (gevoed door de scrape-tracker) doorgeeft. Zelfde reden als bij
    # scrape_moties.py en scrape_stemmingen.py. De skip-check verderop zorgt
    # dat dit vrijwel niets extra kost voor brieven die al volledig verwerkt zijn.
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
    verwerkt      = 0
    herverwerkt   = 0
    code_totaal   = 0
    ai_totaal     = 0
    ai_verworpen  = 0

    for i, row in enumerate(relevante_rows):
        item_id = row.get("DT_RowId")
        titel   = row.get("title", "").strip()
        datum   = parse_datum(row.get("datumbericht"))
        type_   = row.get("typeselectie", "")
        ph_raw  = row.get("portefeuillehouderselectie", "") or ""
        # Nederlandse bestuurlijke naam-notatie ("Laan, van der H.") bevat zelf
        # al een komma; " | " kan niet in een naam voorkomen, dus ondubbelzinnig
        # bij meerdere portefeuillehouders op één brief.
        ph      = " | ".join([p.strip() for p in ph_raw.split("\r\n") if p.strip()])

        print(f"  [{i+1}/{len(relevante_rows)}] {datum} — {titel[:55]}", end=" ", flush=True)

        ai_aan = bool(api_key) and not _GEM["uitgeschakeld"]
        vorig = bestaand.get(item_id)
        pogingen_prev = (vorig or {}).get("ai_pogingen") or 0

        # Overslaan als al volledig verwerkt. Sinds de skip-check alleen keek of
        # de sleutel "claims" bestond, telde een brief waarvan de AI-laag
        # MISLUKTE (bv. een 404 op het model) voor altijd als "klaar". Nu wordt
        # een brief opnieuw opgepakt zolang ai_gedaan == False, er een werkende
        # AI-laag is, en het maximum aantal pogingen niet is bereikt.
        if vorig is not None and "claims" in vorig:
            achterstand = (
                ai_aan
                and _ai_status(vorig) is False
                and pogingen_prev < MAX_POGINGEN
            )
            if not achterstand:
                print("→ al verwerkt, overgeslagen")
                continue
            if herverwerkt >= herverwerk_max:
                print("→ AI-inhaalslag uitgesteld (limiet per run bereikt)")
                continue
            herverwerkt += 1
            print("→ AI-laag ontbrak, opnieuw verwerken", end=" ", flush=True)

        basis = {
            "id": item_id, "titel": titel, "type": type_,
            "datum": datum, "portefeuillehouder": ph,
            "url": f"{BASE_URL}/Reports/Item/{item_id}",
            "bijgewerkt": vandaag.strftime("%Y-%m-%d"),
        }

        # DocumentId ophalen
        doc_id = haal_document_id(opener, item_id)
        if not doc_id:
            print("→ geen documentId gevonden")
            bestaand[item_id] = _behoud_bij_mislukking(basis, vorig, ai_aan, pogingen_prev)
            time.sleep(0.4)
            continue
        pdf_url = f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}"

        # PDF downloaden
        time.sleep(0.3)
        pdf_bytes = download_pdf(opener, item_id, doc_id)
        if not pdf_bytes:
            print("→ PDF niet beschikbaar")
            bestaand[item_id] = _behoud_bij_mislukking(
                basis, vorig, ai_aan, pogingen_prev, pdf_url=pdf_url
            )
            time.sleep(0.4)
            continue

        # Tekst extraheren (None = extractie mislukt, "" = PDF zonder tekstlaag)
        tekst = extraheer_pdf_tekst(pdf_bytes)
        if tekst is None:
            print("→ tekst-extractie mislukt")
            bestaand[item_id] = _behoud_bij_mislukking(
                basis, vorig, ai_aan, pogingen_prev, pdf_url=pdf_url
            )
            time.sleep(0.4)
            continue

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
        ai_claims, ai_info = [], {}
        if not tekst:
            ai_gedaan = None                 # niets om te analyseren
        elif ai_aan:
            ai_claims, ai_gedaan, ai_info = analyseer_ai_claims_status(
                tekst, titel, ph, api_key, claim_context
            )
            time.sleep(0.3)
        else:
            ai_gedaan = False                # geen (werkende) AI → later inhalen

        # Combineren
        alle_claims = combineer_claims(code_claims, ai_claims)

        code_totaal  += len(code_claims)
        ai_totaal    += len(ai_claims)
        ai_verworpen += ai_info.get("verworpen", 0)

        tekst_info = f"{len(tekst)} tekens" if tekst else "geen tekst"
        if ai_info.get("model"):
            extra = f", {ai_info['verworpen']} verworpen: niet in brontekst" if ai_info.get("verworpen") else ""
            ai_deel = f"{len(ai_claims)} AI-claims ({ai_info['model']}{extra})"
        elif ai_gedaan is False and ai_aan:
            ai_deel = "0 AI-claims (AI MISLUKT — wordt volgende run opnieuw geprobeerd)"
        elif ai_gedaan is False:
            ai_deel = "0 AI-claims (AI uit)"
        else:
            ai_deel = f"{len(ai_claims)} AI-claims"
        print(
            f"→ {tekst_info} · "
            f"{len(code_claims)} code-claims · "
            f"{ai_deel} · "
            f"{len(alle_claims)} totaal"
        )

        record = {
            **basis,
            "pdf_url":    pdf_url,
            "tekst":      tekst[:5000] if tekst else None,
            "claims":     alle_claims,
            "ai_gedaan":  ai_gedaan,
        }
        if ai_info.get("model"):
            record["ai_model"] = ai_info["model"]
        if ai_info.get("verworpen"):
            record["ai_verworpen"] = ai_info["verworpen"]
        if ai_info.get("afgekapt"):
            record["ai_afgekapt"] = True
        if ai_gedaan is False and ai_aan:
            record["ai_pogingen"] = pogingen_prev + 1
        bestaand[item_id] = record
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
    in_bereik     = {r.get("DT_RowId") for r in relevante_rows}
    zonder_ai     = [b for b in resultaat if b.get("id") in in_bereik and _ai_status(b) is False]
    opgegeven     = sum(1 for b in zonder_ai if (b.get("ai_pogingen") or 0) >= MAX_POGINGEN)
    wacht_op_ai   = len(zonder_ai) - opgegeven
    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {verwerkt} brieven verwerkt (waarvan {herverwerkt} opnieuw voor de AI-laag)")
    print(f"  {len(resultaat)} totaal in JSON")
    print(f"  {code_totaal} code-claims gedetecteerd")
    print(f"  {ai_totaal} AI-claims gedetecteerd ({ai_verworpen} verworpen: niet terug te vinden in de brontekst)")
    print(f"  {totaal_claims} claims totaal in JSON")
    if api_key:
        print(f"  {wacht_op_ai} brieven wachten nog op de AI-laag")
        if opgegeven:
            print(f"  {opgegeven} brieven opgegeven na {MAX_POGINGEN} mislukte AI-pogingen")
    if _GEM["uitgeschakeld"]:
        print(f"  ⚠ AI-laag was uitgeschakeld tijdens deze run: {_GEM['uitgeschakeld']}")


if __name__ == "__main__":
    main()
