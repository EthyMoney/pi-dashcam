"""End-to-end run of the trip state machine against fake hardware."""

import io
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hwstubs  # noqa: E402,F401

from dashcam import app as app_mod  # noqa: E402
from dashcam.check import run_checks  # noqa: E402
from dashcam.config import Config  # noqa: E402
from dashcam.network import HomeDetector  # noqa: E402
from dashcam.uploader import Uploader  # noqa: E402


def fast_config(tmp, with_webdav=True):
    cfg = Config()
    cfg.paths.data_dir = tmp
    cfg.recording.segment_seconds = 10
    cfg.recording.timestamp_overlay = False
    cfg.power.poll_seconds = 0.02
    cfg.power.loss_confirm_seconds = 0.06
    cfg.power.return_grace_seconds = 0.06
    cfg.network.home_check_seconds = 0.2
    cfg.network.upload_while_recording = False
    cfg.display.enabled = False
    if with_webdav:
        cfg.webdav.hostname = "https://nas.example/dav/"
        cfg.webdav.login = "u"
        cfg.webdav.password = "p"
    return cfg


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        hwstubs.FakeSMBus.instances.clear()
        hwstubs.FakePicamera2.instances.clear()
        hwstubs.FakePicamera2.fail_start = False
        hwstubs.FakeDavClient.instances.clear()
        Uploader.SETTLE_SECONDS = 0.0
        self._orig_reach = HomeDetector.nas_reachable
        HomeDetector.nas_reachable = lambda self: True   # type: ignore
        self.addCleanup(setattr, HomeDetector, "nas_reachable", self._orig_reach)

    def test_full_trip_power_loss_upload_shutdown(self):
        cfg = fast_config(self.tmp.name)
        dc = app_mod.Dashcam(cfg, no_shutdown=True)
        shutdowns = []
        dc._shutdown = lambda why: shutdowns.append(why)   # type: ignore

        # Cut external power shortly after the loop starts.
        def cut_power():
            deadline = time.time() + 5
            while not hwstubs.FakeSMBus.instances and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.15)
            for bus in hwstubs.FakeSMBus.instances:
                bus.regs[0x02] = 0x10

        threading.Thread(target=cut_power, daemon=True).start()
        code = dc.run()
        self.assertEqual(code, 0)
        self.assertTrue(dc.monitor.power_lost)
        self.assertEqual(shutdowns, ["trip complete"])
        # The one segment was closed, uploaded (verified) and removed locally.
        client = hwstubs.FakeDavClient.instances[-1]
        self.assertEqual(len(client.uploads), 1)
        self.assertEqual(dc.storage.closed_files(), [])
        self.assertEqual(os.listdir(dc.storage.current_dir), [])
        self.assertTrue(hwstubs.FakePicamera2.instances[-1].closed)

    def test_power_returns_during_grace_then_signal_exit(self):
        cfg = fast_config(self.tmp.name, with_webdav=False)
        cfg.power.return_grace_seconds = 0.5
        dc = app_mod.Dashcam(cfg, no_shutdown=True)
        shutdowns = []
        dc._shutdown = lambda why: shutdowns.append(why)   # type: ignore

        def script():
            while not hwstubs.FakeSMBus.instances:
                time.sleep(0.01)
            bus = hwstubs.FakeSMBus.instances[-1]
            time.sleep(0.1)
            bus.regs[0x02] = 0x10          # power lost...
            time.sleep(0.2)                # ...confirmed, now in grace
            bus.regs[0x02] = 0x90          # ...and back
            time.sleep(0.2)
            dc.stop_requested = True       # like SIGTERM

        threading.Thread(target=script, daemon=True).start()
        code = dc.run()
        self.assertEqual(code, 0)
        self.assertEqual(shutdowns, [])             # signal path never powers off
        self.assertFalse(dc.monitor.power_lost)
        self.assertEqual(len(dc.storage.closed_files()), 1)   # final segment kept locally

    def test_camera_missing_still_shuts_down_on_power_loss(self):
        hwstubs.FakePicamera2.fail_start = True
        cfg = fast_config(self.tmp.name, with_webdav=False)
        dc = app_mod.Dashcam(cfg, no_shutdown=True)
        shutdowns = []
        dc._shutdown = lambda why: shutdowns.append(why)   # type: ignore

        def cut_power():
            while not hwstubs.FakeSMBus.instances:
                time.sleep(0.01)
            time.sleep(0.1)
            for bus in hwstubs.FakeSMBus.instances:
                bus.regs[0x02] = 0x10

        threading.Thread(target=cut_power, daemon=True).start()
        self.assertEqual(dc.run(), 0)
        self.assertEqual(shutdowns, ["trip complete"])
        self.assertFalse(dc.recorder.recording)

    def test_check_runs(self):
        cfg = fast_config(self.tmp.name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_checks(cfg)
        out = buf.getvalue()
        self.assertIn("pisugar3", out)
        self.assertIn("camera", out)
        self.assertIn("webdav login", out)
        self.assertEqual(code, 0, out)

    def test_cli_parser(self):
        p = app_mod.build_parser()
        ns = p.parse_args(["--no-shutdown", "--config", "/x.toml", "-v"])
        self.assertTrue(ns.no_shutdown and ns.verbose)
        self.assertEqual(ns.config, "/x.toml")


if __name__ == "__main__":
    unittest.main()
