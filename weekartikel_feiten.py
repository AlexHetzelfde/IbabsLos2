#!/usr/bin/env python3
"""
weekartikel_feiten.py — stap 1 van het wekelijkse artikel
==========================================================
Verzamelt uit de JSON-bestanden in data/ de feiten van maandag t/m vrijdag,
zodat een taalmodel (stap 2, weekartikel_schrijf.py) er een artikel van kan
maken zonder zelf iets te hoeven weten, rekenen of verzinnen.

Er zit GEEN AI in dit script: alles is filteren, tellen en opschrijven.
Elk feit krijgt een nummer (F01, F02, ...), een sectie, een tekst in gewoon
Nederlands, het bronbestand, het record-id en (als die er is) een link. In
het artikel verwijst elke alinea later naar die nummers, en de controle in
stap 2 kijkt of elk getal in de tekst ook in een feit staat.

Gebruik (vanuit de hoofdmap van de repo):
    python3 weekartikel_feiten.py                      # meest recente vrijdag
    python3 weekartikel_feiten.py --tot 2026-09-18     # een eerdere week
    python3 weekartikel_feiten.py --tot 2026-09-18 --uit feiten.json

    --tot      laatste dag van de week (een vrijdag); de week loopt vijf dagen
               terug tot en met de maandag ervoor
    --vandaag  overschrijft "vandaag" (alleen om te testen)
    --uit      schrijft de feiten ook als JSON weg

Afspraken die hier vastliggen:
  * "Nieuw" = de datum van het item zelf valt in de week (motie: datum van de
    vergadering; collegebericht: datum; aanbesteding: datum_bekendmaking van
    een publicatie; camera: startdatum van een besluitperiode). Wat later
    wordt gepubliceerd dan de datum die erbij staat, valt buiten deze week.
  * EBS-cijfers tellen pas mee vanaf 2026-08-13 (daarvoor volgde de scraper 3
    in plaats van 12 haltes; zelfde grens als volledigeDekkingVanaf() in
    app.js). Een dag die nog loopt (vandaag) wordt als tussenstand vermeld en
    telt niet mee in ranglijsten.
  * AI-afgeleide velden (claims uit collegebrieven) krijgen soort="ai_analyse"
    zodat het artikel ze niet als vaststaand feit presenteert.
  * NOS lokaal wordt bewust NIET gebruikt: dat zijn landelijke berichten met
    een door AI voorgestelde lokale invalshoek, geen lokaal nieuws.
"""

import argparse
import html
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

EBS_BETROUWBAAR_VANAF = "2026-08-13"   # gelijk aan volledigeDekkingVanaf() in app.js
EBS_NORM_PCT = 2                       # gelijk aan EBS_WEEKNORM_PCT in app.js
EBS_AANTAL_HALTES = 12
MIN_RITTEN_LIJN = 100                  # ranglijst op percentage: kleine lijnen niet overschatten
VOORUITBLIK_DAGEN = 21
MAX_MOTIES = 20
MAX_STEMMINGEN = 15
MAX_CLAIMS_PER_BRIEF = 2
MAX_VOORUITBLIK_PER_SOORT = 6

SECTIE_VOLGORDE = ["ebs", "raad", "college", "besluiten", "aanbestedingen", "vooruitblik"]
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
           "augustus", "september", "oktober", "november", "december"]


# ── HULPJES ──────────────────────────────────────────────────────────────────
def lees(naam, standaard=None):
    pad = DATA / f"{naam}.json"
    if not pad.exists():
        return standaard
    with open(pad, encoding="utf-8") as f:
        return json.load(f)


def d(iso):
    return date.fromisoformat(str(iso)[:10])


def nl(iso, jaar=False):
    x = d(iso)
    s = f"{DAGEN[x.weekday()]} {x.day} {MAANDEN[x.month - 1]}"
    return f"{s} {x.year}" if jaar else s


def nl_kort(iso):
    x = d(iso)
    return f"{x.day} {MAANDEN[x.month - 1]}"


def getal(n):
    return f"{int(round(float(n))):,}".replace(",", ".")


def euro(n):
    return f"€ {getal(n)}"


def pct(a, b):
    return math.floor(a / b * 1000 + 0.5) / 10 if b else None


