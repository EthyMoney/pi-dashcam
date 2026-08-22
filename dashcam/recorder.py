"""Camera recording in crash-safe segments.

Every segment is a fragmented MP4 (ffmpeg `-movflags +frag_keyframe+empty_moov`),
which stays playable even if power is cut mid-write. Segments are rotated on
a timer; with picamera2 >= 0.3.26 the switch happens on a keyframe via
SplittableOutput with no dropped frames, otherwise we fall back to restarting
the encoder (a few frames lost at each boundary).

Camera failure is not fatal to the rest of the dashcam: the power logic still
runs so the Pi shuts down cleanly when the ignition goes off. For the same
reason, every call that can block on a stalled encoder (segment switch,
stop_recording, close) is bounded by a timeout.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from dashcam.config import RecordingConfig
from dashcam.storage import Storage

log = logging.getLogger(__name__)

# Fragmented MP4: decodable even when truncated by a power cut. default_base_moof
# keeps the fragments compatible with browser players (e.g. Nextcloud's viewer).
FRAGMENTED_MP4_FLAGS = "-movflags +frag_keyframe+empty_moov+default_base_moof"


class Recorder:
    SPLIT_TIMEOUT = 15.0    # keyframes come every second; anything longer means a stalled encoder
    STOP_TIMEOUT = 20.0
    CLOSE_TIMEOUT = 10.0

    def __init__(self, cfg: RecordingConfig, storage: Storage):
        self.cfg = cfg
        self.storage = storage
        self.picam2 = None
        self.encoder = None
        self.splitter = None
        self.mode = "split"
        self.available = False      # camera stack importable and started at least once
        self.recording = False
        self.current_path: Optional[str] = None
        self.segment_started: Optional[datetime] = None
        self._segment_started_mono = 0.0
        self._output_broken = False
        self.segments_closed = 0
        self.rotate_errors = 0
        self.last_error: Optional[str] = None

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        """Open the camera and begin the first segment. Returns True on success."""
        if self.recording:
            return True
        try:
            from picamera2 import Picamera2
        except Exception as exc:
            self.last_error = f"picamera2 not importable: {exc}"
            log.error("%s - recording disabled (install python3-picamera2)", self.last_error)
            return False

        try:
            self.picam2 = Picamera2()
            config_kwargs = {
                "main": {"size": (self.cfg.width, self.cfg.height)},
                "controls": {"FrameRate": self.cfg.framerate},
            }
            if self.cfg.hflip or self.cfg.vflip:
                try:
                    from libcamera import Transform
                    config_kwargs["transform"] = Transform(hflip=int(self.cfg.hflip), vflip=int(self.cfg.vflip))
                except Exception as exc:
                    log.warning("could not apply hflip/vflip: %s", exc)
            video_config = self.picam2.create_video_configuration(**config_kwargs)
            self.picam2.configure(video_config)

            if self.cfg.timestamp_overlay:
                self._install_timestamp_overlay()

            self.encoder = self._new_encoder()
            self.current_path = self.storage.new_segment_path()
            self.segment_started = datetime.now()
            self._output_broken = False
            output = self._make_output(self.current_path)

            try:
                from picamera2.outputs import SplittableOutput
                self.splitter = SplittableOutput(output=output)
                self.picam2.start_recording(self.encoder, self.splitter)
                self.mode = "split"
            except ImportError:
                self.splitter = None
                self.picam2.start_recording(self.encoder, output)
                self.mode = "restart"
                log.warning("picamera2 has no SplittableOutput (needs >= 0.3.26); segment "
                            "boundaries will drop a few frames. Consider: sudo apt upgrade python3-picamera2")

            self._segment_started_mono = time.monotonic()
            self.recording = True
            self.available = True
            self.rotate_errors = 0
            self.last_error = None
            log.info("recording %dx%d@%d %.1f Mbit/s, %ds segments (%s mode) -> %s",
                     self.cfg.width, self.cfg.height, self.cfg.framerate, self.cfg.bitrate / 1e6,
                     self.cfg.segment_seconds, self.mode, self.current_path)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.exception("camera failed to start")
            self._close_camera(guarded=True)
            return False

    def _new_encoder(self):
        from picamera2.encoders import H264Encoder
        # iperiod = one keyframe per second so segment splits land within ~1 s and
        # every segment begins on an I-frame; repeat=True puts SPS/PPS before
        # each keyframe so every segment is independently decodable.
        return H264Encoder(bitrate=self.cfg.bitrate, repeat=True,
                           iperiod=int(self.cfg.framerate), framerate=self.cfg.framerate)

    def _make_output(self, path: str):
        from picamera2.outputs import FfmpegOutput
        if " " in path:
            raise ValueError(f"video path must not contain spaces: {path}")
        output = FfmpegOutput(f"{FRAGMENTED_MP4_FLAGS} {path}")
        # picamera2 drops frames silently if ffmpeg dies; this tells us so we rotate.
        output.error_callback = self._on_output_error
        return output

    def _on_output_error(self, *args) -> None:
        if not self._output_broken:
            log.error("ffmpeg output failed mid-segment (%s); starting a fresh segment",
                      args[0] if args else "unknown error")
        self._output_broken = True

    def _install_timestamp_overlay(self) -> None:
        try:
            import cv2
            from picamera2 import MappedArray
        except Exception as exc:
            log.warning("timestamp overlay disabled (%s) - install python3-opencv to enable", exc)
            return
        scale = max(0.5, round(self.cfg.height / 1080 * 0.9, 2))
        thickness = max(1, int(round(scale * 2)))
        margin = int(12 * scale) + 8
        origin = (margin, self.cfg.height - margin)
        font = cv2.FONT_HERSHEY_SIMPLEX

        def apply_timestamp(request):
            text = time.strftime("%Y-%m-%d %H:%M:%S")
            with MappedArray(request, "main") as m:
                cv2.putText(m.array, text, origin, font, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
                cv2.putText(m.array, text, origin, font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

        self.picam2.pre_callback = apply_timestamp
        log.info("timestamp overlay enabled")

    # ---- segments ---------------------------------------------------------

    def segment_age(self) -> float:
        return time.monotonic() - self._segment_started_mono if self.recording else 0.0

    def tick(self) -> Optional[str]:
        """Call regularly. Rotates the segment when due (or when the output broke);
        returns the closed file's path."""
        if not self.recording:
            return None
        if not self._output_broken and self.segment_age() < self.cfg.segment_seconds:
            return None
        return self.rotate()

    def rotate(self) -> Optional[str]:
        """Start a new segment and close the old one."""
        if not self.recording:
            return None
        old_path = self.current_path
        new_path = self.storage.new_segment_path()
        try:
            new_output = self._make_output(new_path)
            if self.mode == "split" and self.splitter is not None:
                # Blocks until the next keyframe (<= ~1 s) - bounded, in case the encoder stalls.
                self._run_bounded(lambda: self.splitter.split_output(new_output),
                                  self.SPLIT_TIMEOUT, "segment switch")
            else:
                self.picam2.stop_encoder()
                self.encoder = self._new_encoder()
                self.picam2.start_encoder(self.encoder, new_output)
            self._output_broken = False
        except Exception as exc:
            self.rotate_errors += 1
            self.last_error = str(exc)
            stalled = isinstance(exc, TimeoutError)
            log.exception("segment rotation failed (%d so far)", self.rotate_errors)
            if stalled or self.rotate_errors >= 3:
                log.error("giving up on this camera session; will try to restart the camera")
                self._close_camera(guarded=True)
                # Keep whatever was written so far; fragmented MP4 plays up to the cut.
                if old_path and self.storage.close_segment(old_path, end=datetime.now(), suffix="_partial"):
                    self.segments_closed += 1
                self.current_path = None
            return None
        self.current_path = new_path
        self.segment_started = datetime.now()
        self._segment_started_mono = time.monotonic()
        closed = self.storage.close_segment(old_path, end=self.segment_started)
        if closed:
            self.segments_closed += 1
        return closed

    def stop(self) -> Optional[str]:
        """Stop recording, finalize and close the current segment, release the camera."""
        if self.picam2 is None:
            return None
        closed = None
        clean = True
        if self.recording:
            clean = False
            try:
                # stop_recording waits for ffmpeg to flush the file; bound it so a hung
                # encoder can never keep the Pi awake on battery.
                self._run_bounded(self.picam2.stop_recording, self.STOP_TIMEOUT, "stop_recording")
                clean = True
            except Exception as exc:
                log.warning("stop_recording failed: %s", exc)
            self.recording = False
            if self.current_path:
                closed = self.storage.close_segment(self.current_path, end=datetime.now(),
                                                    suffix="" if clean else "_partial")
                if closed:
                    self.segments_closed += 1
                self.current_path = None
        self._close_camera(guarded=not clean)
        return closed

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _run_bounded(fn: Callable[[], object], timeout: float, label: str) -> None:
        """Run fn in a helper thread; raise TimeoutError if it does not finish in time.
        (The helper is left to finish on its own - we must not block the main loop.)"""
        outcome = {}

        def runner():
            try:
                fn()
            except Exception as exc:  # propagate to the caller
                outcome["exc"] = exc

        t = threading.Thread(target=runner, name=label.replace(" ", "-"), daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"{label} did not complete within {timeout:.0f}s (encoder stalled?)")
        if "exc" in outcome:
            raise outcome["exc"]

    def _close_camera(self, guarded: bool = False) -> None:
        """Stop any encoder still running and release the camera.

        guarded=True runs the teardown with a timeout (used after failures, when
        the camera stack may be wedged); a stuck teardown is abandoned so the
        power/shutdown logic keeps running."""
        self.recording = False
        cam, self.picam2 = self.picam2, None
        self.splitter = None
        self.encoder = None
        if cam is None:
            return

        def teardown():
            try:
                cam.stop_encoder()      # close() alone leaves the encoder/ffmpeg running
            except Exception:
                pass
            try:
                cam.close()
            except Exception:
                pass

        if not guarded:
            teardown()
            return
        try:
            self._run_bounded(teardown, self.CLOSE_TIMEOUT, "camera close")
        except TimeoutError as exc:
            log.error("%s; abandoning the camera session", exc)
