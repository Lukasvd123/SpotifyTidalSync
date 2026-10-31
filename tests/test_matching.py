"""Matching: scoring, normalisation and the tricky-catalogue cases (MA*)."""

from types import SimpleNamespace as NS

import pytest

from spotifysync.matching import (NOT_ON_TIDAL, MappingStore, TrackQuery, artist_match,
                                  is_generic_title, legacy_mapping_key, make_mapping_key,
                                  pick_best, search_title)


def cand(title, artists, duration_s, album="Album", version=None, tid=None):
    artists = [artists] if isinstance(artists, str) else artists
    objs = [NS(name=a) for a in artists]
    full = f"{title} ({version})" if version else title
    return NS(id=tid or hash((full, duration_s, album)), name=title, full_name=full,
              artist=objs[0], artists=objs, album=NS(name=album), duration=duration_s)


def best(cands, **query):
    track, _score = pick_best(cands, TrackQuery(**query))
    return track


# --- keys -----------------------------------------------------------------
@pytest.mark.parametrize("title,artist", [
    ("Just Give Me a Reason (feat. Nate Ruess)", "P!nk"),
    ("Beyoncé", "Café Tacvba"),
    ("WHERE IS MY HUSBAND!", "RAYE"),
    ("A Bar Song (Tipsy)", "Shaboozey"),
])
def test_MA1_latin_keys_are_unchanged_so_saved_mappings_stay_valid(title, artist):
    assert make_mapping_key(title, artist) == legacy_mapping_key(title, artist)


def test_MA2_non_latin_titles_get_distinct_keys():
    keys = {make_mapping_key(t, "アーティスト") for t in ("東京", "大阪", "Москва", "القاهرة")}
    assert len(keys) == 4
    assert all(k != "|" for k in keys)
    assert legacy_mapping_key("東京", "アーティスト") is None


def test_MA3_search_title_keeps_hyphenated_words():
    assert search_title("Anti-Hero") == "Anti-Hero"
    assert search_title("Song - Remastered 2011") == "Song"
    assert search_title("A Bar Song (Tipsy)") == "A Bar Song"


# --- wrong song guards ----------------------------------------------------
def test_MA4_same_artist_different_song_is_never_picked():
    """The old artist-only fallback and the duration bonus picked these."""
    cands = [cand("Other Song", "Artist", 181), cand("Third Song", "Artist", 175)]
    assert best(cands, title="Missing Song", artist="Artist", duration_ms=180000) is None


def test_MA5_live_and_remix_lose_to_the_studio_version():
    studio = cand("Song", "Artist", 200)
    cands = [cand("Song", "Artist", 200, version="Live"),
             cand("Song", "Artist", 201, version="Remix"), studio]
    assert best(cands, title="Song", artist="Artist", duration_ms=200000) is studio


def test_MA6_remaster_is_the_same_recording():
    c = cand("Song", "Artist", 200)
    assert best([c], title="Song - Remastered 2011", artist="Artist", duration_ms=200000) is c


def test_MA7_duration_far_off_is_a_different_recording():
    extended = cand("Song", "Artist", 420)
    assert best([extended], title="Song", artist="Artist", duration_ms=210000) is None


@pytest.mark.parametrize("src,other", [
    ("Song 1", "Song 2"),
    ("Hello", "Hell"),
    ("Interlude", "Interlude 2"),
    ("Symphony No. 5: I. Allegro", "Symphony No. 5: II. Allegro"),
])
def test_MA23_near_identical_titles_that_are_different_songs(src, other):
    assert best([cand(other, "Artist", 200)], title=src, artist="Artist",
                duration_ms=200000) is None


def test_MA24_roman_and_arabic_numerals_are_equal():
    c = cand("Part 2", "Artist", 200)
    assert best([c], title="Part II", artist="Artist", duration_ms=200000) is c


# --- artists --------------------------------------------------------------
def test_MA8_collab_matches_on_any_credited_artist():
    c = cand("Collab", ["Dua Lipa", "Calvin Harris"], 200)
    assert best([c], title="Collab", artist="Calvin Harris",
                artists=("Calvin Harris", "Dua Lipa"), duration_ms=200000) is c


