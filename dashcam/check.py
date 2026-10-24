"""`dashcam --check`: a setup self-test you can run on the bench, and
`dashcam --upload-now`: drain the upload backlog without recording."""

import logging
import os
import time

from dashcam import clock
from dashcam.config import Config
from dashcam.display import Display
from dashcam.network import HomeDetector, current_ssid
from dashcam.power import PiSugar3
from dashcam.storage import Storage
from dashcam.uploader import Uploader

log = logging.getLogger("dashcam.check")


def _line(ok, label, detail=""):
    mark = {True: "OK  ", False: "FAIL", None: "--  "}[ok]
    print(f"[{mark}] {label}{(': ' + detail) if detail else ''}")
    return ok


def run_checks(cfg: Config) -> int:
    print(f"config: {cfg.source_path or 'built-in defaults (no file found)'}")
    failures = 0

    # Storage
    try:
        storage = Storage(cfg.paths.data_dir, cfg.storage.min_free_gb)
        free = storage.free_bytes() / 1024 ** 3
        _line(free >= cfg.storage.min_free_gb, "storage",
              f"{storage.root} - {free:.1f} GB free (floor {cfg.storage.min_free_gb} GB), "
              f"{len(storage.closed_files())} pending, {len(storage.failed_files())} failed")
    except Exception as exc:
        failures += 1
        _line(False, "storage", str(exc))
        storage = None

    # Clock
    rtc_dev = clock.find_rtc_device()
    if clock.have_rtc():
        _line(True, "rtc", f"{rtc_dev} present; NTP synced: {clock.ntp_synchronized()}")
    elif rtc_dev:
        _line(None, "rtc", f"{rtc_dev} present but hwclock is missing (apt install util-linux-extra)")
    else:
        _line(None, "rtc", "no RTC - add dtoverlay=i2c-rtc,ds3231 to config.txt (PiSugar3 RTC)")
    print(f"       system time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # PiSugar3
    ps = PiSugar3(cfg.power.i2c_bus, cfg.power.pisugar_address)
    if ps.available:
        s = ps.sample()
        apo = ps.auto_power_on_enabled()
        _line(True, "pisugar3", f"{s.label}, battery {s.percent}% ({ps.battery_millivolts()} mV)")
        if apo is False:
            failures += 1
            _line(False, "pisugar3 auto power on", "DISABLED - Pi will not wake on ignition. "
                                                   "Enable in PiSugar web UI / config.json (auto_power_on)")
        else:
            _line(True if apo else None, "pisugar3 auto power on", "enabled" if apo else "unknown")
    else:
        failures += 1
        _line(False, "pisugar3", f"not found on i2c bus {cfg.power.i2c_bus} at 0x{cfg.power.pisugar_address:02X} "
                                 "(is I2C enabled? run: i2cdetect -y 1)")

    # Display
    disp = Display(cfg.display.enabled, cfg.display.i2c_address, cfg.display.width, cfg.display.height)
    if cfg.display.enabled:
        _line(True if disp.ok else None, "oled display",
              "ready" if disp.ok else "not found (dashcam still works without it)")
        if disp.ok:
            disp.show(["Dashcam check", time.strftime("%H:%M:%S"), "display OK"], force=True)
    else:
        _line(None, "oled display", "disabled in config")

    # Camera
    try:
        from picamera2 import Picamera2
        cams = Picamera2.global_camera_info()
        if cams:
            _line(True, "camera", ", ".join(c.get("Model", "?") for c in cams))
        else:
            failures += 1
            _line(False, "camera", "no camera detected (check the ribbon cable / libcamera-hello)")
    except Exception as exc:
        failures += 1
        _line(False, "camera", f"picamera2 unavailable: {exc}")
    try:
        import picamera2.outputs as outputs
        _line(hasattr(outputs, "SplittableOutput"), "seamless segments",
              "SplittableOutput available" if hasattr(outputs, "SplittableOutput")
              else "picamera2 too old for SplittableOutput - segments will drop a few frames at boundaries")
    except Exception:
        pass
    try:
        import cv2  # noqa: F401
        _line(True, "timestamp overlay", "python3-opencv present")
    except Exception:
        _line(None, "timestamp overlay", "python3-opencv missing - overlay disabled")

    # Network / NAS
    ssid = current_ssid()
    print(f"       wifi: {ssid or 'not associated'}"
          + (f" (home_ssid={cfg.network.home_ssid})" if cfg.network.home_ssid else ""))
    if cfg.upload_configured:
        home = HomeDetector(cfg.webdav.hostname, cfg.network.nas_check_timeout, cfg.network.home_ssid)
        reach = home.nas_reachable()
        _line(reach, "nas reachable", f"{home.host}:{home.port}")
        if reach and storage is not None:
            up = Uploader(cfg.webdav, storage, home)
            ok = up.check_server()
            if not ok:
                failures += 1
            _line(ok, "webdav login", "PROPFIND ok" if ok else "failed - check hostname/login/password/ca_cert")
    else:
        _line(None, "webdav", "not configured - videos stay on the Pi")

    print("result:", "all good" if failures == 0 else f"{failures} problem(s)")
    return 0 if failures == 0 else 1


def upload_now(cfg: Config) -> int:
    if not cfg.upload_configured:
        log.error("[webdav] not configured")
        return 2
    storage = Storage(cfg.paths.data_dir, cfg.storage.min_free_gb)
    storage.recover_stale_segments()
    home = HomeDetector(cfg.webdav.hostname, cfg.network.nas_check_timeout, cfg.network.home_ssid)
    up = Uploader(cfg.webdav, storage, home, max_failed=cfg.storage.max_failed_uploads)
    files = storage.closed_files()
    if not files:
        log.info("nothing pending in %s", storage.closed_dir)
        return 0
    log.info("%d pending segment(s), %.0f MB", len(files), storage.pending_bytes() / 1e6)
    report = up.upload_pending()
    log.info(report.summary())
    return 0 if report.uploaded == report.total else 1
