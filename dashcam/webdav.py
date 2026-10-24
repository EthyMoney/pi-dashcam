"""Minimal WebDAV client: PUT + PROPFIND on top of `requests`.

Deliberately tiny. It is everything a dashcam needs, it works with Nextcloud's
upload-only "File drop" shares (which accept nothing but PUT - most WebDAV
libraries HEAD/PROPFIND first and fall over), and it behaves predictably on a
flaky car-to-house Wi-Fi link.
"""

import logging
import os
import time
import xml.etree.ElementTree as ET
from typing import Callable, Optional, Union
from urllib.parse import quote

log = logging.getLogger(__name__)

PROPFIND_SIZE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/></d:prop></d:propfind>'
)

# Returns a reason string to stop, or None to carry on.
AbortCheck = Callable[[], Optional[str]]


class WebDavError(Exception):
    pass


class UploadAborted(WebDavError):
    """Raised from inside a PUT when the caller's abort check returned a reason.
    The server never receives the complete body, so it stores nothing."""


class _AbortableReader:
    """File wrapper for a streamed PUT that can be cut short between chunks.

    requests/urllib3 pull a file body in 8-16 KiB reads; about every
    `check_interval` seconds the wrapper asks `should_abort()` and raises
    UploadAborted if it answers with a reason. __len__ keeps Content-Length on
    the request (no chunked encoding), so a cut-short body is rejected by the
    server instead of being stored truncated.
    """

    def __init__(self, fh, size: int, should_abort: AbortCheck, check_interval: float):
        self._fh = fh
        self._size = size
        self._should_abort = should_abort
        self._interval = check_interval
        self._next_check = 0.0

    def __len__(self) -> int:
        return self._size

    def __iter__(self):
        return iter(lambda: self.read(65536), b"")

    def read(self, n: int = -1) -> bytes:
        now = time.monotonic()
        if now >= self._next_check:
            self._next_check = now + self._interval
            reason = self._should_abort()
            if reason:
                raise UploadAborted(reason)
        return self._fh.read(n)


class WebDavClient:
    # How often a running PUT consults the caller's abort check.
    ABORT_CHECK_INTERVAL = 1.0

    def __init__(self, base_url: str, login: str, password: str,
                 verify: Union[bool, str] = True, timeout: float = 30.0,
                 file_drop: Optional[bool] = None):
        import requests  # lazy: keeps the module importable without it (tests, --help)

        self.base = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.verify = verify
        # Nextcloud public-share endpoint => upload-only File drop semantics.
        self.file_drop = ("public.php/webdav" in self.base) if file_drop is None else file_drop
        self.session = requests.Session()
        # Always send Basic auth - an empty password is legitimate for public shares
        # (the share token is the username).
        self.session.auth = (login, password or "")
        self.session.verify = verify
        self.session.headers["User-Agent"] = "pi-dashcam"
        # Segments are uploaded back-to-back over one keep-alive connection; servers
        # (and NAT after a stint on battery) drop idle ones. Retry only failures that
        # happen before the request is sent, so an upload is never duplicated.
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(total=3, connect=3, read=0, status=0, other=0,
                          backoff_factor=0.5, allowed_methods=None)
            self.session.mount("http://", HTTPAdapter(max_retries=retry))
            self.session.mount("https://", HTTPAdapter(max_retries=retry))
        except Exception as exc:  # very old requests/urllib3
            log.debug("could not configure connection retries: %s", exc)
        if verify is False:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    def url_for(self, name: str) -> str:
        return self.base + quote(name)

    # ---- operations ---------------------------------------------------------

    def check(self) -> bool:
        """Can we reach the folder with these credentials?

        Normal folders: PROPFIND Depth 0 must answer 207. File-drop shares refuse
        everything but PUT, so there we only make sure the credentials are not
        rejected (401/403)."""
        try:
            if self.file_drop:
                r = self.session.head(self.base, timeout=self.timeout)
                if r.status_code in (401, 403):
                    log.error("WebDAV check: share token rejected (HTTP %d)", r.status_code)
                    return False
                return True
            r = self.session.request("PROPFIND", self.base, data=PROPFIND_SIZE,
                                     headers={"Depth": "0", "Content-Type": "application/xml"},
                                     timeout=self.timeout)
        except Exception as exc:
            log.error("WebDAV check failed: %s", exc)
            return False
        if r.status_code == 207:
            return True
        log.error("WebDAV check: HTTP %d %s for %s%s", r.status_code, r.reason, self.base, self._hint(r.status_code))
        return False

    def upload(self, local_path: str, name: str, should_abort: Optional[AbortCheck] = None) -> int:
        """PUT the file. Returns the HTTP status (201 created / 204 overwritten).

        `should_abort`, if given, is polled about once a second while the body
        streams; a reason string cuts the transfer short with UploadAborted."""
        with open(local_path, "rb") as fh:   # requests streams it with Content-Length set
            body = fh
            if should_abort is not None:
                size = os.fstat(fh.fileno()).st_size
                body = _AbortableReader(fh, size, should_abort, self.ABORT_CHECK_INTERVAL)
            r = self.session.put(self.url_for(name), data=body, timeout=self.timeout)
        if r.status_code not in (200, 201, 204):
            raise WebDavError(f"PUT {name}: HTTP {r.status_code} {r.reason}{self._hint(r.status_code)}")
        return r.status_code

    def remote_size(self, name: str) -> Optional[int]:
        """Size of the uploaded file according to the server (None if unverifiable)."""
        if self.file_drop:
            return None   # PROPFIND is not allowed on File drop shares
        r = self.session.request("PROPFIND", self.url_for(name), data=PROPFIND_SIZE,
                                 headers={"Depth": "0", "Content-Type": "application/xml"},
                                 timeout=self.timeout)
        if r.status_code == 404:
            raise WebDavError(f"{name} is not on the server after upload")
        if r.status_code != 207:
            raise WebDavError(f"PROPFIND {name}: HTTP {r.status_code} {r.reason}")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            raise WebDavError(f"PROPFIND {name}: unparseable response ({exc})")
        el = root.find(".//{DAV:}getcontentlength")
        if el is None or not (el.text or "").strip():
            raise WebDavError(f"PROPFIND {name}: server did not report a size")
        return int(el.text.strip())

    @staticmethod
    def _hint(status: int) -> str:
        return {
            401: " (wrong login/password or app password revoked)",
            403: " (no permission to write here)",
            404: " (does the remote folder exist? it is not created automatically)",
            405: " (endpoint refused the method - wrong URL?)",
            409: " (parent folder missing on the server)",
            507: " (server quota exceeded)",
        }.get(status, "")