def pct_tekst(p):
    return f"{p:.1f}".replace(".", ",") + "%"


def dagen_tussen(van, tot):
    x, uit = d(van), []
    while x <= d(tot):
        uit.append(x.isoformat())
        x += timedelta(days=1)
    return uit


def verschuif(iso, dagen):
    return (d(iso) + timedelta(days=dagen)).isoformat()


def in_venster(iso, van, tot):
    return bool(iso) and van <= str(iso)[:10] <= tot


def schoon(tekst, max_tekens=None):
    """HTML-entiteiten weg (&#8211; e.d.), witruimte normaliseren, evt. inkorten op zinsgrens."""
    t = " ".join(html.unescape(str(tekst or "")).split())
    if max_tekens and len(t) > max_tekens:
        knip = t[:max_tekens]
        punt = knip.rfind(". ")
        t = (knip[:punt + 1] if punt > max_tekens * 0.5 else knip.rstrip() + "…")
    return t


def feit(sectie, tekst, bron, bron_id=None, datum=None, link=None, soort="feit"):
    return {"sectie": sectie, "soort": soort, "tekst": tekst, "datum": datum,
            "bron": bron, "bron_id": bron_id, "link": link}


def motiecode(titel):
    m = re.match(r"\s*(\d{2}[A-Z]\d+)", titel or "")
    return m.group(1) if m else (titel or "").strip().lower()


def opsomming(delen):
    delen = list(delen)
    return delen[0] if len(delen) == 1 else ", ".join(delen[:-1]) + " en " + delen[-1]


def afsluiten(tekst):
    """Zet een punt achter een zin, tenzij er al leestekens staan (bv. 'Hamming, J.')."""
    t = tekst.rstrip()
    return t if t.endswith((".", "!", "?", "…")) else t + "."


def beschrijf_dagen(dagen):
    if len(dagen) == 1:
        return nl(dagen[0])
    return f"{nl(dagen[0])} t/m {nl(dagen[-1])} ({len(dagen)} dagen met data)"


# ── EBS ──────────────────────────────────────────────────────────────────────
def som(hist, dagen, veld):
    uit = {}
    for dag in dagen:
        for k, v in (hist[dag].get(veld) or {}).items():
            uit[k] = uit.get(k, 0) + (v or 0)
    return uit


def totalen(hist, dagen):
    r = sum(hist[x].get("totaal", 0) or 0 for x in dagen)
    u = sum(hist[x].get("cancelled", 0) or 0 for x in dagen)
    return r, u, pct(u, r)


def volledige_week(hist, maandag):
    dagen = dagen_tussen(maandag, verschuif(maandag, 6))
    if all(x in hist and x >= EBS_BETROUWBAAR_VANAF for x in dagen):
        return dagen
    return None


def ebs_tussenstand(vandaag):
    """Tussenstand van een dag die nog loopt, uit ebs_totaal_teller + ebs_uitval (ontdubbeld op id)."""
    teller = (lees("ebs_totaal_teller", {}) or {}).get(vandaag)
    if not teller:
        return None
    uitval = lees("ebs_uitval", []) or []
    ids = {r.get("id") for r in uitval if r.get("status") == "cancelled" and r.get("datum") == vandaag}
    return teller.get("totaal", 0) or 0, len(ids)


