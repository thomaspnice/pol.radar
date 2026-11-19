#!/usr/bin/env python3
"""Medienmonitor: holt Feeds, filtert, bewertet und schreibt docs/data/articles.json."""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import feedparser
import requests
import yaml
from rapidfuzz import fuzz

TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|cmpid|wt_mc)", re.I)
UA = {"User-Agent": "Medienmonitor/1.0 (+RSS-Reader)"}


def jetzt():
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING.match(k)]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def entry_date(e) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return jetzt()


def _seiten_url(url, n):
    return url if n == 1 else url + ("&" if "?" in url else "?") + f"paged={n}"


def anriss(text, saetze=2, maxlen=350):
    """Die ersten Sätze eines Textes als Kurzfassung."""
    text = re.sub(r"\s+", " ", text or "").strip()
    teile = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ„\"(])", text)
    out = " ".join(teile[:saetze])
    return out if len(out) <= maxlen else out[:maxlen].rsplit(" ", 1)[0] + " …"


def textanfang(text):
    """hib-Seiten beginnen mit Kopfzeilen; der eigentliche Text folgt nach „(hib/XXX)“."""
    m = re.search(r"\(hib/[A-Za-z]+\)\s*(.+)", text or "", re.S)
    return m.group(1) if m else (text or "")


def collect(cfg, fetch=None):
    """Liefert Rohartikel aus allen Quellen. `fetch` erlaubt Tests ohne Netz.
    Feeds mit `seiten: N` werden über WordPress-Paging (?paged=2 …) tiefer gelesen."""
    quellen = []
    for f in cfg.get("feeds", []):
        quellen.append(dict(name=f["name"], url=f["url"], seiten=f.get("seiten", 1), vt=bool(f.get("volltext")),
                            kategorie=f.get("kategorie"), organisation=f.get("organisation"), google=False))
    for q in cfg.get("google_news", []):
        d = q if isinstance(q, dict) else {"suche": q}
        url = f"https://news.google.com/rss/search?q={quote_plus(d['suche'])}&hl=de&gl=DE&ceid=DE:de"
        quellen.append(dict(name=f"Google News: {d['suche'].strip(chr(34))}", url=url, seiten=1, vt=False,
                            kategorie=d.get("kategorie"), organisation=d.get("organisation"), google=True))

    items, fehler = [], []
    for qd in quellen:
        name, url = qd["name"], qd["url"]
        for n in range(1, qd["seiten"] + 1):
            try:
                if fetch:
                    raw = fetch(_seiten_url(url, n))
                else:
                    r = requests.get(_seiten_url(url, n), headers=UA, timeout=20)
                    r.raise_for_status()
                    raw = r.content
                entries = feedparser.parse(raw).entries
                for e in entries:
                    link = e.get("link")
                    if not link:
                        continue
                    titel, org = strip_html(e.get("title", "")), qd["organisation"]
                    if qd["google"] and not org:  # Google News hängt das Medium an den Titel: „Titel - Medium“
                        m = re.match(r"^(.*\S)\s+[-–]\s+([^-–]{2,60})$", titel)
                        if m:
                            titel, org = m.group(1), m.group(2).strip()
                    items.append({
                        "url": normalize_url(link),
                        "titel": titel,
                        "auszug": strip_html(e.get("summary", ""))[:500],
                        "quelle": name,
                        "kategorie": qd["kategorie"],
                        "organisation": org,
                        "datum": entry_date(e).isoformat(),
                        "_vt": qd["vt"],
                    })
                if not entries:
                    break
            except Exception as ex:  # eine kaputte Quelle darf den Lauf nicht stoppen
                if n == 1:
                    fehler.append(f"{name}: {ex}")
                break  # tiefere Seiten nicht vorhanden: kein Fehler
    return items, fehler


def lade_begriffe(cfg):
    """Liefert [(Begriff, Themenfeld)] aus der Excel-Datei (Spalten Thema, Suchbegriff)
    und/oder der Liste `einschliessen` (Liste oder {Themenfeld: [Begriffe]})."""
    st = cfg["stichworte"]
    begriffe = []
    if st.get("datei"):
        import openpyxl

        ws = openpyxl.load_workbook(st["datei"], read_only=True).active
        for thema, term, *_ in ws.iter_rows(min_row=2, values_only=True):
            if term and str(term).strip():
                begriffe.append((str(term).strip(), str(thema or "Allgemein").strip()))
    inc = st.get("einschliessen") or []
    if isinstance(inc, dict):
        begriffe += [(t, th) for th, ts in inc.items() for t in ts]
    else:
        begriffe += [(t, "Allgemein") for t in inc]
    return begriffe


