"""WebDAV upload of closed segments, with verify-then-delete.

Two ways to run:
  * background thread while recording - whenever the NAS is reachable (engine
    running in the driveway, for example) the backlog drains on ignition power;
  * foreground at trip end, on battery, bounded by a time budget and a
    battery floor.

A file is only deleted locally after the server reports the same size (or,
on an upload-only File drop share where that cannot be asked, after the server
acknowledged the complete PUT). One bad file never blocks the others: it is
retried up to max_failed times, then parked in failed/.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from dashcam import clock, webdav
from dashcam.config import WebdavConfig
from dashcam.display import Display
from dashcam.network import HomeDetector
from dashcam.storage import Storage

log = logging.getLogger(__name__)


@dataclass
class UploadReport:
    total: int = 0
    uploaded: int = 0
    failed: int = 0
    bytes_sent: int = 0
    aborted: Optional[str] = None
    nas_unreachable: bool = False
    remaining: int = 0
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.uploaded}/{self.total} uploaded"]
        if self.bytes_sent:
            parts.append(f"{self.bytes_sent / 1e6:.0f} MB")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.aborted:
            parts.append(f"stopped: {self.aborted}")
        if self.nas_unreachable:
            parts.append("NAS unreachable")
        if self.remaining:
            parts.append(f"{self.remaining} left for next time")
        return ", ".join(parts)


class Uploader:
    # A just-closed segment may still be getting its last bytes from ffmpeg;
    # give it a moment before reading it.
    SETTLE_SECONDS = 3.0

    def __init__(self, cfg: WebdavConfig, storage: Storage, home: HomeDetector,
                 display: Optional[Display] = None, max_failed: int = 3,
                 battery_percent: Optional[Callable[[], Optional[int]]] = None):
        self.cfg = cfg
        self.storage = storage
        self.home = home
        self.display = display
        self.max_failed = max_failed
        self.battery_percent = battery_percent
        self.attempts: Dict[str, int] = {}
        self.current_file: Optional[str] = None
        self._client: Optional[webdav.WebDavClient] = None
        self._cycle_lock = threading.Lock()
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_stop = threading.Event()
        self.last_report: Optional[UploadReport] = None

    @property
    def configured(self) -> bool:
        return bool(self.cfg.hostname and self.cfg.login)

    # ---- client -----------------------------------------------------------

    def client(self) -> webdav.WebDavClient:
        if self._client is None:
            if self.cfg.ca_cert:
                verify = self.cfg.ca_cert            # pin the NAS cert / private CA
            elif self.cfg.verify_tls:
                verify = True
            else:
                verify = False
                log.warning("TLS certificate verification is OFF ([webdav] verify_tls=false)")
            self._client = webdav.WebDavClient(self.cfg.hostname, self.cfg.login, self.cfg.password,
                                               verify=verify, timeout=self.cfg.timeout)
            log.info("upload target: %s (%s)", self.cfg.hostname,
                     "upload-only File drop share" if self._client.file_drop else "WebDAV folder")
        return self._client

    def check_server(self) -> bool:
        """Prove URL, credentials and TLS setup against the server."""
        try:
            return self.client().check()
        except Exception as exc:
            log.error("WebDAV check failed: %s", exc)
            return False

    # ---- uploading --------------------------------------------------------

    def upload_pending(self, deadline: Optional[float] = None,
                       should_abort: Optional[Callable[[], Optional[str]]] = None,
                       stop_event: Optional[threading.Event] = None) -> UploadReport:
        """Upload every closed segment, oldest first. Thread-safe (one cycle at a time).

        deadline      - time.monotonic() value after which no new file is started
        should_abort  - returns a reason string to stop (e.g. "battery 18% < 25%")
        stop_event    - set to stop between files
        """
        with self._cycle_lock:
            files = self.storage.closed_files()
            report = UploadReport(total=len(files))
            if not files:
                return report
            if not self.configured:
                report.aborted = "webdav not configured"
                report.remaining = len(files)
                return report
            if not self.home.nas_reachable():
                report.nas_unreachable = True
                report.remaining = len(files)
                return report
            try:
                self.client()
            except Exception as exc:
                log.error("cannot create WebDAV client: %s", exc)
                report.aborted = f"webdav client error: {exc}"
                report.remaining = len(files)
                return report

            for index, path in enumerate(files, start=1):
                if stop_event is not None and stop_event.is_set():
                    report.aborted = "stopped"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    report.aborted = "time budget used up"
                    break
                if should_abort is not None:
                    reason = should_abort()
                    if reason:
                        report.aborted = reason
                        break
                if not os.path.exists(path):
                    continue  # pruned meanwhile
                self.current_file = path
                try:
                    ok, nbytes = self._upload_one(path, index, len(files))
                finally:
                    self.current_file = None
                if ok:
                    report.uploaded += 1
                    report.bytes_sent += nbytes
                    self.attempts.pop(path, None)
                else:
                    report.failed += 1
                    n = self.attempts.get(path, 0) + 1
                    self.attempts[path] = n
                    if n >= self.max_failed:
                        self.storage.mark_failed(path)
                        self.attempts.pop(path, None)
                    if not self.home.nas_reachable():
                        report.aborted = "NAS became unreachable"
                        break
            report.remaining = len(self.storage.closed_files())
            if report.uploaded:
                # We are evidently home: good moment to save NTP time into the RTC.
                clock.write_rtc_if_synchronized()
            self.last_report = report
            log.info("upload cycle: %s", report.summary())
            return report

    def _upload_one(self, path: str, index: int, total: int):
        name = os.path.basename(path)
        try:
            age = time.time() - os.path.getmtime(path)
            if age < self.SETTLE_SECONDS:
                time.sleep(self.SETTLE_SECONDS - age)
        except OSError:
            pass
        size = os.path.getsize(path)
        if self.display:
            self.display.status("Uploading", f"{index}/{total} {size / 1e6:.0f}MB", extra=name[:21],
                                battery=self.battery_percent() if self.battery_percent else None)
        client = self.client()
        started = time.monotonic()
        try:
            client.upload(path, name)
        except Exception as exc:
            log.warning("upload failed for %s: %s", name, exc)
            return False, 0
        try:
            remote_size = client.remote_size(name)
        except Exception as exc:
            log.warning("uploaded %s but could not verify it on the server: %s (keeping local copy)", name, exc)
            return False, 0
        if remote_size is not None and remote_size != size:
            log.warning("size mismatch for %s: local %d, server %d - keeping local copy for retry",
                        name, size, remote_size)
            return False, 0
        self.storage.remove(path)
        elapsed = max(time.monotonic() - started, 0.001)
        log.info("uploaded %s (%.1f MB in %.0fs, %.1f Mbit/s)%s, local copy removed",
                 name, size / 1e6, elapsed, size * 8 / elapsed / 1e6,
                 "" if remote_size is not None else " [size unverifiable on File drop share]")
        return True, size

    # ---- background mode --------------------------------------------------

    def start_background(self, interval: float = 30.0) -> None:
        if self._bg_thread is not None or not self.configured:
            return
        self._bg_stop.clear()

        def loop():
            log.info("background uploader running (checks every %.0fs)", interval)
            while not self._bg_stop.wait(interval):
                try:
                    if not self.storage.closed_files():
                        continue
                    if not self.home.nas_reachable():
                        continue
                    log.info("NAS reachable while recording - uploading backlog on ignition power")
                    self.upload_pending(stop_event=self._bg_stop)
                except Exception:
                    log.exception("background upload cycle failed")

        self._bg_thread = threading.Thread(target=loop, name="uploader", daemon=True)
        self._bg_thread.start()

    def stop_background(self, timeout: float = 120.0) -> None:
        if self._bg_thread is None:
            return
        self._bg_stop.set()
        self._bg_thread.join(timeout)
        if self._bg_thread.is_alive():
            log.warning("background uploader still busy after %.0fs; continuing anyway", timeout)
        self._bg_thread = None
