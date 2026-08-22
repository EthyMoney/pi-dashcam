# Raspberry Pi Dashcam

A Raspberry Pi that behaves like a dashcam with no buttons and no SD-card shuffling: it
powers on with the ignition, records the whole trip, and when you park at home it uploads
the footage to your NAS over WebDAV and shuts itself off. Next trip, it does it again.

Hardware: a Raspberry Pi with a camera module, a [PiSugar3](https://www.pisugar.com/) UPS
(keeps the Pi alive after ignition-off so it can finish the upload and shut down cleanly),
and optionally a 128×64 SSD1306 OLED for status. Storage is a NAS that speaks WebDAV
(Nextcloud in my case). Python dependencies are small: `requests`, `smbus2`, and the
Adafruit SSD1306 library; the camera stack comes from apt.

## How it works

1. **Ignition on** → the PiSugar3 powers the Pi → the `dashcam` service starts recording
   within seconds of boot. Every segment (2 min by default) is a standalone, crash-safe MP4
   with the date/time burned in.
2. **While driving** the OLED shows recording time, segment count, free space and battery.
   If the NAS happens to be reachable (idling in the driveway), the backlog uploads in the
   background on ignition power.
3. **Ignition off** → the PiSugar3 reports "no external power". After 15 s of confirmed
   loss the trip is considered over, but recording continues for a 60 s grace period in
   case power comes back (fuel stop).
4. **Trip end** → the last segment is finalized. If the NAS becomes reachable within 90 s
   (home Wi-Fi), segments upload oldest-first on battery — each one verified by size on
   the server before the local copy is deleted — bounded by a time budget and a battery
   floor. Not home? Files wait for next time.
5. **Shutdown** → `shutdown -h now`; pisugar-power-manager then cuts the PiSugar3 output so
   the battery is not drained overnight, and *auto power on* wakes the Pi when ignition
   returns.

Things that go wrong are designed to cost little: a bus glitch never ends a trip, a crash
costs at most one segment (systemd restarts the service in 5 s), a dead OLED is ignored, a
full card prunes the oldest footage, and one bad upload never blocks the rest.

## Install

On Raspberry Pi OS **Bookworm** (or newer), with the camera and PiSugar3 attached:

```bash
git clone https://github.com/EthyMoney/pi-dashcam.git
cd pi-dashcam
sudo bash install.sh
```

The installer sets up apt/pip dependencies in a venv, creates `/etc/dashcam/config.toml`,
enables I2C and the PiSugar3 RTC overlay, installs the `dashcam` systemd service, and — if
pisugar-server is present — turns on *auto power on*. Then:

```bash
sudo nano /etc/dashcam/config.toml      # NAS address, login, app password, resolution…
sudo .venv/bin/dashcam --check          # self-test: camera, PiSugar3, OLED, RTC, NAS, login
sudo systemctl start dashcam            # or just reboot
journalctl -u dashcam -f                # watch it run; `-b -1` shows the previous trip
```

(`sudo .venv/bin/python -m dashcam --check` does the same if the `dashcam` command is missing.)

### PiSugar3 power manager (required for the full loop)

Install PiSugar's own service — it is what makes the board cut power after `shutdown` and
power the Pi on again when ignition returns:

```bash
wget https://cdn.pisugar.com/release/pisugar-power-manager.sh
sudo bash pisugar-power-manager.sh -c release     # choose "PiSugar 3"
sudo bash install.sh                                 # re-run: enables auto_power_on
```

You can also toggle *auto power on* in the PiSugar web UI (`http://<pi>:8421`). `dashcam
--check` warns when it is off. Also worth setting there: a **safe shutdown level** (~10 %)
so the board never runs the Pi completely flat.

### RTC (correct timestamps without network)

The PiSugar3 contains a DS3231-compatible clock. The installer adds
`dtoverlay=i2c-rtc,ds3231` to `config.txt`; after a reboot the dashcam copies RTC → system
clock at start and writes NTP-corrected time back to the RTC whenever it is home. Without
this, every trip's filenames and the video overlay carry the time of the *previous*
shutdown.

On a Pi 5 the SoC has its own RTC at `/dev/rtc0`, so the PiSugar one becomes `/dev/rtc1`;
the dashcam picks the DS3231 by driver name, so no configuration is needed either way.

## Configuration

Everything lives in `/etc/dashcam/config.toml` — see
[`config.example.toml`](config.example.toml) for every key with comments. The ones you must
set are in `[webdav]`:

```toml
[webdav]
hostname = "https://nas.local/remote.php/dav/files/USERNAME/dashcam/"   # folder must exist
login = "USERNAME"
password = "APP-PASSWORD"      # Nextcloud: Settings → Security → Devices & sessions
```

**Use an upload-only credential.** The Pi lives in a parked car with this file on its SD
card. A Nextcloud *File drop* share only lets the holder add files — a stolen SD card can
then never read or delete your NAS:

```toml
hostname = "https://nas.local/public.php/webdav/"
login = "SHARE-TOKEN"          # the token at the end of the share link
password = ""                  # or the share password, if you set one
```

The dashcam detects this endpoint and adapts: File drop shares accept nothing but `PUT`, so
segments are trusted once the server acknowledges the complete upload instead of being
re-checked by size (that check needs `PROPFIND`, which such shares refuse).

**TLS.** Self-signed NAS certificate? Copy it to the Pi and point `ca_cert` at it instead of
turning verification off:

```toml
ca_cert = "/etc/dashcam/nas-cert.pem"   # the cert's SAN must include the NAS IP or name
```

Other knobs: resolution/framerate/bitrate and segment length in `[recording]`; debounce,
grace period and battery floor in `[power]`; free-space floor in `[storage]`. Everything is
optional and documented in the example file.

### Tuning bitrate

Work backwards from your Wi-Fi and battery: at the default 6 Mbit/s an hour of driving is
~2.7 GB. Pi 3/4 hardware-encode tops out around 1080p30; a Pi 5 encodes in software (lower
the resolution or framerate if it struggles).

## Day-to-day

```bash
sudo .venv/bin/dashcam --check          # hardware / config / NAS self-test
sudo .venv/bin/dashcam --upload-now     # drain the backlog without recording
sudo .venv/bin/dashcam --no-shutdown    # run on the bench; never powers the Pi off
journalctl -u dashcam -b -1             # log of the previous trip
ls /var/lib/dashcam/videos/closed       # waiting for upload
ls /var/lib/dashcam/videos/failed       # gave up after repeated upload failures
```

`systemctl stop dashcam` (SIGTERM) finalizes the current segment and exits without powering
off. Only the ignition-off path shuts the Pi down.

Segments are named `YYYYMMDD-HHMMSS_TO_YYYYMMDD-HHMMSS.mp4` (start and end). A segment
recovered after a crash or power cut carries `_partial`; it is still playable up to the cut.

## Layout

```
dashcam/
  app.py        trip state machine: RECORDING → STOPPING → HOME_CHECK → UPLOADING → SHUTDOWN
  recorder.py   picamera2, fragmented-MP4 segments, keyframe-aligned rotation, timestamp overlay
  power.py      PiSugar3 over I2C (double-read, debounced loss detection)
  uploader.py   upload queue: verify size then delete, background + on-battery modes
  webdav.py     tiny WebDAV client (PUT + PROPFIND) that also works with Nextcloud File drop shares
  storage.py    current/ closed/ failed/ layout, crash recovery, free-space pruning
  network.py    "am I home" = TCP connect to the NAS; SSID only for logs
  display.py    SSD1306 OLED (optional)
  clock.py      RTC ↔ system clock
  check.py      --check / --upload-now
config.example.toml   documented config template
dashcam.service       systemd unit (installed by install.sh)
install.sh            installer
tests/                hardware-free unit tests:  python -m unittest discover tests
```

## Ideas not done yet

Cabin audio via a USB microphone (`FfmpegOutput(..., audio=True)`), an accelerometer
to flag and protect incident segments, GPS sidecar logs, a second (rear/cabin) camera, a
read-only root filesystem with a separate data partition. Watch the heat: a Pi plus LiPo in a
windshield enclosure in summer sun is a bad combination — keep the battery low and shaded.

## License

MIT — see [LICENSE](LICENSE).
