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


def _ended_window_tracker(platform: str = "teams"):  # type: ignore[no-untyped-def]
    from api.persistence import InMemoryConversationStore, ScreenMeta
    from api.screen.meetings import Cadence, MeetingTracker

    store = InMemoryConversationStore()
    t = MeetingTracker(
        "default",
        "user-1",
        ScreenMeta("tesoro", "Tesoro", platform),
        store=store,
        account_exists=lambda _: True,
        cadence=Cadence(0.2, 1.0, 10.0),
        stable_ms=3000,
        end_ms=180_000,
    )
    return t, store


@pytest.mark.parametrize("platform", ["teams", "zoom"])
def test_a_panel_re_sent_after_its_meeting_ended_starts_no_new_meeting(platform: str) -> None:
    # XERK-2615: a break longer than the end timer, the panel still on screen. Its next crop
    # re-reads only lines the ended meeting stored: no second conversation holding them again.
    import asyncio

    from api.screen.zoom import CaptionBlock

    panel = [
        CaptionBlock("Priya Raman", True, ["Okay, let's start."]),
        CaptionBlock("Lee, Bo", True, ["Sounds good to me."]),
    ]

    async def run() -> None:
        t, store = _ended_window_tracker(platform)
        await t.on_caption(panel, 0)
        await t.on_caption(list(panel), 5_000)
        first = await t.end(200_000)
        assert first is not None
        for ts in range(400_000, 700_000, 10_000):  # long after the end timer
            await t.on_caption(list(panel), ts)
        assert t.conversation_id is None
        assert list(store._conversations("default")) == [first]
        # Then someone speaks again: a new meeting holding only the new line.
        more = [*panel, CaptionBlock("Priya Raman", True, ["Welcome back, everyone."])]
        await t.on_caption(more, 710_000)
        second = await t.end(1_000_000)
        assert second is not None and second != first
        assert [(s.speaker, s.text) for s in store.get("default", second).segments] == [
            ("Priya Raman", "Welcome back, everyone.")
        ]
        assert [s.text for s in store.get("default", first).segments] == [
            "Okay, let's start.",
            "Sounds good to me.",
        ]

    asyncio.run(run())


def test_a_new_meeting_after_the_last_one_ended_still_starts() -> None:
    # The carried lines only hide re-reads: a fresh panel with new text starts a meeting.
    import asyncio

    from api.screen.zoom import CaptionBlock

    async def run() -> None:
        t, store = _ended_window_tracker()
        await t.on_caption([CaptionBlock("Priya Raman", True, ["Okay, let's start."])], 0)
        first = await t.end(200_000)
        await t.on_caption([CaptionBlock("Sean O'Neil", True, ["Morning, all."])], 900_000)
        second = await t.end(1_200_000)
        assert second is not None and second != first
        assert [s.text for s in store.get("default", second).segments] == ["Morning, all."]

    asyncio.run(run())


def test_a_reconnect_over_the_break_still_starts_no_new_meeting() -> None:
    # QA on XERK-2615: the sweep drops an idle tracker with no socket. The client reconnects
    # (a network blip, a PC asleep over the break) and re-sends the panel to a fresh tracker.
    import asyncio

    from api.screen.meetings import ScreenMeetings
    from api.screen.zoom import CaptionBlock

    panel = [CaptionBlock("Kunal Shah", True, ["Thanks, both."])]
    key = ("default", "user-1", "tesoro")

    async def run() -> None:
        reg = ScreenMeetings()
        t, store = _ended_window_tracker()
        reg.put(key, t)
        await t.on_caption(panel, 0)
        assert len(await reg.sweep(200_000)) == 1  # ends the meeting and drops the tracker
        assert reg.get(key) is None  # idle, no socket: dropped
        fresh, _ = _ended_window_tracker()
        fresh.store = store
        assert reg.put(key, fresh) is fresh
        await fresh.on_caption(list(panel), 400_000)
        assert fresh.conversation_id is None
        assert len(store._conversations("default")) == 1

    asyncio.run(run())


