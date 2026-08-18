"""
Extrae el "Estado Diario" del Tribunal Calificador de Elecciones (TCE).

El sitio (https://tricel.lexsoft.cl/tce/estadoDiario) es una aplicación
Knockout.js: la tabla principal muestra, por fecha, un resumen (número de
causas y número de trámites). El detalle real -el listado de causas
(ROL, Carátula, Número de Trámites)- se carga vía AJAX recién cuando se
hace clic en el botón "Detalle" (ícono de ventana nueva) de cada fila,
que abre un modal (id "showDetalle").

Este script usa Playwright (Chromium headless) para:
  1. Cargar la página y esperar a que la tabla principal se pinte.
  2. Por cada fila (fecha), hacer clic en "Detalle", esperar a que el
     modal cargue su tabla de causas, extraerla, y cerrar el modal.
  3. Guardar todo en docs/data/estado_diario.json + histórico diario.
  4. Armar un resumen legible con las fechas nuevas respecto de la
     corrida anterior, para incluir en el correo de aviso.

IMPORTANTE: los selectores del modal ('#showDetalle', clases de Bootstrap)
se definieron inspeccionando el HTML ya renderizado (docs/data/estado_diario_raw.html)
pero no pudieron probarse en vivo antes de la primera ejecución real en
GitHub Actions. Si "Causas" queda vacío en el JSON tras una corrida real,
revisa ese archivo HTML y ajusta extract_causas_from_modal()/extract_entries().
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


def extract_causas_from_modal(page):
    """Lee las filas de la tabla del modal 'Información de Causa' (#showDetalle)."""
    causas = []
    causa_rows = page.query_selector_all("#showDetalle tbody tr")
    for crow in causa_rows:
        cells = [c.inner_text().strip() for c in crow.query_selector_all("td")]
        if not cells or not any(cells):
            continue
        causas.append({
            "ROL": cells[0] if len(cells) > 0 else "",
            "Caratula": cells[1] if len(cells) > 1 else "",
            "Numero_Tramites": cells[2] if len(cells) > 2 else "",
        })
    return causas


def extract_entries(page):
    """
    Extrae las filas de la tabla principal (una por fecha) y, para cada
    una, abre el modal de "Detalle" para capturar el listado real de causas.
    """
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    entries = []

    main_rows = page.query_selector_all("table#selectable tbody tr")
    num_rows = len(main_rows)

    if num_rows == 0:
        body_text = page.inner_text("body")
        return [{"texto_bruto": body_text}]

    for i in range(num_rows):
        rows = page.query_selector_all("table#selectable tbody tr")
        if i >= len(rows):
            break
        row = rows[i]
        cells = row.query_selector_all("td")
        if len(cells) < 3:
            continue

        fecha = cells[0].inner_text().strip()
        numero_causas = cells[1].inner_text().strip()
        numero_tramites = cells[2].inner_text().strip()

        causas = []
        detalle_btn = row.query_selector("td:nth-child(4) span")
        if detalle_btn:
            try:
                detalle_btn.click()
                page.wait_for_selector("#showDetalle", state="visible", timeout=10000)
                page.wait_for_timeout(1200)
                causas = extract_causas_from_modal(page)

                close_btn = page.query_selector("#showDetalle .modal-footer button")
                if close_btn:
                    close_btn.click()
                    page.wait_for_timeout(600)
            except Exception as exc:
                print(f"Aviso: no se pudo abrir el detalle de la fecha {fecha}: {exc}")

        entries.append({
            "Fecha": fecha,
            "Numero_Causas": numero_causas,
            "Numero_Tramites": numero_tramites,
            "Causas": causas,
        })

    if not entries:
        body_text = page.inner_text("body")
        entries = [{"texto_bruto": body_text}]

    return entries


def format_entry(entry, max_len=300):
    """Convierte una entrada (dict) en una línea de texto legible para el correo."""
    if "texto_bruto" in entry:
        snippet = entry["texto_bruto"].strip().replace("\n", " ")[:max_len]
        return f"(texto sin estructurar) {snippet}..."

    if "Fecha" in entry:
        header = (f"{entry.get('Fecha', '?')} — "
                  f"{entry.get('Numero_Causas', '?')} causas, "
                  f"{entry.get('Numero_Tramites', '?')} trámites")
        causas = entry.get("Causas") or []
        if causas:
            sample = "; ".join(
                f"{c.get('ROL', '')} {c.get('Caratula', '')}".strip()
                for c in causas[:5]
            )
            if len(causas) > 5:
                sample += f" ... y {len(causas) - 5} causas más"
            return f"{header}\n      Causas: {sample}"
        return header + " (sin detalle de causas disponible)"

    parts = []
    for key, value in entry.items():
        if isinstance(value, list):
            value = " · ".join(str(v) for v in value)
        value = str(value).strip()
        if value:
            parts.append(f"{key}: {value}")
    line = " | ".join(parts)
    return line[:max_len] + ("..." if len(line) > max_len else "")


def build_summary(previous_entries, new_entries):
    """
    Resumen legible para el correo. Si las entradas tienen 'Fecha' (caso
    normal), compara por fecha: qué fechas son nuevas respecto de la
    última corrida. Si no (modo de respaldo con texto_bruto), compara por
    contenido completo.
    """
    total = len(new_entries)
    lines = [f"Total de fechas de Estado Diario encontradas: {total}", ""]

    has_fechas = any("Fecha" in e for e in new_entries)

    if has_fechas:
        previous_fechas = {e.get("Fecha") for e in previous_entries if "Fecha" in e}
        added = [e for e in new_entries if e.get("Fecha") not in previous_fechas]
    else:
        previous_serialized = {json.dumps(e, ensure_ascii=False, sort_keys=True) for e in previous_entries}
        added = [
            e for e in new_entries
            if json.dumps(e, ensure_ascii=False, sort_keys=True) not in previous_serialized
        ]

    if added:
        lines.append(f"Novedades ({len(added)}):")
        for e in added[:20]:
            lines.append(f"  - {format_entry(e)}")
        if len(added) > 20:
            lines.append(f"  ... y {len(added) - 20} más. Revisa la página completa para verlas todas.")
    else:
        lines.append("No se detectaron fechas nuevas respecto de la última revisión.")

    return "\n".join(lines)


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

    previous_entries = []
    changed = True
    if DATA_FILE.exists():
        try:
            previous = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            previous_entries = previous.get("entries", [])
            changed = previous_entries != entries
        except Exception:
            changed = True

    DATA_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HISTORY_DIR / f"{today}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = build_summary(previous_entries, entries)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write("summary<<EOF_SUMMARY\n")
            f.write(summary + "\n")
            f.write("EOF_SUMMARY\n")

    print(f"Entradas extraídas: {len(entries)} | Cambió respecto de ayer: {changed}")
    print(summary)


if __name__ == "__main__":
    main()
