#!/usr/bin/env python3
"""Sibling-split logic: is a folder a pile of distinct works, or versions of one?

Sources often hold a flat folder of episodes or shorts, e.g.
    Tex Avery - Garden Gopher.mp4
    Tex Avery - Red Hot Riding Hood.mp4
    ...129 files
Ranking those against each other would keep exactly one and junk 128. They are
siblings: each is its own title. The giveaway is a shared prefix up to a
separator, with the remainder being distinct per file.

Pure functions; no I/O beyond the filename list.
"""
from __future__ import annotations
import os, re
from collections import Counter

SEP_RE = re.compile(r"\s[-–—]\s|\.(?=[A-Z])")

# Uniform word tokenisation: any run of separators splits. Used for family
# detection, where a *sequence* of matching tokens is the evidence.
_WORD_SPLIT = re.compile(r"[\s._\-–—/()+]+")


def token_sequence(name: str) -> list[str]:
    """Word tokens of a filename, lowercased: 'cowboy.bebop.e02.1080p.mkv'
    -> ['cowboy','bebop','e02','1080p']."""
    stem = os.path.splitext(name)[0]
    return [t for t in _WORD_SPLIT.split(stem.lower()) if t]


def _tokens(name: str) -> list[str]:
    stem = os.path.splitext(name)[0]
    # split on " - " (the common scene/pack separator) or on dot boundaries
    if re.search(r"\s[-–—]\s", stem):
        return [p.strip() for p in re.split(r"\s[-–—]\s", stem) if p.strip()]
    return [p for p in stem.split(".") if p]


def common_prefix_run(names: list[str], min_len: int = 2, min_group: int = 3) -> list[str]:
    """The longest *contiguous* run of leading tokens every name shares.

    A shared sequence is much stronger evidence of a family than a shared first
    token: "Cowboy Bebop [BDRip]/cowboy.bebop.e02..." and ".../cowboy.bebop.e07..."
    share the whole run ['cowboy','bebop'] before diverging at the episode, while
    two unrelated films in one folder may share only an article. The run stops at
    the first position where the names differ, which is where the per-file part
    (episode number, episode title) begins.

    Returns [] when there is no run of at least `min_len` tokens.
    """
    seqs = [token_sequence(n) for n in names]
    seqs = [s for s in seqs if s]
    if len(seqs) < min_group:
        return []
    run: list[str] = []
    for idx in range(min(len(s) for s in seqs)):
        tok = seqs[0][idx]
        if all(s[idx] == tok for s in seqs):
            run.append(tok)
        else:
            break
    return run if len(run) >= min_len else []


def season_episode_from_remainder(remainder: str):
    """(season, episode) from the first token of a remainder.

    Both or either may be None. Handles every leading token the sources use:
    "01", "e02", "ep07", "episode12", "s01e03", "s0401", "3x01", "09v2".

    Season matters: "s0401" and "s0501" are different instalments, and treating
    the trailing digits alone as the episode would collide them into one group
    when a scan covers more than one season of the same title.
    """
    toks = remainder.split()
    if not toks:
        return None, None
    tok = toks[0]
    m = re.match(r"(?i)^(\d{1,2})x(\d{1,3})$", tok)          # 3x01
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"(?i)^s(\d{1,2})e(\d{1,3})$", tok)         # s01e03
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"(?i)^s(\d{2})(\d{2})$", tok)             # s0401 = s04e01
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"(?i)^s(\d{1,2})$", tok)                    # bare "s04"
    if m:
        return int(m.group(1)), None
    m = re.match(r"(?i)^(?:e|ep|episode)?(\d{1,3})(?:v\d+)?$", tok)   # 01, e02, ep07
    if m:
        return None, int(m.group(1))
    return None, None


def season_from_run(run: list[str]):
    """Season number when the shared run *ends* on a season marker.

    "BUFFY ... Slayer S01 E06 ..." tokenises with 's01' inside the shared run
    (every file has it) and 'e06' as the remainder, so the season is lost unless
    it is read from the run's tail. The season component is uniform for the
    folder, so one value covers every file.
    """
    if not run:
        return None
    m = re.match(r"(?i)^s(\d{1,2})$", run[-1])
    return int(m.group(1)) if m else None