@pytest.mark.parametrize("a,b", [
    ("Florence + The Machine", "Florence and the Machine"),
    ("Simon & Garfunkel", "Simon and Garfunkel"),
    ("AC/DC", "AC/DC"),
    ("Beyoncé", "Beyonce"),
])
def test_MA9_artist_spelling_variants(a, b):
    assert artist_match((a,), (b,)) == "exact"


def test_MA10_artist_mismatch_without_strong_evidence_is_rejected():
    c = cand("Song", "Someone Else", 200)
    assert best([c], title="Song", artist="Artist", duration_ms=200000) is None


# --- study / instrumental / generic titles --------------------------------
def test_MA11_generic_title_picks_the_closest_runtime():
    right = cand("Rain", "Rain Sounds", 181)
    cands = [cand("Rain", "Rain Sounds", 240), right, cand("Rain", "Rain Sounds", 150)]
    assert best(cands, title="Rain", artist="Rain Sounds", duration_ms=180000) is right


def test_MA12_generic_title_without_a_close_runtime_is_no_match():
    cands = [cand("Rain", "Rain Sounds", 240), cand("Rain", "Rain Sounds", 150)]
    assert best(cands, title="Rain", artist="Rain Sounds", duration_ms=180000) is None


def test_MA13_generic_detection():
    assert is_generic_title("Rain")
    assert is_generic_title("Deep Focus")
    assert is_generic_title("Interlude 3")
    assert not is_generic_title("Bohemian Rhapsody")


def test_MA14_instrumental_only_on_tidal_still_matches_study_music():
    c = cand("Snowfall", "Øneheart", 120.5, version="Instrumental")
    assert best([c], title="Snowfall", artist="Øneheart", duration_ms=120000) is c


def test_MA15_instrumental_with_wrong_runtime_is_rejected():
    c = cand("Snowfall", "Øneheart", 128, version="Instrumental")
    assert best([c], title="Snowfall", artist="Øneheart", duration_ms=120000) is None


def test_MA16_vocal_original_preferred_over_instrumental():
    vocal = cand("Song", "Artist", 200)
    inst = cand("Song", "Artist", 200, version="Instrumental")
    assert best([inst, vocal], title="Song", artist="Artist", duration_ms=200000) is vocal


def test_MA17_explicit_instrumental_request_gets_the_instrumental():
    vocal = cand("Song", "Artist", 200)
    inst = cand("Song", "Artist", 201, version="Instrumental")
    assert best([vocal, inst], title="Song (Instrumental)", artist="Artist",
                duration_ms=200000) is inst


def test_MA18_album_breaks_ties_between_identical_titles():
    lofi = cand("Night Drive", "Chill Beats", 150, album="Lofi Nights")
    other = cand("Night Drive", "Chill Beats", 150, album="Study Session Vol. 2")
    assert best([other, lofi], title="Night Drive", artist="Chill Beats",
                album="Lofi Nights", duration_ms=150000) is lofi


def test_MA19_classical_movement_with_performer_credit():
    c = cand('Piano Sonata No. 14 in C♯ Minor, Op. 27 No. 2 "Moonlight": I. Adagio sostenuto',
             ["Daniel Barenboim"], 362)
    q = dict(title="Piano Sonata No. 14 in C-Sharp Minor, Op. 27 No. 2: I. Adagio sostenuto",
             artist="Ludwig van Beethoven",
             artists=("Ludwig van Beethoven", "Daniel Barenboim"), duration_ms=361000)
    assert best([c], **q) is c


def test_MA20_cjk_titles_match():
    c = cand("夜に駆ける", "YOASOBI", 261)
    assert best([c, cand("群青", "YOASOBI", 249)], title="夜に駆ける", artist="YOASOBI",
                duration_ms=261000) is c


def test_MA21_unknown_duration_still_matches_on_title_and_artist():
    c = cand("Song", "Artist", 200)
    assert best([c], title="Song", artist="Artist") is c


# --- mapping store --------------------------------------------------------
def test_MA22_mapping_store_not_on_tidal_and_remove(tmp_path):
    store = MappingStore(str(tmp_path / "m.json"))
    store.put("a|b", 123)
    store.put("c|d", NOT_ON_TIDAL)
    reloaded = MappingStore(str(tmp_path / "m.json"))
    assert reloaded.lookup("a|b") == 123
    assert reloaded.lookup("c|d") is NOT_ON_TIDAL
    reloaded.remove("a|b")
    assert MappingStore(str(tmp_path / "m.json")).lookup("a|b") is None
