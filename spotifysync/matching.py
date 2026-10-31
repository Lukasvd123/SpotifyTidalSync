"""Matching a source track to a Tidal candidate.

Candidates are scored on artist, title, version, album and duration. Two rules
keep the wrong song from ever winning:

  * the title must actually match; a strong artist and a similar runtime used
    to carry a different song by the same artist over the line, and
  * duration is a real signal, not a tiebreak. Study, lo-fi and ambient music
    is full of generic titles ("Rain", "Focus") shared by many recordings of
    the same artist, and the runtime is what tells them apart.
"""

import difflib
import re
import threading
import unicodedata
from dataclasses import dataclass

from .config import MAPPINGS_FILE, logger, _load_json, _save_json

# Descriptors marking a genuinely different recording. "remaster" is
# deliberately absent: it is the same performance and is often the only master
# Tidal carries, so rejecting it would lose real matches.
_ALT_VERSION_TAGS = (
    "remix", "live", "acoustic", "instrumental", "sped up", "spedup",
    "slowed", "reverb", "cover", "karaoke", "demo", "reprise", "a cappella",
    "acapella", "rerecorded", "re recorded", "taylors version",
)
# Descriptors marking an edit of the same recording, a softer mismatch.
_EDIT_VERSION_TAGS = (
    "radio edit", "radio mix", "edit", "extended", "single version", "album version",
)

# Words that make a short title ambiguous: background, focus and sleep music
# reuses them endlessly, so only a tight runtime match is trusted.
_GENERIC_WORDS = {
    "rain", "focus", "study", "sleep", "calm", "relax", "relaxing", "ambient",
    "piano", "lofi", "chill", "meditation", "noise", "ocean", "waves", "forest",
    "thunder", "night", "dream", "dreams", "breathe", "peace", "nature", "intro",
    "outro", "interlude", "untitled", "track", "morning", "snow", "snowfall",
    "silence", "deep", "white", "brown", "pink", "fireplace", "storm", "wind",
    "river", "stream", "birds", "cafe", "coffee", "lullaby", "theme",
}
GENERIC_DURATION_MS = 4000

# Runtimes further apart than both of these are a different recording.
MAX_DURATION_DIFF_MS = 30000
MAX_DURATION_DIFF_RATIO = 0.15

# A solid artist+title+duration match scores ~200.
MATCH_THRESHOLD = 100.0
# Good enough to stop searching and to remember the match.
CONFIDENT_SCORE = 150.0

_REJECT = float("-inf")


def normalize_text(text):
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Letters and digits of every script are kept. The old ASCII-only filter
    reduced every Japanese, Korean, Cyrillic or Arabic title to an empty
    string, so all of them shared one mapping key and one track identity.
    ASCII and accented-Latin output is unchanged, so saved keys stay valid.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text.lower().strip())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = "".join(c for c in text
                   if c == " " or unicodedata.category(c)[0] in ("L", "N"))
    return re.sub(r"\s+", " ", text).strip()


def legacy_normalize_text(text):
    """The pre-0.05 normaliser, only for finding mappings saved with it."""
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text.lower().strip())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def make_mapping_key(title, artist):
    return f"{normalize_text(title)}|{normalize_text(artist)}"


def legacy_mapping_key(title, artist):
    """Old-style key, or None when it would be the meaningless `|`."""
    t, a = legacy_normalize_text(title), legacy_normalize_text(artist)
    if not t:
        return None
    return f"{t}|{a}"


def _compare_text(text):
    """normalize_text tuned for comparison rather than for stable keys."""
    if not text:
        return ""
    text = text.replace("♯", " sharp").replace("#", " sharp").replace("♭", " flat")
    text = re.sub(r"[-_/]", " ", text)
    text = re.sub(r"\s&\s|\s\+\s", " and ", text)
    return normalize_text(text)


def core_title(title):
    """Title with any trailing or parenthetical version descriptor removed."""
    if not title:
        return ""
    t = re.split(r"\s[-–—]\s", title)[0]
    t = re.sub(r"[\(\[].*?[\)\]]", "", t)
    return t.strip() or title.strip()


def search_title(title):
    """Title to put in a search query.

    Only ' - ' separates a version descriptor. Splitting on any hyphen turned
    "Anti-Hero" into "Anti".
    """
    return core_title(title)


