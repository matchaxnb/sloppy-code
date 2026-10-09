#!/usr/bin/env python3
"""Name anthology titles from their opening subtitles, using the local VLM.

A separate worker, run as a *rename phase* after makemkv: each MKV of an
anthology (a disc of shorts, a compilation) is named from the subtitles at the
beginning of the title. Text subtitles (SRT/ASS) are read directly; bitmap
subtitles (PGS/VobSub) are rendered to PNG and OCR'd by the VLM (a vision model,
so no tesseract is needed).

No frame sampling: the video is never sampled — only the subtitle streams are
consulted. A title with no subtitle stream at the start cannot be named this way
and is reported for review rather than guessed.

Behaviour: a title above the confidence threshold is renamed in place (with
`--apply`); anything below, or unreadable, is written to a review file and left
alone. Renaming is a plain `os.rename` (a rename is metadata, not a reflink).

    anthology_names.py DIR [--apply] [--min-confidence 0.6]
                             [--review review.txt] [--window 120] [--dry-run]

    DIR   a folder of MKVs, e.g. MediaLibrary/Remuxes/<Disc>/
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, tempfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import media_ids as M  # noqa: E402
import config as C  # noqa: E402

TEXT_SUB = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text", "srt"}
BITMAP_SUB = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

# Fraction of the frame kept when sampling, as a centred crop. Studio title cards
# and credits sit inside the title-safe area, so the outer border is background
# that costs the vision model pixels for nothing: cropping to ~88% keeps the card
# while discarding the frame edge, giving each tile (or a finer grid) more usable
# resolution. Tiles are padded back to 4:3 afterwards, so the montage stays even.
SAFE_ZONE = 0.88

SYS = ("You are given the subtitles (or on-screen text) from the OPENING of a "
       "short film. Identify the title of the work, and the series it belongs "
       "to if there is one. Answer with exactly three fields separated by '||', "
       "nothing else:\n"
       "TITLE || SERIES || CONFIDENCE\n"
       "TITLE is the title only, or 'UNKNOWN' if the text does not name it.\n"
       "SERIES is the umbrella series/collection the work belongs to (e.g. a "
       "cartoon series banner), or 'none'. It is NEVER the title.\n"
       "CONFIDENCE is a number 0-1: how sure you are this is the real title.")

# Classic American studio cartoons (c.1930-1960) open with a *sequence* of cards
# that are not the title: a studio logo, a character card with an MPAA
# certificate, a Technicolor credit, a series banner. Naming these rejects up
# front is far more reliable than letting the model guess — "Droopy" and
# "Merrie Melodies" both read at confidence 1.0 and are wrong.
CARTOON_CHARACTERS = (
    "Bugs Bunny, Elmer Fudd, Daffy Duck, Porky Pig, Droopy, Tom, Jerry, Sylvester, "
    "Tweety, Road Runner, Wile E. Coyote, Yosemite Sam, Foghorn Leghorn, Tasmanian Devil, "
    "Marvin the Martian, Pepé Le Pew, Speedy Gonzales, Mickey Mouse, Donald Duck, Goofy, "
    "Pluto, Woody Woodpecker, Popeye, Bluto, Betty Boop, Chilly Willy, Andy Panda, "
    "Huckleberry Hound, Quick Draw McGraw, Snagglepuss")
CARTOON_BANNERS = ("Merrie Melodies, Looney Tunes, MGM Cartoon, Tom and Jerry, Silly "
                   "Symphonies, Happy Harmonies, Color Classics, Terrytoons, Noveltoons, "
                   "Screen Songs, Swing Symphonies")
CARTOON_STUDIOS = ("Metro-Goldwyn-Mayer (MGM), Warner Bros, Leon Schlesinger, Walt Disney, "
                   "Fleischer, Walter Lantz, Columbia, RKO, Paramount, 20th Century Fox, "
                   "Universal, Western Electric")
CARTOON_CREDITS = ("Directed by, Produced by, Story, Animation, Music, Color by / "
                   "Technicolor, Approved / MPAA Certificate No., Copyright, "
                   "All rights reserved, Presented by")

SYS_CARTOON = (
    "You are given a 2x2 grid of frames from the opening of ONE classic American "
    "studio cartoon (c.1930-1960). Identify the CARTOON'S TITLE and the SERIES "
    "it was released under. Answer with exactly three fields separated by '||': "
    "TITLE || SERIES || CONFIDENCE.\n"
    "The title card names the film. It is usually large and often in quotes, and "
    "is OFTEN accompanied by (or immediately before) the 'Directed by ...' card — "
    "treat a nearby director credit as a strong hint toward the title card, but do "
    "not require it: some title cards are bare.\n"
    "SERIES is the release series carried on the opening banner of most studio "
    "cartoons, and it is NEVER the title. When a tiled card is one of these, do "
    "not answer it as TITLE — answer it as SERIES instead:\n"
    f"- a series banner: {CARTOON_BANNERS}\n"
    "Answer SERIES 'none' when there is no such banner.\n"
    "These are NOT the title and NOT a series — reject them:\n"
    f"- a character's name: {CARTOON_CHARACTERS}\n"
    f"- a studio name or logo: {CARTOON_STUDIOS}\n"
    f"- a credit or certificate line: {CARTOON_CREDITS}\n"
    "- a scenery/background credit, e.g. 'THE PAINTED DESERT / PAINTED BY ...', "
    "or any card naming an artist, department or place rather than the film\n"
    "A card may name a recurring character, franchise or sub-series the work "
    "belongs to rather than the work itself. That is the SERIES, not the TITLE: "
    "an individual work has its OWN title card, when one is present, usually "
    "elsewhere in the grid. Prefer the individual title over any umbrella name, "
    "and never answer an umbrella name as the TITLE. If only a "
    "character/franchise/sub-series card is visible and no distinct individual "
    "title appears, answer 'UNKNOWN || <that name> || 0'.\n"
    "If no tile shows the work's title, answer 'UNKNOWN || none || 0'.")


def probe_subs(path: str) -> list[dict]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s",
                          "-show_entries", "stream=index,codec_name:stream_tags=language,title",
                          "-of", "json", path], capture_output=True, text=True, timeout=60).stdout
    try:
        return json.loads(out).get("streams", [])
    except ValueError:
        return []


def read_text_subs(path: str, stream_index: int, window: int) -> str:
    """The subtitle text in the first `window` seconds, as plain lines."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-t", str(window),
                          "-i", path, "-map", f"0:{stream_index}", "-f", "srt", "-"],
                         capture_output=True, text=True, timeout=120).stdout
    lines = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or ln.isdigit() or "-->" in ln:
            continue
        ln = re.sub(r"<[^>]+>", "", ln)          # strip ASS/html inline tags
        ln = re.sub(r"\{[^}]*\}", "", ln)
        if ln and ln not in lines:
            lines.append(ln)
    return "\n".join(lines[:40])


