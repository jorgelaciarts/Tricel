"""
Extrae el "Estado Diario" del Tribunal Calificador de Elecciones (TCE).

El sitio (https://tricel.lexsoft.cl/tce/estadoDiario) es una aplicación de
una sola página (SPA) que renderiza el contenido con JavaScript, por lo que
una simple descarga de HTML no trae los datos: hay que ejecutar un navegador
real. Este script usa Playwright (Chromium headless) para eso.

IMPORTANTE - PRIMER USO:
No fue posible inspeccionar el HTML ya renderizado antes de escribir este
script (el entorno de desarrollo no tiene acceso de red a ese dominio), así
que la extracción de la tabla es "best effort": busca cualquier <table> en
la página y, si no encuentra ninguna, guarda todo el texto visible como
respaldo. Después de la primera ejecución exitosa en GitHub Actions:

  1. Revisa docs/data/estado_diario_raw.html (el HTML ya renderizado).
  2. Si la tabla no se extrajo bien, ajusta la función `extract_entries`
     con los selectores CSS reales (clases, ids) que encuentres en ese
     archivo.

El script:
  - Descarga la página con Playwright.
  - Extrae las filas de la tabla del Estado Diario (o texto de respaldo).
  - Guarda un snapshot en docs/data/estado_diario.json.
  - Guarda un histórico diario en docs/data/history/AAAA-MM-DD.json.
  - Expone `changed=true/false` como output de GitHub Actions, para que
    el workflow decida si debe hacer commit y enviar el correo.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://tricel.lexsoft.cl/tce/estadoDiario"

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"
DATA_FILE = DATA_DIR / "estado_diario.json"
RAW_HTML_FILE = DATA_DIR / "estado_diario_raw.html"
HISTORY_DIR = DATA_DIR / "history"


def extract_entries(page):
    """Intenta extraer las filas del Estado Diario. Ver nota arriba."""
    page.wait_for_load_state("networkidle", timeout=30000)
    # Margen extra para que el framework JS termine de pintar el DOM
    page.wait_for_timeout(2000)

    entries = []

    tables = page.query_selector_all("table")
    for table in tables:
        rows = table.query_selector_all("tr")
        headers = None
        for row in rows:
            cells = [c.inner_text().strip() for c in row.query_selector_all("th, td")]
            if not cells or not any(cells):
                continue
            if headers is None and row.query_selector("th"):
                headers = cells
                continue
            entry = dict(zip(headers, cells)) if headers else {"columnas": cells}
            entries.append(entry)

    if not entries:
        # Respaldo: no se encontró tabla, guarda el texto visible completo
        # para poder ajustar el selector manualmente más adelante.
        body_text = page.inner_text("body")
        entries = [{"texto_bruto": body_text}]

    return entries


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        entries = extract_entries(page)
        raw_html = page.content()
        browser.close()

    now_local = datetime.now(timezone.utc).astimezone()
    today = now_local.strftime("%Y-%m-%d")

    snapshot = {
        "source_url": URL,
        "fetched_at": now_local.isoformat(),
        "entries": entries,
    }

    RAW_HTML_FILE.write_text(raw_html, encoding="utf-8")

    changed = True
    if DATA_FILE.exists():
        try:
            previous = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            changed = previous.get("entries") != entries
        except Exception:
            changed = True

    DATA_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HISTORY_DIR / f"{today}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")

    print(f"Entradas extraídas: {len(entries)} | Cambió respecto de ayer: {changed}")


if __name__ == "__main__":
    main()