def test_the_carried_lines_are_capped_oldest_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from api.screen import meetings as m
    from api.screen.zoom import CaptionBlock

    monkeypatch.setattr(m, "_MAX_CARRIES", 2)

    async def run() -> None:
        reg = m.ScreenMeetings()
        for org in ("a", "b", "c"):
            t, _ = _ended_window_tracker()
            reg.put(("default", "user-1", org), t)
            await t.on_caption([CaptionBlock("Ana", True, ["hi"])], 0)
            await reg.sweep(200_000)
        assert list(reg._carries) == [("default", "user-1", "b"), ("default", "user-1", "c")]

    asyncio.run(run())


def test_the_last_speaker_going_on_after_the_break_is_a_new_line() -> None:
    # QA on XERK-2615: the carried newest line must not "grow" (its segment is the ended
    # meeting's), or the continuation aligned onto it, was refused, and stored nothing.
    # A short end timer (< merge._EXTEND_MS) also checks the ended segment stays as it was.
    import asyncio

    from api.screen.zoom import CaptionBlock

    async def run() -> None:
        t, store = _ended_window_tracker()
        t.end_ms = 10_000
        await t.on_caption([CaptionBlock("Kunal Shah", True, ["Thanks, both."])], 0)
        first = await t.end(10_000)
        grown = [CaptionBlock("Kunal Shah", True, ["Thanks, both. And see you tomorrow."])]
        await t.on_caption(grown, 15_000)
        second = await t.end(30_000)
        assert second is not None
        old = store.get("default", first).segments
        new = store.get("default", second).segments
        assert [s.text for s in old] == ["Thanks, both."]
        assert [s.text for s in new] == ["Thanks, both. And see you tomorrow."]
        assert not {s.segment_id for s in old} & {s.segment_id for s in new}
        # A small addition still re-reads the carried line (similar enough): it must leave the
        # ended meeting's segment as it was, not extend it after the meeting ended.
        t, store = _ended_window_tracker()
        await t.on_caption([CaptionBlock("Kunal Shah", True, ["Thanks, both."])], 0)
        first = await t.end(10_000)
        await t.on_caption([CaptionBlock("Kunal Shah", True, ["Thanks, both. Bye."])], 12_000)
        assert [s.text for s in store.get("default", first).segments] == ["Thanks, both."]

    asyncio.run(run())


# --- pixel evidence: avatars in the caption crop (XERK-2614) -------------------------------
#
# Synthetic panels laid out like teamsynth's renders: a 32 px avatar circle at the left (a photo:
# no OCR box), its top level with the 16 px name line, text rows indented under the name. Only
# the avatars are drawn: the parser reads text from the OCR boxes, not from pixels.

_DARK = (31, 31, 31)
_LIGHT = (245, 245, 245)


def panel(
    rows: list[tuple[str, str | None]],
    *,
    width: int = 760,
    text_x: int = 44,
    bg=_DARK,
    above: int = 0,
    fill=(196, 49, 75),
) -> tuple[OcrResult, bytes]:
    """OCR boxes and a JPEG for panel ``rows`` of (text, what is drawn beside it: "circle",
    "square" (a slide's picture) or None). ``above`` px of a grey tile sit over the panel."""
    import io

    from PIL import Image, ImageDraw

    height = above + 26 * len(rows) + 30
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    if above:
        draw.rectangle((0, 0, width, above - 12), fill=(61, 61, 61))
    lines = []
    for i, (text, mark) in enumerate(rows):
        y = above + 4 + 26 * i
        if mark == "circle":
            draw.ellipse((4, y, 36, y + 32), fill=fill)
        elif mark == "square":
            draw.rectangle((4, y, 36, y + 32), fill=fill)
        lines.append(line(text, text_x, y, text_x + 8 * len(text), y + 16))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return OcrResult(width, height, tuple(lines)), buf.getvalue()