def render_bitmap_subs(path: str, stream_index: int, window: int, outdir: str) -> list[str]:
    """Render bitmap subtitles in the window to PNGs; return their paths.

    ffmpeg decodes PGS/VobSub and can encode them to PNG; each subtitle becomes
    one image. Best-effort — some builds refuse, in which case nothing is read.
    """
    subprocess.run(["ffmpeg", "-v", "error", "-t", str(window), "-i", path,
                    "-map", f"0:{stream_index}", "-c:s", "png", "-vsync", "0",
                    os.path.join(outdir, "sub_%04d.png")],
                   capture_output=True, text=True, timeout=180)
    return sorted(os.path.join(outdir, f) for f in os.listdir(outdir) if f.endswith(".png"))


def _post(messages: list, max_tokens: int = 160) -> str:
    endpoint, model = C.vlm_conf()
    body = {"temperature": 0, "max_tokens": max_tokens, "messages": messages}
    if model:
        body["model"] = model          # optional: single-model servers omit it
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.load(r)
    c = out["choices"][0]["message"].get("content")
    if isinstance(c, list):
        c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
    return (c or "").strip()


def ask_title_text(text: str) -> tuple[str, str, float]:
    msg = [{"role": "system", "content": SYS},
           {"role": "user", "content": f"Opening subtitles:\n{text}"}]
    return parse_reply(_post(msg))


def ask_title_image(png_paths: list[str]) -> tuple[str, str, float]:
    """Read a title from frames, ONE AT A TIME.

    A batch of mixed frames (logo, credits, mid-action) confuses the model — it
    answers UNKNOWN. Asking per frame and taking the best title-card hit is far
    more reliable: a real title card yields a confident TITLE, others yield
    NO TEXT or 'UNKNOWN'.
    """
    import base64
    best = ("", "", 0.0)
    for p in png_paths[:12]:
        try:
            b = base64.b64encode(open(p, "rb").read()).decode()
        except OSError:
            continue
        text = ("This is one frame from the opening of a film. If it is the "
                "film's TITLE CARD (the film's name, usually with a director "
                "credit), answer with the title on its own. A character's name "
                "with a certificate number is NOT the title. Otherwise answer "
                "exactly NO TITLE.")
        content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}},
                   {"type": "text", "text": text}]
        reply = _post([{"role": "system", "content": SYS},
                       {"role": "user", "content": content}])
        t, s, c = parse_reply(reply)
        if t and t.upper() not in ("UNKNOWN", "NO TITLE", "NO_TEXT") and c > best[2]:
            best = (t, s, c)
    return best


