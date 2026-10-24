# actions/spreadsheet_builder.py
"""
Spreadsheet (.xlsx) creation for SARA.
Ported from Brahma-Echo (actions/office_builder.py create_spreadsheet),
adapted to SARA's action contract. Builds real Excel workbooks with headers,
rows, formulas, styling, and a native chart, then VERIFIES by reopening.
"""

import re
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="0A6EA8")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)


def _output_dir() -> Path:
    d = Path.home() / "Documents" / "SARA Docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(title: str) -> str:
    keep = [c if c.isalnum() or c in " -_" else "_" for c in title]
    return ("".join(keep).strip() or "spreadsheet")[:60]


def _parse_table(content: str) -> tuple[list[str], list[list]]:
    """Parse '## Sheet' sections of CSV-like lines into (headers, rows).
    Accepts '|'-separated or comma-separated lines after an optional
    'Headers: a,b,c' line."""
    headers: list[str] = []
    rows: list[list] = []

    def _split(line: str) -> list[str]:
        if "|" in line:
            return [c.strip() for c in line.strip().strip("|").split("|")]
        return [c.strip() for c in line.split(",")]

    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("headers:"):
            headers = _split(line.split(":", 1)[1])
            continue
        if line.startswith("- ") or line.startswith("• "):
            line = line[2:].strip()
        cells = _split(line)
        if not any(cells):
            continue
        if not headers:
            headers = [f"Column {get_column_letter(i+1)}" for i in range(len(cells))]
        rows.append(cells)

    return headers, rows


def _cell_value(text: str):
    """Numbers become numbers; '=' stays a formula; else string."""
    t = (text or "").strip()
    if t.startswith("="):
        return t
    try:
        f = float(t.replace(",", "") if re.fullmatch(r"-?[\d,]+(\.\d+)?", t) else t)
        return int(f) if f == int(f) else f
    except (ValueError, OverflowError):
        return t


def spreadsheet_builder(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "create").lower().strip()
    title = (p.get("title") or "Spreadsheet").strip()
    content = (p.get("content") or "").strip()
    chart = (p.get("chart") or "").strip().lower()   # bar | line | pie | ''

    if action not in ("create", "new", "build"):
        return f"Unknown spreadsheet action: '{action}'."
    if not content:
        return "No table content given — pass rows via 'content'."

    headers, rows = _parse_table(content)
    if not rows:
        return "Could not parse any data rows from 'content'."

    wb = Workbook()
    ws = wb.active
    ws.title = (title or "Sheet1")[:31]

    ws.append(headers)
    for c in ws[1]:
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(horizontal="center")

    for row in rows:
        ws.append([_cell_value(v) for v in row])

    # column widths from content length
    for col_idx, header in enumerate(headers, 1):
        cell_lens = [len(str(r[col_idx - 1])) for r in rows if col_idx <= len(r)]
        width = max([len(str(header))] + cell_lens)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(40, width + 3)

    # total row if all columns are numeric except the first
    try:
        numeric_cols = [i for i in range(2, len(headers) + 1)
                        if all(isinstance(r[i-1], (int, float)) for r in rows if i <= len(r))]
        if numeric_cols and rows:
            total_row = ["TOTAL"] + ["" for _ in range(len(headers) - 1)]
            for i in numeric_cols:
                col = get_column_letter(i)
                total_row[i-1] = f"=SUM({col}2:{col}{len(rows)+1})"
            ws.append(total_row)
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
    except Exception:
        pass

    # optional chart over the first two columns (label + first numeric)
    if chart in ("bar", "line", "pie"):
        try:
            chart_cls = {"bar": BarChart, "line": LineChart, "pie": PieChart}[chart]
            ch = chart_cls()
            data = Reference(ws, min_col=2, min_row=1, max_row=1 + len(rows))
            cats = Reference(ws, min_col=1, min_row=2, max_row=1 + len(rows))
            ch.add_data(data, titles_from_data=True)
            ch.set_categories(cats)
            ch.title = title
            ws.add_chart(ch, f"A{ws.max_row + 3}")
        except Exception as e:
            return_msg_note = f" (chart failed: {e})"
        else:
            return_msg_note = ""
    else:
        return_msg_note = ""

    out = _output_dir() / f"{_safe_name(title)} {datetime.now():%Y%m%d %H%M}.xlsx"
    wb.save(str(out))

    # ── verify by reopening ─────────────────────────────────────────────────
    try:
        check = load_workbook(str(out))
        wsr = check.active
        assert wsr.max_row >= len(rows) + 1
        return f"Spreadsheet created and verified: {out} " \
               f"({wsr.max_row - 1} data rows, {len(headers)} columns){return_msg_note}."
    except Exception as e:
        return f"Workbook written but verification failed: {e} — path: {out}"


TOOL = {
    "name": "spreadsheet_builder",
    "description": (
        "Creates a real Excel (.xlsx) spreadsheet from table data: headers, "
        "rows, numeric totals, styling, optional bar/line/pie chart. Pass "
        "rows via 'content' — one row per line, comma- or pipe-separated, "
        "first line 'Headers: Name, Qty, Price'. Cells starting with '=' "
        "become formulas. Use for: make a spreadsheet, create an Excel file, "
        "track this in a table."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "create (default)",
            },
            "title": {
                "type": "STRING",
                "description": "Sheet/workbook title and filename base.",
            },
            "content": {
                "type": "STRING",
                "description": "Table rows, one per line, comma- or pipe-separated. Optional first line 'Headers: a, b, c'.",
            },
            "chart": {
                "type": "STRING",
                "description": "Optional chart over the data: bar | line | pie",
            },
        },
        "required": ["title", "content"],
    },
    "handler": spreadsheet_builder,
}