def blocks_of(blocks) -> list[tuple[str | None, bool, list[str]]]:
    return [(b.speaker, b.has_header, b.lines) for b in blocks]


_PHOTO_AFTER_UNFINISHED = [
    ("Priya Raman", "circle"),
    ("Let's look at the numbers for the north region", None),
    ("Diego Alvarez", "circle"),
    ("Sure, I have them right here.", None),
]


@pytest.mark.parametrize(
    ("bg", "fill"),
    [(_DARK, (196, 49, 75)), (_DARK, (60, 56, 52)), (_LIGHT, (214, 226, 240))],
    ids=["dark", "dark-photo", "light-pastel"],
)
def test_a_photo_avatar_speaker_off_the_roster_gets_their_own_header(bg, fill) -> None:
    # Diego's photo avatar has no initials, so OCR gives no box for it; Priya's line before
    # him is unfinished (no full stop), so position alone merged him into Priya. A dark photo
    # on the dark panel and a pastel circle on a light one differ from it by ~25-45 levels.
    # A tile's grey sits above the panel, as a fixed band catches it: the background must be
    # the panel's own colour, not the crop's (on a light panel the crop's read as all ink).
    ocr, jpeg = panel(_PHOTO_AFTER_UNFINISHED, bg=bg, fill=fill, above=300)
    expected = [
        ("Priya Raman", True, ["Let's look at the numbers for the north region"]),
        (None, True, ["Sure, I have them right here."]),
    ]
    assert blocks_of(teams.parse_caption_crop(ocr, roster("Priya Raman"), jpeg=jpeg)) == expected
    # Without the image the old position rule still merges him (the regression guard).
    assert blocks_of(teams.parse_caption_crop(ocr, roster("Priya Raman")))[0][2][1:] == [
        "Diego Alvarez",
        "Sure, I have them right here.",
    ]


def test_a_guest_with_a_photo_avatar_gets_their_own_header() -> None:
    rows = [*_PHOTO_AFTER_UNFINISHED[:2], ("Tomas Berg (Guest)", "circle"), ("Hello all.", None)]
    ocr, jpeg = panel(rows)
    blocks = teams.parse_caption_crop(ocr, roster("Priya Raman"), jpeg=jpeg)
    assert blocks_of(blocks)[1:] == [(None, True, ["Hello all."])]


@pytest.mark.parametrize("names", [(), ("Priya Raman",)])
def test_a_name_shaped_first_caption_line_stays_under_its_speaker(names: tuple[str, ...]) -> None:
    ocr, jpeg = panel(
        [
            ("Priya Raman", "circle"),
            ("Acme Global Partners", None),
            ("signed the deal today.", None),
            ("Hank Ives", "circle"),
            ("Lena Moss", None),
            ("is joining us next week.", None),
        ]
    )
    speaker = names[0] if names else None
    assert blocks_of(teams.parse_caption_crop(ocr, roster(*names), jpeg=jpeg)) == [
        (speaker, True, ["Acme Global Partners", "signed the deal today."]),
        (None, True, ["Lena Moss", "is joining us next week."]),
    ]


_SLIDE = [
    ("Quarterly Results", None),
    ("Revenue grew in every region this year.", None),
    ("Next Steps", None),
    ("hire two more account managers", None),
]


@pytest.mark.parametrize("mark", [None, "square"])
@pytest.mark.parametrize("names", [(), ("Priya Raman",)])
def test_a_slide_in_the_band_starts_no_meeting(names: tuple[str, ...], mark: str | None) -> None:
    # A slide's headings are name-shaped; a square picture beside one is no avatar either.
    rows = [(_SLIDE[0][0], mark), *_SLIDE[1:]]
    ocr, jpeg = panel(rows)
    assert teams._pixel_avatars(jpeg, ocr) == set()
    assert teams.parse_caption_crop(ocr, roster(*names), jpeg=jpeg) == []
    # Mid-meeting it carries no roster-matched header; the tracker then drops it (test below).
    live = teams.parse_caption_crop(ocr, roster(*names), jpeg=jpeg, live=True)
    assert live and not any(b.speaker for b in live)


