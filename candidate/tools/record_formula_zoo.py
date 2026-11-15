"""Record Microsoft Excel's results for the formula zoo (tests/formula_zoo.py).

Development tool, Windows with Microsoft Excel only. It writes the zoo workbook with Kalillac's own
generator, has Excel open it read-only with macros disabled, recalculate everything and report each
formula's value, then rewrites the EXCEL_RESULTS block at the end of tests/formula_zoo.py. The engine
test compares every case with these recorded values.

Usage (from candidate/):  python tools/record_formula_zoo.py
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))
sys.path.insert(0, str(CANDIDATE / "tests"))

import formula_zoo  # noqa: E402
from kalillac_files import generate  # noqa: E402

ERRORS = {-2146826281: "#DIV/0!", -2146826246: "#N/A", -2146826259: "#NAME?", -2146826288: "#NULL!",
          -2146826252: "#NUM!", -2146826265: "#REF!", -2146826273: "#VALUE!"}
SCRIPT = r'''
param([string]$InputPath, [string]$OutputPath)
$ErrorActionPreference = "Stop"
$app = New-Object -ComObject Excel.Application
$app.Visible = $false
$app.DisplayAlerts = $false
try {
    $app.AutomationSecurity = 3
    $book = $app.Workbooks.Open($InputPath, 0, $true)
    $app.CalculateFull()
    $sheet = $book.Worksheets.Item("Zoo")
    $out = @()
    $row = 2
    while ($sheet.Cells.Item($row, 1).Value2 -ne $null) {
        $value = $sheet.Cells.Item($row, 2).Value2
        $kind = if ($value -is [int]) { "error" } elseif ($value -is [bool]) { "bool" } elseif ($value -is [string]) { "text" } elseif ($value -eq $null) { "blank" } else { "number" }
        $out += ,@{ case = $sheet.Cells.Item($row, 1).Value2; kind = $kind; value = $value }
        $row++
    }
    $book.Close($false)
    $version = $app.Version
} finally { $app.Quit() }
@{ version = $version; results = $out } | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 $OutputPath
'''


def main():
    if platform.system() != "Windows":
        sys.exit("Microsoft Excel on Windows is required to record the zoo.")
    folder = Path(tempfile.mkdtemp(prefix="formula-zoo-"))
    book = folder / "zoo.xlsx"
    book.write_bytes(generate(copy.deepcopy(formula_zoo.workbook()), "xlsx").data)
    script = folder / "record.ps1"
    script.write_text(SCRIPT, encoding="utf-8")
    output = folder / "results.json"
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-InputPath",
                    str(book), "-OutputPath", str(output)], check=True, timeout=300)
    report = json.loads(output.read_text(encoding="utf-8-sig"))
    results = {}
    for item in report["results"]:
        value = item["value"]
        if item["kind"] == "error":
            value = ERRORS.get(value, f"#ERROR{value}")
        results[item["case"]] = value
    missing = [case for case in formula_zoo.CASES if case not in results]
    if missing:
        sys.exit(f"Excel returned no value for {len(missing)} cases")
    source = (CANDIDATE / "tests" / "formula_zoo.py").read_text(encoding="utf-8")
    marker = "\n# --- recorded from Microsoft Excel"
    if marker in source:
        source = source[:source.index(marker)]
    lines = [marker[1:] + f" {report['version']} by tools/record_formula_zoo.py; do not edit ---",
             "EXCEL_RESULTS = {"]
    for case in formula_zoo.CASES:
        lines.append(f"    {case!r}: {results[case]!r},")
    lines.append("}")
    (CANDIDATE / "tests" / "formula_zoo.py").write_text(source.rstrip("\n") + "\n\n\n" + "\n".join(lines) + "\n",
                                                        encoding="utf-8", newline="\n")
    print(f"recorded {len(results)} results from Excel {report['version']}")


if __name__ == "__main__":
    main()