def parse_reply(reply: str) -> tuple[str, str, float]:
    """(title, series, confidence) from a model reply.

    TITLE || SERIES || CONFIDENCE is the current contract. A bare
    'TITLE || CONFIDENCE' reply (an older prompt, or the model dropping a field)
    still parses: the numeric field is the confidence, and everything else is the
    title. SERIES is '' when absent, and 'none' is normalised to ''.
    """
    line = reply.strip().splitlines()[0] if reply.strip() else ""
    parts = [p.strip() for p in line.split("||")]
    title, series, conf = "", "", 0.0
    if parts:
        title = parts[0].strip().strip('"').strip()
    for p in parts[1:]:
        m = re.search(r"[0-9]*\.?[0-9]+", p)
        if m and not conf:
            conf = max(0.0, min(1.0, float(m.group(0))))
        elif p and not series:
            series = p.strip().strip('"').strip()
    if series.lower() in ("none", "no", "n/a", "-", "unknown", "series", "banner"):
        series = ""
    # The model sometimes echoes the schema's placeholder words instead of a
    # value ("TITLE || SERIES || 0.9"). Those are not titles; drop them, or a
    # file would be renamed to "TITLE".
    if title.upper() in ("TITLE", "FILM", "MOVIE", "SHORT", "UNKNOWN", "NO TITLE"):
        title = ""
    if series.upper() in ("SERIES", "BANNER", "COLLECTION"):
        series = ""
    return title, series, max(0.0, min(1.0, conf))


def montages(pngs: list[str], outdir: str, cols: int = 2, rows: int = 2) -> list[str]:
    """Combine frames into cols×rows montage images (one VLM call reads the grid).

    A *single* image with tiles reads far better than several images in one
    message (which the model answers UNKNOWN for): the model sees one picture and
    can name the tile that is a title card. A larger grid (4x4) gives the whole
    opening card sequence at once, so the banner/character/title order is visible.
    """
    out = []
    per = cols * rows
    for i in range(0, len(pngs), per):
        batch = pngs[i:i + per]
        if len(batch) < 2:
            out.append(batch[0])
            continue
        while len(batch) < per:                 # pad with the last frame
            batch.append(batch[-1])
        mp = os.path.join(outdir, f"montage_{i//per:02d}.png")
        # uniform tile size (sample_frames pads to WxH), so the grid is regular
        layout = "|".join(f"{'w0*' + str(c) if c else '0'}_{'h0*' + str(r) if r else '0'}"
                          for r in range(rows) for c in range(cols))
        inputs = sum([["-i", b] for b in batch], [])
        labels = "".join(f"[{k}]" for k in range(per))
        subprocess.run(["ffmpeg", "-v", "error"] + inputs +
                       ["-filter_complex",
                        f"{labels}xstack=inputs={per}:layout={layout}[v]",
                        "-map", "[v]", "-frames:v", "1", "-y", mp],
                       capture_output=True, text=True, timeout=90)
        if os.path.exists(mp):
            out.append(mp)
    return out


def sample_frames(path: str, window: int, outdir: str, every: float = 2.0) -> list[str]:
    """Frames across the first `window` seconds, for reading a video-only title
    card. One ffmpeg pass with `fps` — seeking per-frame spawned a process for
    each frame and dominated the run."""
    subprocess.run(["ffmpeg", "-v", "error", "-t", str(window), "-i", path,
                    "-vf", (f"crop=iw*{SAFE_ZONE}:ih*{SAFE_ZONE},"
                            f"fps=1/{every:g},scale=640:480:force_original_aspect_ratio=decrease,"
                            "pad=640:480:(ow-iw)/2:(oh-ih)/2"),
                    "-y", os.path.join(outdir, "f_%03d.png")],
                   capture_output=True, text=True, timeout=180)
    return sorted(os.path.join(outdir, f) for f in os.listdir(outdir) if f.startswith("f_"))


def sample_keyframes(path: str, window: int, outdir: str, every: float = 2.0) -> list[str]:
    """Decode only KEYFRAMES and keep roughly one per `every` seconds.

    Keyframes are ~1 s apart on a Blu-ray (GOP ≈ 24 at 24 fps) but far denser
    around cuts; a title card dwells 3-5 s, so a 2 s cadence cannot miss a card
    while discarding the cut clusters that would fill montage slots with
    near-identical frames. Decoding I-frames only is far cheaper than the `fps`
    filter in `sample_frames`. NOTE: `-vsync` was removed in ffmpeg n9 — decoding
    to images uses `-fps_mode passthrough`, which keeps exactly the decoded
    keyframes.
    """
    subprocess.run(["ffmpeg", "-v", "error", "-t", str(window), "-skip_frame", "nokey",
                    "-i", path, "-fps_mode", "passthrough",
                    "-vf", f"crop=iw*{SAFE_ZONE}:ih*{SAFE_ZONE},"
                           "scale=640:480:force_original_aspect_ratio=decrease,"
                           "pad=640:480:(ow-iw)/2:(oh-ih)/2",
                    "-f", "image2", "-y", os.path.join(outdir, "k_%05d.png")],
                   capture_output=True, text=True, timeout=180)
    ks = sorted(os.path.join(outdir, f) for f in os.listdir(outdir) if f.startswith("k_"))
    if not ks:
        return []
    # Subsample to the cadence: keyframes are near-uniform away from cuts, so
    # (count / window) is their rate and `every` seconds is `every * rate` frames.
    step = max(1, int(round(every * len(ks) / max(1.0, float(window)))))
    return ks[::step]


