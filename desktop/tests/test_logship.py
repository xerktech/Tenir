"""The client's own log lines, buffered and batched for the api (screen.log, XERK-2875)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from tenir_capture import logship
from tenir_capture.logship import LogShipper


def logger_with(shipper: LogShipper, name: str = "tenir_capture.test_logship") -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.addHandler(shipper)
    return lg


def test_info_and_warnings_ship_debug_and_the_servers_own_errors_do_not() -> None:
    shipper = LogShipper()
    lg = logger_with(shipper)
    lg.debug("frame skipped: noisy")
    lg.info("Org A: capturing")
    lg.warning("Org A: paused (%s)", "no visible window matches")
    logger_with(shipper, logship.SERVER_LOGGER).warning("server: expected a binary frame")
    msg = json.loads(shipper.batch() or "")
    assert msg["type"] == "screen.log" and "dropped" not in msg
    assert [(x["level"], x["message"]) for x in msg["lines"]] == [
        ("INFO", "Org A: capturing"),
        ("WARNING", "Org A: paused (no visible window matches)"),
    ]
    assert all(isinstance(x["ts"], int) for x in msg["lines"])
    assert shipper.batch() is None


def test_window_titles_are_masked_in_any_case_and_lines_capped() -> None:
    shipper = LogShipper(redact=["Horizon 3", "Horizon 3 - Org C client", ""])
    lg = logger_with(shipper)
    lg.warning("capture failed on Horizon 3 - Org C client and HORIZON 3")
    shipper.mask("Zoom Meeting - Client Corp")  # a resolved title, added by the runner
    lg.warning("WGC refused 'zoom meeting - client corp'")
    lg.info("x" * 5000)
    lines = json.loads(shipper.batch() or "")["lines"]
    assert lines[0]["message"] == "capture failed on <window> and <window>"
    assert lines[1]["message"] == "WGC refused '<window>'"
    assert len(lines[2]["message"]) == logship.MAX_LINE


def test_a_title_inside_an_org_name_is_not_masked_so_lines_say_which_org() -> None:
    # Production: titles equal to org names turned "Org B: paused" into "<window>: paused".
    shipper = LogShipper(redact=["Org B", "org", "Horizon 3"], orgs=["Org B", "Org C Corp"])
    lg = logger_with(shipper)
    shipper.mask("ORG C")  # resolved, inside an org name in another case
    shipper.mask("Org C Corp - Zoom")
    lg.warning("Org B: paused; Org C Corp: paused on Org C Corp - Zoom; Horizon 3 lost")
    (line,) = json.loads(shipper.batch() or "")["lines"]
    assert line["message"] == "Org B: paused; Org C Corp: paused on <window>; <window> lost"


def test_a_deep_traceback_keeps_its_exception_line() -> None:
    shipper = LogShipper()
    lg = logger_with(shipper)

    try:
        raise ValueError("the cause")
    except ValueError:
        # A deep stack, made long by the message (Python folds repeated frames).
        lg.exception("Org A: frame skipped %s", "." * 3000)
    (line,) = json.loads(shipper.batch() or "")["lines"]
    msg = line["message"]
    assert len(msg) == logship.MAX_LINE
    assert msg.startswith("Org A: frame skipped") and msg.endswith("ValueError: the cause")


def test_resolved_titles_are_bounded_newest_kept_configured_always() -> None:
    shipper = LogShipper(redact=["Horizon 3"])
    lg = logger_with(shipper)
    for i in range(logship.MAX_RESOLVED + 5):
        shipper.mask(f"Teams call {i:03d}")
    lg.info("Horizon 3 / Teams call 000 / Teams call %03d", logship.MAX_RESOLVED + 4)
    (line,) = json.loads(shipper.batch() or "")["lines"]
    assert line["message"] == "<window> / Teams call 000 / <window>"


def test_a_lone_surrogate_is_replaced_so_the_api_takes_the_batch() -> None:
    shipper = LogShipper()
    logger_with(shipper).warning("bad \ud800 char")
    (line,) = json.loads(shipper.batch() or "")["lines"]
    line["message"].encode("utf-8")  # strict: would raise on a surrogate


def test_batches_fit_the_contract_the_api_validates() -> None:
    schema = json.loads(
        (Path(__file__).parents[2] / "contract" / "ws-messages.schema.json").read_text()
    )["$defs"]
    assert logship.BATCH <= schema["ScreenLog"]["properties"]["lines"]["maxItems"]
    message = schema["ScreenLogLine"]["properties"]["message"]
    assert logship.MAX_LINE <= message["maxLength"]
    levels = set(schema["ScreenLogLine"]["properties"]["level"]["enum"])
    assert {"INFO", "WARNING", "ERROR", "CRITICAL"} <= levels


def test_a_traceback_ships_with_its_line() -> None:
    shipper = LogShipper()
    lg = logger_with(shipper)
    try:
        raise ValueError("odd frame")
    except ValueError:
        lg.exception("Org A: frame skipped")
    (line,) = json.loads(shipper.batch() or "")["lines"]
    assert line["level"] == "ERROR" and "ValueError: odd frame" in line["message"]


def test_a_full_buffer_drops_its_oldest_lines_and_says_how_many() -> None:
    shipper = LogShipper()
    lg = logger_with(shipper)
    for i in range(logship.BUFFER + 7):
        lg.info("line %d", i)
    first = json.loads(shipper.batch() or "")
    assert first["dropped"] == 7 and len(first["lines"]) == logship.BATCH
    assert first["lines"][0]["message"] == "line 7"  # the newest are kept
    sizes = [len(json.loads(b)["lines"]) for b in iter(shipper.batch, None)]
    assert sum(sizes) == logship.BUFFER - logship.BATCH and max(sizes) <= logship.BATCH


def test_a_batch_that_failed_to_send_goes_back_ahead_of_newer_lines() -> None:
    shipper = LogShipper()
    lg = logger_with(shipper)
    lg.info("old 1")
    lg.info("old 2")
    payload = shipper.batch()
    assert payload is not None
    lg.info("new")
    shipper.requeue(payload)
    got = [x["message"] for x in json.loads(shipper.batch() or "")["lines"]]
    assert got == ["old 1", "old 2", "new"]
    # Requeued into a full buffer: what doesn't fit is counted, not kept.
    for i in range(logship.BUFFER):
        lg.info("fill %d", i)
    shipper.requeue(payload)
    assert json.loads(shipper.batch() or "")["dropped"] == 2


def test_a_line_that_fails_to_format_never_reaches_the_caller() -> None:
    shipper = LogShipper()
    lg = logger_with(shipper)
    logging.raiseExceptions, saved = False, logging.raiseExceptions
    try:
        lg.info("%d", "not a number")
    finally:
        logging.raiseExceptions = saved
    assert shipper.batch() is None