def test_with_no_avatar_found_a_roster_keeps_the_position_rule() -> None:
    # A crop whose avatars can't be seen (too faint) falls back to where the rows sit.
    ocr, jpeg = panel(_PHOTO_AFTER_UNFINISHED, fill=(36, 36, 36))
    assert teams._pixel_avatars(jpeg, ocr) == set()
    rows = [("Priya Raman", None), ("Okay.", None), ("Diego Alvarez", None), ("Sure.", None)]
    ocr, jpeg = panel(rows)
    assert blocks_of(teams.parse_caption_crop(ocr, roster("Priya Raman"), jpeg=jpeg)) == [
        ("Priya Raman", True, ["Okay."]),
        (None, True, ["Sure."]),
    ]


def test_a_tile_label_above_the_panel_is_no_caption_line_without_a_roster() -> None:
    rows = [("Sean O'Neil", None), ("Priya Raman", "circle"), ("Okay, let's start.", None)]
    ocr, jpeg = panel(rows, above=40)
    assert [b.lines for b in teams.parse_caption_crop(ocr, Roster(), jpeg=jpeg)] == [
        [],
        ["Okay, let's start."],
    ]


def test_a_text_row_meeting_only_an_avatars_lower_half_is_no_header() -> None:
    # The header row went unread: the first text row overlaps the avatar's lower half only.
    ocr, jpeg = panel([("Priya Raman", "circle"), ("Thanks Everyone", None)])
    unread = OcrResult(ocr.width, ocr.height, ocr.lines[1:])
    assert teams._pixel_avatars(jpeg, unread) == set()


def test_pixel_evidence_is_skipped_when_it_cannot_be_read() -> None:
    ocr, jpeg = panel([("Priya Raman", "circle"), ("Sure, I have them right here.", None)])
    assert teams._pixel_avatars(None, ocr) is None
    assert teams._pixel_avatars(tiny_jpeg(), ocr) is None  # not the size OCR read
    assert teams._pixel_avatars(b"\xff\xd8 not a jpeg", ocr) is None
    flush, flush_jpeg = panel([("Priya Raman", None), ("okay", None)], text_x=4)
    assert teams._pixel_avatars(flush_jpeg, flush) is None  # no column left of the text
    assert teams._pixel_avatars(jpeg, OcrResult(ocr.width, ocr.height, ())) is None


def test_a_name_box_that_swallowed_its_initials_keeps_the_other_avatars() -> None:
    # Real OCR reads a name box from the avatar's initials ("DA Diego Alvarez" at x 13, text
    # at 43): the column is the caption-text rows' median start, and such a box still counts
    # as beside its avatar.
    ocr, jpeg = panel(_PHOTO_AFTER_UNFINISHED)
    y0, y1 = ocr.lines[2].box[1], ocr.lines[2].box[3]
    swallowed = line("DA Diego Alvarez", 8, y0, 150, y1)
    first = line("PR Priya Raman", 8, ocr.lines[0].box[1], 140, ocr.lines[0].box[3])
    crop = OcrResult(ocr.width, ocr.height, (first, ocr.lines[1], swallowed, ocr.lines[3]))
    assert teams._pixel_avatars(jpeg, crop) == {id(first), id(swallowed)}
    blocks = teams.parse_caption_crop(crop, Roster(), jpeg=jpeg)
    assert [(b.has_header, b.lines) for b in blocks] == [
        (True, ["Let's look at the numbers for the north region"]),
        (True, ["Sure, I have them right here."]),
    ]


