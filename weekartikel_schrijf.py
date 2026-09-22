#!/usr/bin/env python3
"""
weekartikel_schrijf.py — stap 2: het wekelijkse artikel schrijven en controleren
================================================================================
Neemt de feitenlijst uit weekartikel_feiten.py, laat Gemini daar een
nieuwsartikel (ongeveer 600-800 woorden) van schrijven en publiceert het
ALLEEN als het alle controles doorstaat. Faalt een controle, dan krijgt Gemini
de fouten terug en probeert het opnieuw (max. 3 pogingen); lukt het dan nog
niet, dan wordt er niets weggeschreven en stopt het script met exit-code 1
(de workflow toont dan een rode run).

Gemini krijgt alleen de feitenlijst te zien, nooit de ruwe data. De
Gemini-aanroep zelf loopt via de bestaande client in scrape_collegeberichten.py
(zelfde modelketen, retries en foutafhandeling; sleutel uit GEMINI_API_KEY).

CONTROLES (allemaal automatisch)
  * Structuur: titel + alinea's, elke alinea met tekst en minstens één feitnummer.
  * Feitnummers bestaan.
  * Getallen: elk getal (cijfers én telwoorden als "drie") in de tekst moet in
    een feit staan. Staat het wel in een feit maar niet in een feit dat de
    alinea noemt, dan wordt de verwijzing automatisch aangevuld.
  * Weekdag + datum kloppen ("vrijdag 25 september" is inderdaad een vrijdag).
  * Geen eigen berekeningen als "de helft" of "verdubbeld" (tenzij een feit
    het zelf zegt).
  * Alinea's over AI-beweringen (soort "ai_analyse") noemen dat het om een
    automatische analyse gaat.
  * Geen opmaak (markdown/HTML) in de tekst.
  * Lengte: doel 600-800 woorden (350-550 bij weinig nieuws); binnen 75%-120%
    van dat doel wordt geaccepteerd ("ongeveer"), daarbuiten afgekeurd.

Het resultaat is altijd een CONCEPT (status "concept"): het is automatisch
samengesteld en bedoeld om door een redacteur te worden nagelopen.

Gebruik (vanuit de hoofdmap van de repo):
    python3 weekartikel_schrijf.py --droog                 # toont alleen de prompt, geen API-aanroep
    python3 weekartikel_schrijf.py --tot 2026-09-18        # schrijft het artikel voor die week
    python3 weekartikel_schrijf.py --tot 2026-09-18 --forceer    # overschrijft een bestaand artikel

    --feiten PAD   gebruik een eerder opgeslagen feitenlijst (JSON van weekartikel_feiten.py --uit)
    --uit-dir MAP  standaard data/weekartikelen
Een bestaand artikel wordt niet overschreven zonder --forceer, zodat een
handmatig bewerkt artikel nooit per ongeluk verdwijnt.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import weekartikel_feiten as wf

MAP_STANDAARD = Path(__file__).resolve().parent / "data" / "weekartikelen"

DOEL_NORMAAL = (600, 800)
DOEL_DUN = (350, 550)
DREMPEL_DUN = 12          # minder inhoudelijke feiten dan dit → korter artikel
MIN_INHOUDELIJK = 4       # minder dan dit → geen artikel
TOLERANTIE_ONDER = 0.75   # accepteer vanaf 75% van de ondergrens
TOLERANTIE_BOVEN = 1.20   # ... tot 120% van de bovengrens
MAX_POGINGEN = 4

TELWOORDEN = {"twee": 2, "drie": 3, "vier": 4, "vijf": 5, "zes": 6, "zeven": 7, "acht": 8, "negen": 9, "tien": 10,
              "elf": 11, "twaalf": 12, "dertien": 13, "veertien": 14, "vijftien": 15, "zestien": 16, "zeventien": 17,
              "achttien": 18, "negentien": 19, "twintig": 20, "honderd": 100, "duizend": 1000}
EIGEN_REKENWERK = r"\b(helft|kwart|verdubbel\w*|verdubbeling|gehalveerd\w*|halveer\w*|tweemaal|driemaal|dubbel zo)\b"
WEEKDAG_DATUM = re.compile(r"\b(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\s+(\d{1,2})\s+(" +
                           "|".join(wf.MAANDEN) + r")\b", re.IGNORECASE)


# ── PROMPT ───────────────────────────────────────────────────────────────────
def inhoudelijke_feiten(res):
    return [x for x in res["feiten"] if x["soort"] != "context" and x["sectie"] != "vooruitblik"]


def kies_doel(res):
    return DOEL_NORMAAL if len(inhoudelijke_feiten(res)) >= DREMPEL_DUN else DOEL_DUN


def maak_prompt(res, doel, fouten=None):
    regels = "\n".join(f"{x['id']} [{x['sectie']}, {x['soort']}] {x['tekst']}" for x in res["feiten"])
    prompt = f"""Je bent verslaggever bij een regionale nieuwssite voor de Zaanstreek en schrijft het wekelijkse overzichtsartikel over gemeente Zaanstad, uitsluitend op basis van de feitenlijst hieronder. De feiten komen uit een lokaal dashboard (raadsdata, EBS-busuitval, aanbestedingen, camerabesluiten).

