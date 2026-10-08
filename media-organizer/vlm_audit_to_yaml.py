#!/usr/bin/env python3
"""Render a vlm_audit*.tsv as a human-readable YAML audit.

Groups the folders by verdict so the interesting lines surface first:
  disagreements      - heuristic series/anthology, but the VLM read it the other way
  confirmed          - heuristic and VLM agree (series/anthology)
  heuristic_unreadable - the heuristic said `none`; the VLM's guess is shown to skim

Usage: vlm_audit_to_yaml.py vlm_audit.tsv [out.yaml]
"""
from __future__ import annotations
import csv, sys, collections

def q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

def main(argv: list[str]) -> int:
    src = argv[0] if argv else "vlm_audit.tsv"
    out = argv[1] if len(argv) > 1 else src.rsplit(".", 1)[0] + ".yaml"
    rows = list(csv.DictReader(open(src, encoding="utf-8"), delimiter="\t"))

    disagree, confirm, unreadable = [], [], []
    for r in rows:
        heur, vlm, agree = r["heuristic"], r["vlm_norm"], r["agree"]
        samples = [s.strip() for s in r["sample_files"].split(";") if s.strip()]
        rec = {"dir": r["dir"], "files": int(r["n_files"]), "heuristic": heur,
               "vlm": r["vlm_label"], "samples": samples}
        if heur == "none":
            unreadable.append(rec)
        elif agree == "NO":
            disagree.append(rec)
        else:
            confirm.append(rec)

    L = []
    L.append("# VLM audit — second opinion on the folder reading")
    L.append("# Heuristic reading = siblings.classify_folder (authoritative).")
    L.append("# VLM = a local vision-language model via vlm_second_opinion.py (hint-only;")
    L.append("#       over-call SERIES, incl. on genuine anthologies).")
    L.append(f"# source: {src}   folders: {len(rows)}")
    L.append("")

    def section(title: str, recs: list[dict]) -> None:
        L.append(f"{title}:  # {len(recs)}")
        if not recs:
            L.append("  []")
        for r in recs:
            L.append(f"  - dir: {q(r['dir'])}")
            L.append(f"    files: {r['files']}")
            L.append(f"    heuristic: {r['heuristic']}")
            L.append(f"    vlm: {q(r['vlm'])}")
            if r["samples"]:
                L.append("    samples:")
                for s in r["samples"]:
                    L.append(f"      - {q(s)}")
        L.append("")

    section("disagreements", disagree)
    section("confirmed", confirm)
    section("heuristic_unreadable", unreadable)

    open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"wrote {out}: {len(disagree)} disagreements, {len(confirm)} confirmed, "
          f"{len(unreadable)} unreadable")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
