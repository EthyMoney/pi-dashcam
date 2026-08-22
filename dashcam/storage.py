"""Local video storage: directory layout, naming, retention, crash recovery.

Layout under <data_dir>/videos:
    current/   the segment being written right now (at most one file)
    closed/    finished segments waiting for upload (valid, playable files)
    failed/    segments that repeatedly failed to upload (kept for manual recovery)

Segment filenames carry start and end time, e.g.
    20260822-081500_TO_20260822-081700.mp4
"""

import logging
import os
import shutil
import threading
import time
from datetime import datetime
from typing import List, Optional

log = logging.getLogger(__name__)

TIME_FMT = "%Y%m%d-%H%M%S"
VIDEO_EXT = ".mp4"


def timestamp(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now()).strftime(TIME_FMT)


class Storage:
    def __init__(self, data_dir: str, min_free_gb: float = 0.0):
        self.root = os.path.join(data_dir, "videos")
        self.current_dir = os.path.join(self.root, "current")
        self.closed_dir = os.path.join(self.root, "closed")
        self.failed_dir = os.path.join(self.root, "failed")
        self.min_free_bytes = int(min_free_gb * 1024 ** 3)
        # Serialises deletes/moves between the recorder thread, the uploader
        # thread and the retention pruner.
        self.lock = threading.RLock()
        for d in (self.current_dir, self.closed_dir, self.failed_dir):
            os.makedirs(d, exist_ok=True)

    # ---- naming ---------------------------------------------------------

    def new_segment_path(self, start: Optional[datetime] = None) -> str:
        """Path for a fresh segment in current/. Never collides with an existing file."""
        base = timestamp(start)
        candidate = os.path.join(self.current_dir, base + VIDEO_EXT)
        n = 1
        while os.path.exists(candidate):
            n += 1
            candidate = os.path.join(self.current_dir, f"{base}-{n}{VIDEO_EXT}")
        return candidate

    def close_segment(self, current_path: str, end: Optional[datetime] = None, suffix: str = "") -> Optional[str]:
        """Move a finished segment from current/ to closed/, appending its end time."""
        if not os.path.exists(current_path):
            log.warning("segment %s vanished before it could be closed", current_path)
            return None
        if os.path.getsize(current_path) == 0:
            log.warning("segment %s is empty; discarding", current_path)
            self._remove(current_path)
            return None
        stem = os.path.splitext(os.path.basename(current_path))[0]
        # Keep "YYYYMMDD-HHMMSS", dropping any collision counter ("-2") we added.
        start_part = "-".join(stem.split("-")[:2])
        end_part = timestamp(end)
        target = os.path.join(self.closed_dir, f"{start_part}_TO_{end_part}{suffix}{VIDEO_EXT}")
        n = 1
        while os.path.exists(target):
            n += 1
            target = os.path.join(self.closed_dir, f"{start_part}_TO_{end_part}{suffix}-{n}{VIDEO_EXT}")
        with self.lock:
            os.replace(current_path, target)
        log.info("segment closed: %s (%.1f MB)", os.path.basename(target), os.path.getsize(target) / 1e6)
        return target

    def recover_stale_segments(self) -> List[str]:
        """At boot, anything left in current/ is from a crash or power cut.

        Segments are written as fragmented MP4, so a truncated file is still
        playable up to the cut; we keep it, marked _partial, using its mtime
        as the end time.
        """
        recovered = []
        for name in sorted(os.listdir(self.current_dir)):
            path = os.path.join(self.current_dir, name)
            if not name.endswith(VIDEO_EXT):
                continue
            try:
                end = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                end = None
            target = self.close_segment(path, end=end, suffix="_partial")
            if target:
                log.warning("recovered partial segment from previous run: %s", os.path.basename(target))
                recovered.append(target)
        return recovered

    # ---- listing --------------------------------------------------------

    def closed_files(self) -> List[str]:
        """Closed segments, oldest first (names sort by start time)."""
        with self.lock:
            names = [n for n in os.listdir(self.closed_dir) if n.endswith(VIDEO_EXT)]
        return [os.path.join(self.closed_dir, n) for n in sorted(names)]

    def failed_files(self) -> List[str]:
        names = [n for n in os.listdir(self.failed_dir) if n.endswith(VIDEO_EXT)]
        return [os.path.join(self.failed_dir, n) for n in sorted(names)]

    def pending_bytes(self) -> int:
        total = 0
        for p in self.closed_files():
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def mark_failed(self, path: str) -> Optional[str]:
        with self.lock:
            if not os.path.exists(path):
                return None
            target = os.path.join(self.failed_dir, os.path.basename(path))
            os.replace(path, target)
        log.warning("moved %s to failed/ after repeated upload failures", os.path.basename(path))
        return target

    def remove(self, path: str) -> None:
        with self.lock:
            self._remove(path)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    # ---- disk space -------------------------------------------------------

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def prune(self, protect: Optional[str] = None) -> List[str]:
        """Delete the oldest closed (then failed) segments until min free space is met.

        Loop-recording behaviour: the newest footage always has room. Files in
        current/ are never touched. `protect` is a path the uploader is busy
        with and must not be removed from under it.
        """
        deleted: List[str] = []
        if self.min_free_bytes <= 0:
            return deleted
        with self.lock:
            if self.free_bytes() >= self.min_free_bytes:
                return deleted
            # Oldest by mtime: filenames may lie if the clock was wrong.
            candidates = sorted(self.failed_files() + self.closed_files(),
                                key=lambda p: self._mtime(p))
            for path in candidates:
                if self.free_bytes() >= self.min_free_bytes:
                    break
                if path == protect:
                    continue
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                except OSError as exc:
                    log.warning("could not prune %s: %s", path, exc)
                    continue
                deleted.append(path)
                log.warning("pruned %s (%.1f MB) to free space", os.path.basename(path), size / 1e6)
            if self.free_bytes() < self.min_free_bytes:
                log.error("still below free-space floor after pruning: %.2f GB free",
                          self.free_bytes() / 1024 ** 3)
        return deleted

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return time.time()
