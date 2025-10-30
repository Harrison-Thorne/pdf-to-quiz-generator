#!/usr/bin/env python3
"""
jsonl_merge_to_excel.py

Merge **all** .jsonl files in a directory (default: ./pic2jsonl_output)
into a single Excel (.xlsx) file.

Key features
------------
* Recursively searches the target directory for ``*.jsonl`` files.
* Reads each line of every file as an individual JSON object (skips blanks).
* Flattens nested structures with a configurable separator (default: ".").
* Unions columns across *all* rows, ensuring that missing fields are present
  in the final sheet and left blank (empty string) when absent.
* Preserves a preferred prefix‑column order
    ``["subject", "pdf", "page", "originID", "partedID",
      "question", "options", "answer", "analysis", "pic"]``
  with all other columns appended afterwards, sorted alphabetically.
* Graceful handling of complex types: lists joined with " | " when simple,
  otherwise JSON‑stringified; dicts are JSON‑stringified.
* Thin CLI powered by ``argparse`` with sensible defaults:

  ::

     python jsonl_merge_to_excel.py                  # scans ./pic2jsonl_output → ./final_excel_output/finalOutput.xlsx
     python jsonl_merge_to_excel.py -d other_dir     # scans other_dir → ./final_excel_output/finalOutput.xlsx
     python jsonl_merge_to_excel.py -d data -o out.xlsx --sep ":"  # custom options

Exit codes
~~~~~~~~~~
0 on success, non‑zero on failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from pandas import json_normalize

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DIR = Path("./pic2jsonl_output")

# 修改默认输出路径常量
# 新增一个默认输出文件夹
DEFAULT_OUTPUT_DIR = Path("./final_excel_output")
# 新增一个默认输出文件名
DEFAULT_OUTPUT_FILENAME = "finalOutput.xlsx"

DEFAULT_PREFIX = [
    "subject", "pdf", "page", "originID", "partedID",
    "question", "options", "answer", "analysis", "pic",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stringify(value: Any) -> Any:
    """Convert complex types into Excel‑friendly scalar strings."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        # Join simple lists for readability; otherwise JSON dump.
        if all(isinstance(x, (str, int, float, bool)) or x is None for x in value):
            return " | ".join("" if x is None else str(x) for x in value)
        return json.dumps(value, ensure_ascii=False)
    # Fallback for dict or other.
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def read_jsonl(path: Path, *, sep: str = ".") -> List[Dict[str, Any]]:
    """Read a single JSONL file and return a list of flattened records."""
    records: List[Dict[str, Any]] = []
    bad_lines: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                flat_df = json_normalize(obj, sep=sep)  # single‑row DataFrame
                rec = {k: _stringify(v) for k, v in flat_df.iloc[0].items()}
                records.append(rec)
            except Exception as e:  # noqa: BLE001
                bad_lines.append(f"{path.name} [line {i}]: {e}")
    if bad_lines:
        sys.stderr.write(
            "Warning: some lines could not be parsed in\n  "
            + "\n  ".join(bad_lines)
            + "\n"
        )
    return records


def union_columns(records: List[Dict[str, Any]]) -> List[str]:
    """Return the union of all keys across *records* sorted alphabetically."""
    keys: set[str] = set()
    for r in records:
        keys.update(r.keys())
    return sorted(keys)


def order_columns(all_cols: List[str], prefix: List[str]) -> List[str]:
    """Return *all_cols* ordered with *prefix* first and the rest sorted."""
    prefix_present = [c for c in prefix if c in all_cols]
    rest = [c for c in all_cols if c not in prefix_present]
    rest_sorted = sorted(rest, key=lambda k: (k.count("."), k))
    return prefix_present + rest_sorted


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:  # noqa: C901  (complexity fine for script)
    parser = argparse.ArgumentParser(description="Merge all JSONL files in a directory into a single Excel file.")
    parser.add_argument(
        "-d",
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help=f"Directory containing .jsonl files (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        # 帮助信息更新以反映新的默认值
        help=f"Output .xlsx path (default: {DEFAULT_OUTPUT_DIR / DEFAULT_OUTPUT_FILENAME})",
    )
    parser.add_argument("--sep", default=".", help="Separator used when flattening nested keys (default: '.')")
    parser.add_argument("--sheet-name", default="Sheet1", help="Excel sheet name (default: Sheet1)")

    args = parser.parse_args(argv)

    dir_path: Path = args.dir.resolve()
    if not dir_path.exists() or not dir_path.is_dir():
        sys.stderr.write(f"Error: directory not found: {dir_path}\n")
        return 2

    # --- 关键修改：处理默认输出路径 ---
    if args.output:
        out_path: Path = args.output
    else:
        # 如果未指定 -o，则使用新的默认路径
        out_path: Path = DEFAULT_OUTPUT_DIR / DEFAULT_OUTPUT_FILENAME

    # 确保输出目录存在
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # --- 关键修改结束 ---

    # Discover .jsonl files
    jsonl_files = sorted(dir_path.rglob("*.jsonl"))
    if not jsonl_files:
        sys.stderr.write(f"Error: no .jsonl files found in {dir_path}\n")
        return 3

    # Read all files
    records: List[Dict[str, Any]] = []
    for jf in jsonl_files:
        records.extend(read_jsonl(jf, sep=args.sep))

    # Build DataFrame
    if records:
        all_cols = union_columns(records)
        # Ensure default prefix columns exist
        for col in DEFAULT_PREFIX:
            if col not in all_cols:
                all_cols.append(col)
        ordered_cols = order_columns(all_cols, DEFAULT_PREFIX)
        df = pd.DataFrame.from_records(records)
        # Add missing columns and fill empties
        for col in ordered_cols:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[ordered_cols].fillna("")  # blank instead of NaN for Excel
    else:
        df = pd.DataFrame(columns=DEFAULT_PREFIX)

    # Save to Excel
    try:
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=args.sheet_name)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Error while writing Excel: {exc}\n")
        return 4

    print(f"Merged {len(jsonl_files)} files – {len(df)} rows × {len(df.columns)} columns → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())