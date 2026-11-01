"""Microsoft Teams meeting captions (XERK-2515): the Teams layout parser and a replayed Teams
meeting over /ws/screen. Shared socket helpers come from test_screen (the Zoom suite)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.screen import teams
from api.screen import ws as screen_ws
from api.screen.meetings import meetings
from api.screen.ocr import OcrResult
from api.screen.zoom import Roster
from test_screen import envelope, line, ocr_of, screen, tiny_jpeg, unique_jpeg, wait_handled

__all__ = ["screen"]  # the socket fixture, shared with the Zoom suite

FIXTURE = json.loads((Path(__file__).parent / "fixtures/screen/teams_meeting.json").read_text())


def teams_header(ts: int, region: str) -> dict:
    return {
        "type": "screen.frame",
        "windowTitle": "Horizon desktop 2",
        "orgSiteKey": "yprime",
        "orgName": "YPrime",
        "platform": "teams",
        "ts": ts,
        "region": region,
    }


def roster(*names: str) -> Roster:
    r = Roster()
    for n in names:
        r.add(n)
    return r


def result(*lines, width: int = 760, height: int = 200) -> OcrResult:
    return OcrResult(width, height, tuple(lines))


# --- names ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Raman, Priya", True),
        ("Sean O'Neil", True),
        ("van Dijk, Jan", False),  # a particle can't lead a name
        ("Dijk, Jan van", True),
        ("Lee, Bo, Cy", False),
        ("okay, sure", False),
        ("Yes, I can see it.", False),
        ("Raman,", False),
        ("Sure.", False),  # Teams punctuates captions: a full stop ends a sentence
        ("Thanks Kunal.", False),
        ("Bo L.", True),  # ... unless it ends an initial, a degree or Jr./Sr.
        ("Tolkien, J.R.R.", True),
        ("Ana Ruiz Ph.D.", True),
        ("Martin King Jr.", True),
        ("Smith, John Jr.", True),
        ("Thanks Bo.", False),
        ("Łukasz Żak", True),
    ],
)
def test_teams_name_shape(text: str, expected: bool) -> None:
    assert teams.is_teams_name(text) is expected


# --- full frames ---------------------------------------------------------------------------


def test_full_frame_reads_title_tiles_and_people_pane_minus_chrome() -> None:
    frame = teams.parse_full_frame(ocr_of(FIXTURE["frames"][0]))
    assert frame.title == "Sprint Planning"  # not the timer or the toolbar
    # Suffixes dropped, the pane's "In this meeting (3)" and the "KS" camera-off tile skipped.
    assert frame.participants == ["Raman, Priya", "Sean O'Neil", "Kunal Shah"]
    assert frame.active_speaker is None


def test_a_capitalised_caption_line_in_the_panel_is_not_a_participant() -> None:
    frame = teams.parse_full_frame(
        result(
            line("Kunal Shah", 30, 300, 110, 320),
            line("Raman, Priya", 56, 580, 150, 596),
            line("Sounds Good", 56, 604, 150, 624),
            line("so we ship the release on thursday then", 56, 630, 400, 650),
            width=1280,
            height=720,
        )
    )
    # The speaker header above the panel may be read as a name (it is one); its caption is not.
    assert "Sounds Good" not in frame.participants and frame.participants[0] == "Kunal Shah"


def test_a_full_frame_with_only_chrome_has_no_title_or_people() -> None:
    frame = teams.parse_full_frame(
        result(
            line("12:04", 40, 10, 80, 30),
            line("Leave", 1200, 10, 1240, 30),
            line("Attendees (12)", 1010, 90, 1120, 110),
            width=1280,
            height=720,
        )
    )
    assert frame.title is None and frame.participants == []


def test_short_punctuated_replies_in_the_panel_are_not_participants() -> None:
    # QA D1: with only short replies on screen there was no caption band, so "Sure." joined
    # the roster and later swallowed caption lines as a speaker header.
    frame = teams.parse_full_frame(
        result(
            line("Kunal Shah", 30, 300, 110, 320),
            line("Raman, Priya", 56, 580, 150, 596),
            line("Thanks.", 56, 604, 120, 624),
            line("Sean O'Neil", 56, 640, 150, 656),
            line("Sure.", 56, 664, 100, 684),
            width=1280,
            height=720,
        )
    )
    assert "Thanks." not in frame.participants and "Sure." not in frame.participants
    assert "Kunal Shah" in frame.participants


def test_chrome_is_not_a_participant() -> None:
    frame = teams.parse_full_frame(
        result(
            *(
                line(t, 1010, 380 + 30 * i, 1100, 400 + 30 * i)
                for i, t in enumerate(["Organizer", "Lobby", "Admit", "Muted", "Unverified"])
            ),
            line("Kunal Shah", 30, 400, 110, 420),
            width=1280,
            height=720,
        )
    )
    assert frame.participants == ["Kunal Shah"]


@pytest.mark.parametrize(
    "pane_text",
    [None, "Invite someone or dial a number", "Ask to join", "In a call", "Out of office"],
)
def test_people_pane_names_level_with_the_captions_survive(pane_text: str | None) -> None:
    # QA D3 / D-B: a full-width caption band, or one stretched by any other pane text, wiped
    # the People pane's names sitting level with the caption panel.
    lines = [
        line("Raman, Priya", 40, 500, 140, 516),
        line("Okay, let's get started with the review.", 70, 520, 400, 536),
        line("Okafor, Chidi", 1010, 520, 1120, 536),
        line("Lee, Bo", 1010, 560, 1080, 576),
    ]
    if pane_text:
        # Status text sits right under a name: its box must not swallow the name above.
        lines.append(line(pane_text, 1010, 580, 1010 + 8 * len(pane_text), 596))
    frame = teams.parse_full_frame(result(*lines, width=1280, height=720))
    assert frame.participants == ["Okafor, Chidi", "Lee, Bo"]


def test_a_counted_people_heading_is_not_the_title() -> None:
    frame = teams.parse_full_frame(
        result(
            line("In this meeting (3)", 10, 14, 150, 34),
            line("Sprint Planning", 200, 14, 320, 34),
            width=1280,
            height=720,
        )
    )
    assert frame.title == "Sprint Planning"


def test_long_suffix_rows_are_not_cleaned() -> None:
    # QA D6: suffixes are stripped one per pass; a crafted 500-char row cost ~1 s a frame.
    crafted = "A" + " (x)" * 125
    assert teams.clean_name(crafted) == crafted
    assert teams.clean_name("Lee, Bo (Guest) (External)") == "Lee, Bo"


# --- caption crops -------------------------------------------------------------------------


def test_an_initials_avatar_spanning_two_rows_is_dropped() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("PR", 10, 4, 42, 44),
            line("Raman, Priya", 56, 4, 150, 20),
            line("Okay, let's start.", 56, 26, 200, 46),
        ),
        roster("Raman, Priya"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Raman, Priya", ["Okay, let's start."])]


@pytest.mark.parametrize(
    ("avatar", "name"),
    [
        ("ŁŻ", "Łukasz Żak"),  # accented initials (QA D2)
        ("ÉD", "Émile Dubois"),
        ("P R", "Raman, Priya"),  # read with a space
        ("PB", "Raman, Priya"),  # misread: the circle's height still gives it away
        ("JD", "jdoe@contoso.com"),  # an email-named user's initials
    ],
)
def test_any_initials_circle_beside_a_name_is_dropped(avatar: str, name: str) -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Kunal Shah", 56, 4, 150, 20),
            line("Over to you.", 56, 26, 200, 46),
            line(avatar, 10, 60, 42, 100),
            line(name, 56, 60, 200, 76),
            line("Hello all.", 56, 82, 200, 102),
        ),
        roster("Kunal Shah", name),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Kunal Shah", ["Over to you."]),
        (name, ["Hello all."]),
    ]


@pytest.mark.parametrize(
    "left_text", ["Q3 roadmap: ship the export fix first", "Your camera is off"]
)
def test_slide_or_toast_text_left_of_the_captions_keeps_them_covered(left_text: str) -> None:
    # QA: boxing only the leftmost text column picked a slide or toast left of the panel and
    # left the capitalised captions free to join the roster.
    frame = teams.parse_full_frame(
        result(
            line("Kunal Shah", 300, 300, 380, 316),
            line(left_text, 10, 560, 10 + 8 * len(left_text), 576),
            line("Raman, Priya", 400, 500, 500, 516),
            line("Thanks Everyone", 430, 520, 560, 536),
            line("see you on monday then", 430, 540, 640, 556),
            width=1280,
            height=720,
        )
    )
    assert "Thanks Everyone" not in frame.participants and "Kunal Shah" in frame.participants


@pytest.mark.parametrize(
    "around",
    [
        ("okay so", "yeah right"),  # unpunctuated interims: lower-case starts
        ("Yes.", "Sure."),  # short but punctuated
        ("We ship it then", "Right after lunch today"),  # unpunctuated but four words
    ],
)
def test_a_panel_without_long_sentences_still_hides_its_captions(around: tuple[str, str]) -> None:
    # QA: gating caption columns on long sentences left an all-interim panel unboxed, and the
    # Title-Case caption between its lines joined the roster.
    frame = teams.parse_full_frame(
        result(
            line("Kunal Shah", 300, 300, 380, 316),
            line("Raman, Priya", 40, 500, 140, 516),
            line(around[0], 70, 520, 250, 536),
            line("Thanks Everyone", 70, 540, 200, 556),
            line(around[1], 70, 560, 250, 576),
            width=1280,
            height=720,
        )
    )
    assert "Thanks Everyone" not in frame.participants and "Kunal Shah" in frame.participants


@pytest.mark.parametrize("sub_line", ["In a call", "is presenting", None])
def test_a_lower_case_guest_in_the_people_pane_keeps_the_other_names(sub_line: str | None) -> None:
    # QA: a lower-case guest name ("john smith") read as a caption interim and hid the whole
    # pane column. Pane rows sit an avatar apart (40 px here), captions a few pixels apart.
    names = ["Okafor, Chidi", "john smith", "Lee, Bo", "Amara Nwosu", "Kunal Shah"]
    lines = [line(n, 1010, 400 + 40 * i, 1010 + 8 * len(n), 416 + 40 * i) for i, n in enumerate(names)]
    if sub_line:
        lines.append(line(sub_line, 1010, 420 + 40 * 4, 1010 + 8 * len(sub_line), 434 + 40 * 4))
    lines += [
        line("Raman, Priya", 40, 600, 140, 616),
        line("Thanks Everyone", 70, 622, 200, 638),
        line("okay so", 70, 644, 140, 660),
    ]
    frame = teams.parse_full_frame(result(*lines, width=1280, height=720))
    assert frame.participants == ["Okafor, Chidi", "Lee, Bo", "Amara Nwosu", "Kunal Shah"]


def test_overlapping_ocr_boxes_of_stacked_caption_rows_still_link() -> None:
    # QA with real tenir-ocr (Paddle) on a rendered 1920x1080 frame: stacked caption rows come
    # back overlapping by a pixel or few, and "gap >= 0" let the Title-Case row escape.
    frame = teams.parse_full_frame(
        result(
            line("Kunal Shah", 600, 500, 700, 520),
            line("Yeah, let's go get the pygmy goats, guys.", 101, 776, 431, 803),
            line("Thanks Everyone", 103, 802, 246, 824),
            width=1920,
            height=1080,
        )
    )
    assert frame.participants == ["Kunal Shah"]


def test_a_tile_label_beside_the_panel_is_not_linked_into_it() -> None:
    # Rows link only with a similar left edge: a tile label right of the panel, just under a
    # caption row, stays a participant.
    frame = teams.parse_full_frame(
        result(
            line("Raman, Priya", 40, 600, 140, 616),
            line("okay let's get started with it", 70, 620, 330, 636),
            line("Lee, Bo", 420, 640, 480, 656),
            width=1280,
            height=720,
        )
    )
    assert frame.participants == ["Lee, Bo"]


def test_initials_far_left_of_a_name_are_caption_text() -> None:
    # Only a box right beside the name is its avatar: "PR" here is a caption word whose box
    # overlaps a name row further right.
    blocks = teams.parse_caption_crop(
        result(
            line("Kunal Shah", 56, 0, 150, 16),
            line("PR", 56, 22, 80, 42),
            line("is merged", 90, 22, 180, 42),
            line("Raman, Priya", 600, 38, 700, 54),
            line("Thanks.", 600, 60, 660, 80),
        ),
        roster("Kunal Shah", "Raman, Priya"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Kunal Shah", ["PR is merged"]),
        ("Raman, Priya", ["Thanks."]),
    ]


def test_a_tall_lone_letter_beside_a_name_is_caption_text() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Raman, Priya", 56, 4, 160, 20),
            line("I", 56, 26, 62, 56),
            line("Love Lucy", 66, 30, 150, 50),  # name-shaped, but not on the roster
        ),
        roster("Raman, Priya"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Raman, Priya", ["I Love Lucy"])]


@pytest.mark.parametrize("row", ["BL Lee, Bo", "B L Lee, Bo"])
def test_initials_read_into_the_name_box_are_stripped(row: str) -> None:
    # A short name: "BL Lee, Bo" is too far from "Lee, Bo" to fuzzy-match unstripped.
    blocks = teams.parse_caption_crop(
        result(line(row, 10, 4, 150, 20), line("Okay.", 56, 26, 100, 46)), roster("Lee, Bo")
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Lee, Bo", ["Okay."])]


def test_capital_caption_words_that_are_not_the_speakers_initials_stay_text() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Raman, Priya", 56, 4, 150, 20),
            line("OK", 10, 26, 30, 46),
            line("then we ship it", 40, 26, 200, 46),
        ),
        roster("Raman, Priya"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Raman, Priya", ["OK then we ship it"])]


def test_a_guest_suffixed_header_matches_and_a_parenthesised_caption_stays_text() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Sean O'Neil (Guest)", 56, 4, 200, 20),
            line("Sure (Probably)", 56, 26, 200, 46),
        ),
        roster("Sean O'Neil"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Sean O'Neil", ["Sure (Probably)"])]


def test_a_crop_of_only_names_yields_nothing() -> None:
    # Tile labels a bottom-band crop catches, and a header whose words haven't arrived.
    r = roster("Kunal Shah", "Sean O'Neil")
    assert teams.parse_caption_crop(ocr_of(FIXTURE["frames"][1]), r) == []
    assert teams.parse_caption_crop(result(line("Kunal Shah", 56, 4, 150, 20)), r) == []


def test_tile_labels_above_the_captions_are_dropped_not_joined() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Kunal Shah", 20, 4, 100, 24),
            line("Sean O'Neil (Guest)", 300, 4, 450, 24),
            line("Sean O'Neil", 56, 40, 150, 56),
            line("Next item.", 56, 62, 140, 82),
        ),
        roster("Kunal Shah", "Sean O'Neil"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Sean O'Neil", ["Next item."])]


def test_without_a_roster_a_directory_name_line_is_an_unverified_header() -> None:
    blocks = teams.parse_caption_crop(
        result(line("Raman, Priya", 56, 4, 150, 20), line("Okay.", 56, 26, 100, 46)), Roster()
    )
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [(None, True, ["Okay."])]


# --- real OCR of rendered Teams windows (XERK-2609) ----------------------------------------
# Boxes tenir-ocr (Paddle, HEAD ocr/) read off synthetic Teams windows rendered by the
# XERK-2608 QA kit: full frames through the api's strip reader, crops from the client's band.

CAPTIONS = [
    "Okay, let's start with the quarterly numbers for the northern region.",
    "Sure. The shipment arrived on Tuesday but two pallets were damaged in transit.",
    "Can we get a replacement order in before the end of the month?",
]
SPEAKERS = ["Priya Raman", "Lee, Bo", "Sean O'Neil"]


def full_1080p_2x2() -> OcrResult:
    """1920x1080, four camera-off tiles, three caption entries."""
    return result(
        line("BL", 1395, 238, 1478, 299),
        line("PR", 441, 239, 528, 298),
        line("Priya Raman", 21, 446, 100, 466),
        line("Lee, Bo", 973, 446, 1024, 468),
        line("SO", 436, 659, 528, 718),
        line("MK", 1383, 659, 1492, 719),
        line("Sean O'Neil", 22, 867, 94, 885),
        line("Marta Kowalski", 974, 868, 1068, 885),
        line("PR Priya Raman", 15, 904, 134, 930),
        line(CAPTIONS[0], 53, 925, 566, 947),
        line("BL Lee, Bo", 17, 956, 102, 978),
        line(CAPTIONS[1], 53, 976, 655, 996),
        line("Sean O'Neil", 52, 1005, 126, 1022),
        line("So", 20, 1018, 35, 1026),
        line(CAPTIONS[2], 54, 1024, 541, 1048),
        width=1920,
        height=1080,
    )


def test_a_tile_label_just_above_the_caption_panel_stays_a_participant() -> None:
    # "Sean O'Neil" (y 867-885) sits 19 px above the panel's first header (y 904): linked into
    # the panel it was dropped from the roster, and his captions went to Lee, Bo.
    frame = teams.parse_full_frame(full_1080p_2x2())
    assert set(frame.participants) == {"Priya Raman", "Lee, Bo", "Sean O'Neil", "Marta Kowalski"}


def test_a_tile_label_above_a_doubled_ocr_box_stays_a_participant() -> None:
    # OCR read the avatar and first caption row as one tall box as well as separately; that
    # box's height let the label link in when the pair's height set the gap.
    frame = teams.parse_full_frame(
        result(
            line("PR", 867, 467, 1054, 596),
            line("Priya Raman", 22, 967, 99, 985),
            line("Priya Raman", 53, 1006, 134, 1022),
            line("PR " + CAPTIONS[0], 17, 1016, 565, 1049),
            line("PR", 18, 1016, 37, 1028),
            line(CAPTIONS[0], 53, 1025, 565, 1047),
            width=1920,
            height=1080,
        )
    )
    assert frame.participants == ["Priya Raman"]


def test_speakers_with_an_avatar_in_the_panel_are_participants() -> None:
    # Speaker view shows one tile; the other speakers are only in the caption panel.
    frame = teams.parse_full_frame(
        result(
            line("Priya Raman", 18, 504, 104, 527),
            line("PR Priya Raman", 16, 543, 135, 569),
            line(CAPTIONS[0], 51, 563, 567, 588),
            line("BL Lee, Bo", 17, 595, 103, 617),
            line(CAPTIONS[1], 52, 614, 656, 637),
            line("SO Sean O'Neil", 18, 645, 128, 665),
            line("SO", 22, 658, 36, 666),
            line(CAPTIONS[2], 52, 663, 542, 688),
            width=1280,
            height=720,
        )
    )
    assert frame.participants == SPEAKERS


@pytest.mark.parametrize("avatar", ["SO", "So", "so"])
def test_a_mis_cased_avatar_read_into_the_header_is_stripped(avatar: str) -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("BL Lee, Bo", 21, 154, 126, 183),
            line(CAPTIONS[1], 65, 181, 798, 204),
            line(f"{avatar} Sean O'Neil", 18, 246, 103, 263),
            line(CAPTIONS[2], 43, 262, 440, 281),
            width=1561,
            height=307,
        ),
        roster(*SPEAKERS),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Lee, Bo", [CAPTIONS[1]]),
        ("Sean O'Neil", [CAPTIONS[2]]),
    ]


@pytest.mark.parametrize("avatar", ["So", "so"])
def test_a_mis_cased_avatar_box_beside_the_header_is_dropped(avatar: str) -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Sean O'Neil", 52, 5, 126, 22),
            line(avatar, 20, 18, 35, 26),
            line(CAPTIONS[2], 54, 24, 541, 48),
        ),
        roster(*SPEAKERS),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Sean O'Neil", [CAPTIONS[2]])]


@pytest.mark.parametrize("row", ["So Bo Lee", "so Sean O'Neil said", "Of Oscar Fry"])
def test_a_lower_case_word_before_a_name_the_roster_lacks_stays_caption(row: str) -> None:
    blocks = teams.parse_caption_crop(
        result(line("Raman, Priya", 56, 4, 150, 20), line(row, 56, 26, 200, 46)),
        roster("Raman, Priya", "Sean O'Neil"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Raman, Priya", [row])]


@pytest.mark.parametrize(
    ("lines", "names"),
    [
        # Tile initials and labels a crop catches over camera-off tiles.
        (
            [
                line("sO", 347, 68, 437, 125),
                line("MK", 1116, 68, 1221, 125),
                line("Sean O'Neil", 15, 273, 80, 292),
                line("Marta Kowalski", 791, 274, 870, 291),
            ],
            ("Sean O'Neil", "Marta Kowalski"),
        ),
        # A misread row of labels over a gallery.
        ([line("Marta KowalskI Tomas Berg (Guest) Alko lanaka", 28, 0, 1145, 14)], ("Tomas Berg",)),
        # A shared slide, before any roster.
        (
            [
                line("Total supplier region region supplier supplier order", 35, 13, 480, 43),
                line("Forecast freight invoice total margin", 36, 43, 602, 73),
                line("Delivery unit supplier invoice", 37, 104, 288, 130),
            ],
            (),
        ),
    ],
)
def test_a_crop_with_no_speaker_header_yields_nothing(lines: list, names: tuple) -> None:
    # Any block from it would start a meeting, and re-sent it kept one open for good.
    assert teams.parse_caption_crop(result(*lines, width=1561, height=307), roster(*names)) == []


def test_text_scrolled_up_above_a_header_is_kept() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("in transit.", 56, 4, 150, 20),
            line("Sean O'Neil", 56, 30, 150, 46),
            line("Next item.", 56, 52, 140, 72),
        ),
        roster("Lee, Bo", "Sean O'Neil"),
    )
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        (None, False, ["in transit."]),
        ("Sean O'Neil", True, ["Next item."]),
    ]


def test_headers_the_roster_lacks_still_split_the_entries() -> None:
    # A gallery too dense to OCR left a roster of one; the other speakers' names were stored
    # as caption text under the previous speaker.
    blocks = teams.parse_caption_crop(
        result(
            line("Priya Raman", 42, 163, 109, 181),
            line(CAPTIONS[0], 42, 180, 460, 200),
            line("Lee,Bo", 43, 206, 84, 220),
            line(CAPTIONS[1], 42, 221, 533, 239),
            line("Sean O'Ne il", 42, 245, 104, 262),
            line(CAPTIONS[2], 42, 262, 441, 281),
            width=1561,
            height=307,
        ),
        roster("Sean O'Neil"),
    )
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        (None, True, [CAPTIONS[0]]),
        (None, True, [CAPTIONS[1]]),
        ("Sean O'Neil", True, [CAPTIONS[2]]),
    ]


def test_an_unknown_header_after_a_finished_entry_still_splits_it() -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Raman, Priya", 56, 4, 150, 20),
            line("Sounds good.", 56, 26, 150, 46),
            line("Kunal Shah", 56, 52, 150, 68),
            line("Next item.", 56, 74, 140, 94),
        ),
        roster("Raman, Priya"),
    )
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        ("Raman, Priya", True, ["Sounds good."]),
        (None, True, ["Next item."]),
    ]


@pytest.mark.parametrize(
    "rows",
    [
        # QA on XERK-2609, real OCR: the middle line was lost and the rest went to no one.
        ["We signed the contract with", "Acme Global Partners", "and they start on Monday."],
        ["I spoke with", "John Smith", "yesterday about the order."],
    ],
)
def test_a_title_case_line_inside_a_caption_stays_caption(rows: list[str]) -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("PR Priya Raman", 18, 4, 165, 30),
            *(line(r, 65, 30 + 26 * i, 600, 52 + 26 * i) for i, r in enumerate(rows)),
        ),
        roster("Priya Raman"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [("Priya Raman", rows)]


@pytest.mark.parametrize(
    "row", ["Hi Hank Ives", "To Tom Owens", "Paris,London"]
)
def test_name_like_words_on_the_caption_column_stay_caption(row: str) -> None:
    # Mis-cased initials only strip off a box left of the text column (the avatar's place),
    # and a lost comma space is only mended where a header can sit.
    blocks = teams.parse_caption_crop(
        result(
            line("Priya Raman", 65, 4, 165, 20),
            line("We flew via", 65, 26, 300, 46),
            line(row, 65, 52, 300, 72),
            line("and back again.", 65, 78, 300, 98),
        ),
        roster("Priya Raman", "Hank Ives", "Tom Owens"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["We flew via", row, "and back again."])
    ]


@pytest.mark.parametrize(
    ("token", "name"),
    [
        ("s", "Sean O'Neil"),  # one letter: a word, not a circle
        ("so", "Sean Mark O'Neil"),  # not all of his initials (S, M, O)
    ],
)
def test_a_mis_cased_box_short_of_the_full_initials_is_not_an_avatar(token: str, name: str) -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("Priya Raman", 65, 4, 165, 20),
            line("We met", 65, 26, 300, 46),
            line(token, 65, 52, 80, 72),
            line(name, 84, 52, 260, 72),
            line("at the depot.", 65, 78, 300, 98),
        ),
        roster("Priya Raman", name),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["We met", f"{token} {name}", "at the depot."])
    ]


def test_a_non_speaker_tile_label_just_above_the_panel_is_a_participant() -> None:
    # Only the half-height link keeps "Nora Lind" (19 px above the panel, never a speaker):
    # no panel header names her back.
    frame = teams.parse_full_frame(
        result(
            line("Nora Lind", 22, 867, 94, 885),
            line("PR Priya Raman", 15, 904, 134, 930),
            line(CAPTIONS[0], 53, 925, 566, 947),
            width=1920,
            height=1080,
        )
    )
    assert frame.participants == ["Nora Lind", "Priya Raman"]


@pytest.mark.parametrize("name", ["Al", "Jo (Guest)"])
def test_a_two_letter_display_name_is_a_participant(name: str) -> None:
    frame = teams.parse_full_frame(
        result(
            line("Priya Raman", 30, 330, 130, 358),
            line(name, 700, 330, 760, 358),
            width=1280,
            height=720,
        )
    )
    assert frame.participants == ["Priya Raman", teams.clean_name(name)]


def test_a_slide_heading_with_a_roster_starts_no_meeting() -> None:
    slide = result(
        line("Quarterly Business Review", 35, 13, 400, 43),
        line("Revenue grew in every region this quarter", 36, 50, 600, 73),
        line("Next Steps", 36, 80, 200, 103),
        line("hire two more drivers for the depot", 37, 110, 500, 130),
        width=1561,
        height=307,
    )
    r = roster("Priya Raman")
    assert teams.parse_caption_crop(slide, r) == []
    # During a meeting text without a known header is kept: it may be an entry whose
    # header scrolled off, and the merge decides what is new.
    assert teams.parse_caption_crop(slide, r, live=True) != []


def test_a_resent_header_less_crop_lets_a_teams_meeting_end() -> None:
    import asyncio

    from api.persistence import InMemoryConversationStore, ScreenMeta
    from api.screen.meetings import Cadence, MeetingTracker
    from api.screen.zoom import CaptionBlock

    async def run(platform: str) -> bool:
        t = MeetingTracker(
            "default",
            "user-1",
            ScreenMeta("tesoro", "Tesoro", platform),
            store=InMemoryConversationStore(),
            account_exists=lambda _: True,
            cadence=Cadence(0.2, 1.0, 10.0),
            stable_ms=3000,
            end_ms=60_000,
        )
        await t.ensure_roster()
        await t.on_caption([CaptionBlock("Priya Raman", True, ["Okay."])], 0)
        tail = [CaptionBlock(None, False, ["a long entry whose header scrolled off"])]
        await t.on_caption(tail, 10_000)  # new text: keeps the meeting open
        for ts in range(20_000, 80_000, 10_000):
            await t.on_caption(list(tail), ts)  # the same crop again
        return t.expired(70_000)

    assert asyncio.run(run("teams")) is True
    assert asyncio.run(run("zoom")) is False  # Zoom keeps its old rule


def test_big_tile_initials_above_the_panel_are_not_caption_text() -> None:
    # 2560x1440: the band caught the bottom tiles' circles, read "so" and "MK".
    blocks = teams.parse_caption_crop(
        result(
            line("so", 353, 0, 431, 40),
            line("MK", 1124, 0, 1215, 39),
            line("Sean O'Neil", 11, 176, 60, 189),
            line("Marta Kowalski", 789, 178, 846, 188),
            line("PR Priya Raman", 10, 201, 81, 214),
            line(CAPTIONS[0], 32, 211, 344, 227),
            line("Lee, Bo", 33, 232, 62, 242),
            line(CAPTIONS[1], 32, 242, 399, 257),
            line("Sean O'Neil", 33, 262, 78, 272),
            line(CAPTIONS[2], 32, 272, 330, 287),
            width=1561,
            height=307,
        ),
        roster(*SPEAKERS, "Marta Kowalski"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        (name, [text]) for name, text in zip(SPEAKERS, CAPTIONS, strict=True)
    ]


@pytest.mark.parametrize("circle", ["So", "sO", "SO"])
def test_a_mis_cased_tile_circle_is_not_a_participant(circle: str) -> None:
    frame = teams.parse_full_frame(
        result(
            line(circle, 1566, 200, 1625, 240),
            line("Sean O'Neil", 1300, 330, 1420, 358),
            line("Bob", 30, 330, 80, 358),  # a short display name is still a name
            width=2560,
            height=1440,
        )
    )
    assert frame.participants == ["Bob", "Sean O'Neil"]


@pytest.mark.parametrize("last", ["Let's look at the numbers for the north region", "and then,"])
@pytest.mark.parametrize("avatar", ["token", "box"])
def test_an_unknown_speaker_with_an_avatar_splits_an_unfinished_entry(last: str, avatar: str) -> None:
    # QA on XERK-2609, real OCR: Diego, not yet on the roster, spoke after an unpunctuated
    # line; his header and caption went to Priya, then were stored again under him.
    diego = (
        [line("DA Diego Alvarez", 18, 60, 140, 80)]
        if avatar == "token"
        else [line("DA", 18, 62, 40, 78), line("Diego Alvarez", 65, 60, 170, 80)]
    )
    blocks = teams.parse_caption_crop(
        result(
            line("PR Priya Raman", 18, 4, 140, 24),
            line(last, 65, 28, 500, 48),
            *diego,
            line("Sure, I have them right here.", 65, 84, 400, 104),
        ),
        roster("Priya Raman", "Lee, Bo"),
    )
    assert [(b.speaker, b.has_header, b.lines) for b in blocks] == [
        ("Priya Raman", True, [last]),
        (None, True, ["Sure, I have them right here."]),
    ]


@pytest.mark.parametrize("misread", ["3 Lee, Bo", "BI Lee, Bo"])
def test_a_misread_avatar_before_a_roster_name_is_stripped(misread: str) -> None:
    # Real OCR, 1920x1080 / 1680x1050: the "BL" circle read as "3" or "BI".
    blocks = teams.parse_caption_crop(
        result(
            line("PR Priya Raman", 18, 4, 140, 24),
            line("Okay, let's start.", 65, 28, 300, 48),
            line(misread, 18, 56, 140, 76),
            line("Thanks.", 65, 80, 200, 100),
        ),
        roster("Priya Raman", "Lee, Bo"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["Okay, let's start."]),
        ("Lee, Bo", ["Thanks."]),
    ]


@pytest.mark.parametrize(
    "row",
    [
        "3 Kunal Shah",  # a name the roster lacks: not known to be a header
        "Okay Lee, Bo",  # a word longer than a circle's misread
    ],
)
def test_only_a_short_token_before_a_roster_name_reads_as_a_misread_avatar(row: str) -> None:
    blocks = teams.parse_caption_crop(
        result(
            line("PR Priya Raman", 18, 4, 140, 24),
            line("We met", 65, 28, 300, 48),
            line(row, 18, 56, 140, 76),
            line("at the depot.", 65, 80, 200, 100),
        ),
        roster("Priya Raman", "Lee, Bo"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["We met", row, "at the depot."])
    ]


def test_a_name_like_last_caption_row_with_no_text_below_stays_caption() -> None:
    # Only a box left of the text below it is an avatar row: at the crop's bottom there is
    # nothing below, so "Hi Hank Ives" stays Priya's words.
    blocks = teams.parse_caption_crop(
        result(
            line("PR Priya Raman", 18, 4, 140, 24),
            line("Okay, let's start.", 65, 28, 300, 48),
            line("Hi Hank Ives", 18, 56, 140, 76),
        ),
        roster("Priya Raman", "Hank Ives"),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        ("Priya Raman", ["Okay, let's start.", "Hi Hank Ives"])
    ]


def test_a_mis_cased_panel_header_beside_a_shared_slide_is_a_participant() -> None:
    # 1920x1080 share with captions, real OCR: the slide's rows link into the panel stack.
    lines = [
        line("Pallet unit cost audit quarter region total region", 29, 601, 368, 622),
        line("Order order margin forecast delivery forecast", 30, 625, 352, 645),
        line("Unit region region delivery stock pallet forecast margin audit", 29, 861, 462, 885),
        line("PR Priya Raman", 14, 903, 134, 930),
        line(CAPTIONS[0], 53, 925, 567, 948),
        line("BL Lee, Bo", 17, 954, 103, 979),
        line(CAPTIONS[1], 51, 973, 656, 998),
        line("so Sean O'Neil", 19, 1005, 127, 1027),
        line(CAPTIONS[2], 52, 1023, 543, 1048),
    ]
    frame = teams.parse_full_frame(result(*lines, width=1920, height=1080))
    assert frame.participants == SPEAKERS


def test_a_header_less_crop_mid_meeting_is_stored(screen) -> None:
    """The socket tells the parser a meeting is live: a crop whose header scrolled off then
    still feeds the merge (refused, a long entry was lost and the meeting ended)."""
    from api.main import app

    store, ocr, token, admin = screen
    base = int(time.time() * 1000)
    crops = [
        result(line("Priya Raman", 56, 4, 150, 20), line("Okay, let's start.", 56, 26, 300, 46)),
        result(
            line("Okay, let's start.", 56, 4, 300, 20),
            line("the north region shipped late again", 56, 26, 400, 46),
        ),
    ]
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()
        for i, crop in enumerate(crops):
            jpeg = ocr.register(unique_jpeg(9000 + i), crop)
            ws.send_bytes(envelope(teams_header(base + 1000 * i, "caption"), jpeg))
            wait_handled(i + 1)
        (summary,) = store.list(admin.household)
        end = base + 1000 + int(settings.screen_meeting_end_seconds * 1000)
        assert client.portal.call(meetings.sweep, end) == [summary.id]
    conv = store.get(admin.household, summary.id)
    assert [s.text for s in conv.segments] == [
        "Okay, let's start.",
        "the north region shipped late again",
    ]


def test_real_ocr_of_a_1080p_meeting_gives_each_caption_its_speaker() -> None:
    frame = teams.parse_full_frame(full_1080p_2x2())
    blocks = teams.parse_caption_crop(
        result(
            line("Sean O'Neil", 14, 131, 80, 150),
            line("Marta Kowalski", 791, 131, 871, 150),
            line("PR Priya Raman", 16, 164, 109, 182),
            line(CAPTIONS[0], 43, 181, 459, 199),
            line("BL Lee, Bo", 16, 206, 84, 224),
            line(CAPTIONS[1], 43, 221, 533, 239),
            line("So Sean O'Neil", 18, 246, 103, 263),
            line(CAPTIONS[2], 43, 262, 440, 281),
            width=1561,
            height=307,
        ),
        roster(*frame.participants),
    )
    assert [(b.speaker, b.lines) for b in blocks] == [
        (name, [text]) for name, text in zip(SPEAKERS, CAPTIONS, strict=True)
    ]


def test_an_unknown_platform_is_refused() -> None:
    bad = dict(teams_header(1, "caption"), platform="webex")
    with pytest.raises(screen_ws.FrameError, match="bad frame header"):
        screen_ws.split_frame(envelope(bad, tiny_jpeg()))


# --- replay --------------------------------------------------------------------------------


def test_replayed_teams_meeting_has_the_right_speakers_and_no_duplicate_lines(screen) -> None:
    """Done-when (XERK-2515): replayed Teams frames produce correct speakers and no
    duplicate lines."""
    from api.main import app

    store, ocr, token, admin = screen
    base = int(time.time() * 1000)
    frames = FIXTURE["frames"]
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # idle cadence
        for i, frame in enumerate(frames):
            jpeg = ocr.register(unique_jpeg(i), ocr_of(frame))
            ws.send_bytes(envelope(teams_header(base + frame["t"], frame["region"]), jpeg))
            wait_handled(i + 1)
            if i == 1:
                # Tile labels alone (no caption text) don't start a meeting.
                assert store.list(admin.household) == []
        assert json.loads(ws.receive_text())["fps"] == settings.screen_meeting_fps
        (summary,) = store.list(admin.household)
        end = base + frames[-1]["t"] + int(settings.screen_meeting_end_seconds * 1000)
        assert client.portal.call(meetings.sweep, end) == [summary.id]

    conv = store.get(admin.household, summary.id)
    assert conv is not None and conv.status == "ready"
    assert (conv.platform, conv.org_site_key, conv.org_name) == ("teams", "yprime", "YPrime")
    expected = FIXTURE["expected"]
    assert conv.meeting_title == expected["title"]
    assert conv.participants == expected["participants"]
    assert [{"speaker": s.speaker, "text": s.text} for s in conv.segments] == expected["lines"]
    starts = [s.start_ms for s in conv.segments]
    assert starts == sorted(starts)
