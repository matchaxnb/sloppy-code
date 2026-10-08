#!/usr/bin/env python3
"""Audit every .alternatives.txt beside a kept file in the library.

For each sidecar:
  * the kept file must exist in the same directory;
  * every rejected source path must still exist (sources are immutable);
  * all entries must share ONE instalment — a sidecar listing *different*
    episodes is the collapse signature (episodes ranked as rival encodes).

The rejected path is taken **after the byte-size field**, not by a trailing
token: paths contain spaces and a trailing-CRC match only ever saw the last word
(the earlier audit's error, which reported 0 failed while 7 botched files stayed).
"""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("MEDIA_ORGANIZER_HOME", HERE))
import media_ids as M
import config as C

LIB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(C.media_library(), "Cinema")

KEPT = re.compile(r"^Kept:\s+(\S.*?)\s*$")
# "  score (…,)  2,347,552,886 B  /abs/path with spaces/title.mkv"
REJ = re.compile(r"^\s*score\b.*?\s(\d[\d,]*)\s+B\s+(\S.*?)\s*$")
HDR = re.compile(r"^Rejected alternatives\b")

def instalment(path):
    p = M.parse_name(path)
    return (p["season"], p["episode"])

files = []
for dp, _dn, fns in os.walk(LIB):
    for f in fns:
        if f.endswith(".alternatives.txt"):
            files.append(os.path.join(dp, f))

n_ok = n_bad = n_parse = 0
missing_src = missing_kept = multi_ep = 0
problems = []
for side in sorted(files):
    d = os.path.dirname(side)
    kept = None
    rejs = []
    try:
        with open(side, encoding="utf-8", errors="replace") as fh:
            in_rej = False
            for line in fh:
                if HDR.match(line):
                    in_rej = True; continue
                m = KEPT.match(line)
                if m and kept is None and not in_rej:
                    kept = m.group(1); continue
                if in_rej:
                    m = REJ.match(line)
                    if m:
                        rejs.append(m.group(2))
    except OSError as e:
        problems.append(f"{side}: unreadable ({e})"); n_bad += 1; continue

    bad_this = False
    if kept is None:
        problems.append(f"{side}: no 'Kept:' line"); n_bad += 1; continue
    if not os.path.exists(os.path.join(d, kept)):
        missing_kept += 1; bad_this = True
        problems.append(f"{side}: kept file missing -> {kept}")
    for s in rejs:
        if not os.path.exists(s.strip()):
            missing_src += 1; bad_this = True
            problems.append(f"{side}: rejected src missing -> {s.strip()}")

    eps = {instalment(s)[1] for s in rejs if instalment(s)[1] is not None}
    if instalment(kept)[1] is not None:
        eps.add(instalment(kept)[1])
    if len(eps) > 1:
        multi_ep += 1; bad_this = True
        problems.append(f"{side}: {len(eps)} distinct episodes in one sidecar -> {sorted(eps)}")

    if bad_this:
        n_bad += 1
    else:
        n_ok += 1

print(f"sidecars: {len(files)}  ok: {n_ok}  failed: {n_bad}")
print(f"  missing kept: {missing_kept}   missing rejected src: {missing_src}   multi-episode: {multi_ep}")
for p in problems[:100000]:
    print("  FAIL", p)
if len(problems) > 100000:
    print(f"  … {len(problems)-40} more")
sys.exit(1 if n_bad else 0)