def begriff_regex(term):
    """Kurze Begriffe und Abkürzungen (KI, OECD, NOOTS): exakt als ganzes Wort, Groß-/Kleinschreibung
    beachtet. Mehrwortbegriffe: ganze Wörter (mit optionalem Plural-s). Lange Einzelwörter:
    Teilwort-Suche, damit Komposita und Beugungen (Registermodernisierungsgesetz) treffen."""
    t = term.strip()
    if t.isupper() or len(t) <= 4:
        return re.compile(rf"(?<!\w){re.escape(t)}(?!\w)")
    if " " in t:
        return re.compile(rf"(?<!\w){re.escape(t)}s?(?!\w)", re.I)
    return re.compile(re.escape(t), re.I)


def hole_text(url, get=None):
    """Lädt die Artikelseite und gibt den Haupttext zurück (ohne Navigation/Fußzeile)."""
    html = (get or (lambda u: requests.get(u, headers=UA, timeout=10).text))(url)
    m = re.search(r"<main\b.*?</main>", html, re.S | re.I) or re.search(r"<article\b.*?</article>", html, re.S | re.I)
    body = m.group(0) if m else html
    body = re.sub(r"<(script|style|nav|header|footer|aside|form)\b.*?</\1>", " ", body, flags=re.S | re.I)
    return strip_html(body)[:4000]


def keyword_filter(items, cfg):
    st = cfg["stichworte"]
    muster = [(begriff_regex(t), t, th) for t, th in lade_begriffe(cfg)]
    exc = [w.lower() for w in st.get("ausschliessen") or []]
    out = []
    for it in items:
        text = f"{it['titel']} {it['auszug']} {it.get('text', '')}"
        treffer = [(t, th) for rx, t, th in muster if rx.search(text)]
        if (treffer or not muster) and not any(w in text.lower() for w in exc):
            it["treffer"] = sorted({t for t, _ in treffer})
            it["themenfelder"] = sorted({th for _, th in treffer})
            out.append(it)
    return out


def dedupe(neu, vorhanden):
    """Entfernt identische URLs und sehr ähnliche Titel (Agenturmeldungen)."""
    urls = {a["url"] for a in vorhanden}
    titel = [a["titel"] for a in vorhanden]
    out = []
    for it in neu:
        if it["url"] in urls:
            continue
        if any(fuzz.token_set_ratio(it["titel"], t) >= 92 for t in titel):
            continue
        urls.add(it["url"])
        titel.append(it["titel"])
        out.append(it)
    return out


SYSTEM = (
    "Du bewertest Presseartikel für ein Medienmonitoring. Antworte ausschließlich mit "
    "einem JSON-Objekt: {\"relevanz\": 0-100, \"tonalitaet\": \"positiv|neutral|negativ\", "
    "\"thema\": \"1-3 Wörter\", \"zusammenfassung\": \"max. 2 Sätze, deutsch\"}. "
    "Relevanz 0 = Fehltreffer (z. B. Namensverwechslung), 100 = zentral für das Beobachtungsziel."
)


class KontingentFehler(RuntimeError):
    """Gratis-Kontingent oder Dienst erschöpft: weitere Anfragen in diesem Lauf sind sinnlos."""