def test_the_text_column_ignores_name_boxes_that_start_on_their_avatar() -> None:
    # Most rows are names read from their initials (short entries, the last one's text not
    # yet in): a median over every row would put the column on the avatars.
    ocr, jpeg = panel(
        [
            ("Priya Raman", "circle"),
            ("Okay.", None),
            ("Diego Alvarez", "circle"),
            ("Sure.", None),
            ("Lee, Bo", "circle"),
        ]
    )
    names = {0: "PR Priya Raman", 2: "DA Diego Alvarez", 4: "BL Lee, Bo"}
    lines = tuple(
        line(names[i], 8, b.box[1], 150, b.box[3]) if i in names else b
        for i, b in enumerate(ocr.lines)
    )
    found = teams._pixel_avatars(jpeg, OcrResult(ocr.width, ocr.height, lines))
    assert found == {id(lines[i]) for i in names}


@pytest.mark.parametrize("names", [(), ("Priya Raman",)])
def test_initials_read_into_a_name_box_are_avatar_evidence_when_its_circle_is_missed(
    names: tuple[str, ...],
) -> None:
    # Diego's circle isn't found round (JPEG speckle, a cut edge: drawn square here), but OCR
    # read its "DA" into his name box and the column is lit under it: he is a header. With no
    # roster his name was stored as caption text, or the crop started nothing (QA). His line
    # reads like a name ("OK Lee, Bo", 1680x1050 real OCR).
    rows = [*_PHOTO_AFTER_UNFINISHED[:2], ("Diego Alvarez", "square"), ("OK Lee, Bo", None)]
    ocr, jpeg = panel(rows)
    b = ocr.lines[2]
    diego = line("DA Diego Alvarez", 8, b.box[1], 150, b.box[3])
    crop = OcrResult(ocr.width, ocr.height, (*ocr.lines[:2], diego, ocr.lines[3]))
    assert teams._pixel_avatars(jpeg, crop) == {id(crop.lines[0])}
    blocks = teams.parse_caption_crop(crop, roster(*names), jpeg=jpeg)
    assert [(b.has_header, b.avatar, b.lines) for b in blocks][1:] == [(True, True, ["OK Lee, Bo"])]
    # Alone (Priya's entry scrolled off, no roster), the crop still starts the meeting.
    alone = OcrResult(ocr.width, ocr.height, (diego, ocr.lines[3]))
    assert [b.lines for b in teams.parse_caption_crop(alone, Roster(), jpeg=jpeg)] == [["OK Lee, Bo"]]


def test_the_panel_colour_is_the_gaps_most_common_colour() -> None:
    # A gap sample that catches avatar edges and glyphs (40% panel, the rest five colours):
    # its median drifted ~29 levels, past the ink threshold, and lit the whole column (QA).
    import numpy as np

    others = [(196, 90, 75), (0, 120, 212), (73, 130, 5), (214, 214, 214), (98, 100, 167)]
    # The panel's own pixels vary by JPEG noise: only quantised do they share one colour.
    panel_px = [(31 + d, 31 - d, 31 + d) for d in (-3, -2, -1, 0, 1, 2, 3, 0)] * 5
    gap = np.array(panel_px + [c for c in others for _ in range(12)], dtype=np.int16)
    assert np.abs(np.median(gap, axis=0) - 31).max() > teams._INK_DELTA
    assert np.abs(teams._mode_colour(gap) - 31).max() < 4


