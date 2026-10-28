#!/usr/bin/env python3
"""CSV file(s) -> one .xlsx, a sheet per CSV. Standard library only (no
openpyxl on the server): bold header row, frozen, with a filter; column widths
from the contents; numbers stay numbers.

    python3 csv_to_xlsx.py ~/blob_repos.csv --out ~/blob_repos.xlsx
    python3 csv_to_xlsx.py a.csv b.csv --names "files,paths" --out report.xlsx
"""
import argparse
import csv
import os
import re
import sys
import zipfile
from xml.sax.saxutils import escape

MAX_ROWS = 1048576
NUM = re.compile(r"^-?(0|[1-9][0-9]{0,14})(\.[0-9]+)?$")
BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


def col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_xml(rows):
    widths = {}
    out = []
    for i, row in enumerate(rows, 1):
        cells = []
        for j, v in enumerate(row, 1):
            if v == "":
                continue
            widths[j] = min(max(widths.get(j, 8), len(v) + 2), 80)
            ref = "%s%d" % (col(j), i)
            st = ' s="1"' if i == 1 else ""
            if i > 1 and NUM.match(v):
                cells.append('<c r="%s"%s><v>%s</v></c>' % (ref, st, v))
            else:
                t = escape(BAD.sub("", v))
                cells.append('<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>' % (ref, st, t))
        out.append('<row r="%d">%s</row>' % (i, "".join(cells)))
    ncol = max((len(r) for r in rows), default=1)
    cols = "".join('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (j, j, w) for j, w in sorted(widths.items()))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            + ("<cols>%s</cols>" % cols if cols else "")
            + "<sheetData>%s</sheetData>" % "".join(out)
            + ('<autoFilter ref="A1:%s%d"/>' % (col(ncol), max(len(rows), 1)) if rows else "")
            + "</worksheet>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--names", help="sheet names, comma separated (default: the file names)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    names = a.names.split(",") if a.names else [os.path.splitext(os.path.basename(p))[0] for p in a.csv]
    if len(names) != len(a.csv):
        sys.exit("--names: %d names for %d files" % (len(names), len(a.csv)))
    names = [re.sub(r"[\[\]:*?/\\]", "_", n)[:31] for n in names]

    sheets = []
    for p in a.csv:
        with open(p, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.reader(fh))
        if len(rows) > MAX_ROWS:
            sys.exit("%s: %d rows, more than Excel's %d - keep it as CSV" % (p, len(rows), MAX_ROWS))
        sheets.append(rows)
        print("  %-30s %d rows" % (os.path.basename(p), max(len(rows) - 1, 0)))

    ct = "".join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i
                 for i in range(1, len(sheets) + 1))
    wb_sheets = "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(n, {'"': "&quot;"}), i, i)
                        for i, n in enumerate(names, 1))
    wb_rels = "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i)
                      for i in range(1, len(sheets) + 1))
    n = len(sheets)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + ct + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   "<sheets>%s</sheets>" % wb_sheets
                   + "<definedNames>%s</definedNames>" % "".join(
                       '<definedName name="_xlnm._FilterDatabase" localSheetId="%d" hidden="1">\'%s\'!$A$1:$%s$%d</definedName>'
                       % (i, escape(nm.replace("'", "''")), col(max((len(r) for r in s), default=1)), max(len(s), 1))
                       for i, (nm, s) in enumerate(zip(names, sheets)) if s)
                   + "</workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + wb_rels
                   + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1)
                   + "</Relationships>")
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
                   '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill></fills>'
                   '<borders count="1"><border/></borders>'
                   '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
                   '<cellXfs count="2"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/></cellXfs>'
                   "</styleSheet>")
        for i, rows in enumerate(sheets, 1):
            z.writestr("xl/worksheets/sheet%d.xml" % i, sheet_xml(rows))
    print("-> %s" % a.out)


if __name__ == "__main__":
    main()
