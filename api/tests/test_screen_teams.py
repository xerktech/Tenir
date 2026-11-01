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