def ebs_feiten(van, tot, vandaag):
    f, notities = [], []
    bron = "ebs_percentage_historie.json"
    hist = lees("ebs_percentage_historie", None)
    if not hist:
        return f, ["ebs_percentage_historie.json ontbreekt: geen EBS-feiten."]

    venster = dagen_tussen(van, tot)
    compleet = [x for x in venster if x in hist and x >= EBS_BETROUWBAAR_VANAF and x != vandaag]
    voor_grens = [x for x in venster if x < EBS_BETROUWBAAR_VANAF]
    ontbreekt = [x for x in venster if x >= EBS_BETROUWBAAR_VANAF and x not in hist and x != vandaag]
    tussen = None
    if vandaag in venster and vandaag not in hist and vandaag >= EBS_BETROUWBAAR_VANAF:
        tussen = ebs_tussenstand(vandaag)

    if voor_grens:
        notities.append(f"EBS: {len(voor_grens)} dag(en) van deze week liggen vóór {EBS_BETROUWBAAR_VANAF} "
                        f"en zijn niet vergelijkbaar (toen volgde de scraper 3 haltes); niet meegenomen.")
    if ontbreekt:
        notities.append("EBS: geen data voor " + ", ".join(ontbreekt) + ".")
    if not compleet and not tussen:
        return f, notities

    f.append(feit("ebs",
                  f"De EBS-cijfers gaan over ritten langs de {EBS_AANTAL_HALTES} haltes die het dashboard volgt "
                  f"(niet het hele EBS-net) en alleen over uitgevallen ritten, niet over vertragingen.",
                  bron, soort="context"))

    if compleet:
        r, u, p = totalen(hist, compleet)
        f.append(feit("ebs", f"Over {beschrijf_dagen(compleet)} viel {getal(u)} van de {getal(r)} ritten uit "
                             f"({pct_tekst(p)}).", bron, datum=compleet[-1]))
        for dag in compleet:
            h = hist[dag]
            f.append(feit("ebs", f"{nl(dag).capitalize()}: {getal(h['cancelled'])} van de {getal(h['totaal'])} ritten "
                                 f"uitgevallen ({pct_tekst(pct(h['cancelled'], h['totaal']))}).", bron, datum=dag))
        if len(compleet) >= 3:
            per_dag = {x: pct(hist[x]["cancelled"], hist[x]["totaal"]) for x in compleet}
            slecht = max(compleet, key=lambda x: per_dag[x])
            best = min(compleet, key=lambda x: per_dag[x])
            f.append(feit("ebs", f"De slechtste dag was {nl(slecht)} ({pct_tekst(per_dag[slecht])} uitval), "
                                 f"de beste {nl(best)} ({pct_tekst(per_dag[best])}).", bron, datum=slecht))

        # lijnen
        uitg, rit = som(hist, compleet, "per_lijn"), som(hist, compleet, "totaal_per_lijn")
        if rit:
            top = sorted(uitg.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            for lijn, n in top:
                f.append(feit("ebs", f"Lijn {lijn}: {getal(n)} uitgevallen ritten van {getal(rit.get(lijn, 0))} "
                                     f"({pct_tekst(pct(n, rit.get(lijn, 0)))}) in deze periode; behoort tot de drie lijnen "
                                     f"met de meeste uitgevallen ritten.", bron, bron_id=lijn))
            kand = [(l, uitg.get(l, 0), rit[l]) for l in rit if rit[l] >= MIN_RITTEN_LIJN]
            if kand:
                l, n, r_ = max(kand, key=lambda x: (pct(x[1], x[2]), x[1]))
                f.append(feit("ebs", f"Het hoogste uitvalpercentage van de lijnen met minstens {MIN_RITTEN_LIJN} ritten "
                                     f"had lijn {l}: {pct_tekst(pct(n, r_))} ({getal(n)} van {getal(r_)} ritten).",
                              bron, bron_id=l))
        # oorzaken
        oz = {k: v for k, v in som(hist, compleet, "per_oorzaak").items() if k != "-"}
        if oz:
            top = sorted(oz.items(), key=lambda kv: -kv[1])[:3]
            f.append(feit("ebs", "Meest genoemde oorzaken van uitval: " + ", ".join(f"{k} ({getal(v)}x)" for k, v in top) +
                                 ". Dit zijn vermeldingen, geen ritten: niet bij elke uitgevallen rit is een oorzaak "
                                 "genoemd en een rit kan meerdere oorzaken hebben.", bron))
        # dagdelen
        dd = som(hist, compleet, "per_dagdeel")
        if dd:
            top = sorted(dd.items(), key=lambda kv: -kv[1])
            f.append(feit("ebs", "Uitgevallen ritten per dagdeel (op geplande vertrektijd): " +
                                 ", ".join(f"{k} {getal(v)}" for k, v in top) + ".", bron))

        # vergelijking met de week ervoor (zelfde dagen)
        vorige = [verschuif(x, -7) for x in compleet]
        if all(x in hist and x >= EBS_BETROUWBAAR_VANAF for x in vorige):
            rv, uv, pv = totalen(hist, vorige)
            verschil = round(p - pv, 1)
            if verschil:
                richting = "hoger" if verschil > 0 else "lager"
                slot = f"De uitval was dus {str(abs(verschil)).replace('.', ',')} procentpunt {richting} dan een week eerder."
            else:
                slot = "De uitval was dus even hoog als een week eerder."
            zin = (f"Ter vergelijking: in de week ervoor ({nl_kort(vorige[0])} t/m {nl_kort(vorige[-1])}) viel "
                   f"{pct_tekst(pv)} van de ritten uit ({getal(uv)} van {getal(rv)}). {slot}")
            f.append(feit("ebs", zin, bron))

        # norm: vorige volledige week en stand sinds de eerste volledige week
        maandag_nu = verschuif(compleet[0], -d(compleet[0]).weekday())
        vw = volledige_week(hist, verschuif(maandag_nu, -7))
        if vw:
            rw, uw, pw = totalen(hist, vw)
            oordeel = "boven" if pw > EBS_NORM_PCT else "binnen"
            f.append(feit("ebs", f"Het dashboard hanteert een norm van maximaal {EBS_NORM_PCT}% uitgevallen ritten per week "
                                 f"(maandag t/m zondag). De vorige volledige week ({nl_kort(vw[0])} t/m {nl_kort(vw[-1])}) "
                                 f"kwam uit op {pct_tekst(pw)} ({getal(uw)} van {getal(rw)} ritten), dus {oordeel} de norm.",
                          bron))
        maandagen, m = [], d(EBS_BETROUWBAAR_VANAF)
        while m.weekday() != 0:
            m += timedelta(days=1)
        while m.isoformat() < maandag_nu:
            w = volledige_week(hist, m.isoformat())
            if w:
                maandagen.append((m.isoformat(), totalen(hist, w)[2]))
            m += timedelta(days=7)
        if maandagen:
            boven = sum(1 for _, p_ in maandagen if p_ > EBS_NORM_PCT)
            f.append(feit("ebs", f"Sinds de eerste volledige week ({nl_kort(maandagen[0][0])}) zaten {boven} van de "
                                 f"{len(maandagen)} volledige weken boven de norm van {EBS_NORM_PCT}%. De lopende week "
                                 f"({nl_kort(maandag_nu)} t/m {nl_kort(verschuif(maandag_nu, 6))}) kan pas na zondag "
                                 f"worden getoetst.", bron))
    if tussen:
        r, u = tussen
        f.append(feit("ebs", f"{nl(vandaag).capitalize()} (tussenstand, de dag is nog niet afgelopen): {getal(u)} van de "
                             f"{getal(r)} ritten uitgevallen ({pct_tekst(pct(u, r))}).",
                      "ebs_uitval.json + ebs_totaal_teller.json", datum=vandaag))
    return f, notities


# ── RAAD ─────────────────────────────────────────────────────────────────────
def raad_feiten(van, tot):
    f, notities = [], []
    ruw = [m for m in (lees("moties", []) or []) if in_venster(m.get("datum"), van, tot)]
    # moties.json bevat soms dubbele registraties van dezelfde motie (zelfde code, bv. 26M54);
    # per code houden we één record: bij voorkeur met bekende uitslag, dan de GEWIJZIGDE tekst.
    per_code = {}
    for m in ruw:
        k = motiecode(m.get("titel"))
        beter = k not in per_code or (
            (m.get("status") is not None, "GEWIJZIGD" in (m.get("titel") or "")) >
            (per_code[k].get("status") is not None, "GEWIJZIGD" in (per_code[k].get("titel") or "")))
        if beter:
            per_code[k] = m
    moties = sorted(per_code.values(), key=lambda m: (m.get("datum", ""), motiecode(m.get("titel"))))
    if len(ruw) > len(moties):
        notities.append(f"Moties: {len(ruw) - len(moties)} dubbele registratie(s) in moties.json samengevoegd (zelfde motiecode).")
    if moties:
        tel = {"aangenomen": 0, "verworpen": 0, "ingetrokken": 0}
        onbekend = 0
        for m in moties:
            st = m.get("status")
            if st in tel:
                tel[st] += 1
            else:
                onbekend += 1
        n_am = sum(1 for m in moties if (m.get("type") or "").lower() == "amendement")
        f.append(feit("raad", f"In deze week zijn {len(moties) - n_am} moties en {n_am} amendementen geregistreerd: "
                              f"{tel['aangenomen']} aangenomen, {tel['verworpen']} verworpen, {tel['ingetrokken']} "
                              f"ingetrokken en {onbekend} zonder bekende uitslag.", "moties.json"))
        for m in moties[:MAX_MOTIES]:
            st = m.get("status") or "uitslag nog niet bekend"
            tekst = f"{m.get('type') or 'Motie'} '{schoon(m.get('titel'))}' van {m.get('indiener') or 'onbekend'} ({m.get('partij') or '?'}): {st}"
            if m.get("voor_pct") is not None:
                tekst += f"; {m['voor_pct']}% voor, {m.get('tegen_pct')}% tegen"
            f.append(feit("raad", afsluiten(tekst), "moties.json", m.get("id"), m.get("datum")))
        if len(moties) > MAX_MOTIES:
            notities.append(f"Moties: {len(moties) - MAX_MOTIES} van de {len(moties)} niet als apart feit opgenomen.")

    stemmingen = [s for s in (lees("stemmingen", []) or []) if in_venster(s.get("datum"), van, tot)]
    stemmingen.sort(key=lambda s: (s.get("datum", ""), s.get("titel", "")))
    for s in stemmingen[:MAX_STEMMINGEN]:
        tekst = f"Stemming over '{schoon(s.get('titel'))}' ({nl_kort(s['datum'])}): {s.get('uitslag_tekst') or 'uitslag onbekend'}"
        if s.get("fracties_voor"):
            tekst += f"; voor: {s['fracties_voor']}"
        if s.get("fracties_tegen"):
            tekst += f"; tegen: {s['fracties_tegen']}"
        if s.get("fracties_onthouding"):
            tekst += f"; onthouding: {s['fracties_onthouding']}"
        f.append(feit("raad", afsluiten(tekst), "stemmingen.json", s.get("id"), s.get("datum")))
    if len(stemmingen) > MAX_STEMMINGEN:
        notities.append(f"Stemmingen: {len(stemmingen) - MAX_STEMMINGEN} van de {len(stemmingen)} niet opgenomen.")

    per_dag = {}
    for v in lees("vergaderingen", []) or []:
        if in_venster(v.get("datum"), van, tot):
            sleutel = (v["datum"], v.get("type"))
            if sleutel not in per_dag or len(v.get("agendapunten") or []) > len(per_dag[sleutel].get("agendapunten") or []):
                per_dag[sleutel] = v
    for (_, _), v in sorted(per_dag.items()):
        n = len(v.get("agendapunten") or [])
        f.append(feit("raad", f"{v.get('type') or 'Vergadering'} op {nl(v['datum'], True)} in de {v.get('locatie') or 'raadzaal'}"
                              + (f", met {n} agendapunten." if n else "."), "vergaderingen.json", v.get("id"), v.get("datum"), v.get("url")))
    ontbreekt = []
    if not per_dag:
        ontbreekt.append("raadsvergadering")
    if not moties:
        ontbreekt.append("moties of amendementen")
    if not stemmingen:
        ontbreekt.append("stemmingen")
    if ontbreekt:
        f.append(feit("raad", f"In de data van het dashboard staan voor deze week {opsomming(['geen ' + x for x in ontbreekt])}.",
                      "moties.json, stemmingen.json, vergaderingen.json", soort="context"))
    return f, notities


# ── COLLEGE ──────────────────────────────────────────────────────────────────
def college_feiten(van, tot):
    f = []
    for b in sorted(lees("collegeberichten", []) or [], key=lambda x: (x.get("datum", ""), x.get("titel", ""))):
        if not in_venster(b.get("datum"), van, tot):
            continue
        ph = f", portefeuillehouder {b['portefeuillehouder']}" if b.get("portefeuillehouder") else ""
        f.append(feit("college", afsluiten(f"{b.get('type') or 'Collegebericht'} van {nl(b['datum'])}: '{schoon(b.get('titel'))}'{ph}"),
                      "collegeberichten.json", b.get("id"), b.get("datum"), b.get("url")))
        claims = [c for c in (b.get("claims") or [])
                  if c.get("bron") == "ai" and c.get("prioriteit") == "HOOG" and c.get("brontekst_check") == "exact"]
        for c in claims[:MAX_CLAIMS_PER_BRIEF]:
            f.append(feit("college", f"Bewering uit de brief '{schoon(b.get('titel'), 80)}', door de automatische analyse van "
                                     f"het dashboard aangemerkt als belangrijk om te controleren: {schoon(c.get('claim'))}",
                          "collegeberichten.json", b.get("id"), b.get("datum"), b.get("url"), soort="ai_analyse"))
    if not f:
        f.append(feit("college", "In de data van het dashboard staan voor deze week geen nieuwe collegeberichten "
                                 "(zoals raadsinformatiebrieven).", "collegeberichten.json", soort="context"))
    return f, []


# ── BESLUITEN (woningsluitingen, camera's) ───────────────────────────────────
def besluiten_feiten(van, tot, vandaag):
    f = []
    for w in sorted(lees("woningsluitingen", []) or [], key=lambda x: x.get("datum", "")):
        if in_venster(w.get("datum"), van, tot):
            f.append(feit("besluiten", f"Woningsluiting ({w.get('bron') or 'bron onbekend'}, {nl(w['datum'])}): "
                                       f"{schoon(w.get('titel'))}. {schoon(w.get('excerpt'), 320)}",
                          "woningsluitingen.json", None, w.get("datum"), w.get("link")))
    gezien = set()
    for bestand in ("cameras_actief", "cameras_geschiedenis"):
        for cam in lees(bestand, []) or []:
            for p in cam.get("periodes") or []:
                sleutel = (cam.get("camera"), p.get("start"), p.get("eind"))
                if sleutel in gezien:
                    continue
                gezien.add(sleutel)
                reden = ", ".join(p.get("reden_categorieen") or [])
                if in_venster(p.get("start"), van, tot):
                    f.append(feit("besluiten", f"Tijdelijk cameratoezicht {cam.get('camera')}: nieuwe periode van {nl(p['start'])} "
                                               f"t/m {nl(p['eind'])}" + (f" (reden: {reden})" if reden else "") +
                                               (f"; dit is een verlenging (eerder {cam.get('keer_verlengd')}x verlengd)"
                                                if cam.get("keer_verlengd") else "") + ".",
                                  bestand + ".json", cam.get("camera"), p.get("start"), p.get("link")))
                elif in_venster(p.get("eind"), van, tot) and p["eind"] < vandaag:
                    f.append(feit("besluiten", f"Het tijdelijke cameratoezicht {cam.get('camera')} liep af op {nl(p['eind'])}.",
                                  bestand + ".json", cam.get("camera"), p.get("eind"), p.get("link")))
    if not f:
        f.append(feit("besluiten", "In de data van het dashboard staan voor deze week geen nieuwe woningsluitingen en "
                                   "geen nieuwe of beëindigde perioden van tijdelijk cameratoezicht.",
                      "woningsluitingen.json, cameras_actief.json", soort="context"))
    return f, []


# ── AANBESTEDINGEN (TED + TenderNed) ─────────────────────────────────────────
def aanbestedingen_feiten(van, tot):
    f, notities = [], []
    for proc in lees("aanbestedingen", []) or []:
        for pub in proc.get("publicaties") or []:
            dag = str(pub.get("datum_bekendmaking") or "")[:10]
            if not in_venster(dag, van, tot):
                continue
            tekst = (f"Op {nl(dag)} publiceerde {pub.get('koper') or proc.get('koper') or 'de gemeente'} op TED een "
                     f"aankondiging ({pub.get('type_aankondiging') or 'type onbekend'}): '{schoon(pub.get('titel'))}'")
            if pub.get("sluitingsdatum"):
                tekst += f"; inschrijven kan tot {nl(pub['sluitingsdatum'][:10])}"
            if pub.get("geraamde_waarde"):
                tekst += f"; geraamde waarde {euro(pub['geraamde_waarde'])}"
            if pub.get("gegunde_waarde"):
                tekst += f"; gegunde waarde {euro(pub['gegunde_waarde'])}"
            if pub.get("winnaar"):
                tekst += f"; winnaar {pub['winnaar']}"
            f.append(feit("aanbestedingen", afsluiten(tekst), "aanbestedingen.json", pub.get("publicatienummer"), dag, pub.get("link")))

    tn = lees("tenderned", None)
    if tn is not None:
        genegeerd = 0
        for p in sorted(tn, key=lambda x: x.get("datum_publicatie") or ""):
            if not in_venster(p.get("datum_publicatie"), van, tot):
                continue
            opdrachtgever = p.get("opdrachtgever") or ""
            if "zaanstad" not in opdrachtgever.lower():
                genegeerd += 1      # vangnet: filter op aanbestedende dienst wordt mogelijk stil genegeerd door TenderNed
                continue
            soort = "Europese" if p.get("europees") else ("nationale" if p.get("europees") is False else "")
            tekst = (f"Op {nl(p['datum_publicatie'][:10])} verscheen op TenderNed een {soort} {p.get('type_publicatie') or 'publicatie'} "
                     f"van {opdrachtgever}: '{schoon(p.get('titel'))}'").replace("  ", " ")
            if p.get("procedure"):
                tekst += f" (procedure: {p['procedure']})"
            if p.get("sluitingsdatum"):
                tekst += f"; sluiting {nl(str(p['sluitingsdatum'])[:10])}"
            f.append(feit("aanbestedingen", afsluiten(tekst), "tenderned.json", p.get("publicatie_id"), p["datum_publicatie"][:10], p.get("link")))
        if genegeerd:
            notities.append(f"TenderNed: {genegeerd} publicatie(s) in de week overgeslagen omdat de opdrachtgever niet "
                            f"Zaanstad is (mogelijk negeert TenderNed het filter op aanbestedende dienst).")
    if not f:
        wat = "TED" if tn is None else "TED of TenderNed"
        f.append(feit("aanbestedingen", f"In de data van het dashboard staan voor deze week geen nieuwe aanbestedingspublicaties "
                                        f"van de gemeente op {wat}.", "aanbestedingen.json", soort="context"))
    return f, notities


# ── VOORUITBLIK ──────────────────────────────────────────────────────────────
def vooruitblik_feiten(tot, vandaag):
    f = []
    eind = verschuif(tot, VOORUITBLIK_DAGEN)
    komend = lambda iso: bool(iso) and tot < str(iso)[:10] <= eind

    gezien = set()
    for v in sorted(lees("vergaderingen", []) or [], key=lambda x: x.get("datum", "")):
        if komend(v.get("datum")) and (v["datum"], v.get("type")) not in gezien:
            gezien.add((v["datum"], v.get("type")))
            n = len(v.get("agendapunten") or [])
            f.append(feit("vooruitblik", f"{v.get('type') or 'Vergadering'} op {nl(v['datum'], True)} in de "
                                         f"{v.get('locatie') or 'raadzaal'} (" + (f"agenda met {n} punten" if n else "agenda nog niet gepubliceerd") + ").",
                          "vergaderingen.json", v.get("id"), v.get("datum"), v.get("url")))

    n_cam = 0
    for cam in lees("cameras_actief", []) or []:
        laatste = max((p for p in cam.get("periodes") or [] if p.get("eind")), key=lambda p: p["eind"], default=None)
        if laatste and komend(laatste.get("eind")) and n_cam < MAX_VOORUITBLIK_PER_SOORT:
            n_cam += 1
            tekst = f"Het tijdelijke cameratoezicht {cam.get('camera')} loopt af op {nl(laatste['eind'])}"
            tekst += "." if cam.get("keer_verlengd") else "; in de data van het dashboard staat nog geen verlenging."
            f.append(feit("vooruitblik", tekst, "cameras_actief.json", cam.get("camera"), laatste["eind"], laatste.get("link")))

    n_aanb = 0
    for proc in sorted(lees("aanbestedingen", []) or [], key=lambda x: x.get("sluitingsdatum") or ""):
        if proc.get("status") == "actief lopend" and komend(proc.get("sluitingsdatum")) and n_aanb < MAX_VOORUITBLIK_PER_SOORT:
            n_aanb += 1
            f.append(feit("vooruitblik", f"De inschrijving voor '{schoon(proc.get('titel'))}' sluit op {nl(proc['sluitingsdatum'][:10])}.",
                          "aanbestedingen.json", proc.get("procedure_id"), proc["sluitingsdatum"][:10]))
    return f, []


# ── NOS (bewust niet meegenomen; alleen een notitie) ─────────────────────────
def nos_notitie(van, tot):
    nos = [r for r in (lees("nos_lokaal", []) or []) if in_venster(r.get("datum"), van, tot)]
    if not nos:
        return []
    hoek = sum(1 for r in nos if r.get("heeft_lokale_hoek"))
    return [f"NOS lokaal: {len(nos)} landelijke berichten in de week, waarvan {hoek} met een door AI voorgestelde lokale "
            f"invalshoek. Niet meegenomen: het zijn geen lokale nieuwsfeiten maar suggesties voor vervolgonderzoek."]


# ── SAMENSTELLEN ─────────────────────────────────────────────────────────────
def verzamel(tot, vandaag):
    van = verschuif(tot, -4)
    feiten, notities = [], []
    for sectie, (fs, ns) in {
        "ebs": ebs_feiten(van, tot, vandaag),
        "raad": raad_feiten(van, tot),
        "college": college_feiten(van, tot),
        "besluiten": besluiten_feiten(van, tot, vandaag),
        "aanbestedingen": aanbestedingen_feiten(van, tot),
        "vooruitblik": vooruitblik_feiten(tot, vandaag),
    }.items():
        feiten += fs
        notities += ns
    notities += nos_notitie(van, tot)

    feiten.sort(key=lambda x: SECTIE_VOLGORDE.index(x["sectie"]))   # stabiel: volgorde binnen sectie blijft
    for i, x in enumerate(feiten, 1):
        x["id"] = f"F{i:02d}"
    aantallen = {s: sum(1 for x in feiten if x["sectie"] == s) for s in SECTIE_VOLGORDE}
    return {
        "versie": 1,
        "week": {"van": van, "tot": tot, "label": f"{nl(van)} t/m {nl(tot)} {d(tot).year}"},
        "gegenereerd_op": datetime.now().isoformat(timespec="seconds"),
        "feiten": feiten,
        "aantallen": aantallen,
        "notities": notities,
    }


def laatste_vrijdag(vandaag):
    x = d(vandaag)
    while x.weekday() != 4:
        x -= timedelta(days=1)
    return x.isoformat()


def toon(res):
    print(f"WEEK {res['week']['label']}   ({len(res['feiten'])} feiten)")
    print("Per sectie:", ", ".join(f"{s} {n}" for s, n in res["aantallen"].items()))
    huidig = None
    for x in res["feiten"]:
        if x["sectie"] != huidig:
            huidig = x["sectie"]
            print(f"\n── {huidig.upper()} ──")
        vlag = "  [AI-analyse]" if x["soort"] == "ai_analyse" else ("  [context]" if x["soort"] == "context" else "")
        print(f"{x['id']}  {x['tekst']}{vlag}")
        print(f"     bron: {x['bron']}" + (f" | {x['link']}" if x.get("link") else ""))
    if res["notities"]:
        print("\n── NOTITIES (niet meegenomen / let op) ──")
        for n in res["notities"]:
            print(" •", n)


def main():
    ap = argparse.ArgumentParser(description="Verzamelt de feiten voor het wekelijkse artikel (ma t/m vr).")
    ap.add_argument("--tot", help="laatste dag van de week, YYYY-MM-DD (standaard: meest recente vrijdag)")
    ap.add_argument("--vandaag", help="overschrijft 'vandaag' (alleen om te testen)")
    ap.add_argument("--uit", help="schrijf de feiten ook als JSON naar dit pad")
    a = ap.parse_args()

    vandaag = a.vandaag or date.today().isoformat()
    tot = a.tot or laatste_vrijdag(vandaag)
    if d(tot).weekday() != 4:
        print(f"Let op: {tot} is geen vrijdag ({DAGEN[d(tot).weekday()]}); de week loopt toch 5 dagen terug.", file=sys.stderr)
    res = verzamel(tot, vandaag)
    toon(res)
    if a.uit:
        Path(a.uit).parent.mkdir(parents=True, exist_ok=True)
        with open(a.uit, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"\nGeschreven naar {a.uit}")


if __name__ == "__main__":
    main()
