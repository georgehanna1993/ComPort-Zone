"""Direct unit tests for ``ComPort_Zone.cli.command_file_runner``.

The runner is exercised end-to-end via ``test_cli_run.py``, but a few
internal semantics deserve targeted assertions:

* The EXPECT buffer is trimmed past the match so a subsequent EXPECT
  doesn't re-match the same bytes.
* ``--continue-on-expect-fail`` accumulates failures without aborting.
* Each ``FAILURE_*`` constant maps to a specific failure path.
"""

from __future__ import annotations

import time
import unittest

from ComPort_Zone.cli.command_file_runner import (
    FAILURE_EXPECT,
    FAILURE_PARAM,
    FAILURE_PARSE,
    FAILURE_SEND,
    RunOutcome,
    run_command_file,
)
from ComPort_Zone.core.batch import parse_batch_template
from tests.fakes.fake_serial_transport import FakeSerialTransport


def _steps(text: str):
    return parse_batch_template(text)


def _no_events_sink(kind: str, fields: dict) -> None:
    """Drop every event — most tests only assert on the RunOutcome."""


class RunOutcomeShapeTests(unittest.TestCase):
    def test_empty_steps_succeed_with_zero_run_count(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(fake, [], {}, on_event=_no_events_sink)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.steps_run, 0)

    def test_send_step_sets_steps_run_to_one(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake, _steps("SEND ping\n"), {}, on_event=_no_events_sink
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.steps_run, 1)
        self.assertEqual(fake.sent_text, [("ping", None)])

    def test_hex_step_sends_bytes(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake, _steps("HEX 55 AA\n"), {}, on_event=_no_events_sink
        )
        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_bytes, [b"\x55\xaa"])


class ExpectBufferTrimTests(unittest.TestCase):
    def test_second_expect_does_not_match_first_response_again(self) -> None:
        """Each EXPECT must consume only its own bytes from the buffer.

        Without buffer trimming, the first ``EXPECT OK`` would leave "OK\\r\\n"
        in the buffer and the second ``EXPECT OK`` would match the same bytes
        immediately, masking a missing response from the device.
        """
        fake = FakeSerialTransport()
        fake.queue_response(b"OK\r\n")  # answer to step 1
        # No second response queued — step 2 must time out.
        outcome = run_command_file(
            fake,
            _steps(
                """\
SEND first
EXPECT OK
SEND second
EXPECT OK
"""
            ),
            {},
            on_event=_no_events_sink,
            expect_timeout_ms=50,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_EXPECT)
        # First EXPECT matched; second did not. expect_failures counts only
        # the unmatched ones, regardless of whether the run stopped.
        self.assertEqual(outcome.expect_failures, 1)


class ContinueOnExpectFailTests(unittest.TestCase):
    def test_continue_keeps_running_and_counts_failures(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake,
            _steps(
                """\
SEND a
EXPECT NEVER
SEND b
EXPECT NEVER
SEND c
"""
            ),
            {},
            on_event=_no_events_sink,
            stop_on_expect_fail=False,
            expect_timeout_ms=30,
        )
        # No failure should abort the run — all three SENDs happen.
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.expect_failures, 2)
        self.assertEqual([text for text, _ in fake.sent_text], ["a", "b", "c"])


class SettingsDirectiveTests(unittest.TestCase):
    def test_send_mode_hex_sends_bare_lines_as_bytes(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake, _steps("@@send-mode hex\n55 AA\n"), {}, on_event=_no_events_sink
        )
        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_bytes, [b"\x55\xaa"])

    def test_on_error_continue_overrides_default_stop(self) -> None:
        # Default is stop; the @@ directive flips it mid-run so SEND b still runs.
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake,
            _steps("SEND a\n@@on-error continue\nEXPECT NEVER\nSEND b\n"),
            {},
            on_event=_no_events_sink,
            expect_timeout_ms=20,
        )
        self.assertTrue(outcome.success)
        self.assertEqual([text for text, _ in fake.sent_text], ["a", "b"])

    def test_expect_timeout_setting_lowers_timeout(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake,
            _steps("@@expect-timeout 20\nSEND x\nEXPECT NEVER\n"),
            {},
            on_event=_no_events_sink,
            expect_timeout_ms=1000,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_EXPECT)
        self.assertIn("after 20 ms", outcome.failure_message)


