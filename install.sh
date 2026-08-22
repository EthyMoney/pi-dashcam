#!/usr/bin/env bash
# pi-dashcam installer for Raspberry Pi OS (Bookworm or newer recommended).
# Run from the cloned repository:   sudo bash install.sh
#
# What it does:
#   - installs apt + pip dependencies into a venv that can see the system picamera2
#   - creates /etc/dashcam/config.toml from the example (never overwrites yours)
#   - enables I2C and the PiSugar3 RTC overlay (correct clock at boot, no network needed)
#   - enables PiSugar "auto power on" if pisugar-server is installed
#   - installs and enables the dashcam systemd service
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "please run as root:  sudo bash install.sh" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${APP_DIR}/.venv"
CONFIG_DIR=/etc/dashcam
DATA_DIR=/var/lib/dashcam

echo "==> apt packages"
apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg python3-pil python3-numpy \
  python3-picamera2 python3-libcamera i2c-tools libcap-dev
apt-get install -y python3-opencv || echo "   python3-opencv not available; timestamp overlay will be off"
apt-get install -y util-linux-extra 2>/dev/null || true   # hwclock on minimal Bookworm images

echo "==> enabling I2C"
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
fi

echo "==> python environment (${VENV})"
python3 -m venv --system-site-packages "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
"${VENV}/bin/pip" install --quiet -e "${APP_DIR}" || echo "   (editable install skipped; 'python -m dashcam' still works)"

echo "==> configuration"
install -d -m 750 "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
  install -m 600 "${APP_DIR}/config.example.toml" "${CONFIG_DIR}/config.toml"
  echo "   created ${CONFIG_DIR}/config.toml  <-- EDIT THIS (NAS address, login, app password)"
else
  echo "   keeping existing ${CONFIG_DIR}/config.toml"
fi
install -d "${DATA_DIR}/videos"

echo "==> PiSugar3 RTC (DS3231-compatible at 0x68)"
BOOT_CFG=/boot/firmware/config.txt
[[ -f "${BOOT_CFG}" ]] || BOOT_CFG=/boot/config.txt
if grep -q '^dtoverlay=i2c-rtc,ds3231' "${BOOT_CFG}"; then
  echo "   overlay already present in ${BOOT_CFG}"
else
  echo "dtoverlay=i2c-rtc,ds3231" >> "${BOOT_CFG}"
  echo "   added dtoverlay=i2c-rtc,ds3231 to ${BOOT_CFG} (active after reboot)"
fi
if [[ -e /dev/rtc0 ]] && command -v hwclock >/dev/null 2>&1; then
  if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qi yes; then
    hwclock --systohc && echo "   RTC set from the (NTP-synced) system clock"
  fi
fi

echo "==> PiSugar power manager"
if command -v pisugar-server >/dev/null 2>&1 || systemctl list-unit-files 2>/dev/null | grep -q '^pisugar-server'; then
  echo "   pisugar-server is installed"
else
  cat <<'MSG'
   pisugar-power-manager is NOT installed. It is what makes the PiSugar3
   cut power after 'shutdown' (instead of draining the battery overnight)
   and power the Pi back on when ignition returns. Install it with:

       wget https://cdn.pisugar.com/release/pisugar-power-manager.sh
       sudo bash pisugar-power-manager.sh -c release      # choose "PiSugar 3"

   then run this installer again to switch on auto power on.
MSG
fi
PS_CFG=/etc/pisugar-server/config.json
if [[ -f "${PS_CFG}" ]]; then
  python3 - "${PS_CFG}" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as fh:
    cfg = json.load(fh)
if cfg.get("auto_power_on") is True:
    print("   auto_power_on already enabled")
else:
    cfg["auto_power_on"] = True
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print("   enabled auto_power_on in", path)
PY
  systemctl restart pisugar-server 2>/dev/null || true
fi

echo "==> systemd service"
sed -e "s|@APP_DIR@|${APP_DIR}|g" -e "s|@PYTHON@|${VENV}/bin/python|g" \
  "${APP_DIR}/dashcam.service" > /etc/systemd/system/dashcam.service
systemctl daemon-reload
systemctl enable dashcam.service

cat <<EOF

Installed. Next:
  1. sudo nano ${CONFIG_DIR}/config.toml          # NAS address, login, app password, resolution
  2. sudo ${VENV}/bin/dashcam --check             # hardware / NAS / config self-test
  3. sudo systemctl start dashcam                  # or simply reboot
  4. journalctl -u dashcam -f                      # watch it run;  -b -1  shows the previous trip
EOF