def test_avatars_are_found_when_the_gap_sample_catches_other_colours() -> None:
    # Stripes in 3 of the gap's 5 columns on the text rows (an avatar edge, glyph fringes):
    # a median of them reads ~120 for a 31 panel, which then lights every line.
    import io

    from PIL import Image, ImageDraw

    ocr, _ = panel(_PHOTO_AFTER_UNFINISHED)
    img = Image.new("RGB", (ocr.width, ocr.height), _DARK)
    draw = ImageDraw.Draw(img)
    for i, (_, mark) in enumerate(_PHOTO_AFTER_UNFINISHED):
        y = 4 + 26 * i
        if mark:
            draw.ellipse((4, y, 36, y + 32), fill=(196, 49, 75))
        else:
            for x, v in zip((35, 36, 37), (200, 150, 120), strict=True):
                draw.line((x, y, x, y + 16), fill=(v, v, v))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=100, subsampling=0)  # keeps the 1 px stripes
    assert teams._pixel_avatars(buf.getvalue(), ocr) == {id(ocr.lines[0]), id(ocr.lines[2])}


def test_an_avatar_patch_may_start_below_the_name_boxs_top() -> None:
    # OCR boxes are padded: the circle's top can sit a few px under its name box's top, so
    # the patch need only start by the box's middle.
    import io

    from PIL import Image, ImageDraw

    rows = [*_PHOTO_AFTER_UNFINISHED[:2], ("Diego Alvarez", None), ("OK Lee, Bo", None)]
    ocr, _ = panel(rows)
    b = ocr.lines[2]
    diego = line("DA Diego Alvarez", 8, b.box[1], 150, b.box[3])
    crop = OcrResult(ocr.width, ocr.height, (*ocr.lines[:2], diego, ocr.lines[3]))
    img = Image.new("RGB", (ocr.width, ocr.height), _DARK)
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 36, 36), fill=(196, 49, 75))
    top = int(b.box[1]) + 5  # under the box's top, above its middle (box 16 px tall)
    draw.rectangle((4, top, 36, top + 32), fill=(196, 49, 75))  # square: not found round
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    blocks = teams.parse_caption_crop(crop, Roster(), jpeg=buf.getvalue())
    assert [(b.has_header, b.avatar) for b in blocks][1:] == [(True, True)]


@pytest.mark.parametrize("indent", [30, 60])
def test_an_initials_heading_over_indented_slide_text_is_no_avatar(indent: int) -> None:
    # The heading box starts left of the text under it, as a name box read from its avatar's
    # initials does, but no circle reaches below it (QA, real OCR of bulleted slides).
    rows = [
        ("AI Artificial Intelligence", None),
        ("Revenue grew in every region this year.", None),
        ("finalize the vendor shortlist by Friday", None),
    ]
    ocr, jpeg = panel(rows)
    lines = tuple(
        b if i == 0 else line(b.text, b.box[0] + indent, b.box[1], b.box[2] + indent, b.box[3])
        for i, b in enumerate(ocr.lines)
    )
    live = teams.parse_caption_crop(
        OcrResult(ocr.width, ocr.height, lines), roster("Priya Raman"), jpeg=jpeg, live=True
    )
    assert not any(b.avatar or b.speaker for b in live)


def test_a_slide_page_filling_the_avatar_column_vouches_for_no_heading() -> None:
    # 1920 real OCR (QA): a light slide page starts at x 10 inside the dark frame and most of
    # its lines at x 25, so the avatar column straddles the page edge and is lit on every
    # line. Ink under a box counts only as an avatar-sized patch, not a column-long one.
    import io

    from PIL import Image, ImageDraw

    body = [line(f"Freight order unit audit margin {i}", 25, 20 * i, 400, 20 * i + 16) for i in range(8)]
    slide = [
        line("AI Artificial Intelligence", 63, 165, 278, 181),
        line("Revenue grew twenty percent this year", 88, 190, 312, 206),
        line("A New Approach", 63, 220, 211, 236),
        line("finalize the vendor shortlist by Friday", 86, 250, 302, 266),
    ]
    crop = OcrResult(760, 300, (*body, *slide))
    img = Image.new("RGB", (760, 300), _DARK)
    ImageDraw.Draw(img).rectangle((10, 0, 760, 300), fill=_LIGHT)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    for names in ((), ("Priya Raman",)):
        assert teams.parse_caption_crop(crop, roster(*names), jpeg=buf.getvalue()) == []
        live = teams.parse_caption_crop(crop, roster(*names), jpeg=buf.getvalue(), live=True)
        assert not any(b.avatar or b.speaker for b in live)