def season_episode_tokens(toks: list[str]):
    """(season, episode) when season and episode are *separate* tokens.

    Sources write "S01 E06" with a space, which tokenising splits in two. Read as
    separate tokens the pair is still unambiguous, so it is worth handling here
    rather than leaving the season blank ("S-E6" in the audit).
    """
    for i in range(len(toks) - 1):
        ms = re.match(r"(?i)^s(\d{1,2})$", toks[i])
        me = re.match(r"(?i)^e(\d{1,3})$", toks[i + 1])
        if ms and me:
            return int(ms.group(1)), int(me.group(1))
    return None, None


def episode_from_remainder(remainder: str):
    """Episode number from the first token of a remainder, or None."""
    return season_episode_from_remainder(remainder)[1]


# Explicit episode forms: "s01e01", "e02", "1x02", "ep07". These name an episode
# unambiguously wherever they appear, unlike a bare "3" or "1".
_EXPLICIT_EP_RE = re.compile(
    r"(?i)^s\d{1,2}e\d{1,3}$|^\d{1,2}x\d{1,3}$|^s\d{2}\d{2}$|^ep?\d{1,3}(?:v\d+)?$|^episode\d{1,3}$")


def episode_anywhere(remainder: str):
    """(season, episode) from anywhere in a remainder, by this rule:

      * an *explicit* episode token ("s01e01", "e02", "1x02") counts wherever it
        appears — "AMICALEMENT_VOTRE_DVD1.S01E01" is an episode even though the
        token before it ("dvd1") is a disc label;
      * a *bare* number counts only as the first token — otherwise
        "R.Kelly...Chapter.1" would read as an episode, when it is a documentary
        in chapters.

    This keeps the reading at the token level; no sub-token surgery is needed.
    """
    toks = remainder.split()
    if not toks:
        return None, None
    for tok in toks:
        if _EXPLICIT_EP_RE.match(tok):
            return season_episode_from_remainder(tok)
    pair = season_episode_tokens(toks)      # "S01 E06" split across tokens
    if pair[1] is not None:
        return pair
    # no explicit marker: accept a leading bare number only
    return season_episode_from_remainder(toks[0])



def leads_with_episode(remainder: str) -> bool:
    return episode_from_remainder(remainder) is not None


def remainders_after(names: list[str], run: list[str]) -> list[str]:
    """Each name with the shared leading run removed; the first token is the
    per-file part ('01', 'e02', 'garden', ...)."""
    out = []
    for n in names:
        seq = token_sequence(n)
        if seq[:len(run)] == run:
            out.append(" ".join(seq[len(run):]))
    return out


def common_prefix(names: list[str], min_group: int = 3, min_unique: int = 3) -> str | None:
    """The shared leading token of a folder's filenames, or None.

    Deliberately *not* the same question as "are these siblings?" — a folder of
    numbered episodes has a common prefix but is one work, not an anthology. The
    prefix is what both readings need; the remainder decides which it is.
    """
    if len(names) < min_group:
        return None
    firsts = []
    for n in names:
        t = _tokens(n)
        if len(t) >= 2:
            firsts.append((t[0], " - ".join(t[1:])))
    if len(firsts) < min_group:
        return None
    prefix, _ = Counter(p for p, _r in firsts).most_common(1)[0]
    group = [(p, r) for p, r in firsts if p == prefix]
    if len(group) < min_group:
        return None
    if len({r for _p, r in group}) < min_unique:
        return None
    return prefix