class ParameterTests(unittest.TestCase):
    def test_param_value_substituted_into_send(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake,
            _steps("SEND v={{VOLT}}\n"),
            {"VOLT": "3.3"},
            on_event=_no_events_sink,
        )
        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_text, [("v=3.3", None)])

    def test_missing_param_returns_param_failure(self) -> None:
        fake = FakeSerialTransport()
        outcome = run_command_file(
            fake,
            _steps("SEND v={{VOLT}}\n"),
            {},  # no value for VOLT, no default in template
            on_event=_no_events_sink,
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_PARAM)


class ParseErrorTests(unittest.TestCase):
    def test_malformed_hex_after_substitution_returns_parse_failure(self) -> None:
        fake = FakeSerialTransport()
        # Odd-nibble HEX triggers BatchParseError.
        outcome = run_command_file(
            fake, _steps("HEX 5\n"), {}, on_event=_no_events_sink
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_PARSE)


class SendFailureTests(unittest.TestCase):
    def test_send_exception_returns_send_failure(self) -> None:
        fake = FakeSerialTransport()

        def explode(text: str, line_ending_override=None) -> None:
            raise RuntimeError("simulated write error")

        fake.send_text = explode  # type: ignore[assignment]
        outcome = run_command_file(
            fake, _steps("SEND ping\n"), {}, on_event=_no_events_sink
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_SEND)


class EventSinkTests(unittest.TestCase):
    def test_event_sink_receives_tx_and_rx_and_expect(self) -> None:
        fake = FakeSerialTransport()
        fake.queue_response(b"OK\r\n")
        recorded: list[tuple[str, dict]] = []

        def sink(kind: str, fields: dict) -> None:
            recorded.append((kind, dict(fields)))

        outcome = run_command_file(
            fake,
            _steps("SEND ping\nEXPECT OK\n"),
            {},
            on_event=sink,
            expect_timeout_ms=200,
        )
        self.assertTrue(outcome.success)
        kinds = [kind for kind, _ in recorded]
        self.assertIn("tx", kinds)
        self.assertIn("rx", kinds)
        self.assertIn("expect", kinds)
        # The expect record should report matched=True.
        expect_payloads = [fields for kind, fields in recorded if kind == "expect"]
        self.assertTrue(expect_payloads[0]["matched"])


class WaitStepTests(unittest.TestCase):
    def test_wait_delays_at_least_specified_ms(self) -> None:
        fake = FakeSerialTransport()
        start = time.monotonic()
        outcome = run_command_file(
            fake, _steps("WAIT 80\n"), {}, on_event=_no_events_sink
        )
        elapsed = time.monotonic() - start
        self.assertTrue(outcome.success)
        # Allow a small scheduling slop on the lower bound.
        self.assertGreaterEqual(elapsed, 0.060)