def ask_questions(mp_path: str, questions: list, system: str = SYS,
                  max_tokens: int = 160) -> list[str]:
    """Ask several *separate* questions about one image, image-first.

    Every request begins with the same image, so the vision tokens are an
    invariant prefix: a server with prefix caching (vLLM, LM Studio) prefills
    them once and each further question is a cheap continuation — many answers
    for close to the price of one. Each question also stays focused, which reads
    better than one crowded instruction.

    A question may be a string, or a callable ``prior -> str`` so a later
    question can be *refined* by the answers already collected (a pipeline).
    Returns the raw reply per question, in order.
    """
    import base64
    try:
        b = base64.b64encode(open(mp_path, "rb").read()).decode()
    except OSError:
        return []
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}}
    replies: list[str] = []
    for q in questions:
        text = q(replies) if callable(q) else q
        if not text:
            continue
        # image first, question last: the shared prefix is cached across calls
        content = [img, {"type": "text", "text": text}]
        try:
            replies.append(_post([{"role": "system", "content": system},
                                  {"role": "user", "content": content}], max_tokens=max_tokens))
        except Exception as e:
            replies.append(f"!{type(e).__name__}: {e}")
    return replies


def ask_montage(mp_path: str, system: str = SYS) -> tuple[str, str, float]:
    """Ask the model about a grid of opening frames (one image, several tiles)."""
    text = ("This is a grid of frames from the opening of one film "
            "(tiles read left to right, top to bottom).")
    if system is SYS:      # generic: restate the card rule inline
        text += (
            " One tile is the film's TITLE CARD, which names the film and usually sits "
            "with the director credit (\"TITLE\" / Directed by ...). Another may show a "
            "CHARACTER's name with a certificate number — that is NOT the title. "
            "Answer TITLE, then the SERIES if a banner names one, then CONFIDENCE.")
    r = ask_questions(mp_path, [text], system)
    return parse_reply(r[0]) if r else ("", "", 0.0)


def pick_title(cards: list, pngs: list, td: str, system: str = SYS,
               gc: int = 2, gr: int = 2) -> tuple[str, str, float]:
    """Choose the film's TITLE and SERIES from several card readings.

    Confidence cannot decide: a character card ("DROOPY") and the title card
    ("DUMB-HOUNDED") both read at 1.0. The title card is the one that names the
    *film* (usually with a director credit); a bare name next to a certificate
    number is a character. When there is a single candidate it is used; otherwise
    the VLM arbitrates over the candidates.
    """
    titles = [t for t, _s, _c, _i in cards]
    # the series, if any card named a banner; first non-empty wins
    series = next((s for _t, s, _c, _i in cards if s), "")
    uniq = list(dict.fromkeys(titles))
    if len(uniq) == 1:
        return uniq[0], series, cards[0][2]
    import base64
    listing = "\n".join(f"- {t}" for t in uniq)
    question = ("Cards read from the opening of ONE film, in order:\n" + listing +
                "\n\nWhich is the FILM'S TITLE? Reject a character's name with a "
                "certificate number, a studio name, a series banner, and any card "
                "that names an artist, place or department rather than the film. "
                "A nearby 'Directed by ...' credit favours the card next to it. "
                "Answer TITLE || SERIES || CONFIDENCE.")
    # Image first, question last (better prefill/attention on the image)
    content = []
    mps = montages(pngs, td, cols=gc, rows=gr)
    i0 = cards[0][3]
    if i0 < len(mps):
        try:
            b = base64.b64encode(open(mps[i0], "rb").read()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + b}})
        except OSError:
            pass
    content.append({"type": "text", "text": question})
    try:
        t, s2, c = parse_reply(_post([{"role": "system", "content": system},
                                      {"role": "user", "content": content}]))
    except Exception:
        return uniq[0], series, cards[0][2]
    if t and t.upper() not in ("UNKNOWN", "NO TITLE"):
        return t, s2 or series, max(c, 0.6)
    return uniq[0], series, cards[0][2]


DOMAIN_SYS = (
    "You are given a 2x2 grid of frames from the opening of ONE audiovisual work. "
    "Report its FORM and STRUCTURE, not its genre. Answer with exactly:\n"
    "FORM || STRUCTURE || STYLE\n"
    "FORM is one of: animated | live-action\n"
    "STRUCTURE is one of: short | feature | series-episode\n"
    "STYLE is a short free note (or 'none'), e.g. 'documentary-like' for a fiction "
    "shot like a documentary, 'technicolor cartoon', 'anime'.\n"
    "Example: animated || short || classic Hollywood cartoon")


def classify_domain(pngs: list[str], td: str) -> str:
    """Best-effort form/структure/style from opening frames. Advisory only."""
    mps = montages(pngs, td)
    if not mps:
        return ""
    import base64
    try:
        b = base64.b64encode(open(mps[0], "rb").read()).decode()
    except OSError:
        return ""
    reply = _post([{"role": "system", "content": DOMAIN_SYS},
                   {"role": "user", "content": [
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}},
                       {"type": "text", "text": "Classify this work's form and structure."}]}])
    return reply.strip().splitlines()[0] if reply.strip() else ""


