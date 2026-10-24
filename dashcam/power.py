"""PiSugar3 UPS: external-power detection and battery readings over I2C.

Reads only. Writes to the PiSugar3 are left to pisugar-server (it owns the
unlock/lock dance on register 0x0B). Register map (official datasheet):
    0x02  control 1: bit7 external power present, bit4 auto-power-on,
          bit5 output-off countdown, bit6 charging enabled
    0x22  battery voltage high byte (mV), 0x23 low byte
    0x2A  battery percentage (0-100)
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

REG_CTRL1 = 0x02
REG_VOLT_HI = 0x22
REG_VOLT_LO = 0x23
REG_PERCENT = 0x2A

BIT_EXTERNAL_POWER = 0x80
BIT_AUTO_POWER_ON = 0x10
BIT_CHARGING_ENABLED = 0x40


@dataclass
class PowerSample:
    external: Optional[bool]      # None = could not read (bus glitch / no PiSugar)
    percent: Optional[int]
    millivolts: Optional[int]

    @property
    def label(self) -> str:
        if self.external is None:
            return "power: ?"
        return "on ignition" if self.external else "ON BATTERY"


class PiSugar3:
    """`available` means the board has answered at least once.

    A board that does not answer at boot (the MCU NAKs the odd read, or is
    still waking up) is polled again every cycle and becomes available the
    moment it responds: one bad read at startup must not switch off
    power-loss detection for the whole trip. Only a missing I2C bus
    (/dev/i2c-N absent, I2C disabled) disables it for good.
    """

    def __init__(self, bus: int = 1, address: int = 0x57):
        self.address = address
        self.bus = None
        self.available = False
        self._last_good_ctrl1: Optional[int] = None
        # The main loop, the background uploader and the display path may all
        # read the board; serialise so the double-read below stays meaningful.
        self._lock = threading.Lock()
        try:
            try:
                from smbus2 import SMBus  # pure python, maintained
            except ImportError:
                from smbus import SMBus  # type: ignore
            self.bus = SMBus(bus)
        except Exception as exc:
            log.error("I2C bus %d not usable (%s); power-loss detection disabled", bus, exc)
            self.bus = None
            return
        # Probe once so a missing board is reported at startup, not mid-trip.
        if self.read_reg(REG_CTRL1) is None:
            log.warning("PiSugar3 did not answer at 0x%02X on I2C bus %d; will keep trying every poll",
                        address, bus)

    # ---- low level -------------------------------------------------------

    def _read_raw(self, reg: int) -> Optional[int]:
        if self.bus is None:
            return None
        try:
            return self.bus.read_byte_data(self.address, reg)
        except Exception as exc:
            log.debug("i2c read 0x%02X failed: %s", reg, exc)
            return None

    def _double_read(self, reg: int) -> Optional[int]:
        """Two consecutive reads must agree; the official driver does the same
        because the MCU can change a byte between reads."""
        a = self._read_raw(reg)
        if a is None:
            return None
        b = self._read_raw(reg)
        if b == a:
            return a
        # Mismatch or a failed second read: one more try before giving up on this poll.
        time.sleep(0.02)
        c = self._read_raw(reg)
        if c is not None and c == (b if b is not None else a):
            return c
        return None

    def read_reg(self, reg: int) -> Optional[int]:
        """Read a register with the consistency check; None on error or mismatch."""
        if self.bus is None:
            return None
        with self._lock:
            value = self._double_read(reg)
        if value is not None and not self.available:
            self.available = True
            log.info("PiSugar3 found at 0x%02X", self.address)
        return value

    # ---- readings --------------------------------------------------------

    def external_power(self) -> Optional[bool]:
        val = self.read_reg(REG_CTRL1)
        if val is None:
            return None
        self._last_good_ctrl1 = val
        return bool(val & BIT_EXTERNAL_POWER)

    def battery_percent(self) -> Optional[int]:
        val = self.read_reg(REG_PERCENT)
        if val is None:
            return None
        return max(0, min(100, val))

    def battery_millivolts(self) -> Optional[int]:
        hi = self.read_reg(REG_VOLT_HI)
        lo = self.read_reg(REG_VOLT_LO)
        if hi is None or lo is None:
            return None
        return (hi << 8) | lo

    def auto_power_on_enabled(self) -> Optional[bool]:
        val = self.read_reg(REG_CTRL1)
        if val is None:
            return None
        return bool(val & BIT_AUTO_POWER_ON)

    def sample(self, with_battery: bool = True) -> PowerSample:
        ext = self.external_power()
        # No point asking for the battery when the board just failed to answer.
        pct = self.battery_percent() if (with_battery and ext is not None) else None
        return PowerSample(external=ext, percent=pct, millivolts=None)

    def startup_report(self) -> None:
        """Log the things that bite people in the field."""
        if self.bus is None:
            log.warning("no I2C bus: the dashcam will record until stopped or the power is cut")
            return
        if not self.available:
            log.warning("no PiSugar3 answer yet: until it responds the dashcam records until stopped or the "
                        "power is cut (is the board seated? i2cdetect -y 1 should show 0x%02X)", self.address)
            return
        apo = self.auto_power_on_enabled()
        if apo is False:
            log.warning("PiSugar3 'auto power on' is DISABLED: the Pi will not wake when ignition "
                        "returns. Enable it in the PiSugar web UI (auto_power_on) or see README.")
        elif apo:
            log.info("PiSugar3 auto power on: enabled")
        s = self.sample()
        mv = self.battery_millivolts()
        log.info("power at startup: %s, battery %s%% (%s mV)", s.label,
                 s.percent if s.percent is not None else "?", mv if mv is not None else "?")


class PowerMonitor:
    """Debounced external-power state.

    A single bad I2C read must never end a trip. Only consecutive seconds of
    *confirmed* "no external power" count towards a loss; unknown readings
    leave the streak untouched. A loss is reported only after
    `loss_confirm_seconds`. `power_lost` then keeps returning True until power
    is confirmed back, at which point the streak resets.
    """

    def __init__(self, pisugar: PiSugar3, loss_confirm_seconds: float):
        self.pisugar = pisugar
        self.loss_confirm_seconds = loss_confirm_seconds
        self.loss_streak = 0.0
        self.last_known: Optional[bool] = None
        self.last_sample: Optional[PowerSample] = None
        self.unknown_streak = 0
        self.confirmed_lost = False

    def poll(self, elapsed: float) -> PowerSample:
        sample = self.pisugar.sample()
        self.last_sample = sample
        if sample.external is None:
            self.unknown_streak += 1
            if self.unknown_streak in (5, 30, 120):
                log.warning("PiSugar3 unreadable for %d polls; last known state: %s",
                            self.unknown_streak, self.last_known)
            return sample
        if self.unknown_streak:
            self.unknown_streak = 0
        self.last_known = sample.external
        if sample.external:
            if self.confirmed_lost:
                log.info("external power is back")
            self.loss_streak = 0.0
            self.confirmed_lost = False
        else:
            self.loss_streak += elapsed
            if not self.confirmed_lost and self.loss_streak >= self.loss_confirm_seconds:
                self.confirmed_lost = True
                log.info("external power lost (confirmed after %.0fs)", self.loss_streak)
        return sample

    @property
    def power_lost(self) -> bool:
        return self.confirmed_lost

    def battery_percent(self) -> Optional[int]:
        """Current charge, read fresh from the board.

        The recording loop's last sample goes stale the moment we stop polling
        to upload on battery, and the battery-floor check must see the battery
        actually draining. A read that fails falls back to the last good sample.
        """
        pct = self.pisugar.battery_percent()
        if pct is None and self.last_sample is not None:
            pct = self.last_sample.percent
        return pct
