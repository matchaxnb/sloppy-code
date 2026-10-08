#!/usr/bin/env python3
"""Sweep the corpus folders through the local VLM and the heuristic, side by side.

Writes a durable TSV (default `vlm_audit.tsv`) so the reading of every folder can
be audited by hand: dir, file count, the heuristic's reading, the VLM's label, and
whether they agree. Reads source paths on stdin (from the state DB) so no
filesystem walk on the pool is needed.

Columns:
  dir  n_files  heuristic  vlm_label  agree  sample_files
"""
import os, sys, collections, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")
import siblings
import vlm_second_opinion as V

VID = V.VID
OUT = os.environ.get("VLM_AUDIT_OUT", "vlm_audit.tsv")

paths = [l.strip() for l in sys.stdin if l.strip()]
paths = [p for p in paths if os.path.splitext(p)[1].lower() in VID]
bydir = collections.defaultdict(list)
for p in paths:
    bydir[os.path.dirname(p)].append(p)
if os.environ.get("VLM_COALESCE", "1") != "0":
    lifted = V.coalesce(bydir).get("__lifted__", 0)
    bydir = V.coalesce(bydir)
    bydir.pop("__lifted__", None)
    print(f"coalesced {lifted} single-file leaf dirs into parents", file=sys.stderr)
dirs = sorted(bydir)
print(f"folders: {len(dirs)}  files: {len(paths)}", file=sys.stderr)

H2V = {"series": "SERIES", "anthology": "ANTHOLOGY"}


def norm(label: str) -> str:
    head = label.split("-")[0].strip().upper().split()[0] if label else ""
    for k in ("SERIES", "ANTHOLOGY", "VERSIONS", "FILMS"):
        if head.startswith(k):
            return k
    return head or "?"


with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("dir\tn_files\theuristic\tvlm_label\tvlm_norm\tagree\tsample_files\n")
    for i, d in enumerate(dirs, 1):
        names = sorted(os.path.basename(p) for p in bydir[d])
        reading, payload = siblings.classify_folder(names)
        heur = reading or "none"
        try:
            lab = V.label(names, V.folder_hint(d))
        except Exception as e:
            lab = f"(error: {type(e).__name__})"
        vn = norm(lab)
        agree = "yes" if H2V.get(heur) == vn else ("n/a" if heur == "none" else "NO")
        sample = "; ".join(names[:4])
        fh.write(f"{d}\t{len(names)}\t{heur}\t{lab}\t{vn}\t{agree}\t{sample}\n")
        fh.flush()
        print(f"  [{i}/{len(dirs)}] {heur:9s} vs {vn:9s} {agree}  {os.path.basename(d)}",
              file=sys.stderr)
        time.sleep(0.05)

print(f"wrote {OUT}", file=sys.stderr)
