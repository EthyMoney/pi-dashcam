"""Trip lifecycle.

    BOOT -> RECORDING -> (power lost, debounced) STOPPING (grace: power may return)
         -> HOME_CHECK (bounded wait for the NAS) -> UPLOADING (time budget + battery floor)
         -> SHUTDOWN

SIGTERM/SIGINT finalize the current segment and exit *without* powering off
(so `systemctl stop dashcam` on the bench is harmless). Only the power-loss
path - and an unhandled crash while already on battery - shuts the Pi down.
"""

import argparse
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from typing import Optional

from dashcam import __version__, clock
from dashcam.config import Config, ConfigError, load_config
from dashcam.display import Display
from dashcam.network import HomeDetector
from dashcam.power import PiSugar3, PowerMonitor
from dashcam.recorder import Recorder
from dashcam.storage import Storage
from dashcam.uploader import Uploader

log = logging.getLogger("dashcam")


def setup_logging(verbose: bool = False) -> None:
    # Under systemd the journal already stamps each line; keep it clean there.
    under_journal = "JOURNAL_STREAM" in os.environ
    fmt = "%(levelname)s %(name)s: %(message)s" if under_journal else \
          "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("picamera2").setLevel(logging.WARNING)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Dashcam:
    def __init__(self, cfg: Config, no_shutdown: bool = False):
        self.cfg = cfg
        self.no_shutdown = no_shutdown
        self.stop_requested = False
        self.storage: Optional[Storage] = None
        self.display: Optional[Display] = None
        self.pisugar: Optional[PiSugar3] = None
        self.monitor: Optional[PowerMonitor] = None
        self.recorder: Optional[Recorder] = None
        self.home: Optional[HomeDetector] = None
        self.uploader: Optional[Uploader] = None
        self.trip_started = time.monotonic()

    # ---- signals ------------------------------------------------------------

    def _on_signal(self, signum, _frame):
        name = signal.Signals(signum).name
        if self.stop_requested:
            log.warning("second %s - exiting immediately", name)
            raise SystemExit(130)
        log.info("%s received: finishing the current segment and exiting (no power-off)", name)
        self.stop_requested = True

    # ---- run ----------------------------------------------------------------

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        cfg = self.cfg
        log.info("pi-dashcam %s starting (config: %s)", __version__, cfg.source_path or "built-in defaults")

        clock.sync_system_from_rtc()

        self.storage = Storage(cfg.paths.data_dir, cfg.storage.min_free_gb)
        self.storage.recover_stale_segments()
        self.storage.prune()

        self.pisugar = PiSugar3(cfg.power.i2c_bus, cfg.power.pisugar_address)
        self.monitor = PowerMonitor(self.pisugar, cfg.power.loss_confirm_seconds)

        # Camera first: every second before recording starts is footage lost.
        self.recorder = Recorder(cfg.recording, self.storage)
        self.recorder.start()

        self.display = Display(cfg.display.enabled, cfg.display.i2c_address,
                               cfg.display.width, cfg.display.height)
        self.display.status("Dashcam", "starting...")

        self.pisugar.startup_report()
        self.home = HomeDetector(cfg.webdav.hostname, cfg.network.nas_check_timeout, cfg.network.home_ssid)
        self.uploader = Uploader(cfg.webdav, self.storage, self.home, self.display,
                                 cfg.storage.max_failed_uploads, self.monitor.battery_percent)
        if not cfg.upload_configured:
            log.warning("[webdav] not configured: videos stay on the Pi under %s", self.storage.closed_dir)
        elif cfg.network.upload_while_recording:
            self.uploader.start_background(cfg.network.background_check_seconds)

        pending = len(self.storage.closed_files())
        if pending:
            log.info("%d segment(s) waiting for upload (%.0f MB)", pending, self.storage.pending_bytes() / 1e6)
        log.info("free space: %.1f GB (floor %.1f GB)", self.storage.free_bytes() / 1024 ** 3,
                 cfg.storage.min_free_gb)

        exit_code = 0
        try:
            reason = self._recording_loop()
            if reason == "signal":
                closed = self.recorder.stop()
                if closed:
                    log.info("final segment saved: %s", os.path.basename(closed))
                self.uploader.stop_background(timeout=30)
                log.info("exited on request; %d segment(s) pending upload", len(self.storage.closed_files()))
                return 0
            self._trip_end()
        except Exception:
            log.exception("unhandled error")
            exit_code = 1
            if self.monitor.power_lost and not self.stop_requested:
                # We're on battery with nobody to shut us down otherwise; don't drain it flat.
                try:
                    self.recorder.stop()
                except Exception:
                    pass
                self._shutdown("after an unhandled error while on battery")
        finally:
            try:
                self.recorder.stop()
            except Exception:
                pass
            try:
                self.uploader.stop_background(timeout=10)
            except Exception:
                pass
            try:
                self.display.clear()
            except Exception:
                pass
        return exit_code

    # ---- RECORDING / STOPPING --------------------------------------------------

    def _recording_loop(self) -> str:
        """Returns 'power_lost' or 'signal'."""
        cfg = self.cfg
        poll = cfg.power.poll_seconds
        grace = cfg.power.return_grace_seconds
        state = "RECORDING"
        grace_deadline = 0.0
        last = time.monotonic()
        next_camera_retry = 0.0
        next_info_log = time.monotonic() + 300

        while not self.stop_requested:
            now = time.monotonic()
            elapsed, last = now - last, now
            sample = self.monitor.poll(elapsed)

            if self.monitor.power_lost:
                if state == "RECORDING":
                    state = "STOPPING"
                    grace_deadline = now + grace
                    log.info("ignition off? keeping the recording going for %.0fs in case power returns", grace)
                elif now >= grace_deadline:
                    return "power_lost"
            elif state == "STOPPING":
                log.info("power returned during the grace period - trip continues")
                state = "RECORDING"

            closed = self.recorder.tick()
            if closed:
                self.storage.prune(protect=self.uploader.current_file)

            if not self.recorder.recording and now >= next_camera_retry:
                next_camera_retry = now + 30
                if self.recorder.available or self.recorder.last_error:
                    log.warning("camera not recording (%s) - retrying", self.recorder.last_error)
                self.recorder.start()

            self._update_display(state, sample, grace_deadline - now if state == "STOPPING" else 0)

            if now >= next_info_log:
                next_info_log = now + 300
                log.info("trip %s: %d segments closed, %d pending upload, %.1f GB free, battery %s%%, %s",
                         fmt_duration(now - self.trip_started), self.recorder.segments_closed,
                         len(self.storage.closed_files()), self.storage.free_bytes() / 1024 ** 3,
                         sample.percent if sample.percent is not None else "?", sample.label)

            time.sleep(poll)
        return "signal"

    def _update_display(self, state: str, sample, grace_left: float) -> None:
        elapsed = fmt_duration(time.monotonic() - self.trip_started)
        if state == "STOPPING":
            title = "Power lost"
            detail = f"ending in {int(max(grace_left, 0))}s"
        elif self.recorder.recording:
            title = f"REC {elapsed}"
            detail = f"seg {self.recorder.segments_closed + 1}  {self.storage.free_bytes() / 1024 ** 3:.1f}GB free"
        else:
            title = "CAMERA ERROR"
            detail = (self.recorder.last_error or "")[:21]
        pending = len(self.storage.closed_files())
        if self.uploader.current_file:
            extra = f"uploading {pending} left"
        elif pending:
            extra = f"{pending} to upload"
        else:
            extra = ""
        self.display.status(title, detail, battery=sample.percent, power_label=sample.label, extra=extra)

    # ---- HOME_CHECK / UPLOADING / SHUTDOWN ------------------------------------

    def _trip_end(self) -> None:
        cfg = self.cfg
        log.info("trip over after %s", fmt_duration(time.monotonic() - self.trip_started))
        self.display.status("Trip ended", "saving video...", battery=self.monitor.battery_percent())
        closed = self.recorder.stop()
        if closed:
            log.info("final segment saved: %s", os.path.basename(closed))
        self.uploader.stop_background(timeout=120)

        pending = self.storage.closed_files()
        if not pending:
            log.info("nothing to upload")
        elif not cfg.upload_configured:
            log.info("%d segment(s) kept locally ([webdav] not configured)", len(pending))
        else:
            self._home_check_and_upload(len(pending))

        if self.stop_requested:
            log.info("stop requested during trip end; not powering off")
            return
        self._shutdown("trip complete")

    def _home_check_and_upload(self, pending_count: int) -> None:
        cfg = self.cfg
        deadline = time.monotonic() + cfg.network.home_check_seconds
        reachable = False
        attempt = 0
        while not self.stop_requested:
            attempt += 1
            if self.home.nas_reachable():
                reachable = True
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if attempt == 1:
                log.info("NAS %s:%d not reachable yet - waiting up to %.0fs for home Wi-Fi (%s)",
                         self.home.host, self.home.port, left, self.home.describe())
            self.display.status("Looking for home", f"wifi... {int(left)}s", extra=f"{pending_count} to upload",
                                battery=self.monitor.battery_percent())
            time.sleep(min(5.0, max(left, 0.1)))

        if not reachable:
            log.info("not home (NAS unreachable after %.0fs): %d segment(s) will upload next time",
                     cfg.network.home_check_seconds, pending_count)
            return

        log.info("home: NAS reachable (%s). uploading %d segment(s) on battery, budget %.0fs, floor %d%%",
                 self.home.describe(), pending_count, cfg.webdav.upload_time_budget_seconds,
                 cfg.power.upload_battery_floor)
        floor = cfg.power.upload_battery_floor

        def should_abort() -> Optional[str]:
            if self.stop_requested:
                return "stop requested"
            pct = self.monitor.battery_percent()
            if pct is not None and pct < floor:
                return f"battery {pct}% below floor {floor}%"
            return None

        report = self.uploader.upload_pending(deadline=time.monotonic() + cfg.webdav.upload_time_budget_seconds,
                                              should_abort=should_abort)
        log.info("upload result: %s", report.summary())
        self.display.status("Upload done" if not report.remaining else "Upload partial",
                            f"{report.uploaded}/{report.total} sent", battery=self.monitor.battery_percent())
        time.sleep(1)

    def _shutdown(self, why: str) -> None:
        cmd = self.cfg.power.shutdown_command
        self.display.status("Shutting down", why[:21], battery=self.monitor.battery_percent() if self.monitor else None)
        if self.no_shutdown:
            log.info("would shut down now (%s) - skipped because of --no-shutdown", why)
            return
        log.info("shutting down (%s): %s", why, cmd)
        sys.stdout.flush()
        try:
            subprocess.run(shlex.split(cmd), check=False, timeout=60)
        except Exception as exc:
            print(f"shutdown command failed: {exc}", file=sys.stderr)


# ---- CLI ------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dashcam", description="Raspberry Pi dashcam")
    p.add_argument("--config", help="path to config.toml (default: /etc/dashcam/config.toml)")
    p.add_argument("--no-shutdown", action="store_true", help="never power the Pi off (bench testing)")
    p.add_argument("--check", action="store_true", help="test hardware, config, NAS and exit")
    p.add_argument("--upload-now", action="store_true", help="upload pending segments and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"pi-dashcam {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2

    if args.check:
        from dashcam.check import run_checks
        return run_checks(cfg)
    if args.upload_now:
        from dashcam.check import upload_now
        return upload_now(cfg)
    return Dashcam(cfg, no_shutdown=args.no_shutdown).run()
