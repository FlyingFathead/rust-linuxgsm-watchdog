import json
import sys
import types
import unittest
from unittest import mock

import rust_watchdog as watchdog


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.received = 0
        self.sent = []
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        self.received += 1
        if not self.frames:
            raise TimeoutError("timed out")
        return self.frames.pop(0)

    def close(self):
        self.closed = True


def generic_frame(message):
    return json.dumps(
        {
            "Identifier": 0,
            "Message": message,
            "Type": "Generic",
        }
    )


class RconAsyncCompletionTests(unittest.TestCase):
    def invoke(self, fake_socket, matcher, *, monotonic=None):
        websocket_module = types.SimpleNamespace(
            create_connection=mock.Mock(return_value=fake_socket)
        )
        patches = [
            mock.patch.object(
                watchdog,
                "get_rcon_endpoint",
                return_value=("127.0.0.1", 28016, "secret", "config"),
            ),
            mock.patch.object(
                watchdog,
                "websocket_dep_status",
                return_value=(True, ""),
            ),
            mock.patch.dict(
                sys.modules,
                {"websocket": websocket_module},
            ),
        ]
        if monotonic is not None:
            patches.append(
                mock.patch.object(
                    watchdog.time,
                    "monotonic",
                    side_effect=monotonic,
                )
            )
        with patches[0], patches[1], patches[2]:
            if monotonic is None:
                return watchdog.rcon_send(
                    {},
                    "oxide.reload HeliRide",
                    response_matcher=matcher,
                    timeout_s=10,
                )
            with patches[3]:
                return watchdog.rcon_send(
                    {},
                    "oxide.reload HeliRide",
                    response_matcher=matcher,
                    timeout_s=1,
                )

    def test_waits_past_acknowledgement_for_terminal_compile_event(self):
        fake_socket = FakeWebSocket(
            [
                generic_frame("oxide.reload HeliRide accepted"),
                generic_frame("UberTool was compiled successfully in 50ms"),
                generic_frame(
                    "HeliRide was compiled successfully in 625ms"
                ),
            ]
        )
        matcher = lambda text: (
            "HeliRide was compiled successfully" in text
        )

        ok, response = self.invoke(fake_socket, matcher)

        self.assertTrue(ok)
        self.assertEqual(fake_socket.received, 3)
        self.assertTrue(fake_socket.closed)
        self.assertIn("oxide.reload HeliRide accepted", response)
        self.assertIn("HeliRide was compiled successfully", response)

    def test_delayed_compile_failure_is_returned_as_terminal_output(self):
        fake_socket = FakeWebSocket(
            [
                generic_frame("oxide.reload HeliRide accepted"),
                generic_frame(
                    "UberTool - Failed to compile: unrelated failure"
                ),
                generic_frame(
                    "Error while compiling HeliRide: missing ClientRPCPlayer"
                ),
            ]
        )
        matcher = lambda text: (
            "Error while compiling HeliRide" in text
        )

        ok, response = self.invoke(fake_socket, matcher)

        self.assertTrue(ok)
        self.assertEqual(fake_socket.received, 3)
        self.assertTrue(fake_socket.closed)
        self.assertIn("Error while compiling HeliRide", response)

    def test_timeout_is_not_reported_as_command_success(self):
        fake_socket = FakeWebSocket(
            [generic_frame("oxide.reload HeliRide accepted")]
        )

        ok, response = self.invoke(
            fake_socket,
            lambda _text: False,
            monotonic=[1.0, 1.0, 1.0, 1.0, 3.0],
        )

        self.assertFalse(ok)
        self.assertTrue(fake_socket.closed)
        self.assertIn("waiting for terminal response", response)
        self.assertIn("oxide.reload HeliRide accepted", response)


if __name__ == "__main__":
    unittest.main()
