# Medienmonitor

Holt regelmäßig RSS-Feeds und Google-News-Suchen, filtert nach Stichwörtern, entfernt Dubletten,
lässt die Treffer optional von einer KI bewerten (Relevanz, Tonalität, Zusammenfassung) und
veröffentlicht alles als durchsuchbares Dashboard auf GitHub Pages. Läuft kostenlos in GitHub Actions.

## Einrichtung (ca. 10 Minuten)

1. **Repository anlegen** auf github.com (privat oder öffentlich) und den Inhalt dieses Ordners hochladen.
   > GitHub Pages ist bei privaten Repositories nur mit bezahltem Plan verfügbar.
   > Das Dashboard enthält Ihre Artikelliste; bei Bedarf Repository öffentlich lassen und nichts Vertrauliches konfigurieren.
2. **`config.yml` und `suchbegriffe.xlsx` anpassen**: Beobachtungsziel (`thema`), Feeds. Die Suchbegriffe stehen in `suchbegriffe.xlsx` (Spalte A Themenfeld, Spalte B Begriff, Zeile 1 ist die Überschrift).
3. **API-Schlüssel hinterlegen** (nur für die KI-Bewertung):
   - Gemini (Standard, Gratis-Kontingent): Schlüssel in **aistudio.google.com** unter „Get API key“ erstellen.
   - Eintragen unter Settings → Secrets and variables → Actions → New repository secret → Name `GEMINI_API_KEY`.
   - Alternativ `anbieter: anthropic` in `config.yml` und Secret `ANTHROPIC_API_KEY`.
   Ohne Schlüssel läuft das Tool nur mit dem Stichwortfilter.
   > Im Gratis-Kontingent von Google dürfen Anfragen zur Produktverbesserung genutzt werden. Das Tool sendet nur Titel, Quelle und Auszug öffentlicher Artikel, aber keine internen Daten.
4. **Pages aktivieren**: Settings → Pages → Source „Deploy from a branch“ → Branch `main`, Ordner `/docs`.
5. **Ersten Lauf starten**: Tab Actions → „Medienmonitor“ → Run workflow.
   Danach läuft es alle 30 Minuten automatisch (GitHub kann Läufe verzögern) (Zeitplan in `.github/workflows/scan.yml`).

Das Dashboard liegt dann unter `https://<ihr-name>.github.io/<repo-name>/`.

## Lokal ausprobieren

```bash
pip install -r requirements.txt
python scanner/scan.py --ohne-ki        # nur Stichwortfilter
export GEMINI_API_KEY=...               # mit KI-Bewertung
python scanner/scan.py
cd docs && python -m http.server        # Dashboard auf http://localhost:8000
```

## Wie Begriffe gesucht werden

- Abkürzungen und kurze Begriffe (KI, OECD, NOOTS, BMDS) nur als ganzes Wort mit exakter Schreibweise, damit „KI“ nicht in „Kinder“ trifft.
- Mehrwortbegriffe (Data Act, Open Data) als ganze Wörter.
- Lange Einzelwörter auch als Wortteil, damit Komposita und Beugungen treffen (Statistik → Agrarstatistik, Statistiken).
- Sehr allgemeine Begriffe wie „Statistik“, „Digitalisierung“ oder „KI“ erzeugen viele Treffer. Die KI-Bewertung sortiert Randerwähnungen über `mindest_relevanz` aus.

## Volltext und Merkliste

- Feeds mit `volltext: true` (bei hib gesetzt): Das Tool lädt die Artikelseite und sucht im Haupttext, nicht nur im Titel. Navigation und Fußzeile werden ignoriert. Gespeichert wird der Text nicht.
- Geprüfte Artikel ohne Treffer oder mit zu niedriger Relevanz werden in `docs/data/articles.json` unter `gesehen` vermerkt und nicht erneut abgerufen oder bewertet. Das schont das KI-Kontingent.
- Das Protokoll eines Laufs (Actions → Lauf → „Feeds scannen“) zeigt je Quelle, wie viele Einträge neu waren und wie viele einen Suchbegriff enthielten.

## Anpassen

| Ziel | Wo |
|---|---|
| Neue Quelle | `feeds` bzw. `google_news` in `config.yml` |
| Weniger Fehltreffer | `ausschliessen`-Liste und `mindest_relevanz` erhöhen |
| Gratis-Limit einhalten | `max_ki_pro_lauf` senken, `pause_sekunden` erhöhen, Intervall im Workflow vergrößern |
| Nur Stichwörter | `ki_aktiv: false` |

## Grenzen

- Paywall-Medien, Print, TV und Radio werden nicht abgedeckt, nur was als Feed oder Google-News-Treffer sichtbar ist.
- Google News per RSS ist inoffiziell und kann sich ändern.
- Gespeichert werden nur Titel, kurzer Auszug und Link, nicht der Volltext.
- Wenn Sie Personen systematisch beobachten, prüfen Sie die DSGVO-Vorgaben.
- Die KI-Bewertung gelegentlich stichprobenartig prüfen, besonders die Tonalität.
