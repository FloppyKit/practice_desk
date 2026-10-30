#!/usr/bin/env python3
"""Import a Carepatron export into Practice Desk. Dry run (on a throwaway copy) unless --apply."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import carepatron_import as ci  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--clients", type=Path)
    ap.add_argument("--notes", type=Path, help="folder of .md notes")
    ap.add_argument("--appointments", type=Path, help="CSV or zip of CSVs")
    ap.add_argument("--pdfs", type=Path)
    ap.add_argument("--out", type=Path, default=Path("carepatron-import-report"))
    ap.add_argument("--apply", action="store_true", help="write to --db (default: dry run on a copy)")
    ap.add_argument("--extract", action="store_true", help="Nova Lite suggestions to extract_suggestions.csv")
    ap.add_argument("--apply-clinical", action="store_true", help="with --apply: fill empty standing_dx from DX Code")
    a = ap.parse_args()
    if a.apply_clinical and not a.apply:
        ap.error("--apply-clinical requires --apply")
    rep = ci.run(db=a.db, clients_csv=a.clients, notes_dir=a.notes, appts=a.appointments, pdf_dir=a.pdfs,
                 out_dir=a.out, apply=a.apply, do_extract=a.extract,
                 apply_clinical_fields=a.apply_clinical)
    for k, c in rep.counts.items():
        print(f"{k}: in={c.get('in', 0)} out={c.get('out', 0)} gaps={c.get('gap', 0)}")
    if hasattr(rep, "cost_usd"):
        print(f"bedrock cost ~${rep.cost_usd:.5f}")
    print(("APPLIED to " + str(a.db)) if a.apply else "DRY RUN: live db untouched", "| report:", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
