"""Unit tests for the hardware-independent logic. Run:  python -m unittest discover tests"""

import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hwstubs  # noqa: E402,F401  (installs fake hardware modules)

from dashcam.config import ConfigError, RecordingConfig, WebdavConfig, load_config  # noqa: E402
from dashcam.network import HomeDetector, host_port_from_url  # noqa: E402
from dashcam.power import PiSugar3, PowerMonitor  # noqa: E402
from dashcam.recorder import Recorder  # noqa: E402
from dashcam.storage import Storage  # noqa: E402
from dashcam.uploader import Uploader  # noqa: E402


class ConfigTests(unittest.TestCase):
    def write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as fh:
            fh.write(textwrap.dedent(text))
        self.addCleanup(os.remove, path)
        return path

    def test_defaults_and_overrides(self):
        cfg = load_config(self.write("""
            [recording]
            bitrate = 4000000
            segment_seconds = 60
            [webdav]
            hostname = "https://10.0.0.5/remote.php/dav/files/x/dashcam/"
            login = "x"
            password = "y"
            [power]
            pisugar_address = 0x57
        """))
        self.assertEqual(cfg.recording.bitrate, 4_000_000)
        self.assertEqual(cfg.recording.width, 1920)
        self.assertEqual(cfg.power.pisugar_address, 0x57)
        self.assertTrue(cfg.upload_configured)

    def test_example_file_parses(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = load_config(os.path.join(root, "config.example.toml"))
        self.assertEqual(cfg.recording.segment_seconds, 120)
        self.assertEqual(cfg.display.i2c_address, 0x3C)

    def test_validation(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[recording]\nsegment_seconds = 2\n"))
        with self.assertRaises(ConfigError):
            load_config(self.write("[webdav]\nhostname = 'nas.local/dav'\n"))
        with self.assertRaises(ConfigError):
            load_config(self.write("[recording]\ntimestamp_overlay = 'yes'\n"))
        with self.assertRaises(ConfigError):                       # bad port used to crash at startup
            load_config(self.write("[webdav]\nhostname = 'https://nas.local:abc/dav/'\n"))
        with self.assertRaises(ConfigError):                       # 0 would busy-loop the uploader
            load_config(self.write("[network]\nbackground_check_seconds = 0\n"))

    def test_missing_explicit_file(self):
        with self.assertRaises(ConfigError):
            load_config("/definitely/not/here.toml")


class PowerTests(unittest.TestCase):
    def setUp(self):
        hwstubs.FakeSMBus.instances.clear()
        self.ps = PiSugar3(1, 0x57)
        self.bus = hwstubs.FakeSMBus.instances[-1]

    def test_reads(self):
        self.assertTrue(self.ps.available)
        self.assertTrue(self.ps.external_power())
        self.assertEqual(self.ps.battery_percent(), 87)
        self.assertEqual(self.ps.battery_millivolts(), 0x101F)
        self.assertTrue(self.ps.auto_power_on_enabled())

    def test_single_glitch_does_not_end_trip(self):
        mon = PowerMonitor(self.ps, loss_confirm_seconds=3)
        mon.poll(1.0)
        self.bus.fail = True                      # transient: every read errors
        s = mon.poll(1.0)
        self.assertIsNone(s.external)
        self.assertFalse(mon.power_lost)
        self.bus.fail = False
        mon.poll(1.0)
        self.assertFalse(mon.power_lost)
        self.assertEqual(mon.loss_streak, 0.0)

    def test_debounced_loss_and_return(self):
        mon = PowerMonitor(self.ps, loss_confirm_seconds=3)
        self.bus.regs[0x02] = 0x10                # external power gone
        mon.poll(1.0); mon.poll(1.0)
        self.assertFalse(mon.power_lost)          # only 2 s so far
        mon.poll(1.0)
        self.assertTrue(mon.power_lost)           # 3 s confirmed
        self.bus.regs[0x02] = 0x90                # power back
        mon.poll(1.0)
        self.assertFalse(mon.power_lost)
        self.assertEqual(mon.loss_streak, 0.0)

    def test_unknown_readings_do_not_count_as_loss(self):
        mon = PowerMonitor(self.ps, loss_confirm_seconds=2)
        self.bus.regs[0x02] = 0x10
        mon.poll(1.0)
        self.bus.fail = True
        for _ in range(10):
            mon.poll(1.0)
        self.assertFalse(mon.power_lost)          # 10 unknowns never confirm a loss
        self.bus.fail = False
        mon.poll(1.0)
        self.assertTrue(mon.power_lost)           # second confirmed reading does

    def test_silent_pisugar_at_boot_is_retried_every_poll(self):
        hwstubs.FakeSMBus.instances.clear()
        orig = hwstubs.FakeSMBus.read_byte_data
        hwstubs.FakeSMBus.read_byte_data = lambda self, a, r: (_ for _ in ()).throw(OSError("no device"))
        try:
            ps = PiSugar3(1, 0x57)
            self.assertFalse(ps.available)
            self.assertIsNone(ps.sample().external)
            mon = PowerMonitor(ps, loss_confirm_seconds=2)
            mon.poll(1.0)
            self.assertFalse(mon.power_lost)
        finally:
            hwstubs.FakeSMBus.read_byte_data = orig
        # The board starts answering: detection comes alive without a restart.
        self.assertTrue(mon.poll(1.0).external)
        self.assertTrue(ps.available)
        hwstubs.FakeSMBus.instances[-1].regs[0x02] = 0x10
        mon.poll(1.0); mon.poll(1.0)
        self.assertTrue(mon.power_lost)

    def test_missing_i2c_bus_disables_detection(self):
        class NoBus:
            def __init__(self, bus):
                raise FileNotFoundError("/dev/i2c-1")
        saved = hwstubs.smbus2.SMBus
        hwstubs.smbus2.SMBus = NoBus
        try:
            ps = PiSugar3(1, 0x57)
        finally:
            hwstubs.smbus2.SMBus = saved
        self.assertIsNone(ps.bus)
        self.assertFalse(ps.available)
        self.assertIsNone(ps.sample().external)
        self.assertIsNone(PowerMonitor(ps, 2).battery_percent())

    def test_battery_percent_is_read_fresh(self):
        mon = PowerMonitor(self.ps, loss_confirm_seconds=3)
        mon.poll(1.0)
        self.assertEqual(mon.battery_percent(), 87)
        self.bus.regs[0x2A] = 20            # drained while we were busy uploading, no poll since
        self.assertEqual(mon.battery_percent(), 20)
        self.bus.fail = True                # glitch: fall back to the last good sample
        self.assertEqual(mon.battery_percent(), 87)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = Storage(self.tmp.name, min_free_gb=0)

    def touch(self, path, size=10, mtime=None):
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_layout_and_naming(self):
        for d in (self.st.current_dir, self.st.closed_dir, self.st.failed_dir):
            self.assertTrue(os.path.isdir(d))
        start = datetime(2026, 8, 22, 8, 15, 0)
        p1 = self.st.new_segment_path(start)
        self.assertTrue(p1.endswith("20260822-081500.mp4"))
        self.touch(p1)
        p2 = self.st.new_segment_path(start)          # collision -> counter
        self.assertTrue(p2.endswith("20260822-081500-2.mp4"))
        self.touch(p2)
        closed = self.st.close_segment(p2, end=datetime(2026, 8, 22, 8, 17, 0))
        self.assertEqual(os.path.basename(closed), "20260822-081500_TO_20260822-081700.mp4")
        self.assertFalse(os.path.exists(p2))
        self.assertEqual([os.path.basename(f) for f in self.st.closed_files()],
                         ["20260822-081500_TO_20260822-081700.mp4"])

    def test_empty_segment_discarded(self):
        p = self.touch(self.st.new_segment_path(), size=0)
        self.assertIsNone(self.st.close_segment(p))
        self.assertEqual(self.st.closed_files(), [])

    def test_recover_stale(self):
        p = self.touch(os.path.join(self.st.current_dir, "20260101-120000.mp4"), size=500)
        rec = self.st.recover_stale_segments()
        self.assertEqual(len(rec), 1)
        self.assertIn("_partial", rec[0])
        self.assertTrue(rec[0].startswith(self.st.closed_dir))
        self.assertFalse(os.path.exists(p))

    def test_prune_oldest_first_and_protect(self):
        self.st.min_free_bytes = 10 ** 30         # impossible floor -> prune everything allowed
        old = self.touch(os.path.join(self.st.closed_dir, "a.mp4"), mtime=1000)
        mid = self.touch(os.path.join(self.st.closed_dir, "b.mp4"), mtime=2000)
        new = self.touch(os.path.join(self.st.closed_dir, "c.mp4"), mtime=3000)
        cur = self.touch(os.path.join(self.st.current_dir, "d.mp4"))
        deleted = self.st.prune(protect=mid)
        self.assertEqual(sorted(os.path.basename(d) for d in deleted), ["a.mp4", "c.mp4"])
        self.assertTrue(os.path.exists(mid))
        self.assertTrue(os.path.exists(cur))      # current/ is never pruned
        self.assertFalse(os.path.exists(old) or os.path.exists(new))

    def test_mark_failed(self):
        p = self.touch(os.path.join(self.st.closed_dir, "x.mp4"))
        t = self.st.mark_failed(p)
        self.assertTrue(t.startswith(self.st.failed_dir))
        self.assertEqual(self.st.closed_files(), [])
        self.assertEqual(len(self.st.failed_files()), 1)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = Storage(self.tmp.name)
        hwstubs.FakePicamera2.instances.clear()
        hwstubs.FakePicamera2.fail_start = False

    def test_segments_rotate_and_close(self):
        cfg = RecordingConfig(segment_seconds=10, timestamp_overlay=False)
        rec = Recorder(cfg, self.st)
        self.assertTrue(rec.start())
        self.assertTrue(rec.recording)
        self.assertEqual(rec.mode, "split")
        cam = hwstubs.FakePicamera2.instances[-1]
        self.assertIn("frag_keyframe", cam.output.output.output_filename)
        self.assertIsNone(rec.tick())               # not due yet
        rec._segment_started_mono -= 11             # pretend 11 s passed
        closed = rec.tick()
        self.assertIsNotNone(closed)
        self.assertTrue(closed.startswith(self.st.closed_dir))
        self.assertIn("_TO_", closed)
        self.assertEqual(rec.segments_closed, 1)
        self.assertTrue(rec.recording)
        final = rec.stop()
        self.assertIsNotNone(final)
        self.assertEqual(len(self.st.closed_files()), 2)
        self.assertEqual(os.listdir(self.st.current_dir), [])
        self.assertTrue(cam.closed)
        self.assertFalse(rec.recording)

    def test_camera_failure_is_not_fatal(self):
        hwstubs.FakePicamera2.fail_start = True
        rec = Recorder(RecordingConfig(timestamp_overlay=False), self.st)
        self.assertFalse(rec.start())
        self.assertFalse(rec.recording)
        self.assertIn("camera busy", rec.last_error or "")
        self.assertIsNone(rec.tick())
        self.assertIsNone(rec.stop())
        hwstubs.FakePicamera2.fail_start = False
        self.assertTrue(rec.start())                # retry works
        rec.stop()

    def test_restart_mode_without_splittable(self):
        outputs = sys.modules["picamera2.outputs"]
        saved = outputs.SplittableOutput
        del outputs.SplittableOutput
        try:
            rec = Recorder(RecordingConfig(segment_seconds=10, timestamp_overlay=False), self.st)
            self.assertTrue(rec.start())
            self.assertEqual(rec.mode, "restart")
            rec._segment_started_mono -= 11
            self.assertIsNotNone(rec.tick())
            rec.stop()
            self.assertEqual(len(self.st.closed_files()), 2)
        finally:
            outputs.SplittableOutput = saved


    def test_stalled_split_does_not_block_and_recovers(self):
        rec = Recorder(RecordingConfig(segment_seconds=10, timestamp_overlay=False), self.st)
        self.assertTrue(rec.start())
        Recorder.SPLIT_TIMEOUT = 0.2
        self.addCleanup(setattr, Recorder, "SPLIT_TIMEOUT", 15.0)
        orig_split = rec.splitter.split_output
        rec.splitter.split_output = lambda *a, **k: time.sleep(5)    # encoder "stalled"
        rec._segment_started_mono -= 11
        t0 = time.monotonic()
        self.assertIsNone(rec.tick())
        self.assertLess(time.monotonic() - t0, 3)         # main loop not held hostage
        self.assertFalse(rec.recording)                   # session abandoned...
        self.assertIn("stalled", rec.last_error or "")
        closed = self.st.closed_files()
        self.assertEqual(len(closed), 1)                   # ...footage kept as partial
        self.assertIn("_partial", closed[0])
        self.assertTrue(rec.start())                       # ...and a fresh session works
        self.assertTrue(rec.recording)
        rec.stop()

    def test_broken_output_triggers_immediate_rotation(self):
        rec = Recorder(RecordingConfig(segment_seconds=600, timestamp_overlay=False), self.st)
        self.assertTrue(rec.start())
        self.assertIsNone(rec.tick())
        rec._on_output_error(RuntimeError("ffmpeg exited"))
        closed = rec.tick()                                # rotates without waiting 600 s
        self.assertIsNotNone(closed)
        self.assertTrue(rec.recording)
        self.assertFalse(rec._output_broken)
        rec.stop()

    def test_stop_timeout_marks_partial(self):
        rec = Recorder(RecordingConfig(timestamp_overlay=False), self.st)
        self.assertTrue(rec.start())
        Recorder.STOP_TIMEOUT = 0.2
        Recorder.CLOSE_TIMEOUT = 0.2
        self.addCleanup(setattr, Recorder, "STOP_TIMEOUT", 20.0)
        self.addCleanup(setattr, Recorder, "CLOSE_TIMEOUT", 10.0)
        cam = hwstubs.FakePicamera2.instances[-1]
        cam.stop_recording = lambda: time.sleep(5)
        t0 = time.monotonic()
        closed = rec.stop()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIsNotNone(closed)
        self.assertIn("_partial", closed)
        self.assertIsNone(rec.picam2)


class NetworkTests(unittest.TestCase):
    def test_host_port(self):
        self.assertEqual(host_port_from_url("https://10.0.0.5/remote.php/dav/"), ("10.0.0.5", 443))
        self.assertEqual(host_port_from_url("http://nas.local:8080/x"), ("nas.local", 8080))

    def test_unconfigured(self):
        h = HomeDetector("")
        self.assertFalse(h.configured)
        self.assertFalse(h.nas_reachable())


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = Storage(self.tmp.name)
        self.cfg = WebdavConfig(hostname="https://nas/dav/", login="u", password="p")
        self.home = HomeDetector(self.cfg.hostname)
        self.home.nas_reachable = lambda: True     # type: ignore
        hwstubs.FakeDavClient.instances.clear()
        Uploader.SETTLE_SECONDS = 0.0              # freshly written test files are "settled"

    def seg(self, name, size=100):
        p = os.path.join(self.st.closed_dir, name)
        with open(p, "wb") as fh:
            fh.write(b"v" * size)
        return p

    def test_upload_verify_delete_and_continue_past_failures(self):
        a = self.seg("20260101-000000_TO_20260101-000200.mp4")
        b = self.seg("20260101-000200_TO_20260101-000400.mp4")
        c = self.seg("20260101-000400_TO_20260101-000600.mp4")
        up = Uploader(self.cfg, self.st, self.home, max_failed=2)
        client = up.client()
        client.fail_names.add(os.path.basename(a))          # head-of-queue file always fails
        client.truncate_names.add(os.path.basename(b))      # server reports short size
        rep = up.upload_pending()
        self.assertEqual(rep.uploaded, 1)                   # c made it
        self.assertEqual(rep.failed, 2)
        self.assertFalse(os.path.exists(c))
        self.assertTrue(os.path.exists(a) and os.path.exists(b))   # kept for retry
        rep = up.upload_pending()                            # second round -> both hit max_failed
        self.assertEqual(len(self.st.closed_files()), 0)
        self.assertEqual(len(self.st.failed_files()), 2)
        self.assertEqual(client.verify, True)

    def test_abort_on_battery_and_deadline(self):
        for i in range(3):
            self.seg(f"2026010{i}-000000_TO_2026010{i}-000200.mp4")
        up = Uploader(self.cfg, self.st, self.home)
        calls = {"n": 0}

        def abort():
            calls["n"] += 1
            return "battery 10% below floor 25%" if calls["n"] >= 2 else None

        rep = up.upload_pending(should_abort=abort)
        self.assertEqual(rep.uploaded, 1)
        self.assertIn("battery", rep.aborted)
        self.assertEqual(rep.remaining, 2)
        rep = up.upload_pending(deadline=time.monotonic() - 1)
        self.assertEqual(rep.uploaded, 0)
        self.assertIn("time budget", rep.aborted)

    def test_nas_unreachable(self):
        self.seg("x.mp4")
        self.home.nas_reachable = lambda: False     # type: ignore
        rep = Uploader(self.cfg, self.st, self.home).upload_pending()
        self.assertTrue(rep.nas_unreachable)
        self.assertEqual(len(self.st.closed_files()), 1)

    def test_tls_options(self):
        cfg = WebdavConfig(hostname="https://nas/", login="u", password="p", verify_tls=False)
        c = Uploader(cfg, self.st, self.home).client()
        self.assertIs(c.verify, False)
        cfg = WebdavConfig(hostname="https://nas/", login="u", password="p", ca_cert="/etc/dashcam/nas.pem")
        c = Uploader(cfg, self.st, self.home).client()
        self.assertEqual(c.verify, "/etc/dashcam/nas.pem")

    def test_file_drop_share_trusts_put(self):
        self.seg("a.mp4")
        cfg = WebdavConfig(hostname="https://nas/public.php/webdav/", login="TOKEN", password="")
        up = Uploader(cfg, self.st, self.home)
        rep = up.upload_pending()
        self.assertTrue(up.client().file_drop)
        self.assertEqual(rep.uploaded, 1)                    # no PROPFIND possible -> PUT 201 is the proof
        self.assertEqual(self.st.closed_files(), [])

    def test_abort_mid_transfer_keeps_file_and_is_not_a_failure(self):
        a = self.seg("a.mp4")
        self.seg("b.mp4")
        up = Uploader(self.cfg, self.st, self.home, max_failed=1)
        client = up.client()
        client.abort_during.add("a.mp4")
        calls = {"n": 0}

        def abort():                        # fine between files, battery floor hit during the transfer
            calls["n"] += 1
            return None if calls["n"] < 2 else "battery 10% below floor 25%"

        rep = up.upload_pending(should_abort=abort)
        self.assertEqual((rep.uploaded, rep.failed), (0, 0))
        self.assertIn("battery", rep.aborted)
        self.assertEqual(rep.remaining, 2)
        self.assertTrue(os.path.exists(a))
        self.assertEqual(self.st.failed_files(), [])      # max_failed=1 would have parked it
        self.assertEqual(up.attempts, {})

    def test_stop_event_interrupts_a_transfer(self):
        self.seg("a.mp4")
        up = Uploader(self.cfg, self.st, self.home)
        client = up.client()
        ev = threading.Event()
        client.abort_during.add("a.mp4")
        client.mid_transfer = ev.set        # trip ends while the background upload is running
        rep = up.upload_pending(stop_event=ev)
        self.assertEqual(rep.uploaded, 0)
        self.assertEqual(rep.aborted, "stop requested")
        self.assertEqual(len(self.st.closed_files()), 1)

    def test_lost_response_is_rescued_by_the_size_check(self):
        a = self.seg("a.mp4", size=100)
        up = Uploader(self.cfg, self.st, self.home, max_failed=1)
        client = up.client()
        client.fail_names.add("a.mp4")      # PUT raises...
        client.remote["a.mp4"] = 100        # ...but the server has the complete file
        rep = up.upload_pending()
        self.assertEqual((rep.uploaded, rep.failed), (1, 0))
        self.assertFalse(os.path.exists(a))
        client.remote["a.mp4"] = 99         # short on the server: a real failure
        b = self.seg("a.mp4", size=100)
        rep = up.upload_pending()
        self.assertEqual((rep.uploaded, rep.failed), (0, 1))
        self.assertFalse(os.path.exists(b))               # parked in failed/ (max_failed=1)

    def test_background_thread_uploads(self):
        self.seg("a.mp4")
        up = Uploader(self.cfg, self.st, self.home)
        up.start_background(interval=0.05)
        for _ in range(100):
            if not self.st.closed_files():
                break
            time.sleep(0.05)
        up.stop_background(timeout=2)
        self.assertEqual(self.st.closed_files(), [])


if __name__ == "__main__":
    unittest.main()
