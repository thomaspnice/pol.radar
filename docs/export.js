/* Erzeugt aus der Vorlage (vorlage.xlsx) eine Excel-Datei mit den ausgewählten Artikeln.
   Läuft im Browser (ExcelJS über <script>) und in Node (Tests). */
(function (root) {
  "use strict";

  const BLAETTER = ["Bundesregierung", "Bundestag", "Behörde", "Europa", "Presse", "Personal"];
  const NICHT_BENUTZT = ["Anfragen", "Hauptstadt"];
  const SCHRIFT = { name: "Statis Sans", size: 9 };

  function datumUtc(iso) {
    const d = new Date(iso);                     // lokales Kalenderdatum übernehmen, Uhrzeit verwerfen
    return new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  }

  function spaltenIndex(ws) {
    const idx = {};
    ws.getRow(1).eachCell((c, i) => { idx[String(c.value).trim()] = i; });
    return idx;
  }

  function schreibeZeile(ws, r, idx, e) {
    const setze = (name, wert, opt) => {
      const i = idx[name];
      if (!i) return;
      const c = ws.getCell(r, i);
      c.value = wert;
      c.font = Object.assign({}, SCHRIFT, (opt && opt.font) || {});
      c.alignment = { wrapText: true, vertical: "top", horizontal: (opt && opt.h) || "left" };
      if (opt && opt.fmt) c.numFmt = opt.fmt;
    };
    setze("Datum", datumUtc(e.datum), { fmt: "dd.mm.yyyy" });
    setze("Organisation/Medium", e.organisation || "");
    setze("Titel", e.titel || "");
    setze("Erläuterung", e.erlaeuterung || "");
    setze("Link", { text: e.url, hyperlink: e.url }, { font: { color: { argb: "FF0563C1" }, underline: true } });
    if (idx["Paywall"]) setze("Paywall", e.paywall ? "x" : "", { h: "center" });
    setze("Quelle", { formula: `IF(B${r}="","",B${r})` });
    setze("Bild", "");
    setze("Highlight", e.highlight ? "x" : "", { h: "center" });
    setze("Stichwörter", e.stichwoerter || "");
    ws.getRow(r).height = undefined;             // Zeilenhöhe automatisch (Vorlage hat teils 288 pt)
  }

  async function erzeugeExcel(ExcelJS, vorlage, eintraege) {
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(vorlage);
    for (const n of NICHT_BENUTZT) {
      const ws = wb.getWorksheet(n);
      if (ws) wb.removeWorksheet(ws.id);
    }
    for (const name of BLAETTER) {
      const ws = wb.getWorksheet(name);
      if (!ws) continue;
      const idx = spaltenIndex(ws);
      if (idx["Stichwörter"] && (ws.getColumn(idx["Stichwörter"]).width || 0) < 22) ws.getColumn(idx["Stichwörter"]).width = 24;
      const liste = eintraege.filter(e => e.kategorie === name)
        .sort((a, b) => new Date(a.datum) - new Date(b.datum));
      // Alte Datenzeilen/Beispiele der Vorlage leeren (Kopfzeile bleibt)
      const letzte = Math.max(ws.rowCount, liste.length + 1);
      for (let r = 2; r <= letzte; r++) {
        for (let c = 1; c <= ws.columnCount; c++) ws.getCell(r, c).value = null;
        ws.getRow(r).height = undefined;
      }
      liste.forEach((e, k) => schreibeZeile(ws, k + 2, idx, e));
      // Leere Vorlagenzeilen behalten die Formel in "Quelle", wie in der Vorlage
      for (let r = liste.length + 2; r <= Math.max(letzte, 12); r++) {
        if (idx["Quelle"]) ws.getCell(r, idx["Quelle"]).value = { formula: `IF(B${r}="","",B${r})` };
      }
    }
    return wb.xlsx.writeBuffer();
  }

  const api = { erzeugeExcel, BLAETTER };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Politikradar = api;
})(typeof window !== "undefined" ? window : globalThis);
