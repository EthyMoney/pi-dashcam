"""Configuration loading.

All tunables and every secret live in a TOML file outside the repo
(default: /etc/dashcam/config.toml). See config.example.toml for the
documented template. Nothing in this module knows about hardware.
"""

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = (
    os.environ.get("DASHCAM_CONFIG", ""),
    "/etc/dashcam/config.toml",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml"),
)


class ConfigError(Exception):
    pass


@dataclass
class PathsConfig:
    data_dir: str = "/var/lib/dashcam"


@dataclass
class RecordingConfig:
    width: int = 1920
    height: int = 1080
    framerate: int = 30
    bitrate: int = 6_000_000
    segment_seconds: int = 120
    timestamp_overlay: bool = True
    hflip: bool = False
    vflip: bool = False


@dataclass
class PowerConfig:
    i2c_bus: int = 1
    pisugar_address: int = 0x57
    poll_seconds: float = 1.0
    loss_confirm_seconds: float = 15.0
    return_grace_seconds: float = 60.0
    upload_battery_floor: int = 25
    shutdown_command: str = "shutdown -h now"


@dataclass
class NetworkConfig:
    home_ssid: str = ""
    nas_check_timeout: float = 3.0
    home_check_seconds: float = 90.0
    upload_while_recording: bool = True
    background_check_seconds: float = 30.0


@dataclass
class WebdavConfig:
    hostname: str = ""
    login: str = ""
    password: str = ""
    verify_tls: bool = True
    ca_cert: str = ""
    timeout: int = 30
    upload_time_budget_seconds: float = 900.0


@dataclass
class StorageConfig:
    min_free_gb: float = 4.0
    max_failed_uploads: int = 3


@dataclass
class DisplayConfig:
    enabled: bool = True
    i2c_address: int = 0x3C
    width: int = 128
    height: int = 64


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    webdav: WebdavConfig = field(default_factory=WebdavConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    source_path: Optional[str] = None

    @property
    def upload_configured(self) -> bool:
        return bool(self.webdav.hostname and self.webdav.login)


def _apply_section(target: Any, values: Dict[str, Any], section: str) -> None:
    known = {f.name: f.type for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            log.warning("config: unknown key [%s] %s ignored", section, key)
            continue
        current = getattr(target, key)
        # Light coercion so "6000000" and 6000000 both work; bools must be real bools.
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"[{section}] {key} must be true or false")
        elif isinstance(current, int) and not isinstance(value, bool):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"[{section}] {key} must be an integer")
        elif isinstance(current, float):
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ConfigError(f"[{section}] {key} must be a number")
        elif isinstance(current, str):
            value = str(value)
        setattr(target, key, value)


def load_config(path: Optional[str] = None) -> Config:
    """Load the TOML config. A missing file yields defaults (with a loud warning)."""
    candidates = [path] if path else [p for p in DEFAULT_CONFIG_PATHS if p]
    chosen = next((p for p in candidates if p and os.path.isfile(p)), None)

    cfg = Config()
    if chosen is None:
        if path:
            raise ConfigError(f"config file not found: {path}")
        log.warning("no config file found (looked at %s); using built-in defaults, uploads disabled",
                    ", ".join(candidates))
        return cfg

    with open(chosen, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except Exception as exc:  # tomllib.TOMLDecodeError
            raise ConfigError(f"could not parse {chosen}: {exc}")

    for section_name in ("paths", "recording", "power", "network", "webdav", "storage", "display"):
        section = data.pop(section_name, None)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"[{section_name}] must be a table")
        _apply_section(getattr(cfg, section_name), section, section_name)
    for leftover in data:
        log.warning("config: unknown section [%s] ignored", leftover)

    cfg.source_path = chosen
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    rec = cfg.recording
    if rec.width <= 0 or rec.height <= 0 or rec.framerate <= 0:
        raise ConfigError("[recording] width, height and framerate must be positive")
    if rec.bitrate < 500_000:
        raise ConfigError("[recording] bitrate looks too low (bits per second, e.g. 6000000)")
    if rec.segment_seconds < 10:
        raise ConfigError("[recording] segment_seconds must be at least 10")
    if cfg.power.poll_seconds <= 0:
        raise ConfigError("[power] poll_seconds must be positive")
    if cfg.power.loss_confirm_seconds < 0 or cfg.power.return_grace_seconds < 0:
        raise ConfigError("[power] loss_confirm_seconds / return_grace_seconds cannot be negative")
    if not 0 <= cfg.power.upload_battery_floor <= 100:
        raise ConfigError("[power] upload_battery_floor must be 0-100")
    if cfg.webdav.hostname and not cfg.webdav.hostname.lower().startswith(("http://", "https://")):
        raise ConfigError("[webdav] hostname must start with http:// or https://")
    if cfg.webdav.ca_cert and not os.path.isfile(cfg.webdav.ca_cert):
        raise ConfigError(f"[webdav] ca_cert file not found: {cfg.webdav.ca_cert}")
    if cfg.storage.min_free_gb < 0:
        raise ConfigError("[storage] min_free_gb cannot be negative")
    if cfg.storage.max_failed_uploads < 1:
        raise ConfigError("[storage] max_failed_uploads must be at least 1")
