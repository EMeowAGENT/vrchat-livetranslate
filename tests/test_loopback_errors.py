"""Loopback capture failures are visible without exposing driver error details."""
import contextlib
import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.platform.win import PyaudioLoopbackSource


class LoopbackFailureTests(unittest.TestCase):
    def test_poll_and_read_failures_are_visible_without_private_exception_details(self):
        for stage in ('poll', 'read'):
            with self.subTest(stage=stage):
                source = object.__new__(PyaudioLoopbackSource)
                source._chunk_max = 4800
                source._stream = Mock()
                source._stream.get_read_available.return_value = 4800
                failure = OSError(-9999, 'private-driver-detail')
                if stage == 'poll':
                    source._stream.get_read_available.side_effect = failure
                else:
                    source._stream.read.side_effect = failure
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    source._pump(threading.Event())
                self.assertIn('loopback', output.getvalue())
                self.assertIn('OSError', output.getvalue())
                self.assertIn('-9999', output.getvalue())
                self.assertNotIn('private-driver-detail', output.getvalue())

    def test_requested_stop_is_quiet(self):
        source = object.__new__(PyaudioLoopbackSource)
        stop = threading.Event()
        stop.set()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            source._pump(stop)
        self.assertFalse(output.getvalue())


if __name__ == '__main__':
    unittest.main()