@pytest.mark.parametrize(
    "picture",
    [(4, 4, 36, 20), (4, 18, 36, 50)],
    ids=["icon-as-tall-as-the-heading", "picture-starting-under-it"],
)
def test_ink_beside_a_heading_is_no_avatar_unless_it_starts_level_and_reaches_below(
    picture: tuple[int, int, int, int],
) -> None:
    # An avatar starts level with its name line and outlasts it. An icon the heading's own
    # height ends with it; a picture beside the bullets starts under it.
    import io

    from PIL import Image, ImageDraw

    heading = line("AI Artificial Intelligence", 44, 4, 252, 20)
    body = [line(f"bullet point number {i} here", 74, 30 + 26 * i, 300, 46 + 26 * i) for i in range(3)]
    crop = OcrResult(760, 120, (heading, *body))
    img = Image.new("RGB", (760, 120), _DARK)
    ImageDraw.Draw(img).rectangle(picture, fill=(196, 49, 75))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    live = teams.parse_caption_crop(crop, roster("Priya Raman"), jpeg=buf.getvalue(), live=True)
    assert not any(b.avatar for b in live)


def test_an_initials_shaped_slide_heading_is_no_avatar() -> None:
    # "AI Artificial Intelligence" reads like a name behind its circle's initials, but starts
    # where the slide's text does, not left of it on an avatar (QA).
    rows = [
        ("AI Artificial Intelligence", None),
        ("Revenue grew in every region this year.", None),
        ("A New Approach", None),
        ("finalize the vendor shortlist by Friday", None),
    ]
    ocr, jpeg = panel(rows)
    live = teams.parse_caption_crop(ocr, roster("Priya Raman"), jpeg=jpeg, live=True)
    assert not any(b.avatar or b.speaker for b in live)


def test_a_photo_speaker_off_the_roster_alone_in_the_panel_is_stored() -> None:
    """The parser's avatar flag reaches the tracker: Diego's entry, with Priya's scrolled off,
    shares no line with what was stored and has no roster header, only his photo."""
    import asyncio

    from api.persistence import InMemoryConversationStore, ScreenMeta
    from api.screen.meetings import Cadence, MeetingTracker

    async def run() -> list[tuple[str | None, str]]:
        store = InMemoryConversationStore()
        t = MeetingTracker(
            "default",
            "user-1",
            ScreenMeta("tesoro", "Tesoro", "teams"),
            store=store,
            account_exists=lambda _: True,
            cadence=Cadence(0.2, 1.0, 10.0),
            stable_ms=3000,
            end_ms=60_000,
        )
        ros = await t.ensure_roster()
        ros.add("Priya Raman")
        for ts, rows in (
            (0, _PHOTO_AFTER_UNFINISHED[:2]),
            (1000, [("Diego Alvarez", "circle"), ("Sure, I have them right here.", None)]),
        ):
            ocr, jpeg = panel(rows)
            live = t.conversation_id is not None
            await t.on_caption(teams.parse_caption_crop(ocr, ros, jpeg=jpeg, live=live), ts)
        await t.end(70_000)
        (summary,) = store.list("default")
        conv = store.get("default", summary.id)
        assert conv is not None
        return [(s.speaker, s.text) for s in sorted(conv.segments, key=lambda s: s.start_ms)]

    assert asyncio.run(run()) == [
        ("Priya Raman", "Let's look at the numbers for the north region"),
        (None, "Sure, I have them right here."),
    ]