def version_tags(title):
    """Descriptor tokens found inside (), [] or after a ' - ' separator."""
    if not title:
        return set()
    low = title.lower()
    extras = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", low)
    parts = re.split(r"\s[-–—]\s", low)
    if len(parts) > 1:
        extras += parts[1:]
    hay = " " + " ".join(_compare_text(e) for e in extras) + " "
    return {tag for tag in _ALT_VERSION_TAGS + _EDIT_VERSION_TAGS if f" {tag} " in hay}


def has_descriptor(title):
    if not title:
        return False
    return bool(re.search(r"[\(\[]", title) or re.search(r"\s[-–—]\s", title))


def is_generic_title(title):
    words = _compare_text(core_title(title)).split()
    if not words:
        return False
    if re.fullmatch(r"(track|untitled|interlude|intro|outro)( \d+)?", " ".join(words)):
        return True
    return len(words) <= 2 and any(w in _GENERIC_WORDS for w in words)


_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|\s&\s|\s\+\s|\sx\s|\sfeat\.?\s|\sft\.?\s|\sfeaturing\s|\swith\s)\s*",
                           re.IGNORECASE)


def split_artists(names):
    """All individual artist names from one or more credit strings."""
    out = []
    for name in names:
        if not name:
            continue
        out.append(name)
        out.extend(p for p in _ARTIST_SPLIT.split(name) if p and p != name)
    return out


def _artist_form(name):
    """Comparable artist name: 'Florence + The Machine' == 'Florence and the Machine'."""
    words = [w for w in _compare_text(name).split() if w not in ("the", "and")]
    return " ".join(words)


def artist_match(src_artists, cand_artists):
    """'exact', 'partial' or None."""
    src = {_artist_form(a) for a in split_artists(src_artists)} - {""}
    cand = {_artist_form(a) for a in split_artists(cand_artists)} - {""}
    if not src or not cand:
        return None
    if src & cand:
        return "exact"
    for s in src:
        for c in cand:
            if min(len(s), len(c)) >= 3 and (s in c or c in s):
                return "partial"
    return None


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10, "xi": 11, "xii": 12}


def _numbers(words):
    """Numbers in a title, Roman numerals included: "Part II" == "Part 2"."""
    out = set()
    for w in words:
        if w.isdigit():
            out.add(int(w))
        elif w in _ROMAN:
            out.add(_ROMAN[w])
    return out


def title_match(a, b):
    """1.0 identical, 0.9 near-identical, 0.6 similar, 0.0 different.

    Whole words have to overlap: character similarity alone rated "Hello" vs
    "Hell" and "Song 1" vs "Song 2" as near matches. Numbers must agree, so
    numbered interludes and classical movements never cross-match.
    """
    a, b = _compare_text(core_title(a)), _compare_text(core_title(b))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    wa = [str(_ROMAN[w]) if w in _ROMAN else w for w in a.split()]
    wb = [str(_ROMAN[w]) if w in _ROMAN else w for w in b.split()]
    if _numbers(wa) != _numbers(wb):
        return 0.0
    if wa == wb:
        return 1.0
    ta, tb = set(wa), set(wb)
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = difflib.SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio()
    if len(wa) == len(wb) == 1 and any(ord(c) >= 0x2E80 for c in a + b):
        # CJK titles have no spaces, so no words to compare; go by characters.
        jaccard = ratio
    if jaccard >= 0.8 or (ratio >= 0.9 and jaccard >= 0.5):
        return 0.9
    if jaccard >= 0.6 and ratio >= 0.75:
        return 0.6
    return 0.0


@dataclass(frozen=True)
class TrackQuery:
    """What we know about the source track."""
    title: str
    artist: str = ""
    artists: tuple = ()
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""

    @property
    def all_artists(self):
        return tuple(self.artists) or ((self.artist,) if self.artist else ())


@dataclass(frozen=True)
class Candidate:
    title: str
    artists: tuple = ()
    album: str = ""
    duration_ms: int = 0


def describe(track):
    """A Tidal track as a Candidate. `full_name` carries the version suffix
    ("Instrumental", "Live") that `name` leaves out."""
    title = getattr(track, "full_name", None) or track.name
    names = []
    for artist in getattr(track, "artists", None) or []:
        name = getattr(artist, "name", None)
        if name:
            names.append(name)
    main = getattr(getattr(track, "artist", None), "name", None)
    if main and main not in names:
        names.insert(0, main)
    album = getattr(getattr(track, "album", None), "name", "") or ""
    return Candidate(title=title, artists=tuple(names), album=album,
                     duration_ms=int((getattr(track, "duration", 0) or 0) * 1000))