def sibling_prefixes(names: list[str], min_group: int = 3, min_unique: int = 3) -> str | None:
    """If these files look like an anthology of separate works, return the prefix.

    The reading is decided by the remainder after the shared prefix: a remainder
    that *leads with a number* is an episode of one work, so the folder belongs
    to the series path, not here. A remainder that leads with words is a distinct
    title — "Tex Avery - Garden Gopher", "Tex Avery - Red Hot Riding Hood" — and
    each file is its own work.

    A dot-separated film release ("Do.the.Right.Thing.1989.2160p...") does not
    qualify either: its remainder varies in quality tokens, not in a title.
    """
    prefix = common_prefix(names, min_group, min_unique)
    if prefix is None:
        return None
    group = [(p, r) for p, r in ((_tokens(n)[0], " - ".join(_tokens(n)[1:])) for n in names
                                 if len(_tokens(n)) >= 2) if p == prefix]
    remainders = {r for _p, r in group}
    # numbered remainders are episodes of one work, not distinct works
    num = numbered_remainders(remainders)
    if num >= max(min_unique, int(0.6 * len(remainders))):
        return None
    # reject release-name style names: remainders would be quality tokens
    quality = re.compile(r"(?i)^(?:19|20)\d{2}$|\b(2160p|1080p|720p|480p|x264|x265|hevc|"
                         r"bluray|blu-ray|web-?dl|remux|hdr|dvdrip|aac|dts|ddp?5\.1)\b")
    plausible = [r for r in remainders if not quality.search(r) and re.search(r"[A-Za-z]", r)]
    if len(plausible) < min_unique:
        return None
    return prefix


def episode_title(name: str, prefix: str) -> str:
    """The part after the shared prefix, i.e. this sibling's own title."""
    stem = os.path.splitext(name)[0]
    for sep in (" - ", " – ", " — "):
        if stem.startswith(prefix + sep):
            return stem[len(prefix) + len(sep):].strip()
    t = _tokens(name)
    return " - ".join(t[1:]) if len(t) > 1 else stem


# A remainder that begins with an episode number. Deliberately a bare digit
# test, not a word-boundary one: "3x01 - Anne" leads with a number but has no
# boundary after the 3, and it is still an episode. Capped at 3 digits so a
# four-digit year is not taken for an episode, and not followed by another
# digit so "1969" cannot match through its first three.
_EP_LEAD = re.compile(r"^\s*\d{1,3}(?!\d)", re.UNICODE)

# A first *token* that is an episode: "01", "e02", "ep07", "episode12", "s01e03",
# "09v2". This is what the token-sequence reading needs, where "e02" is a token
# in its own right ("cowboy.bebop.e02.multi.1080p").
_EP_TOKEN_RE = re.compile(r"(?i)^(?:s(\d{1,2)})?$|^(?:e|ep|episode|s\d{1,2}e|x)?(\d{1,3})(?:v(\d+))?$")


def numbered_remainders(remainders) -> int:
    """How many remainders lead with a number (episodes) rather than a title."""
    return sum(1 for r in remainders if _EP_LEAD.match(r))


def series_episodes(names: list[str], run: list[str] | None = None, min_episodes: int = 3):
    """If these files are numbered instalments of ONE work, map name -> episode.

    Reads the remainder after the shared token run, uniformly for every naming
    style: "Show - 01", "cowboy.bebop.e02.multi.1080p", "Show.S01E03". A
    numbered remainder means episodes of one title; a titled remainder means
    separate works (an anthology), for which this returns {}.

    Returns {} unless the numbered reading fits most of the folder, so a stray
    numbered file among titled ones does not flip the whole group.
    """
    if not names:
        return {}
    run = run if run is not None else common_prefix_run(names)
    if not run:
        return {}
    run_season = season_from_run(run)
    found = {}
    for n in names:
        seq = token_sequence(n)
        if seq[:len(run)] != run:
            continue
        rem = " ".join(seq[len(run):])
        season, num = episode_anywhere(rem)
        if num is not None:
            found[n] = (season if season is not None else run_season, num)
    # require a real run of episodes with mostly-unique (season, episode) pairs:
    # a folder where every file carries the same pair is versions, not instalments.
    if len(found) < min_episodes or len(set(found.values())) < min_episodes:
        return {}
    if len(found) < max(min_episodes, int(0.6 * len(names))):
        return {}
    return found


