#!/usr/bin/env python3
"""Medienmonitor: holt Feeds, filtert, bewertet und schreibt docs/data/articles.json."""
import argparse
import json
import os
import re
import sys
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


def collect(cfg, fetch=None):
    """Liefert Rohartikel aus allen Quellen. `fetch` erlaubt Tests ohne Netz."""
    quellen = [(f["name"], f["url"]) for f in cfg.get("feeds", [])]
    for q in cfg.get("google_news", []):
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=de&gl=DE&ceid=DE:de"
        quellen.append((f"Google News: {q.strip(chr(34))}", url))

    items, fehler = [], []
    for name, url in quellen:
        try:
            if fetch:
                raw = fetch(url)
            else:
                r = requests.get(url, headers=UA, timeout=20)
                r.raise_for_status()
                raw = r.content
            feed = feedparser.parse(raw)
            for e in feed.entries:
                link = e.get("link")
                if not link:
                    continue
                items.append({
                    "url": normalize_url(link),
                    "titel": strip_html(e.get("title", "")),
                    "auszug": strip_html(e.get("summary", ""))[:500],
                    "quelle": name,
                    "datum": entry_date(e).isoformat(),
                })
        except Exception as ex:  # eine kaputte Quelle darf den Lauf nicht stoppen
            fehler.append(f"{name}: {ex}")
    return items, fehler


def keyword_filter(items, cfg):
    st = cfg["stichworte"]
    inc = [w.lower() for w in st.get("einschliessen", [])]
    exc = [w.lower() for w in st.get("ausschliessen", [])]
    out = []
    for it in items:
        text = f"{it['titel']} {it['auszug']}".lower()
        treffer = [w for w in inc if w in text]
        if treffer and not any(w in text for w in exc):
            it["treffer"] = treffer
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


def ki_bewerten(items, cfg):
    import anthropic

    b = cfg["bewertung"]
    client = anthropic.Anthropic()
    for it in items[: b["max_ki_pro_lauf"]]:
        prompt = (
            f"Beobachtungsziel: {cfg['thema']}\n\nTitel: {it['titel']}\n"
            f"Quelle: {it['quelle']}\nAuszug: {it['auszug']}"
        )
        try:
            msg = client.messages.create(
                model=b["modell"], max_tokens=300, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            m = re.search(r"\{.*\}", msg.content[0].text, re.S)
            d = json.loads(m.group(0))
            it.update(
                relevanz=int(d["relevanz"]), tonalitaet=d.get("tonalitaet", "neutral"),
                thema=d.get("thema", ""), zusammenfassung=d.get("zusammenfassung", ""),
            )
        except Exception as ex:
            it.update(relevanz=None, tonalitaet="unbewertet", thema="", zusammenfassung="")
            print(f"KI-Bewertung fehlgeschlagen für {it['url']}: {ex}", file=sys.stderr)
    for it in items[b["max_ki_pro_lauf"]:]:  # über dem Limit: ohne KI übernehmen
        it.update(relevanz=None, tonalitaet="unbewertet", thema="", zusammenfassung="")
    return items


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


def lauf(cfg, out, bestand, ki, fetch=None):
    roh, fehler = collect(cfg, fetch)
    gefiltert = keyword_filter(roh, cfg)
    neu = dedupe(gefiltert, bestand)

    if ki and neu:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY fehlt, nutze nur Stichwortfilter.", file=sys.stderr)
            ki = False
    if ki and neu:
        neu = ki_bewerten(neu, cfg)
        mind = cfg["bewertung"]["mindest_relevanz"]
        neu = [n for n in neu if n["relevanz"] is None or n["relevanz"] >= mind]
    else:
        for n in neu:
            n.update(relevanz=None, tonalitaet="unbewertet", thema="", zusammenfassung="")

    grenze = jetzt() - timedelta(days=cfg.get("behalten_tage", 30))
    alle = [x for x in neu + bestand if datetime.fromisoformat(x["datum"]) >= grenze]
    alle.sort(key=lambda x: x["datum"], reverse=True)

    out.write_text(json.dumps({
        "aktualisiert": jetzt().isoformat(), "fehler": fehler, "artikel": alle,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(roh)} geholt, {len(gefiltert)} nach Stichwortfilter, {len(neu)} neu, "
          f"{len(alle)} gesamt, {len(fehler)} Quellenfehler")
    return 0


if __name__ == "__main__":
    sys.exit(main())
