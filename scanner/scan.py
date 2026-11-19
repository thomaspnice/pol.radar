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


def collect(cfg, fetch=None):
    """Liefert Rohartikel aus allen Quellen. `fetch` erlaubt Tests ohne Netz.
    Feeds mit `seiten: N` werden über WordPress-Paging (?paged=2 …) tiefer gelesen."""
    quellen = [(f["name"], f["url"], f.get("seiten", 1), bool(f.get("volltext"))) for f in cfg.get("feeds", [])]
    for q in cfg.get("google_news", []):
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=de&gl=DE&ceid=DE:de"
        quellen.append((f"Google News: {q.strip(chr(34))}", url, 1, False))

    items, fehler = [], []
    for name, url, seiten, vt in quellen:
        for n in range(1, seiten + 1):
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
                    items.append({
                        "url": normalize_url(link),
                        "titel": strip_html(e.get("title", "")),
                        "auszug": strip_html(e.get("summary", ""))[:500],
                        "quelle": name,
                        "datum": entry_date(e).isoformat(),
                        "_vt": vt,
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
    html = (get or (lambda u: requests.get(u, headers=UA, timeout=15).text))(url)
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


def _frage_anthropic(prompt, b):
    import anthropic

    msg = anthropic.Anthropic().messages.create(
        model=b["modell"], max_tokens=300, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def _frage_gemini(prompt, b, post=requests.post, pause=time.sleep):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{b['modell']}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 400},
    }
    for versuch in range(4):  # Gratis-Kontingent: bei 429 warten und erneut versuchen
        r = post(url, json=body, timeout=60,
                 headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        if r.status_code in (429, 503):
            pause(15 * (versuch + 1))
            continue
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError("Kontingent erschöpft oder Dienst nicht erreichbar")


def ki_bewerten(items, cfg, frage=None):
    """Bewertet höchstens `max_ki_pro_lauf` Artikel. Nicht bewertete oder fehlgeschlagene
    Artikel werden NICHT gespeichert und im nächsten Lauf erneut versucht."""
    b = cfg["bewertung"]
    gemini = b.get("anbieter", "gemini") == "gemini"
    if frage is None:
        frage = _frage_gemini if gemini else _frage_anthropic
    ok, meldungen = [], []
    for it in items[: b["max_ki_pro_lauf"]]:
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
        except Exception as ex:
            if len(meldungen) < 3:
                meldungen.append(f"KI-Bewertung: {str(ex)[:150]}")
            print(f"KI-Bewertung fehlgeschlagen für {it['url']}: {ex}", file=sys.stderr)
        if gemini:
            time.sleep(b.get("pause_sekunden", 4))  # Rate-Limit schonen
    return ok, meldungen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--out", default="docs/data/articles.json")
    ap.add_argument("--ohne-ki", action="store_true")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    bestand = json.loads(out.read_text(encoding="utf-8"))["artikel"] if out.exists() else []

    return lauf(cfg, out, bestand, ki=cfg["bewertung"]["ki_aktiv"] and not a.ohne_ki)


def lauf(cfg, out, bestand, ki, fetch=None, seite=None):
    roh, fehler = collect(cfg, fetch)
    gesamt_geholt = len(roh)
    if cfg.get("max_alter_tage"):
        grenze_neu = (jetzt() - timedelta(days=cfg["max_alter_tage"])).isoformat()
        roh = [x for x in roh if x["datum"] >= grenze_neu]

    # Schon bekannte Artikel (gespeichert oder bereits geprüft und aussortiert) überspringen
    gesehen = {}
    if out.exists():
        gesehen = json.loads(out.read_text(encoding="utf-8")).get("gesehen", {})
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
        if x.get("_vt") and geladen < cfg.get("max_volltext_pro_lauf", 80):
            geladen += 1
            try:
                x["text"] = hole_text(x["url"], seite)
            except Exception as ex:
                if len(fehler) < 8:
                    fehler.append(f"Volltext {x['url']}: {str(ex)[:100]}")

    gefiltert = keyword_filter(roh, cfg)
    treffer_urls = {x["url"] for x in gefiltert}
    kandidaten = dedupe(gefiltert, bestand)
    neu = kandidaten
    bewertet_urls = set()  # Artikel, die endgültig geprüft sind (nicht erneut prüfen)

    if ki and neu and not os.environ.get(
            "GEMINI_API_KEY" if cfg["bewertung"].get("anbieter", "gemini") == "gemini" else "ANTHROPIC_API_KEY"):
        print("API-Schlüssel fehlt, nutze nur Stichwortfilter.", file=sys.stderr)
        ki = False
    if ki and neu:
        bewertet, ki_fehler = ki_bewerten(neu, cfg)
        fehler += ki_fehler
        bewertet_urls = {x["url"] for x in bewertet}
        mind = cfg["bewertung"]["mindest_relevanz"]
        neu = [n for n in bewertet if n["relevanz"] is None or n["relevanz"] >= mind]
    else:
        for n in neu:
            n.update(relevanz=None, tonalitaet="unbewertet", thema="", zusammenfassung="")
            bewertet_urls.add(n["url"])

    # Als geprüft merken: Stichwort-Fehltreffer, Titel-Dubletten und bewertete Artikel.
    # Nicht gemerkt werden Artikel, deren KI-Bewertung fehlschlug oder wegen des Limits aufgeschoben
    # wurde: Sie werden im nächsten Lauf erneut versucht.
    heute = jetzt().isoformat()
    kandidaten_urls = {n["url"] for n in kandidaten}
    for x in roh:
        if x["url"] not in treffer_urls or x["url"] not in kandidaten_urls:
            gesehen[x["url"]] = heute
    for u in bewertet_urls:
        gesehen[u] = heute

    grenze = jetzt() - timedelta(days=cfg.get("behalten_tage", 30))
    gesehen = {u: d for u, d in gesehen.items() if datetime.fromisoformat(d) >= grenze}
    for n in neu:
        n.pop("text", None)
    alle = [x for x in neu + bestand if datetime.fromisoformat(x["datum"]) >= grenze]
    for x in alle:
        x.pop("_vt", None)
    alle.sort(key=lambda x: x["datum"], reverse=True)

    out.write_text(json.dumps({
        "aktualisiert": jetzt().isoformat(), "fehler": fehler, "artikel": alle, "gesehen": gesehen,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # Diagnose je Quelle
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
    print(f"{gesamt_geholt} geholt, {len(roh)} noch nicht geprüft, {len(gefiltert)} nach Stichwortfilter, "
          f"{len(neu)} neu gespeichert, {len(alle)} gesamt, {len(fehler)} Meldungen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