def domain_style_file(path: str, window: int) -> str:
    """Convenience: classify one file's domain (opens its own temp dir)."""
    with tempfile.TemporaryDirectory() as td:
        pngs = sample_frames(path, min(window, 30), td)
        return classify_domain(pngs, td) if pngs else ""


def _verdict_text(struct: str, conf: float, rows: list, note: str = "") -> str:
    seen = sorted({s for _f, _t, s in rows if s})
    head = f"# disc structure: {struct or '(unknown)'}  conf {conf:.2f}\n"
    if note:
        head += f"# {note}\n"
    if seen:
        head += f"# series seen: {', '.join(seen)}\n"
    head += "# <file>\t<title>\t<series>\n"
    return head + "\n".join(f"{f}\t{t or '?'}\t{s}" for f, t, s in rows) + "\n"


def derive_structure(rows: list) -> str:
    """anthology / series / collection / single, DERIVED from the readings.

    The vision model already read each work's title and release line; the
    structure of the disc is a function of those, so asking the model again is a
    redundant round-trip. The rule:
      * one work (or none) -> single
      * every work shares ONE non-empty release line -> collection
      * the works are numbered instalments (a name repeated with an incrementing
        number, or every title leading with a number) -> series
      * otherwise (distinct, unnumbered titles, mixed lines) -> anthology
    """
    titled = [(t or "", s or "") for _f, t, s in rows]
    if len(titled) <= 1:
        return "single"
    lines = {s for _t, s in titled if s}
    # shared single release line on distinct works = a collection
    if len(lines) == 1 and all(s for _t, s in titled):
        if not _looks_numbered([t for t, _s in titled]):
            return "collection"
    if _looks_numbered([t for t, _s in titled]):
        return "series"
    return "anthology"


_NUM_RE = re.compile(r"(?i)(?:^|\b)(?:s\d{1,2}\s*)?(?:e|ep|episode|part|ep)?\s*\d{1,3}(?:v\d+)?\s*$")


def _looks_numbered(titles: list) -> bool:
    """True when the works read as numbered instalments of one name.

    Either every title ends in a number ("Foo 01", "Foo 02"), or they share a
    leading name and differ only in a trailing number — the two shapes a series
    takes after a rename.
    """
    if len(titles) < 2 or any(not t for t in titles):
        return False
    if all(_NUM_RE.search(t) for t in titles):
        return True
    # a shared stem with distinct trailing numbers: "Show 1", "Show 2"
    stems = {re.sub(r"(?i)\s*\d{1,3}(?:v\d+)?\s*$", "", t).strip().lower() for t in titles}
    nums = {re.search(r"(\d{1,3})(?:v\d+)?\s*$", t) for t in titles}
    return len(stems) == 1 and all(nums) and len({n.group(1) for n in nums if n}) >= 2


def classify_folder(rows: list) -> tuple[str, float]:
    """(structure, confidence) for a whole disc — derived, not asked.

    Kept as the layer's entry point, but the answer is a function of the per-work
    readings the model already returned (see `derive_structure`): asking again
    over a frame grid was a redundant round-trip that spent an image prefill to
    recompute what the titles and release lines already imply.
    """
    if len(rows) <= 1:
        return ("single", 0.3) if rows else ("", 0.0)
    return derive_structure(rows), 0.9


_CHAR_WORDS = {w.strip().lower() for w in CARTOON_CHARACTERS.replace(" and ", ", ").split(",")}
_CHAR_WORDS |= {"droopy", "bugs", "daffy", "porky", "elmer", "tom", "jerry", "sylvester",
                "tweety", "goofy", "pluto", "mickey", "donald", "popeye", "woody"}


def _looks_like_character(title: str) -> bool:
    """True when the answer is exactly a character name (not a film title)."""
    return title.strip().lower() in _CHAR_WORDS


def _title_from_credited_card(pngs: list[str]) -> tuple[str, str]:
    """(title, series) from a frame whose card also carries a director credit.

    For a title that shares a word with a character card ("SEÑOR DROOPY" beside a
    bare "DROOPY" card), the credited card is the authority: read each frame and,
    where it shows 'directed by', take the text *above* the credit — that is the
    film's title, not the character name.
    """
    import base64
    for p in pngs:
        try:
            b = base64.b64encode(open(p, "rb").read()).decode()
        except OSError:
            continue
        reply = _post([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}},
            {"type": "text", "text": "Reply with the exact text on this card, or NO TEXT."}]}], max_tokens=80)
        rl = reply.lower()
        if "directed by" not in rl and "dirigé par" not in rl and "directed" not in rl:
            continue
        head = re.split(r"(?i)directed by|dirigé par|directed", reply)[0]
        # A title card's text varies (multi-line titles, a stray character name
        # under the title). Rather than a pile of line-level rules, hand the text
        # above the credit to the model and let it name the title.
        head = " ".join(ln.strip() for ln in head.splitlines() if ln.strip()).strip()
        head = re.sub(r"\s{2,}", " ", head)
        if not head:
            continue
        t, s, _c = parse_reply(_post(
            [{"role": "system", "content":
              "The user gives the card text printed above the 'Directed by' line on "
              "one film's title card. The card may also carry the release series "
              "banner. Reply TITLE || SERIES || 1, with SERIES 'none' if no banner "
              "is present."},
             {"role": "user", "content": head}], max_tokens=40))
        t = t.strip().strip('"').strip("'").strip("“”")
        s = s.strip().strip('"').strip("'").strip("“”")
        if 2 <= len(t) <= 120:
            return t, s
    return "", ""