WEEK: {res['week']['label']}

REGELS (streng):
1. Gebruik ALLEEN informatie uit de feitenlijst. Geen voorkennis, geen aannames, geen oorzaken of verklaringen, geen citaten, geen namen of getallen die er niet staan.
2. Getallen neem je letterlijk over, in dezelfde notatie als in het feit (bijvoorbeeld 13.648 en 3,4%). Niet afronden, niet zelf optellen, aftrekken of percentages uitrekenen, en geen eigen berekeningen als "de helft" of "verdubbeld". Een vergelijking mag alleen als een feit die zelf maakt.
3. Elke alinea heeft een lijst "feiten" met de nummers van álle feiten waarop die alinea steunt (bijvoorbeeld ["F02", "F03"]).
4. Feiten met soort "ai_analyse" zijn beweringen uit een collegebrief die een automatische analyse als controlewaardig aanmerkte. Presenteer ze nooit als vaststaand: schrijf uitdrukkelijk dat het gaat om een bewering uit de brief die volgens de automatische analyse van het dashboard gecontroleerd moet worden, en gebruik daarbij het woord "analyse".
5. Feiten met soort "context" gebruik je waar ze relevant zijn: vermeld bij EBS-cijfers kort dat het gaat om de 12 gevolgde haltes en om uitgevallen ritten (geen vertragingen). Een "geen ... in de data"-feit gebruik je om eerlijk te zeggen dat er iets ontbrak in de data van het dashboard, zonder te suggereren dat er in werkelijkheid niets is gebeurd.
6. Feiten uit sectie "vooruitblik" horen in de slotalinea's.
7. Schrijf een nieuwsartikel in helder, neutraal Nederlands voor een lokale lezer: een sterke kop, een lead van twee zinnen met het belangrijkste nieuws, daarna alinea's met korte tussenkoppen (maximaal vier) en een slot met vooruitblik. Geen opsommingstekens, geen opmaak (geen markdown of HTML), geen "ik".
8. Lengte: ongeveer {doel[0]}-{doel[1]} woorden. Is er weinig nieuws, zeg dat dan eerlijk en werk de aanwezige feiten grondiger uit; vul nooit op met algemeenheden of achtergrond die niet in de feiten staat.
9. Weekdagen en datums schrijf je zoals in de feiten.

UITVOER: alleen geldige JSON, precies in dit formaat (de eerste alinea heeft "kop": null):
{{"titel": "...", "alineas": [{{"kop": null, "tekst": "...", "feiten": ["F01", "F02"]}}, {{"kop": "Korte tussenkop", "tekst": "...", "feiten": ["F03"]}}]}}

