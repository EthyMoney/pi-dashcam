"""SSD1306 OLED status display. Strictly optional: if the display is missing or
flaky, everything degrades to log lines and the dashcam keeps recording.
"""

import logging
import threading
import time
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)


class Display:
    def __init__(self, enabled: bool = True, address: int = 0x3C, width: int = 128, height: int = 64,
                 min_interval: float = 0.5):
        self.ok = False
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_lines: Optional[List[str]] = None
        self._last_draw = 0.0
        self._errors = 0
        if not enabled:
            log.info("display disabled in config")
            return
        try:
            import adafruit_ssd1306
            import busio
            from board import SCL, SDA
            from PIL import Image, ImageDraw, ImageFont

            self._i2c = busio.I2C(SCL, SDA)
            self._disp = adafruit_ssd1306.SSD1306_I2C(width, height, self._i2c, addr=address)
            self._disp.fill(0)
            self._disp.show()
            self.width, self.height = width, height
            self._image = Image.new("1", (width, height))
            self._draw = ImageDraw.Draw(self._image)
            self._font = ImageFont.load_default()
            self.ok = True
            log.info("OLED display ready at 0x%02X", address)
        except Exception as exc:
            log.warning("OLED display unavailable (%s); continuing without it", exc)
            self.ok = False

    def show(self, lines: Sequence[str], force: bool = False) -> None:
        """Render up to 5 short lines. Skips the I2C write when nothing changed."""
        lines = [str(l)[:21] for l in lines][:5]
        if not self.ok:
            return
        with self._lock:
            now = time.monotonic()
            if not force and lines == self._last_lines:
                return
            if not force and now - self._last_draw < self.min_interval:
                return
            try:
                self._draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)
                y = 0
                for line in lines:
                    self._draw.text((0, y), line, font=self._font, fill=255)
                    y += 12
                self._disp.image(self._image)
                self._disp.show()
                self._last_lines = list(lines)
                self._last_draw = now
                self._errors = 0
            except Exception as exc:
                self._errors += 1
                if self._errors in (1, 10, 100):
                    log.warning("display write failed (%d so far): %s", self._errors, exc)
                if self._errors >= 200:
                    log.error("display failing persistently; disabling it")
                    self.ok = False

    def status(self, title: str, detail: str = "", battery: Optional[int] = None,
               power_label: str = "", extra: str = "") -> None:
        lines = [title]
        if detail:
            lines.append(detail)
        if extra:
            lines.append(extra)
        bottom = []
        if battery is not None:
            bottom.append(f"Bat {battery}%")
        if power_label:
            bottom.append(power_label)
        if bottom:
            while len(lines) < 4:
                lines.append("")
            lines.append(" ".join(bottom))
        self.show(lines)

    def clear(self) -> None:
        if not self.ok:
            return
        with self._lock:
            try:
                self._disp.fill(0)
                self._disp.show()
                self._last_lines = None
            except Exception:
                pass