def title_for(path: str, window: int, frames: bool = True,
              domain: str | None = None, grid: str = "3x3",
              keyframes: bool = False) -> tuple[str, str, float, str]:
    """(title, series, confidence, method) for one file. method: frames|image|text|none.

    A **title card** in the video is the reliable signal — it carries the work's
    own title, whereas subtitle streams are often in a *different* language
    (a Dutch/French sub on an English cartoon reads as dialogue, not a title).
    So frames are tried first; subtitle text is the fallback. `frames=False`
    reverts to subtitles only.

    `series` is the release series named on the opening banner (e.g. "Merrie
    Melodies") or '' — it is the anthology the short belongs to, and it is
    carried alongside the title so a downstream pass can group a collection.

    `domain` (from `classify_domain`) selects a domain-specific title prompt; it
    is advisory — it only changes which cards are trusted, never gates the read.
    If `grid` is given as "COLSxROWS" it controls the montage density (default 4x4);
    a denser grid shows the whole opening card sequence at once, which is what
    tells a series banner from the film's own title card.
    """
    d = (domain or "").lower()
    system = SYS_CARTOON if ("cartoon" in d or "classic" in d) else SYS
    try:
        gc, gr = (int(x) for x in (grid or "4x4").lower().split("x"))
    except ValueError:
        gc, gr = 4, 4
    if frames:
        with tempfile.TemporaryDirectory() as td:
            # The title card lives in the first ~18 s; sampling beyond that pulls
            # in-film signage ("MALIBU SALOON") which the model then mistakes for
            # the title. A tight, dense window beats a wide one: context *quality*
            # over quantity. `window` raises it for titles whose card comes late.
            if keyframes:
                # Decode only keyframes, subsampled to a 2 s cadence: a title
                # card dwells 3-5 s, so this cannot miss one, and it avoids the
                # fps-filter decode (far cheaper on a long Blu-ray title).
                pngs = sample_keyframes(path, min(window, 24), td, every=2.0)
            else:
                pngs = sample_frames(path, min(window, 18), td, every=1.2)
            if pngs:
                # A montage may hold several cards: a bare CHARACTER name
                # ("DROOPY" + certificate) and the real TITLE card. Confidence is
                # useless here — both read 1.0 — so the choice is by *content*:
                # collect every card, reject the character/certificate ones, and
                # prefer the title. See `pick_title`.
                cards = []
                for i, mp in enumerate(montages(pngs, td, cols=gc, rows=gr)):
                    t, s, c = ask_montage(mp, system)
                    if t and t.upper() not in ("UNKNOWN", "NO TITLE"):
                        cards.append((t, s, c, i))
                # The grid can MISS a title that shares a word with a character
                # card ("SEÑOR DROOPY": the bare "DROOPY" card trips the reject
                # rule). It can also fire on a tile the montage diluted. So when
                # the grid found nothing, or its pick looks like a bare character
                # name, confirm per-frame — where the title card reads cleanly.
                picked = pick_title(cards, pngs, td, system, gc, gr) if cards else ("", "", 0.0)
                if not picked[0] or _looks_like_character(picked[0]):
                    ct, cs = _title_from_credited_card(pngs)
                    if ct:
                        return ct, cs or picked[1], 0.9, "frames"
                if picked[0]:
                    return picked[0], picked[1], picked[2], "frames"
    subs = probe_subs(path)
    for s in subs:
        codec = (s.get("codec_name") or "").lower()
        if codec in TEXT_SUB:
            txt = read_text_subs(path, s["index"], window)
            if txt:
                t, se, c = ask_title_text(txt)
                if t and t.upper() != "UNKNOWN":
                    return t, se, c, "text"
    for s in subs:
        codec = (s.get("codec_name") or "").lower()
        if codec in BITMAP_SUB:
            with tempfile.TemporaryDirectory() as td:
                pngs = render_bitmap_subs(path, s["index"], window, td)
                if pngs:
                    t, se, c = ask_title_image(pngs)
                    if t and t.upper() != "UNKNOWN":
                        return t, se, c, "image"
    return "", "", 0.0, "none"


def title_from_dir(dirpath: str) -> str:
    """The title implied by a disc folder name, via the pipeline's own parser.

    A feature film's own name is in the *directory* (the release name), so no
    vision model is needed: `Lutine.2016.DVD9.PAL...` -> "Lutine (2016)". This is
    the whole rule for feature films — the directory is the authority.
    """
    base = os.path.basename(dirpath.rstrip(os.sep))
    g = M.parse_with_guessit(base)
    title = g.get("title") or base
    year = M._int_year(g.get("year"))
    return f"{title} ({year})" if year else title