def _frage_anthropic(prompt, b):
    import anthropic

    msg = anthropic.Anthropic().messages.create(
        model=b["modell"], max_tokens=300, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def _frage_gemini(prompt, b, post=requests.post, pause=time.sleep):
    """Fragt Gemini. Probiert bei „Modell unbekannt“ (404) die Ersatzmodelle aus `modell_alternativen`."""
    modelle = [m for m in [b["modell"]] + list(b.get("modell_alternativen") or []) if m not in b.setdefault("_unbekannt", set())]
    letzter_fehler = "kein Modell verfügbar"
    for modell in modelle:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modell}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"parts": [{"text": prompt}]}],
            # Großzügiges Budget: Modelle mit „Denkphase“ zählen diese zur Ausgabegrenze
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 2000},
        }
        for versuch in range(2):  # bei 429 einmal kurz warten (Minutenlimit), dann aufgeben
            r = post(url, json=body, timeout=45, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            if r.status_code in (429, 503):
                if versuch == 0:
                    pause(20)
                    continue
                raise KontingentFehler(f"Gemini-Kontingent erschöpft oder Dienst überlastet (HTTP {r.status_code})")
            break
        if r.status_code == 404:
            b["_unbekannt"].add(modell)
            letzter_fehler = f"Modell „{modell}“ unbekannt (404)"
            print(letzter_fehler + ", versuche nächstes Modell.", file=sys.stderr)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {r.status_code} bei {modell}: {str(getattr(r, 'text', ''))[:200]}")
        data = r.json()
        kand = data.get("candidates") or []
        if not kand:
            raise RuntimeError(f"Keine Antwort von {modell}: {str(data.get('promptFeedback') or data)[:200]}")
        teile = (kand[0].get("content") or {}).get("parts") or []
        text = "".join(t.get("text", "") for t in teile if not t.get("thought"))
        if not text.strip():
            raise RuntimeError(f"Leere Antwort von {modell} (finishReason={kand[0].get('finishReason')})")
        return text
    raise RuntimeError(letzter_fehler)


def ki_bewerten(items, cfg, frage=None):
    """Bewertet höchstens `max_ki_pro_lauf` Artikel, höchstens `max_ki_sekunden` lang. Bei erschöpftem
    Kontingent wird der Lauf sofort abgebrochen. Nicht bewertete Artikel werden NICHT gespeichert
    und im nächsten Lauf erneut versucht."""
    b = cfg["bewertung"]
    gemini = b.get("anbieter", "gemini") == "gemini"
    if frage is None:
        frage = _frage_gemini if gemini else _frage_anthropic
    ok, meldungen = [], []
    start = time.time()
    for it in items[: b["max_ki_pro_lauf"]]:
        if time.time() - start > b.get("max_ki_sekunden", 420):
            meldungen.append("KI-Bewertung: Zeitbudget des Laufs erreicht, Rest folgt im nächsten Lauf.")
            break
        prompt = (
            f"Beobachtungsziel: {cfg['thema']}\n\nTitel: {it['titel']}\n"
            f"Quelle: {it['quelle']}\nAuszug: {it['auszug']}\nText: {it.get('text', '')[:1500]}\n"
            f"Gefundene Suchbegriffe: {', '.join(it.get('treffer', [])) or '-'}"
        )
        try:
            m = re.search(r"\{.*\}", frage(prompt, b), re.S)
            d = json.loads(m.group(0))
            it.update(
                relevanz=int(d["relevanz"]), tonalitaet=d.get("tonalitaet", "neutral"),
                thema=d.get("thema", ""), zusammenfassung=d.get("zusammenfassung", ""),
            )
            ok.append(it)
        except KontingentFehler as ex:
            meldungen.append(f"KI-Bewertung abgebrochen: {ex}. Rest folgt im nächsten Lauf.")
            print(meldungen[-1], file=sys.stderr)
            break
        except Exception as ex:
            if len(meldungen) < 3:
                meldungen.append(f"KI-Bewertung: {str(ex)[:150]}")
            print(f"KI-Bewertung fehlgeschlagen für {it['url']}: {ex}", file=sys.stderr)
        if gemini:
            time.sleep(b.get("pause_sekunden", 4))  # Rate-Limit schonen
    return ok, meldungen


def _lade(out):
    if out.exists():
        d = json.loads(out.read_text(encoding="utf-8"))
        return d.get("artikel", []), d.get("gesehen", {}), d.get("fehler", [])
    return [], {}, []


def _speichere(out, cfg, bestand, gesehen, fehler):
    grenze = jetzt() - timedelta(days=cfg.get("behalten_tage", 30))
    gesehen = {u: d for u, d in gesehen.items() if datetime.fromisoformat(d) >= grenze}
    alle = [x for x in bestand if datetime.fromisoformat(x["datum"]) >= grenze]
    for x in alle:
        x.pop("_vt", None)
        x.pop("text", None)
    alle.sort(key=lambda x: x["datum"], reverse=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "aktualisiert": jetzt().isoformat(), "fehler": fehler, "artikel": alle, "gesehen": gesehen,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return alle


def suche(cfg, out, bestand=None, fetch=None, seite=None):
    """Phase 1 (schnell, ohne KI): Feeds holen, nach Suchbegriffen filtern, Treffer sofort speichern."""
    alt_bestand, gesehen, alt_fehler = _lade(out)
    bestand = alt_bestand if bestand is None else bestand
    roh, fehler = collect(cfg, fetch)
    gesamt_geholt = len(roh)
    if cfg.get("max_alter_tage"):
        grenze_neu = (jetzt() - timedelta(days=cfg["max_alter_tage"])).isoformat()
        roh = [x for x in roh if x["datum"] >= grenze_neu]

    # Schon bekannte Artikel (gespeichert oder bereits geprüft und aussortiert) überspringen
    bekannt = set(gesehen) | {a["url"] for a in bestand}
    frisch, urls = [], set()
    for x in roh:
        if x["url"] not in bekannt and x["url"] not in urls:
            urls.add(x["url"])
            frisch.append(x)
    roh = frisch

    # Optional: Volltext laden (z. B. hib-Titel sind sehr kurz)
    geladen = 0
    for x in roh:
        if x.get("_vt") and geladen < cfg.get("max_volltext_pro_lauf", 40):
            geladen += 1
            try:
                x["text"] = hole_text(x["url"], seite)
                if not x["auszug"]:
                    x["auszug"] = anriss(textanfang(x["text"]))
            except Exception as ex:
                if len(fehler) < 8:
                    fehler.append(f"Volltext {x['url']}: {str(ex)[:100]}")

    gefiltert = keyword_filter(roh, cfg)
    treffer_urls = {x["url"] for x in gefiltert}
    neu = dedupe(gefiltert, bestand)
    for n in neu:  # sofort sichtbar, die KI-Bewertung folgt in Phase 2
        n.update(relevanz=None, tonalitaet="unbewertet", thema="", zusammenfassung="")

    heute = jetzt().isoformat()
    neu_urls = {n["url"] for n in neu}
    for x in roh:  # ohne Suchbegriff oder Titel-Dublette: nicht noch einmal prüfen
        if x["url"] not in treffer_urls or x["url"] not in neu_urls:
            gesehen[x["url"]] = heute

    fehler += [f for f in alt_fehler if f.startswith("KI-Bewertung")]  # KI-Hinweise bleiben stehen
    alle = _speichere(out, cfg, neu + bestand, gesehen, fehler)

    pro = {}
    for x in roh:
        z = pro.setdefault(x["quelle"], [0, 0])
        z[0] += 1
        z[1] += x["url"] in treffer_urls
    for name, (n, t) in pro.items():
        print(f"  {name}: {n} neue Einträge, {t} mit Suchbegriff")
    if roh and not gefiltert:
        print("  Beispiele ohne Treffer:")
        for x in roh[:6]:
            print(f"    - {x['quelle']} | {x['titel'][:70]} | {(x.get('text') or x['auszug'])[:70]!r}")
    print(f"SUCHE: {gesamt_geholt} geholt, {len(roh)} noch nicht geprüft, {len(gefiltert)} nach Stichwortfilter, "
          f"{len(neu)} neu gespeichert, {len(alle)} gesamt, {len(fehler)} Meldungen")
    return 0


def bewerten(cfg, out, bestand=None):
    """Phase 2 (kann dauern): gespeicherte, noch unbewertete Artikel von der KI bewerten lassen.
    Artikel unter `mindest_relevanz` werden entfernt. Schlägt die KI fehl, bleibt Phase 1 erhalten."""
    alt_bestand, gesehen, alt_fehler = _lade(out)
    bestand = alt_bestand if bestand is None else bestand
    nachholen = [a for a in bestand if a.get("tonalitaet") == "unbewertet"]
    fehler = [f for f in alt_fehler if not f.startswith("KI-Bewertung")]
    if not nachholen:
        print("BEWERTUNG: nichts zu bewerten")
        _speichere(out, cfg, bestand, gesehen, fehler)
        return 0
    var = "GEMINI_API_KEY" if cfg["bewertung"].get("anbieter", "gemini") == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(var):
        print(f"{var} fehlt, KI-Bewertung übersprungen.", file=sys.stderr)
        fehler.append(f"KI-Bewertung: {var} fehlt (Secret in GitHub anlegen und in scan.yml unter env eintragen).")
        _speichere(out, cfg, bestand, gesehen, fehler)
        return 0
    warteschlange = sorted(nachholen, key=lambda x: x["datum"], reverse=True)
    bewertet, ki_fehler = ki_bewerten(warteschlange, cfg)
    fehler += ki_fehler
    mind = cfg["bewertung"]["mindest_relevanz"]
    abgelehnt = {x["url"] for x in bewertet if x["relevanz"] < mind}
    heute = jetzt().isoformat()
    for x in bewertet:
        gesehen[x["url"]] = heute
    bestand = [a for a in bestand if a["url"] not in abgelehnt]
    _speichere(out, cfg, bestand, gesehen, fehler)
    print(f"BEWERTUNG: {len(bewertet)} von {len(nachholen)} bewertet, {len(abgelehnt)} unter Schwelle entfernt, "
          f"{len(nachholen) - len(bewertet)} offen, {len(fehler)} Meldungen")
    return 0


def lauf(cfg, out, bestand, ki, fetch=None, seite=None):
    suche(cfg, out, bestand, fetch, seite)
    return bewerten(cfg, out) if ki else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--out", default="docs/data/articles.json")
    ap.add_argument("--phase", choices=["suche", "bewertung", "beide"], default="beide")
    ap.add_argument("--ohne-ki", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    out = Path(a.out)
    ki = cfg["bewertung"]["ki_aktiv"] and not a.ohne_ki
    if a.phase in ("suche", "beide"):
        suche(cfg, out)
    if a.phase in ("bewertung", "beide") and ki:
        bewerten(cfg, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
