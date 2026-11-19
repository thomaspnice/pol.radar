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


if __name__ == "__main__":
    test_pipeline()
    print("OK")