def _episode_number(remainder: str):
    """The episode number a remainder starts with, or None.

    Handles the leading forms the sources use: "01", "09v2", "47 (1080p...)"
    and "3x01 - Anne" (season x episode, where the episode is the number after
    the x).
    """
    m = re.match(r"^\s*(\d{1,2})\s*[xX]\s*(\d{1,3})", remainder)   # 3x01
    if m:
        return int(m.group(2))
    m = re.match(r"^\s*(\d{1,3})", remainder)                      # 01, 47, 09v2
    if m:
        return int(m.group(1))
    return None


def family_title(run: list[str]) -> str:
    """A display title from the shared token run: 'neon genesis evangelion' ->
    'Neon Genesis Evangelion'. Used to name a series whose per-file names do not
    yield a usable title on their own (anime "- 01 -" naming)."""
    return " ".join(w.capitalize() if w.islower() else w for w in run).strip()


def classify_folder(names: list[str], min_group: int = 3):
    """Read a folder as one of: ('series', eps) | ('anthology', prefix) | (None, None).

    For a series, the returned payload also carries the family title under the
    key ``"__family__"`` — the episodes map is keyed by filename, so a caller
    iterating it must skip that key or use the helper below.

    The decision uses the token *sequence*, which is uniform across separators —
    dotted scene names ("cowboy.bebop.e02...") and spaced ones ("Show - 01 ...")
    are read the same way, so neither needs its own pattern. The question is what
    follows the shared run:

      * a number  -> one work's instalments, so it is a series, and the numbers
        are the episode axis;
      * a word    -> distinct works, so it is an anthology (Tex Avery shorts);
      * neither   -> unreadable as a family; left to the per-file heuristics.

    This is a heuristic, and its failure modes are bounded: a folder whose
    titles happen to lead with numbers reads as a series, which is why the
    explicit override file exists for troublemakers.
    """
    # min_len=1: a single shared title word is enough, because the remainder
    # decides — "Show.S01E01" shares only "show", yet every remainder leads with
    # an episode. A one-word run with *titled* remainders still reads as an
    # anthology (or as nothing), so this does not weaken the guard.
    run = common_prefix_run(names, min_len=1, min_group=min_group)
    if not run:
        # No shared prefix at all. The files may still be a numbered run with the
        # number first ("001 Un Yaourt...", "002 Poppi..."): the number is the
        # leading token, so nothing is shared and the run is empty. Treat a
        # majority of distinct leading numbers as the same evidence.
        pairs = [episode_anywhere(" ".join(token_sequence(x))) for x in names]
        ok = [p for p in pairs if p[1] is not None]
        if len(ok) >= max(min_group, int(0.6 * len(names))) and len(set(ok)) >= min_group:
            eps = {x: p for x, p in zip(names, pairs) if p[1] is not None}
            eps["__family__"] = ""
            return "series", eps
        return None, None
    rems = remainders_after(names, run)
    if len(rems) < min_group:
        return None, None
    lead_numbers = [r for r in rems if episode_anywhere(r)[1] is not None]
    if len(lead_numbers) >= max(3, int(0.6 * len(rems))):
        eps = series_episodes(names, run)
        if eps:
            eps["__family__"] = family_title(run)
            return "series", eps
    # anthology: keep using the separator-aware prefix the router expects
    p = sibling_prefixes(names, min_group=min_group)
    if p:
        return "anthology", p
    return None, None


if __name__ == "__main__":
    import sys
    for d in sys.argv[1:]:
        names = [f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in
                 (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts")]
        p = sibling_prefixes(names)
        print(f"{d}: {len(names)} files -> sibling prefix {p!r}")
        if p:
            for n in sorted(names)[:5]:
                print(f"    {n!r} -> {episode_title(n, p)!r}")


def series_family(payload) -> str:
    """The family title from a classify_folder('series') payload, or ''."""
    return (payload or {}).get("__family__", "")


def series_episode_map(payload) -> dict:
    """A classify_folder('series') payload minus its metadata keys."""
    return {k: v for k, v in (payload or {}).items() if not k.startswith("__")}
