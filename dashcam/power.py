"""PiSugar3 UPS: external-power detection and battery readings over I2C.

Reads only. Writes to the PiSugar3 are left to pisugar-server (it owns the
unlock/lock dance on register 0x0B). Register map (official datasheet):
    0x02  control 1: bit7 external power present, bit4 auto-power-on,
          bit5 output-off countdown, bit6 charging enabled
    0x22  battery voltage high byte (mV), 0x23 low byte
    0x2A  battery percentage (0-100)
"""

import logging
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
    def __init__(self, bus: int = 1, address: int = 0x57):
        self.address = address
        self.bus = None
        self.available = False
        self._last_good_ctrl1: Optional[int] = None
        try:
            try:
                from smbus2 import SMBus  # pure python, maintained
            except ImportError:
                from smbus import SMBus  # type: ignore
            self.bus = SMBus(bus)
            # Probe once so a missing board is reported at startup, not mid-trip.
            probe = self._read_raw(REG_CTRL1)
            self.available = probe is not None
        except Exception as exc:
            log.error("PiSugar3 not available (%s); power-loss detection disabled", exc)
            self.bus = None
            self.available = False
        if self.available:
            log.info("PiSugar3 found at 0x%02X", address)

    # ---- low level -------------------------------------------------------

    def _read_raw(self, reg: int) -> Optional[int]:
        if self.bus is None:
            return None
        try:
            return self.bus.read_byte_data(self.address, reg)
        except Exception as exc:
            log.debug("i2c read 0x%02X failed: %s", reg, exc)
            return None

    def read_reg(self, reg: int) -> Optional[int]:
        """Double-read a register; the official driver does the same because the
        MCU can change a byte between reads. A mismatch or error yields None."""
        a = self._read_raw(reg)
        if a is None:
            return None
        b = self._read_raw(reg)
        if b is None or a != b:
            # One more try before giving up on this poll.
            time.sleep(0.02)
            c = self._read_raw(reg)
            if c is None or c != b:
                return None
            return c
        return a

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
        ext = self.external_power() if self.available else None
        pct = self.battery_percent() if (with_battery and self.available) else None
        return PowerSample(external=ext, percent=pct, millivolts=None)

    def startup_report(self) -> None:
        """Log the things that bite people in the field."""
        if not self.available:
            log.warning("no PiSugar3: the dashcam will record until stopped or the power is cut")
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
    `loss_confirm_seconds`. `external_present()` then keeps returning False
    until power is confirmed back, at which point the streak resets.
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
        if self.last_sample and self.last_sample.percent is not None:
            return self.last_sample.percent
        return self.pisugar.battery_percent() if self.pisugar.available else None