class LoopStepTests(unittest.TestCase):
    def test_finite_loop_repeats_enclosed_steps(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps("LOOP 3\nSEND ping\nENDLOOP\n"),
            {},
            on_event=_no_events_sink,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_text, [("ping", None)] * 3)

    def test_nested_loops_repeat_independently(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps("LOOP 2\nLOOP 2\nSEND ping\nENDLOOP\nENDLOOP\n"),
            {},
            on_event=_no_events_sink,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_text, [("ping", None)] * 4)

    def test_unmatched_loop_returns_parse_failure(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps("LOOP 2\nSEND ping\n"),
            {},
            on_event=_no_events_sink,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_PARSE)
        self.assertEqual(outcome.failure_line, 1)

    def test_break_exits_only_innermost_loop(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps(
                "LOOP 2\nSEND outer\n"
                "LOOP FOREVER\nSEND inner\nBREAK\nSEND skipped\nENDLOOP\n"
                "SEND after-inner\nENDLOOP\n"
            ),
            {},
            on_event=_no_events_sink,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(
            fake.sent_text,
            [("outer", None), ("inner", None), ("after-inner", None)] * 2,
        )

    def test_break_outside_loop_returns_parse_failure(self) -> None:
        outcome = run_command_file(
            FakeSerialTransport(),
            _steps("BREAK\n"),
            {},
            on_event=_no_events_sink,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_PARSE)


class SetAndLogStepTests(unittest.TestCase):
    def test_set_drives_if_send_and_log_and_can_be_overwritten(self) -> None:
        fake = FakeSerialTransport()
        events: list[tuple[str, dict]] = []

        outcome = run_command_file(
            fake,
            _steps(
                'SET MODE "SAFE MODE"\n'
                "LOG Selected $MODE\n"
                'IF $MODE == "SAFE MODE"\nSEND safe\nENDIF\n'
                "SET MODE FAST\nSEND mode=$MODE\n"
            ),
            {},
            on_event=lambda kind, fields: events.append((kind, dict(fields))),
        )

        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_text, [("safe", None), ("mode=FAST", None)])
        script_messages = [fields.get("message") for kind, fields in events if kind == "script"]
        status_messages = [fields.get("message") for kind, fields in events if kind == "status"]
        self.assertIn("Selected SAFE MODE", script_messages)
        self.assertFalse(any(str(message).startswith("SET ") for message in status_messages))


class ConditionStepTests(unittest.TestCase):
    def test_condition_selects_true_and_false_branches(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps(
                "IF {{VOLTAGE}} > 12\nSEND high\nELSE\nSEND low\nENDIF\n"
                "IF {{MODE}} == FAST\nSEND fast\nELSE\nSEND slow\nENDIF\n"
            ),
            {"VOLTAGE": "13.5", "MODE": "SLOW"},
            on_event=_no_events_sink,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_text, [("high", None), ("slow", None)])

    def test_invalid_numeric_condition_returns_parse_failure(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps("IF FAST > SLOW\nSEND wrong\nENDIF\n"),
            {},
            on_event=_no_events_sink,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_PARSE)
        self.assertEqual(outcome.failure_line, 1)


class CaptureStepTests(unittest.TestCase):
    def test_capture_extracts_response_and_resolves_runtime_variable(self) -> None:
        fake = FakeSerialTransport()
        fake.queue_response(b"VOLT: 13.7 V\r\n")

        outcome = run_command_file(
            fake,
            _steps(
                "SEND MEASURE?\n"
                r"CAPTURE VOLTAGE REGEX VOLT:\s*([0-9.]+)\s+V" "\n"
                "IF $VOLTAGE > 12\nSEND OUTPUT:OFF\nELSE\nSEND OUTPUT:ON\nENDIF\n"
                "SEND RECORDED=$VOLTAGE\n"
            ),
            {},
            on_event=_no_events_sink,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(
            fake.sent_text,
            [("MEASURE?", None), ("OUTPUT:OFF", None), ("RECORDED=13.7", None)],
        )

    def test_capture_optional_unmatched_group_becomes_empty_text(self) -> None:
        fake = FakeSerialTransport()
        fake.queue_response(b"STATE:\r\n")

        outcome = run_command_file(
            fake,
            _steps(
                "SEND STATE?\n"
                r"CAPTURE STATE REGEX STATE:(ON)?\r" "\n"
                "SEND VALUE=[$STATE]\n"
            ),
            {},
            on_event=_no_events_sink,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(fake.sent_text, [("STATE?", None), ("VALUE=[]", None)])

    def test_capture_timeout_uses_expect_failure(self) -> None:
        fake = FakeSerialTransport()

        outcome = run_command_file(
            fake,
            _steps("CAPTURE VALUE REGEX ([0-9.]+)\n"),
            {},
            on_event=_no_events_sink,
            expect_timeout_ms=5,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.failure_kind, FAILURE_EXPECT)


if __name__ == "__main__":
    unittest.main()