FEITENLIJST:
{regels}
"""
    if fouten:
        prompt += ("\nJE VORIGE POGING WERD AFGEKEURD OMDAT:\n" + "\n".join(f"- {f}" for f in fouten) +
                   "\nSchrijf het hele artikel opnieuw en los deze fouten op.\n")
    return prompt


# ── GEMINI (via de bestaande client) ─────────────────────────────────────────
AI_MAX_TOKENS_ARTIKEL = 24576   # ruimer dan scrape_collegeberichten.py's 8192: een heel artikel
                                # is een langer antwoord dan een claims-JSON, en bij Gemini 3.x
                                # tellen "denktokens" mee in ditzelfde budget


def gemini_schrijf(prompt):
    """Geeft (tekst, modelnaam). Gebruikt de client uit scrape_collegeberichten.py (modelketen + retries)."""
    import scrape_collegeberichten as sc
    sleutel = os.environ.get("GEMINI_API_KEY", "").strip()
    if not sleutel:
        raise RuntimeError("GEMINI_API_KEY ontbreekt in de omgeving")
    if not sc._GEM["keten"]:
        if not sc.init_gemini(sleutel):
            raise RuntimeError(sc._GEM["uitgeschakeld"] or "Gemini niet beschikbaar")
    origineel = sc.AI_MAX_TOKENS
    sc.AI_MAX_TOKENS = AI_MAX_TOKENS_ARTIKEL
    try:
        return sc.gemini_genereer(prompt, sleutel)
    finally:
        sc.AI_MAX_TOKENS = origineel


def parse_json(tekst):
    t = tekst.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i:j + 1])
        raise


# ── CONTROLES ────────────────────────────────────────────────────────────────
def norm_getal(s):
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        return float(s.replace(".", ""))
    return float(s.replace(",", "."))


def getallen_cijfers(tekst):
    """Getallen in cijfers: 13.648, 3,4, 26 (uit 26M52)."""
    return {norm_getal(m) for m in re.findall(r"\d+(?:[.,]\d+)*", tekst)}


def getallen_woorden(tekst):
    """Telwoorden: twee, drie, ... twintig, honderd, duizend."""
    return {float(n) for woord, n in TELWOORDEN.items() if re.search(rf"\b{woord}\b", tekst, re.IGNORECASE)}


def getallen(tekst):
    return getallen_cijfers(tekst) | getallen_woorden(tekst)


def alle_tekst(art):
    return [art["titel"]] + [f"{a.get('kop') or ''} {a['tekst']}".strip() for a in art["alineas"]]


def tel_woorden(art):
    return len(re.findall(r"\S+", " ".join(alle_tekst(art))))


def controleer(art, res, doel):
    """Geeft (fouten, waarschuwingen, aangevuld). Muteert art['alineas'][i]['feiten'] bij aanvullen."""
    fouten, waarschuwingen, aangevuld = [], [], []
    feiten = {x["id"]: x for x in res["feiten"]}

    # 1. structuur
    if not isinstance(art, dict) or not isinstance(art.get("titel"), str) or not art["titel"].strip():
        return ["Geen geldige 'titel'."], [], []
    if len(art["titel"]) > 120:
        fouten.append("De titel is langer dan 120 tekens.")
    al = art.get("alineas")
    if not isinstance(al, list) or len(al) < 3:
        return fouten + ["'alineas' moet een lijst met minstens 3 alinea's zijn."], [], []
    for i, a in enumerate(al, 1):
        if not isinstance(a, dict) or not isinstance(a.get("tekst"), str) or not a["tekst"].strip():
            return fouten + [f"Alinea {i} heeft geen 'tekst'."], [], []
        if not isinstance(a.get("feiten"), list) or not a["feiten"] or not all(isinstance(f, str) for f in a["feiten"]):
            fouten.append(f"Alinea {i} noemt geen feitnummers in 'feiten'.")
            a["feiten"] = a["feiten"] if isinstance(a.get("feiten"), list) else []
        if a.get("kop") is not None and not isinstance(a["kop"], str):
            fouten.append(f"Alinea {i}: 'kop' moet tekst of null zijn.")
    if fouten:
        return fouten, [], []

    # 2. opmaak
    for i, a in enumerate(al, 1):
        if re.search(r"[<>]|\*\*|^#|\n\s*[-*•] ", a["tekst"]) or re.search(r"[<>]", (a.get("kop") or "")):
            fouten.append(f"Alinea {i} bevat opmaak (markdown/HTML of opsommingstekens); gebruik gewone tekst.")

    # 3. feitnummers bestaan
    for i, a in enumerate(al, 1):
        onbekend = [f for f in a["feiten"] if f not in feiten]
        if onbekend:
            fouten.append(f"Alinea {i} noemt feitnummers die niet bestaan: {', '.join(onbekend)}.")
    if fouten:
        return fouten, [], []

    # 4. getallen (met automatische aanvulling van verwijzingen)
    label_getallen = getallen(res["week"]["label"])
    per_feit = {fid: getallen(x["tekst"]) for fid, x in feiten.items()}
    alle_feit_getallen = set().union(*per_feit.values()) if per_feit else set()
    alle_feit_tekst = " ".join(x["tekst"] for x in feiten.values()).lower()
    titel_onbekend = sorted(g for g in getallen(art["titel"]) if g not in alle_feit_getallen and g not in label_getallen)
    if titel_onbekend:
        fouten.append("De titel bevat getallen die in geen enkel feit staan: " + ", ".join(fmt_g(g) for g in titel_onbekend) + ".")
    for i, a in enumerate(al, 1):
        tekst = f"{a.get('kop') or ''} {a['tekst']}"
        gedekt = set(label_getallen)
        for fid in a["feiten"]:
            gedekt |= per_feit[fid]
        inhoudelijk_geciteerd = [feiten[fid] for fid in a["feiten"] if feiten[fid]["soort"] != "context"]
        per_sectie_geciteerd = {}
        for x in inhoudelijk_geciteerd:
            per_sectie_geciteerd[x["sectie"]] = per_sectie_geciteerd.get(x["sectie"], 0) + 1
        toegestane_tellingen = {len(inhoudelijk_geciteerd)} | set(per_sectie_geciteerd.values())
        for g in sorted(getallen_woorden(tekst) - gedekt):
            if g in toegestane_tellingen:
                continue   # bv. "twee brieven" bij precies twee geciteerde (niet-context) feiten: een natuurlijke telling, geen verzonnen rekenwerk
            fouten.append(f"Alinea {i}: het telwoord voor {fmt_g(g)} staat niet in de feiten die je bij deze alinea noemt en komt ook niet "
                          f"overeen met het aantal geciteerde feiten. Tel niet zelf items op die niet als zodanig in de feiten staan.")
        for g in sorted(getallen_cijfers(tekst)):
            if g in gedekt:
                continue
            bron = [fid for fid, gs in per_feit.items() if g in gs]
            if not bron:
                fouten.append(f"Alinea {i}: het getal {fmt_g(g)} staat in geen enkel feit. Gebruik alleen getallen uit de feitenlijst, "
                              f"letterlijk overgenomen (niet afgerond of zelf berekend).")
            else:
                kies = next((f for f in bron if feiten[f]["soort"] != "context"), bron[0])
                a["feiten"].append(kies)
                gedekt |= per_feit[kies]
                aangevuld.append(f"alinea {i}: {kies} toegevoegd voor het getal {fmt_g(g)}")
        # eigen rekenwerk
        for m in re.finditer(EIGEN_REKENWERK, tekst, re.IGNORECASE):
            if m.group(0).lower() not in alle_feit_tekst:
                fouten.append(f"Alinea {i}: '{m.group(0)}' is een eigen berekening/vergelijking die niet in de feiten staat.")

    # 5. weekdag + datum kloppen. Alleen toetsen tegen jaren die in de feiten zelf voorkomen
    # (i.p.v. blind jaar±1), anders kan een fout toevallig kloppen in een ander jaar.
    jaren = {d_jaar(x["datum"]) for x in feiten.values() if x.get("datum")} | {d_jaar(res["week"]["van"]), d_jaar(res["week"]["tot"])}
    for i, tekst in enumerate(alle_tekst(art)):
        for m in WEEKDAG_DATUM.finditer(tekst):
            dag, nr, maand = m.group(1).lower(), int(m.group(2)), wf.MAANDEN.index(m.group(3).lower()) + 1
            geldig = False
            for j in jaren:
                try:
                    geldig = geldig or wf.DAGEN[date(j, maand, nr).weekday()] == dag
                except ValueError:
                    pass
            if not geldig:
                waar = "titel" if i == 0 else f"alinea {i}"
                fouten.append(f"In de {waar} staat '{m.group(0)}', maar die datum valt niet op die weekdag.")

    # 6. AI-beweringen moeten als analyse worden gepresenteerd
    for i, a in enumerate(al, 1):
        if any(feiten[f]["soort"] == "ai_analyse" for f in a["feiten"]) and "analyse" not in a["tekst"].lower():
            fouten.append(f"Alinea {i} gebruikt een feit met soort 'ai_analyse' maar noemt niet dat het om een automatische "
                          f"analyse van het dashboard gaat (gebruik het woord 'analyse').")

    # 7. lengte
    n = tel_woorden(art)
    laag, hoog = doel
    if n < laag * TOLERANTIE_ONDER:
        fouten.append(f"Het artikel is te kort ({n} woorden; doel ongeveer {laag}-{hoog}). Werk de feiten uitgebreider uit, zonder iets toe te voegen.")
    elif n > hoog * TOLERANTIE_BOVEN:
        fouten.append(f"Het artikel is te lang ({n} woorden; doel ongeveer {laag}-{hoog}).")
    elif not (laag <= n <= hoog):
        waarschuwingen.append(f"Lengte {n} woorden ligt net buiten het doel van {laag}-{hoog}, maar binnen de marge.")

    # 8. ongebruikte belangrijke feiten (alleen een waarschuwing)
    if not any(feiten[f]["sectie"] == "vooruitblik" for a in al for f in a["feiten"]) and any(x["sectie"] == "vooruitblik" for x in res["feiten"]):
        waarschuwingen.append("Het artikel gebruikt geen enkel vooruitblik-feit.")
    return fouten, waarschuwingen, aangevuld


def d_jaar(iso):
    return date.fromisoformat(iso).year


def fmt_g(g):
    return str(int(g)) if float(g).is_integer() else str(g).replace(".", ",")


# ── WEGSCHRIJVEN ─────────────────────────────────────────────────────────────
def schrijf_weg(art, res, doel, model, pogingen, waarschuwingen, aangevuld, map_):
    map_.mkdir(parents=True, exist_ok=True)
    tot = res["week"]["tot"]
    n = tel_woorden(art)
    uit = {
        "versie": 1,
        "status": "concept",
        "week": res["week"],
        "titel": art["titel"].strip(),
        "alineas": [{"kop": (a.get("kop") or None), "tekst": a["tekst"].strip(), "feiten": list(dict.fromkeys(a["feiten"]))}
                    for a in art["alineas"]],
        "woorden": n,
        "doel_woorden": list(doel),
        "feiten": res["feiten"],
        "notities": res["notities"],
        "gegenereerd_op": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "controles": {"pogingen": pogingen, "waarschuwingen": waarschuwingen, "aangevulde_verwijzingen": aangevuld},
    }
    pad = map_ / f"{tot}.json"
    pad.write_text(json.dumps(uit, ensure_ascii=False, indent=2), encoding="utf-8")

    idx_pad = map_ / "index.json"
    index = json.loads(idx_pad.read_text(encoding="utf-8")) if idx_pad.exists() else []
    index = [e for e in index if e.get("datum") != tot]
    index.append({"datum": tot, "week": res["week"]["label"], "titel": uit["titel"], "woorden": n,
                  "bestand": pad.name, "gegenereerd_op": uit["gegenereerd_op"], "status": "concept"})
    index.sort(key=lambda e: e["datum"], reverse=True)
    idx_pad.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return pad


def toon_artikel(art):
    print("\n" + "=" * 72)
    print(art["titel"].upper())
    for a in art["alineas"]:
        print()
        if a.get("kop"):
            print(a["kop"])
        print(a["tekst"] + f"   [{', '.join(a['feiten'])}]")
    print("=" * 72)


# ── HOOFDPROGRAMMA ───────────────────────────────────────────────────────────
def schrijf(res, map_, forceer=False, gemini=None):
    """Kernlogica (los van de CLI, zodat hij te testen is). Geeft (status, pad|None)."""
    gemini = gemini or gemini_schrijf
    tot = res["week"]["tot"]
    pad = map_ / f"{tot}.json"
    n_inh = len(inhoudelijke_feiten(res))
    doel = kies_doel(res)
    print(f"Week {res['week']['label']}: {len(res['feiten'])} feiten, waarvan {n_inh} inhoudelijk (doel {doel[0]}-{doel[1]} woorden).")
    if n_inh < MIN_INHOUDELIJK:
        print(f"Te weinig inhoudelijke feiten (minder dan {MIN_INHOUDELIJK}): geen artikel deze week.")
        return "overgeslagen", None
    if pad.exists() and not forceer:
        print(f"{pad} bestaat al; niet overschreven (gebruik --forceer om opnieuw te schrijven).")
        return "bestaat", pad

    fouten = None
    for poging in range(1, MAX_POGINGEN + 1):
        print(f"Poging {poging}/{MAX_POGINGEN}: Gemini schrijft...", flush=True)
        tekst, model = gemini(maak_prompt(res, doel, fouten))
        try:
            art = parse_json(tekst)
        except ValueError as e:
            fouten = [f"Je antwoord was geen geldige JSON ({e}). Geef alleen het JSON-object."]
            print("  ✗ " + fouten[0])
            print(f"    (model {model}, {len(tekst)} tekens ontvangen; fragment: {tekst[:200]!r} ... {tekst[-200:]!r})")
            continue
        fouten, waarschuwingen, aangevuld = controleer(art, res, doel)
        if not fouten:
            p = schrijf_weg(art, res, doel, model, poging, waarschuwingen, aangevuld, map_)
            print(f"  ✓ goedgekeurd ({tel_woorden(art)} woorden, model {model}) → {p}")
            for w in waarschuwingen:
                print("  ⚠ " + w)
            for a_ in aangevuld:
                print("  + verwijzing aangevuld: " + a_)
            toon_artikel(art)
            return "geschreven", p
        print(f"  ✗ afgekeurd (model {model}):")
        for f_ in fouten:
            print("    - " + f_)
    print(f"\nMislukt na {MAX_POGINGEN} pogingen; er is niets weggeschreven.")
    return "mislukt", None


def main():
    ap = argparse.ArgumentParser(description="Schrijft het wekelijkse artikel (stap 2).")
    ap.add_argument("--tot", help="laatste dag van de week (vrijdag), YYYY-MM-DD; standaard de meest recente vrijdag")
    ap.add_argument("--vandaag", help="overschrijft 'vandaag' (alleen om te testen)")
    ap.add_argument("--feiten", help="gebruik een eerder opgeslagen feitenlijst (JSON)")
    ap.add_argument("--uit-dir", default=str(MAP_STANDAARD))
    ap.add_argument("--droog", action="store_true", help="toon alleen de prompt; geen API-aanroep, niets wegschrijven")
    ap.add_argument("--forceer", action="store_true", help="overschrijf een bestaand artikel")
    a = ap.parse_args()

    if a.feiten:
        res = json.loads(Path(a.feiten).read_text(encoding="utf-8"))
    else:
        vandaag = a.vandaag or date.today().isoformat()
        res = wf.verzamel(a.tot or wf.laatste_vrijdag(vandaag), vandaag)

    if a.droog:
        doel = kies_doel(res)
        print(maak_prompt(res, doel))
        print(f"\n[droog] {len(res['feiten'])} feiten, {len(inhoudelijke_feiten(res))} inhoudelijk, doel {doel[0]}-{doel[1]} woorden, "
              f"prompt {len(maak_prompt(res, doel))} tekens.")
        return 0

    try:
        status, _ = schrijf(res, Path(a.uit_dir), a.forceer)
    except Exception as e:
        print(f"✗ Fout: {e}", file=sys.stderr)
        return 1
    return 1 if status == "mislukt" else 0


if __name__ == "__main__":
    sys.exit(main())
