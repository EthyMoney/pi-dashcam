"""Fake hardware modules so the logic can be tested off the Pi.

Import this before anything from `dashcam` in a test. It installs minimal
stand-ins for smbus2, picamera2, board/busio/adafruit_ssd1306 and the WebDAV client.
"""

import sys
import types


class FakeSMBus:
    """Scriptable I2C bus. `regs` maps register -> value; set `fail` to raise."""
    instances = []

    def __init__(self, bus):
        self.bus = bus
        self.regs = {0x02: 0x90, 0x2A: 87, 0x22: 0x10, 0x23: 0x1F}   # ext power + auto-power-on, 87%
        self.fail = False
        self.reads = 0
        self.flaky_once = False
        FakeSMBus.instances.append(self)

    def read_byte_data(self, addr, reg):
        self.reads += 1
        if self.fail:
            raise OSError(121, "Remote I/O error")
        if self.flaky_once:
            self.flaky_once = False
            raise OSError(121, "Remote I/O error")
        return self.regs[reg]


smbus2 = types.ModuleType("smbus2")
smbus2.SMBus = FakeSMBus
sys.modules.setdefault("smbus2", smbus2)


# --- picamera2 --------------------------------------------------------------

class FakeOutput:
    def __init__(self, output_filename, **kw):
        self.output_filename = output_filename
        self.started = False
        self.stopped = False
        # create the file the way ffmpeg would, path is the last token
        self.path = output_filename.split()[-1]

    def start(self):
        self.started = True
        with open(self.path, "wb") as fh:
            fh.write(b"\x00" * 1024)

    def stop(self):
        self.stopped = True


class FakeSplittable:
    def __init__(self, output=None):
        self.output = output
        self.splits = []

    def start(self):
        if self.output:
            self.output.start()

    def stop(self):
        if self.output:
            self.output.stop()

    def split_output(self, new_output, wait_for_keyframe=True):
        if self.output:
            self.output.stop()
        new_output.start()
        self.splits.append(new_output)
        self.output = new_output


class FakeEncoder:
    def __init__(self, bitrate=None, repeat=True, iperiod=None, **kw):
        self.bitrate, self.repeat, self.iperiod = bitrate, repeat, iperiod


class FakePicamera2:
    instances = []
    fail_start = False

    def __init__(self):
        self.configured = None
        self.pre_callback = None
        self.encoder = None
        self.output = None
        self.closed = False
        self.recording = False
        FakePicamera2.instances.append(self)

    def create_video_configuration(self, **kw):
        return kw

    def configure(self, cfg):
        self.configured = cfg

    def start_recording(self, encoder, output):
        if FakePicamera2.fail_start:
            raise RuntimeError("camera busy")
        self.encoder, self.output = encoder, output
        output.start()
        self.recording = True

    def stop_recording(self):
        self.output.stop()
        self.recording = False

    def stop_encoder(self):
        self.output.stop()

    def start_encoder(self, encoder, output):
        self.encoder, self.output = encoder, output
        output.start()

    def close(self):
        self.closed = True

    @staticmethod
    def global_camera_info():
        return [{"Model": "fake"}]


picamera2 = types.ModuleType("picamera2")
picamera2.Picamera2 = FakePicamera2
picamera2.MappedArray = object
encoders = types.ModuleType("picamera2.encoders")
encoders.H264Encoder = FakeEncoder
outputs = types.ModuleType("picamera2.outputs")
outputs.FfmpegOutput = FakeOutput
outputs.SplittableOutput = FakeSplittable
picamera2.encoders = encoders
picamera2.outputs = outputs
sys.modules.setdefault("picamera2", picamera2)
sys.modules.setdefault("picamera2.encoders", encoders)
sys.modules.setdefault("picamera2.outputs", outputs)


# --- display stack: make init fail so Display degrades to no-op --------------

board = types.ModuleType("board")
board.SCL, board.SDA = 3, 2
busio = types.ModuleType("busio")


def _no_i2c(*a, **k):
    raise OSError("no i2c on this box")


busio.I2C = _no_i2c
sys.modules.setdefault("board", board)
sys.modules.setdefault("busio", busio)
sys.modules.setdefault("adafruit_ssd1306", types.ModuleType("adafruit_ssd1306"))


# --- WebDAV client (dashcam.webdav.WebDavClient) -----------------------------

class FakeDavClient:
    """Same interface as dashcam.webdav.WebDavClient, no network."""
    instances = []

    def __init__(self, base_url, login, password, verify=True, timeout=30, file_drop=None):
        self.base = base_url
        self.login, self.password = login, password
        self.verify = verify
        self.timeout = timeout
        self.file_drop = ("public.php/webdav" in base_url) if file_drop is None else file_drop
        self.remote = {}          # name -> size
        self.fail_names = set()   # basenames that always fail to upload
        self.truncate_names = set()
        self.abort_during = set()   # names for which upload() polls should_abort once mid-transfer
        self.mid_transfer = None    # optional callable run at that point (e.g. to set a stop event)
        self.uploads = []
        FakeDavClient.instances.append(self)

    def check(self):
        return True

    def upload(self, local_path, name, should_abort=None):
        import os
        self.uploads.append(name)
        if name in self.abort_during and should_abort is not None:
            if self.mid_transfer is not None:
                self.mid_transfer()
            reason = should_abort()
            if reason:
                raise _webdav.UploadAborted(reason)
        if name in self.fail_names:
            raise ConnectionError("boom")
        size = os.path.getsize(local_path)
        if name in self.truncate_names:
            size -= 1
        self.remote[name] = size
        return 201

    def remote_size(self, name):
        if self.file_drop:
            return None
        return self.remote[name]


import dashcam.webdav as _webdav  # noqa: E402  (importable without requests)

# Swap in the fake globally so `dashcam.app` gets it without any wiring.
# The real class stays reachable as RealWebDavClient - test_webdav.py needs it,
# and unittest imports these modules before it.
if not hasattr(_webdav, "RealWebDavClient"):
    _webdav.RealWebDavClient = _webdav.WebDavClient
_webdav.WebDavClient = FakeDavClient