def say(msg: str) -> None:
    print(msg, flush=True)


def plausible(title: str) -> bool:
    if not title or title.upper() == "UNKNOWN":
        return False
    if len(title) < 2 or len(title) > 120:
        return False
    # a studio / credit / notice card is not the work's title. These read at
    # confidence 1.0 too, so content must be filtered, not just confidence.
    bad = re.compile(r"(?i)\b(metro[- ]goldwyn[- ]mayer|\bm\.?g\.?m\b|cartoon\b|technicolor|"
                     r"color by|certificate|approved|directed by|produced by|presents?\b|"
                     r"all rights|copyright|released? by|distributed by|warner bros|"
                     r"a \w+ (?:cartoon|presentation|feature))\b")
    if bad.search(title):
        return False
    return True


def normalize_title(title: str, src_stem: str = "") -> str:
    """Tidy the model's title and drop answers that cannot be right.

    - strip a trailing parenthetical subtitle: "X (BUCK OF THE MONTH)" -> "X";
    - drop a trailing credit glued on with a dash/pipe;
    - reject a *truncated* echo of the source name: if the source stem begins
      with the title and has substantially more of it (a word was cut), keep the
      longer source stem instead — "WILD and Wo" from "WILD and WOOLFY".
    """
    t = title.strip().strip('"').strip()
    t = re.split(r"\s+[|]\s+|\s+-\s+(?:Directed|Produced|A \w+ Cartoon)\b", t)[0].strip()
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()      # trailing parenthetical
    # truncation guard: the source stem (episode aside) is the ground truth name
    src = re.sub(r"[_.]", " ", src_stem or "").strip()
    if src and len(src) > len(t) + 1 and src.lower().startswith(t.lower()):
        return src
    return t


