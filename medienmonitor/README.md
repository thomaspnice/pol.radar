# Medienmonitor

Holt regelmäßig RSS-Feeds und Google-News-Suchen, filtert nach Stichwörtern, entfernt Dubletten,
lässt die Treffer optional von einer KI bewerten (Relevanz, Tonalität, Zusammenfassung) und
veröffentlicht alles als durchsuchbares Dashboard auf GitHub Pages. Läuft kostenlos in GitHub Actions.

## Einrichtung (ca. 10 Minuten)

1. **Repository anlegen** auf github.com (privat oder öffentlich) und den Inhalt dieses Ordners hochladen.
   > GitHub Pages ist bei privaten Repositories nur mit bezahltem Plan verfügbar.
   > Das Dashboard enthält Ihre Artikelliste; bei Bedarf Repository öffentlich lassen und nichts Vertrauliches konfigurieren.
2. **`config.yml` anpassen**: Beobachtungsziel (`thema`), Stichwörter, Feeds, Google-News-Suchen.
3. **API-Schlüssel hinterlegen** (nur für die KI-Bewertung):
   Settings → Secrets and variables → Actions → New repository secret → Name `ANTHROPIC_API_KEY`.
   Ohne Schlüssel läuft das Tool nur mit dem Stichwortfilter.
4. **Pages aktivieren**: Settings → Pages → Source „Deploy from a branch“ → Branch `main`, Ordner `/docs`.
5. **Ersten Lauf starten**: Tab Actions → „Medienmonitor“ → Run workflow.
   Danach läuft es alle 2 Stunden automatisch (Zeitplan in `.github/workflows/scan.yml`).

Das Dashboard liegt dann unter `https://<ihr-name>.github.io/<repo-name>/`.

## Lokal ausprobieren

```bash
pip install -r requirements.txt
python scanner/scan.py --ohne-ki        # nur Stichwortfilter
export ANTHROPIC_API_KEY=...            # mit KI-Bewertung
python scanner/scan.py
cd docs && python -m http.server        # Dashboard auf http://localhost:8000
```

## Anpassen

| Ziel | Wo |
|---|---|
| Neue Quelle | `feeds` bzw. `google_news` in `config.yml` |
| Weniger Fehltreffer | `ausschliessen`-Liste und `mindest_relevanz` erhöhen |
| Kosten begrenzen | `max_ki_pro_lauf` senken, Intervall im Workflow vergrößern |
| Nur Stichwörter | `ki_aktiv: false` |

## Grenzen

- Paywall-Medien, Print, TV und Radio werden nicht abgedeckt, nur was als Feed oder Google-News-Treffer sichtbar ist.
- Google News per RSS ist inoffiziell und kann sich ändern.
- Gespeichert werden nur Titel, kurzer Auszug und Link, nicht der Volltext.
- Wenn Sie Personen systematisch beobachten, prüfen Sie die DSGVO-Vorgaben.
- Die KI-Bewertung gelegentlich stichprobenartig prüfen, besonders die Tonalität.
