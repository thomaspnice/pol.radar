import json
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
import scan  # noqa: E402

NOW = format_datetime(datetime.now(timezone.utc))
FEED = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Beispiel GmbH stellt Beispielprodukt vor</title><link>https://a.de/x?utm_source=foo</link><description>Neu</description><pubDate>{NOW}</pubDate></item>
<item><title>Beispiel GmbH stellt Beispielprodukt vor!</title><link>https://b.de/y</link><description>Agenturmeldung</description><pubDate>{NOW}</pubDate></item>
<item><title>Gewinnspiel der Beispiel GmbH</title><link>https://c.de/z</link><description>Preise</description><pubDate>{NOW}</pubDate></item>
<item><title>Wetter morgen</title><link>https://d.de/w</link><description>Sonne</description><pubDate>{NOW}</pubDate></item>
</channel></rss>""".encode()

CFG = {
    "thema": "Test", "behalten_tage": 30,
    "stichworte": {"einschliessen": ["Beispiel GmbH"], "ausschliessen": ["Gewinnspiel"]},
    "feeds": [{"name": "Test", "url": "http://x/ok"}, {"name": "Kaputt", "url": "http://x/bad"}],
    "bewertung": {"ki_aktiv": False, "modell": "m", "mindest_relevanz": 40, "max_ki_pro_lauf": 5},
}


def fake_fetch(url):
    if url.endswith("bad"):
        raise RuntimeError("Timeout")
    return FEED


def test_pipeline():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.json"
        scan.lauf(CFG, out, [], ki=False, fetch=fake_fetch)
        data = json.loads(out.read_text())
        assert len(data["artikel"]) == 1, data          # Dublette + Ausschluss + Fehltreffer entfernt
        assert data["artikel"][0]["url"] == "https://a.de/x"  # Tracking entfernt
        assert len(data["fehler"]) == 1                 # kaputte Quelle gemeldet, Lauf lief weiter
        scan.lauf(CFG, out, data["artikel"], ki=False, fetch=fake_fetch)
        assert len(json.loads(out.read_text())["artikel"]) == 1  # zweiter Lauf: keine Duplikate


def test_gemini():
    class R:
        def __init__(self, code, data=None): self.status_code, self._d = code, data
        def json(self): return self._d
        def raise_for_status(self): pass
    antworten = [R(429), R(200, {"candidates": [{"content": {"parts": [{"text": '{"relevanz": 80, "tonalitaet": "positiv", "thema": "Produkt", "zusammenfassung": "Ok."}'}]}}]})]
    import os; os.environ["GEMINI_API_KEY"] = "x"
    wartezeiten = []
    txt = scan._frage_gemini("p", {"modell": "m"}, post=lambda *a, **k: antworten.pop(0), pause=wartezeiten.append)
    assert wartezeiten == [15]  # nach 429 einmal gewartet
    items = [{"titel": "t", "quelle": "q", "auszug": "a", "url": "u"}]
    cfg = {"thema": "x", "bewertung": {"anbieter": "gemini", "max_ki_pro_lauf": 5, "pause_sekunden": 0}}
    out, fehler = scan.ki_bewerten(items, cfg, frage=lambda p, b: txt)
    assert not fehler
    assert out[0]["relevanz"] == 80 and out[0]["tonalitaet"] == "positiv"


def test_ki_fehler_und_limit():
    items = [{"titel": f"t{i}", "quelle": "q", "auszug": "", "url": f"u{i}"} for i in range(5)]
    cfg = {"thema": "x", "bewertung": {"anbieter": "anthropic", "max_ki_pro_lauf": 3}}
    def f(p, b):
        if "t1" in p: raise RuntimeError("quota")
        return '{"relevanz": 50, "tonalitaet": "neutral", "thema": "a", "zusammenfassung": "b"}'
    out, fehler = scan.ki_bewerten(items, cfg, frage=f)
    assert [x["url"] for x in out] == ["u0", "u2"]  # u1 fehlgeschlagen, u3/u4 über dem Limit: nicht gespeichert
    assert len(fehler) == 1


def test_alter_und_leere_stichworte():
    alt = format_datetime(datetime(2020, 1, 1, tzinfo=timezone.utc))
    feed = FEED.replace(NOW.encode(), alt.encode(), 1)  # erster Artikel wird alt
    cfg = {**CFG, "max_alter_tage": 14, "stichworte": {"einschliessen": [], "ausschliessen": ["Gewinnspiel"]}}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.json"
        scan.lauf(cfg, out, [], ki=False, fetch=lambda u: feed if u.endswith("ok") else (_ for _ in ()).throw(RuntimeError("x")))
        titel = [a["titel"] for a in json.loads(out.read_text())["artikel"]]
        assert "Wetter morgen" in titel and not any("Gewinnspiel" in x for x in titel)
        urls = [a["url"] for a in json.loads(out.read_text())["artikel"]]
        assert "https://a.de/x" not in urls  # älter als 14 Tage: nicht aufgenommen
        assert "https://b.de/y" in urls


def test_paging():
    abgerufen = []
    def f(u):
        abgerufen.append(u)
        if "paged=3" in u: raise RuntimeError("404")
        return FEED
    cfg = {**CFG, "feeds": [{"name": "T", "url": "http://x/feed", "seiten": 5}], "google_news": []}
    items, fehler = scan.collect(cfg, f)
    assert abgerufen == ["http://x/feed", "http://x/feed?paged=2", "http://x/feed?paged=3"]
    assert not fehler  # fehlende tiefere Seite ist kein Fehler


def test_begriffe():
    m = lambda term, text: bool(scan.begriff_regex(term).search(text))
    assert m("KI", "Neue KI-Strategie") and not m("KI", "Kinder und Ski")
    assert not m("KI", "ki im Text")                 # Abkürzung: exakte Schreibweise
    assert m("OECD", "OECD-Studie") and not m("OECD", "oecdx")
    assert m("Statistik", "Die Agrarstatistik zeigt") and m("Statistik", "Statistiken")
    assert m("Registermodernisierung", "Registermodernisierungsgesetz")
    assert m("Data Act", "Der Data Act gilt") and not m("Data Act", "data actually")
    assert m("Open Data", "open data portal")
    assert m("Bundeswahlleiterin", "Die Bundeswahlleiterin sagte")


def test_excel():
    cfg = {"stichworte": {"datei": str(Path(__file__).resolve().parent.parent / "suchbegriffe.xlsx"),
                          "einschliessen": [], "ausschliessen": []}}
    b = scan.lade_begriffe(cfg)
    assert len(b) == 93 and ("Destatis", "Allgemein") in b
    items = [{"titel": "Bundeswahlleiterin legt Wahlstatistik vor", "auszug": ""},
             {"titel": "Kinder spielen Ski", "auszug": ""}]
    out = scan.keyword_filter(items, cfg)
    assert len(out) == 1 and "Bundeswahlleiterin" in out[0]["themenfelder"] and "Wahlstatistik" in out[0]["treffer"]


def test_gesehen_und_volltext():
    cfg = {**CFG, "stichworte": {"einschliessen": ["Statistik"], "ausschliessen": []},
           "feeds": [{"name": "T", "url": "http://x/ok", "volltext": True}], "google_news": [],
           "max_volltext_pro_lauf": 10}
    seite = lambda u: ("<html><nav>Statistik Menü</nav><main><p>Heute geht es um die Statistik der Löhne.</p></main>"
                       "<footer>Datenschutz</footer></html>") if "a.de" in u else "<main>Nichts Passendes</main>"
    aufrufe = []
    def s2(u):
        aufrufe.append(u); return seite(u)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.json"
        scan.lauf(cfg, out, [], ki=False, fetch=fake_fetch, seite=s2)
        daten = json.loads(out.read_text())
        assert [a["url"] for a in daten["artikel"]] == ["https://a.de/x"]   # Treffer nur im Haupttext, Menü ignoriert
        assert "text" not in daten["artikel"][0] and "_vt" not in daten["artikel"][0]
        assert "https://d.de/w" in daten["gesehen"]                        # Fehltreffer gemerkt
        n = len(aufrufe)
        scan.lauf(cfg, out, daten["artikel"], ki=False, fetch=fake_fetch, seite=s2)
        assert len(aufrufe) == n                                           # zweiter Lauf: keine erneuten Seitenabrufe


def test_ki_abgelehnt_wird_nicht_wiederholt():
    cfg = {**CFG, "bewertung": {**CFG["bewertung"], "anbieter": "gemini", "ki_aktiv": True, "pause_sekunden": 0}}
    cfg["stichworte"] = {"einschliessen": ["Beispiel GmbH"], "ausschliessen": []}
    import os; os.environ["GEMINI_API_KEY"] = "x"
    aufrufe = []
    orig = scan._frage_gemini
    scan._frage_gemini = lambda p, b: (aufrufe.append(p), '{"relevanz": 5, "tonalitaet": "neutral", "thema": "", "zusammenfassung": ""}')[1]
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "a.json"
            scan.lauf(cfg, out, [], ki=True, fetch=fake_fetch)
            assert json.loads(out.read_text())["artikel"] == []            # unter mindest_relevanz
            n = len(aufrufe); assert n >= 1
            scan.lauf(cfg, out, [], ki=True, fetch=fake_fetch)
            assert len(aufrufe) == n                                       # nicht erneut an die KI geschickt
    finally:
        scan._frage_gemini = orig


if __name__ == "__main__":
    test_gemini()
    test_ki_fehler_und_limit()
    test_alter_und_leere_stichworte()
    test_paging()
    test_begriffe()
    test_excel()
    test_gesehen_und_volltext()
    test_ki_abgelehnt_wird_nicht_wiederholt()
    test_pipeline()
    print("OK")