def chapter_count(path: str) -> int:
    """Number of chapters in the container (0 if none)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "csv=p=0",
                          "-i", path], capture_output=True, text=True, timeout=60).stdout
    return len([l for l in out.splitlines() if l.strip()])


COMPILATION_CHAPTERS = 8   # more than this = a "play all" of several works


def _duration(path: str) -> float:
    """Duration in seconds (0 if unknown)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def _run_feature(d: str, vids: list[str], args, apply: bool, review: str) -> int:
    """Feature-film mode: the disc folder name is the title; the longest file is
    the feature. No vision model — a feature's name is in the release name."""
    title = title_from_dir(d)
    if not vids:
        print(f"no video files in {d}")
        return 0
    ranked = sorted(vids, key=lambda f: _duration(os.path.join(d, f)), reverse=True)
    feature = ranked[0]
    others = ranked[1:]
    dur = _duration(os.path.join(d, feature))
    safe = M.sanitize(title)
    ext = os.path.splitext(feature)[1]
    target = os.path.join(d, safe + ext)
    say(f"feature: {title}   ({dur/60:.0f} min, largest of {len(vids)} file(s))")
    say(f"  {feature}  ->  {safe}{ext}")
    for f in others:
        say(f"  review:  {f}   (other title on the disc, {_duration(os.path.join(d, f))/60:.0f} min)")

    if apply and feature != os.path.basename(target):
        if os.path.exists(target) and os.path.realpath(target) != os.path.realpath(os.path.join(d, feature)):
            say(f"  skip: target exists: {os.path.basename(target)}")
        else:
            os.rename(os.path.join(d, feature), target)
            say(f"  renamed -> {os.path.basename(target)}")
    with open(review, "w", encoding="utf-8") as fh:
        fh.write(f"# Feature: {title}\n# Renamed: {feature} -> {os.path.basename(target)}\n")
        fh.write("# Other titles on the disc (left alone; a disc can hold extras):\n")
        for f in others:
            fh.write(f"{f}\n")
    print(f"\n1 feature renamed, {len(others)} other file(s) for review"
          f"{'' if apply else ' (dry run — pass --apply to rename)'}")
    print(f"review file: {review}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--apply", action="store_true", help="rename (default: propose only)")
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--review", default=None, help="review file (default: <dir>/review-titles.txt)")
    ap.add_argument("--window", type=int, default=120, help="seconds from the start to read")
    ap.add_argument("--grid", default="3x3", help="montage density, e.g. 3x3 (default) or 2x2. "
                    "A 4x4 grid makes a 2560x1920 montage, which the vision "
                    "model downsizes until each 640x480 tile is unreadable; 3x3 "
                    "(1920x1440) keeps card text legible.")
    ap.add_argument("--domain", default=None,
                    help="domain hint (e.g. cartoon-classic) to tune the title prompt; "
                         "advisory only. 'auto' classifies per file.")
    ap.add_argument("--no-frames", action="store_true",
                    help="do not sample video frames (subtitles only)")
    ap.add_argument("--feature", action="store_true",
                    help="feature-film mode: no VLM. The disc folder name IS the "
                         "title; the longest file (by duration) is the feature and "
                         "is renamed to it. Other files are left for review.")
    ap.add_argument("--dry-run", action="store_true", help="force propose-only")
    ap.add_argument("--keyframes", action="store_true",
                    help="sample decoded KEYFRAMES at a 2s cadence instead of an fps filter")
    args = ap.parse_args(argv)

    d = os.path.abspath(args.dir)
    if not os.path.isdir(d):
        raise SystemExit(f"not a directory: {d}")
    review = args.review or os.path.join(d, "review-titles.txt")
    apply = args.apply and not args.dry_run

    vids = sorted(f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in M.VIDEO_EXT)

    if args.feature:
        return _run_feature(d, vids, args, apply, review)

    renamed, review_lines, failed, playalls = 0, [], 0, 0
    series_index: list[str] = []      # <series>\t<file>\t<title>
    readings: list[tuple] = []        # (file, title, series) for the disc verdict
    for f in vids:
        p = os.path.join(d, f)
        # a title with many chapters is a "play all" of several works: its chapter
        # starts are where each work's title card sits, so do not name the whole
        # file after its first short — flag it for the split pass instead.
        try:
            nch = chapter_count(p)
        except subprocess.SubprocessError:
            nch = 0
        if nch > COMPILATION_CHAPTERS:
            review_lines.append(f"{f}\tPLAY-ALL ({nch} chapters)\t— split per chapter")
            playalls += 1
            say(f"  play-all: {f}   ({nch} chapters — split by chapter)")
            continue
        try:
            dom = args.domain
            if dom == "auto":
                dom = domain_style_file(p, args.window)   # advisory hint
            title, series, conf, method = title_for(p, args.window, frames=not args.no_frames,
                                                    domain=dom, grid=args.grid,
                                                    keyframes=args.keyframes)
        except subprocess.SubprocessError as e:
            title, series, conf, method = "", "", 0.0, f"error:{e}"
        series = normalize_title(series, "") if series else ""   # tidy; series is not de-truncated
        title = normalize_title(title, os.path.splitext(f)[0])   # tidy / de-truncate
        readings.append((f, title, series))
        if series:
            series_index.append(f"{series}\t{f}\t{title or ''}")
        series_tag = f"  [{series}]" if series else ""
        if title and conf >= args.min_confidence and plausible(title):
            safe = M.sanitize(title)
            ext = os.path.splitext(f)[1]
            target = os.path.join(d, safe + ext)
            if os.path.exists(target) and target != p:
                review_lines.append(f"{f}\tSKIP (target exists)\t{title} conf={conf:.2f} [{method}]")
                failed += 1
                continue
            if apply:
                try:
                    os.rename(p, target)
                    renamed += 1
                    say(f"  renamed: {f}  ->  {safe}{ext}   (conf {conf:.2f}, {method}){series_tag}")
                except OSError as e:
                    review_lines.append(f"{f}\tRENAME FAILED {e}\t{title}")
                    failed += 1
            else:
                renamed += 1
                say(f"  PROPOSE: {f}  ->  {safe}{ext}   (conf {conf:.2f}, {method}){series_tag}")
        else:
            review_lines.append(f"{f}\t{title or '(none)'} conf={conf:.2f} [{method}]{series_tag}")
            failed += 1
            say(f"  review:  {f}   ({title or 'no title'}, conf {conf:.2f}, {method}){series_tag}")

    # Disc-structure verdict, derived from the readings (no VLM call: the
    # structure is a function of the titles and release lines already read).
    struct, struct_conf = classify_folder(readings)
    if struct:
        say(f"  disc structure: {struct} (conf {struct_conf:.2f}, {len(readings)} works)")
        vfile = os.path.join(d, "disc-structure.txt")
        with open(vfile, "w", encoding="utf-8") as fh:
            fh.write(_verdict_text(struct, struct_conf, readings, note="derived"))

    if review_lines:
        with open(review, "w", encoding="utf-8") as fh:
            fh.write("# Titles below the confidence threshold, or unreadable. Review and rename by hand.\n")
            fh.write("# <file>\t<proposed title> conf=<n> [<method>] [<series>]\n\n")
            fh.write("\n".join(review_lines) + "\n")
    if series_index:
        sfile = os.path.join(d, "series-index.tsv")
        with open(sfile, "w", encoding="utf-8") as fh:
            fh.write("# Release series read from the opening banner. Group a collection by column 1.\n")
            fh.write("# <series>\t<file>\t<title>\n")
            fh.write("\n".join(sorted(series_index)) + "\n")
    n_series = len({ln.split("\t")[0] for ln in series_index})
    print(f"\n{len(vids)} video(s): {renamed} {'renamed' if apply else 'proposed'}, "
          f"{failed} for review"
          f"{f', {playalls} play-all' if playalls else ''}"
          f"{f', {n_series} series tagged' if series_index else ''}"
          f"{f', disc: {struct}' if struct else ''}"
          f"{'' if apply else ' (dry run — pass --apply to rename)'}")
    if review_lines:
        print(f"review file: {review}")
    if series_index:
        print(f"series index: {sfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
