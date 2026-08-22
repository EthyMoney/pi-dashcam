"""System clock vs. the PiSugar3 RTC.

The Pi has no battery-backed clock of its own (the Pi 5 has one, but without
a backup battery it forgets too). With `dtoverlay=i2c-rtc,ds3231` the PiSugar3's
RTC appears as /dev/rtcN; we copy it into the system clock at startup (before
any NTP is possible in the car) and write NTP-corrected time back to it
whenever we notice we're synchronised (i.e. at home).
"""

import logging
import os
import shutil
import subprocess
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

SYS_RTC = "/sys/class/rtc"
_wrote_rtc_this_run = False


def find_rtc_device() -> Optional[str]:
    """The RTC to use: prefer the DS3231/DS1307 (PiSugar3 via the i2c-rtc overlay).

    On a Pi 5 the SoC's own RTC is /dev/rtc0 and the PiSugar one becomes
    /dev/rtc1, so we pick by driver name rather than number."""
    if not os.path.isdir(SYS_RTC):
        return None
    first = None
    for entry in sorted(os.listdir(SYS_RTC)):
        dev = "/dev/" + entry
        if not os.path.exists(dev):
            continue
        first = first or dev
        try:
            with open(os.path.join(SYS_RTC, entry, "name")) as fh:
                name = fh.read().strip().lower()
        except OSError:
            name = ""
        if "ds1307" in name or "ds3231" in name:   # the ds3231 overlay binds the rtc-ds1307 driver
            return dev
    return first


def have_rtc() -> bool:
    return find_rtc_device() is not None and shutil.which("hwclock") is not None


def _hwclock(*args: str) -> subprocess.CompletedProcess:
    dev = find_rtc_device()
    cmd = ["hwclock"] + (["-f", dev] if dev else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def sync_system_from_rtc() -> bool:
    """hwclock --hctosys from the PiSugar RTC. Returns True if the system clock was set."""
    if not have_rtc():
        log.info("no hardware RTC found under %s - filenames use whatever time the Pi booted with", SYS_RTC)
        return False
    before = datetime.now()
    try:
        res = _hwclock("--hctosys")
    except Exception as exc:
        log.warning("hwclock --hctosys failed: %s", exc)
        return False
    if res.returncode != 0:
        log.warning("hwclock --hctosys failed: %s", res.stderr.strip() or res.stdout.strip())
        return False
    after = datetime.now()
    delta = (after - before).total_seconds()
    if abs(delta) > 5:
        log.info("system clock set from RTC %s: %s -> %s", find_rtc_device(),
                 before.strftime("%F %T"), after.strftime("%F %T"))
    else:
        log.info("system clock already matched RTC %s (%s)", find_rtc_device(), after.strftime("%F %T"))
    if after.year < 2024:
        log.warning("RTC time looks unset (%s); it will be corrected the next time NTP syncs", after)
    return True


def ntp_synchronized() -> bool:
    if not shutil.which("timedatectl"):
        return False
    try:
        res = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=10)
        return res.stdout.strip().lower() == "yes"
    except Exception:
        return False


def write_rtc_if_synchronized() -> bool:
    """Once per run: if NTP has synced the system clock, save it to the RTC."""
    global _wrote_rtc_this_run
    if _wrote_rtc_this_run or not have_rtc():
        return False
    if not ntp_synchronized():
        return False
    try:
        res = _hwclock("--systohc")
    except Exception as exc:
        log.warning("hwclock --systohc failed: %s", exc)
        return False
    if res.returncode == 0:
        _wrote_rtc_this_run = True
        log.info("RTC %s updated from NTP-synchronised system clock", find_rtc_device())
        return True
    log.warning("hwclock --systohc failed: %s", res.stderr.strip())
    return False
