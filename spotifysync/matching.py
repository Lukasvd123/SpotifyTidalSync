"""Matching a source track to a Tidal candidate.

Candidates are scored on artist, title, version and duration. The alternative
(taking the first search result within a few seconds of the right runtime)
happily grabs remixes and live cuts that share a runtime with the original.
"""

import re
import threading
import unicodedata

from .config import MAPPINGS_FILE, logger, _load_json, _save_json

# Descriptors marking a genuinely different recording. "remaster" is
# deliberately absent: it is the same performance and is often the only master
# Tidal carries, so rejecting it would lose real matches.
_ALT_VERSION_TAGS = (
    "remix", "live", "acoustic", "instrumental", "sped up", "spedup",
    "slowed", "reverb", "cover", "karaoke", "demo", "reprise", "a cappella",
    "acapella", "rerecorded", "re recorded", "taylors version",
)
# Descriptors marking an edit of the same recording — a softer mismatch.
_EDIT_VERSION_TAGS = (
    "radio edit", "radio mix", "edit", "extended", "single version", "album version",
)

# A solid artist+title match scores ~160. Below this floor we would rather fall
# through to the softer fallbacks (or ask the user) than force a bad pick.
MATCH_THRESHOLD = 70.0


def normalize_text(text):
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text.lower().strip())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def make_mapping_key(title, artist):
    return f"{normalize_text(title)}|{normalize_text(artist)}"


def _core_title(title):
    """Title with any trailing or parenthetical version descriptor removed."""
    if not title:
        return ""
    t = re.split(r"\s[-–]\s", title)[0]
    t = re.sub(r"[\(\[].*?[\)\]]", "", t)
    return t.strip()


def _version_tags(title):
    """Descriptor tokens found inside (), [] or after a ' - ' separator."""
    if not title:
        return set()
    low = title.lower()
    extras = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", low)
    parts = re.split(r"\s[-–]\s", low)
    if len(parts) > 1:
        extras += parts[1:]
    hay = " " + " ".join(extras) + " "
    return {tag for tag in _ALT_VERSION_TAGS + _EDIT_VERSION_TAGS if tag in hay}


def _has_descriptor(title):
    if not title:
        return False
    return bool(re.search(r"[\(\[]", title) or re.search(r"\s[-–]\s", title))


def score_candidate(src_title, src_artist, src_duration_ms,
                    cand_title, cand_artist, cand_duration_ms):
    """Score a Tidal candidate. Higher is better; a wrong artist disqualifies."""
    s_core = normalize_text(_core_title(src_title))
    c_core = normalize_text(_core_title(cand_title))
    s_art = normalize_text(src_artist)
    c_art = normalize_text(cand_artist)

    if s_art and c_art:
        if c_art == s_art:
            score = 100.0
        elif s_art in c_art or c_art in s_art:
            score = 45.0
        else:
            return -1e9
    else:
        score = 0.0

    if s_core and c_core:
        if c_core == s_core:
            score += 60.0
        elif s_core in c_core or c_core in s_core:
            score += 30.0
        else:
            score -= 40.0

    s_tags, c_tags = _version_tags(src_title), _version_tags(cand_title)
    s_alt = {t for t in s_tags if t in _ALT_VERSION_TAGS}
    c_alt = {t for t in c_tags if t in _ALT_VERSION_TAGS}
    if not s_alt and c_alt:
        score -= 80.0          # the main wrong pick to avoid
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
    if not _has_descriptor(src_title) and _has_descriptor(cand_title):
        score -= 12.0

    if src_duration_ms > 0 and cand_duration_ms > 0:
        diff = abs(cand_duration_ms - src_duration_ms)
        if diff <= 2000:
            score += 40.0
        elif diff <= 7000:
            score += 40.0 - (diff - 2000) / 200.0
        else:
            score -= min(60.0, (diff - 7000) / 1000.0 * 5.0)

    return score


def pick_best(candidates, src_title, src_artist, src_duration_ms):
    """Choose the best Tidal track from search results, or None."""
    best, best_score = None, -1e18
    for cand in candidates:
        try:
            duration = (cand.duration or 0) * 1000
            score = score_candidate(src_title, src_artist, src_duration_ms,
                                    cand.name, cand.artist.name, duration)
        except Exception:
            continue
        if score > best_score:
            best, best_score = cand, score

    if best is not None and best_score >= MATCH_THRESHOLD:
        logger.debug(f"Matched '{best.name}' by '{best.artist.name}' (score {best_score:.0f})")
        return best

    # Fallback: first result whose artist matches exactly.
    src_norm = normalize_text(src_artist)
    for cand in candidates:
        try:
            if normalize_text(cand.artist.name) == src_norm:
                logger.debug(f"Artist-match fallback: '{cand.name}'")
                return cand
        except Exception:
            continue

    # Last resort: best scorer, if its artist is at least close.
    if best is not None:
        try:
            cand_norm = normalize_text(best.artist.name)
            if src_norm and (src_norm in cand_norm or cand_norm in src_norm):
                logger.debug(f"Fuzzy artist match: '{best.name}' (score {best_score:.0f})")
                return best
        except Exception:
            pass
    return None


class MappingStore:
    """User corrections: mapping key -> Tidal track id.

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
        """First mapping found for any of the given keys."""
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

    def __len__(self):
        with self._lock:
            return len(self._data)