def score_candidate(query, cand):
    """Score a Candidate against a TrackQuery. Higher is better; -inf rejects."""
    similarity = title_match(query.title, cand.title)
    if similarity == 1.0:
        score = 60.0
    elif similarity >= 0.9:
        score = 50.0
    elif similarity > 0:
        score = 25.0
    else:
        return _REJECT

    artist = artist_match(query.all_artists, cand.artists)
    if artist == "exact":
        score += 100.0
    elif artist == "partial":
        score += 45.0
    elif query.all_artists and cand.artists:
        score -= 60.0

    diff = None
    if query.duration_ms > 0 and cand.duration_ms > 0:
        diff = abs(cand.duration_ms - query.duration_ms)
        if diff > MAX_DURATION_DIFF_MS and diff > query.duration_ms * MAX_DURATION_DIFF_RATIO:
            return _REJECT
        if is_generic_title(query.title) and diff > GENERIC_DURATION_MS:
            return _REJECT
        if diff <= 1000:
            score += 45.0
        elif diff <= 2000:
            score += 40.0
        elif diff <= 7000:
            score += 40.0 - (diff - 2000) / 200.0
        else:
            score -= min(80.0, 10.0 + (diff - 7000) / 1000.0 * 5.0)

    s_tags, c_tags = version_tags(query.title), version_tags(cand.title)
    s_alt = {t for t in s_tags if t in _ALT_VERSION_TAGS}
    c_alt = {t for t in c_tags if t in _ALT_VERSION_TAGS}
    if not s_alt and c_alt:
        # Study and piano music is often the same file labelled "Instrumental"
        # on Tidal only. Trust it when everything else lines up.
        same_file = (c_alt == {"instrumental"} and artist == "exact"
                     and diff is not None and diff <= 2000)
        score -= 10.0 if same_file else 80.0
    elif s_alt and c_alt:
        score += 20.0 if s_alt == c_alt else -30.0
    elif s_alt and not c_alt:
        score -= 40.0

    s_edit = {t for t in s_tags if t in _EDIT_VERSION_TAGS}
    c_edit = {t for t in c_tags if t in _EDIT_VERSION_TAGS}
    if (s_edit or c_edit) and s_edit != c_edit:
        score -= 8.0

    # Prefer the plain original when the candidate carries an extra descriptor
    # we have no explicit tag for, e.g. "(Spanish Version)". Small, so it only
    # breaks ties.
    if not has_descriptor(query.title) and has_descriptor(cand.title):
        score -= 12.0

    if query.album and cand.album:
        album_similarity = title_match(query.album, cand.album)
        if album_similarity == 1.0:
            score += 20.0
        elif album_similarity >= 0.9:
            score += 10.0

    return score


def pick_best(candidates, query):
    """Best Tidal track from search results as (track, score); track is None
    when nothing clears the threshold."""
    best, best_score = None, _REJECT
    for cand in candidates:
        try:
            score = score_candidate(query, describe(cand))
        except Exception as e:
            logger.debug(f"Could not score candidate: {e}")
            continue
        if score > best_score:
            best, best_score = cand, score

    if best is not None and best_score >= MATCH_THRESHOLD:
        logger.debug(f"Matched '{describe(best).title}' (score {best_score:.0f})")
        return best, best_score
    return None, best_score


# Stored in place of a Tidal id: the user confirmed the track is not on Tidal.
NOT_ON_TIDAL = False


class MappingStore:
    """Mapping key -> Tidal track id (or NOT_ON_TIDAL).

    Held in memory; the old code re-read and re-parsed the JSON file on every
    single search and rewrote it in full on every correction.
    """

    def __init__(self, path=MAPPINGS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data = _load_json(path)

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def lookup(self, *keys):
        """First mapping found for any of the given keys, else None."""
        with self._lock:
            for key in keys:
                if key and key in self._data:
                    return self._data[key]
        return None

    def put(self, key, tidal_id):
        with self._lock:
            self._data[key] = tidal_id
            snapshot = dict(self._data)
        _save_json(self._path, snapshot)
        logger.info(f"Mapping saved: {key} -> {tidal_id}")

    def remove(self, *keys):
        with self._lock:
            changed = False
            for key in keys:
                if key and key in self._data:
                    del self._data[key]
                    changed = True
            snapshot = dict(self._data)
        if changed:
            _save_json(self._path, snapshot)

    def __len__(self):
        with self._lock:
            return len(self._data)