def test_ws_passes_the_crop_image_to_the_teams_parser(screen) -> None:
    """The pixels reach the parser end to end: without them Diego merges into Priya."""
    from api.main import app

    store, ocr, token, admin = screen
    base = int(time.time() * 1000)
    tiles, full_jpeg = (
        result(line("Weekly sync", 10, 4, 200, 20), line("Priya Raman", 300, 400, 380, 416),
               width=1280, height=720),
        unique_jpeg(1),
    )
    crop, crop_jpeg = panel(_PHOTO_AFTER_UNFINISHED)
    ocr.register(full_jpeg, tiles)
    ocr.register(crop_jpeg, crop)
    with TestClient(app) as client, client.websocket_connect(f"/ws/screen?token={token}") as ws:
        ws.receive_text()  # idle cadence
        ws.send_bytes(envelope(teams_header(base, "full"), full_jpeg))
        wait_handled(1)
        ws.send_bytes(envelope(teams_header(base + 1000, "caption"), crop_jpeg))
        wait_handled(2)
        (summary,) = store.list(admin.household)
        end = base + 1000 + int(settings.screen_meeting_end_seconds * 1000)
        assert client.portal.call(meetings.sweep, end) == [summary.id]
    conv = store.get(admin.household, summary.id)
    assert conv is not None
    assert [(s.speaker, s.text) for s in conv.segments] == [
        ("Priya Raman", "Let's look at the numbers for the north region"),
        (None, "Sure, I have them right here."),
    ]


def test_a_crop_sharing_no_recent_line_and_no_roster_header_is_dropped_mid_meeting() -> None:
    import asyncio

    from api.persistence import InMemoryConversationStore, ScreenMeta
    from api.screen.meetings import Cadence, MeetingTracker
    from api.screen.zoom import CaptionBlock

    async def run(platform: str) -> list[str]:
        store = InMemoryConversationStore()
        t = MeetingTracker(
            "default",
            "user-1",
            ScreenMeta("tesoro", "Tesoro", platform),
            store=store,
            account_exists=lambda _: True,
            cadence=Cadence(0.2, 1.0, 10.0),
            stable_ms=3000,
            end_ms=60_000,
        )
        await t.ensure_roster()
        entry = ["We shipped the north region numbers.", "the pallets arrived on Tuesday"]
        await t.on_caption([CaptionBlock("Priya Raman", True, entry)], 0)
        # Her header scrolled off: still the panel, it re-reads a known line and adds one.
        cont = [entry[1], "and two were damaged."]
        await t.on_caption([CaptionBlock(None, False, cont)], 1000)
        # A new unknown speaker under her last line: re-reads it, so it stays.
        diego = [CaptionBlock(None, False, [cont[1]]), CaptionBlock(None, True, ["Sure."])]
        await t.on_caption(diego, 2000)
        # Her lines scrolled off: only his avatar vouches for the crop, and it stays (QA).
        more = [CaptionBlock(None, True, ["I have them here."], avatar=True)]
        await t.on_caption(more, 2500)
        # A slide in the band: header-less, or headings read as unknown headers; nothing it
        # shows was said.
        slide = [
            [CaptionBlock(None, False, ["Quarterly Results", "Revenue grew this year."])],
            [CaptionBlock(None, True, ["Revenue grew this year."]), CaptionBlock(None, True, ["hire two"])],
        ]
        for ts, blocks in zip((3000, 4000), slide, strict=True):
            await t.on_caption(blocks, ts)
        await t.end(70_000)
        (summary,) = store.list("default")
        conv = store.get("default", summary.id)
        assert conv is not None
        return [s.text for s in sorted(conv.segments, key=lambda s: s.start_ms)]

    said = [
        "We shipped the north region numbers.",
        "the pallets arrived on Tuesday",
        "and two were damaged.",
        "Sure.",
        "I have them here.",
    ]
    assert asyncio.run(run("teams")) == said
    assert asyncio.run(run("zoom"))[-1] == "hire two"  # Zoom keeps storing what it is sent
