#!/usr/bin/env python3
"""Second opinion on how a folder's files should be read, from a local VLM.

NON-AUTHORITATIVE. `siblings.classify_folder` is the authority; this is a
cross-check for the handful of folders where the heuristic is genuinely unsure
(see HANDOVER §5). Measured behaviour of a small local vision-language model:

  * it eagerly labels any folder of same-named files **SERIES**, including a
    genuine anthology (`Tex Avery - <title>.mp4`) — so it must not be trusted
    for the series/anthology call;
  * asked to *partition* a folder, it **over-merges**: it puts all 26 distinct
    Cowboy Bebop episodes in ONE group, keeping only the "same episode, two
    encodes" split. So it cannot supply the episode axis either.

Treat its output as a hint that a human or the heuristic then adjudicates.

Usage:
    vlm_second_opinion.py label  DIR [DIR ...]
    vlm_second_opinion.py group  DIR [DIR ...]
    vlm_second_opinion.py label  --stdin-paths < paths.txt   # dirs from paths

Requires $VLM_ENDPOINT (an OpenAI-compatible chat-completions URL) and $VLM_MODEL
(a model id). Neither has a default.
"""
from __future__ import annotations
import json, os, re, sys, urllib.request, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

VID = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts")

# Source roots are named Films / CleanFilms / Movies / Series / CleanSeries, and
# the folder a file sits in often says "Series" or "Movies". Feeding those words
# to the model biases it (measured: it then answers SERIES). Redact the substrings
# wherever they appear (so `CleanSeries` and `FilmsCollec` are covered too), and
# show only the last few path components rather than the whole absolute path.
_BIAS_RE = re.compile(r"(?i)(series|films|movies)")

def redact(s: str) -> str:
    return _BIAS_RE.sub("[redacted]", s or "")

def folder_hint(dirpath: str, n: int = 3) -> str:
    """The last `n` path components, prefixed with an ellipsis — no source root."""
    parts = [p for p in str(dirpath).split(os.sep) if p and p != os.sep]
    return os.sep.join(["…"] + parts[-n:]) if parts else "…"


def coalesce(files_by_dir: dict, min_children: int = 2) -> dict:
    """Roll single-file leaf dirs up into a parent that has several of them.

    A scene tree puts each episode in its OWN directory
    (`.../Twin.Peaks.S03E01.../x.mkv`, `.../Sample/x.mkv`), so classifying the
    child sees one file and can say nothing. Lifting such children into their
    parent passes the whole set at once — the same shape as a flat folder like
    Cowboy Bebop's 26 files. Applied repeatedly so nested cases collapse too.
    Returns a new dict; also returns the number lifted under key "__lifted__".
    """
    fb = {d: list(v) for d, v in files_by_dir.items()}
    lifted = 0
    changed = True
    while changed:
        changed = False
        kids = collections.defaultdict(list)
        for d in fb:
            kids[os.path.dirname(d)].append(d)
        for parent, children in kids.items():
            leaves = [d for d in children if len(fb[d]) == 1]
            if len(leaves) >= min_children:
                merged = []
                for d in leaves:
                    merged += fb.pop(d)
                fb.setdefault(parent, []).extend(merged)
                lifted += len(leaves)
                changed = True
    out = {d: v for d, v in fb.items() if v}
    out["__lifted__"] = lifted
    return out

_LABEL_SYS = (
    "You classify the video files of ONE folder. Answer with exactly one label word "
    "from this list, then a hyphen and a reason of at most 12 words.\n"
    "SERIES - numbered episodes of a single TV or anime work.\n"
    "ANTHOLOGY - distinct separate works (each a different title).\n"
    "VERSIONS - the same episode/work in different encodes or qualities.\n"
    "FILMS - unrelated films or extras that merely share the folder.\n"
    "Format: LABEL - reason")

_GROUP_SYS = (
    "You are given video file names from ONE folder, each with a number.\n"
    "Output ONE line per file, TAB-separated, exactly three fields:\n"
    "number<TAB>label<TAB>kind\n"
    "  * number - the number shown for that file\n"
    "  * label  - a short name; files that are the SAME work share one label\n"
    "  * kind   - one of: episode | version | work\n"
    "Files that are different encodes/qualities of the same work share a label and "
    "kind 'version'. Different episodes of a series get separate labels, kind "
    "'episode'. Unrelated works get separate labels, kind 'work'.\n"
    "Output ONLY those lines: no header, no prose, no JSON, no blank lines.")


def _post(system: str, user: str, max_tokens: int):
    endpoint, model = C.vlm_conf()
    body = {"temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if model:
        body["model"] = model          # optional: single-model servers omit it
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    content = out["choices"][0]["message"].get("content")
    # some builds put text in content blocks instead of a plain string
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return (content or "").strip()


def label(names: list[str], hint: str = "") -> str:
    body = (f"Folder: {hint}\n" if hint else "") + \
           f"Folder files ({len(names)}):\n" + "\n".join(redact(n) for n in names)
    return _post(_LABEL_SYS, body, 60)


def group(names: list[str], hint: str = "") -> tuple[list[tuple[int, str, str]], int]:
    """Parse the VLM's TSV. Returns (rows, n_covered) where rows are
    (index, label, kind). Tolerates stray prose lines by keeping only lines whose
    first field is an int in range."""
    body = (f"Folder: {hint}\n" if hint else "") + f"{len(names)} files:\n" + \
           "\n".join(f"{i}\t{redact(n)}" for i, n in enumerate(names))
    raw = _post(_GROUP_SYS, body, 1500)
    rows, seen = [], set()
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        i = int(parts[0])
        if 0 <= i < len(names) and i not in seen:
            seen.add(i)
            rows.append((i, parts[1] or "?", parts[2].lower() or "?"))
    return rows, len(seen)


def folders(argv: list[str]) -> list[str]:
    if "--stdin-paths" in argv:
        bydir = collections.defaultdict(list)
        for line in sys.stdin:
            p = line.strip()
            if p and os.path.splitext(p)[1].lower() in VID:
                bydir[os.path.dirname(p)].append(p)
        return sorted(bydir)
    return [a for a in argv if not a.startswith("-")]


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("label", "group"):
        print(__doc__); return 2
    mode, dirs = argv[0], folders(argv[1:])
    for d in dirs:
        try:
            names = sorted(f for f in os.listdir(d)
                           if os.path.splitext(f)[1].lower() in VID)
        except OSError as e:
            print(f"{d}: unreadable ({e})"); continue
        if not names:
            continue
        try:
            hint = folder_hint(d)
            if mode == "label":
                print(f"{d}  [{len(names)} files]\n   {label(names, hint)}")
            else:
                rows, covered = group(names, hint)
                print(f"{d}  [{len(names)} files] lines={len(rows)} covered={covered}/{len(names)}")
                for i, lab, kind in sorted(rows):
                    print(f"   {i:3d}  {kind:8s} {lab!r:40s} {names[i]}")
        except Exception as e:
            print(f"{d}: VLM error {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
