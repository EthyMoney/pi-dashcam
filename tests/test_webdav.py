"""dashcam.webdav against a tiny in-process WebDAV server (PUT/PROPFIND/HEAD)."""

import base64
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests  # noqa: F401
    HAVE_REQUESTS = True
except ImportError:  # pragma: no cover
    HAVE_REQUESTS = False

# The real client, never the stub: hwstubs (imported by the other test modules,
# which unittest loads first) replaces dashcam.webdav.WebDavClient with a fake
# and stashes the original as RealWebDavClient.
import dashcam.webdav as _wd  # noqa: E402
from dashcam.webdav import UploadAborted, WebDavError  # noqa: E402

WebDavClient = getattr(_wd, "RealWebDavClient", _wd.WebDavClient)

TOKEN = "sharetoken"
USER, PASS = "logan", "secret"


class Handler(BaseHTTPRequestHandler):
    """Enough WebDAV to exercise the client. Keep-alive correct: every handler
    drains the request body and every response carries a Content-Length, so a
    rejected request never desyncs the connection."""

    protocol_version = "HTTP/1.1"
    store = {}      # path -> bytes
    log = []

    def log_message(self, *a):   # silence
        pass

    def _read_body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))

    def _respond(self, status, body=b"", content_type="application/xml"):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _filedrop(self):
        return self.path.startswith("/public.php/webdav")

    def _authorized(self):
        hdr = self.headers.get("Authorization", "")
        cred = base64.b64decode(hdr[6:]).decode() if hdr.startswith("Basic ") else None
        return cred == (f"{TOKEN}:" if self._filedrop() else f"{USER}:{PASS}")

    def _reject_unauthorized(self):
        """Body already drained by the caller, so the connection stays usable."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="x"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        expected = int(self.headers.get("Content-Length", "0") or 0)
        body = self._read_body()
        if len(body) != expected:
            # Client went away mid-body. Like Nextcloud: store nothing.
            self.close_connection = True
            return self._respond(400)
        if not self._authorized():
            return self._reject_unauthorized()
        if "/missing/" in self.path:
            return self._respond(404)
        if "truncate" in self.path:
            body = body[:-1]
        existed = self.path in Handler.store
        Handler.store[self.path] = body
        Handler.log.append(("PUT", self.path, len(body)))
        self._respond(204 if existed else 201)

    def do_HEAD(self):
        self._read_body()
        if not self._authorized():
            return self._reject_unauthorized()
        self._respond(405 if self._filedrop() else 200)

    def do_PROPFIND(self):
        self._read_body()
        if not self._authorized():
            return self._reject_unauthorized()
        if self._filedrop():
            return self._respond(405)
        if self.path.endswith("/"):
            body = (f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
                    f'<d:href>{self.path}</d:href></d:response></d:multistatus>').encode()
        elif self.path in Handler.store:
            size = len(Handler.store[self.path])
            body = (f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>{self.path}</d:href>'
                    f'<d:propstat><d:prop><d:getcontentlength>{size}</d:getcontentlength></d:prop>'
                    f'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>').encode()
        else:
            return self._respond(404)
        self._respond(207, body)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass   # a client that hangs up mid-upload is expected in these tests


@unittest.skipUnless(HAVE_REQUESTS, "requests not installed")
class WebDavClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.store.clear()
        Handler.log.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = os.path.join(self.tmp.name, "seg.mp4")
        with open(self.local, "wb") as fh:
            fh.write(b"\x00" * 5000)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_folder_mode_upload_and_verify(self):
        c = WebDavClient(self.url("/remote.php/dav/files/logan/dashcam/"), USER, PASS, timeout=5)
        self.assertFalse(c.file_drop)
        self.assertTrue(c.check())
        self.assertEqual(c.upload(self.local, "a b.mp4"), 201)
        self.assertIn("/remote.php/dav/files/logan/dashcam/a%20b.mp4", Handler.store)
        self.assertEqual(c.remote_size("a b.mp4"), 5000)
        self.assertEqual(c.upload(self.local, "a b.mp4"), 204)      # overwrite

    def test_many_uploads_reuse_the_connection(self):
        c = WebDavClient(self.url("/dav/"), USER, PASS, timeout=5)
        for i in range(12):
            self.assertIn(c.upload(self.local, f"seg{i}.mp4"), (201, 204))
            self.assertEqual(c.remote_size(f"seg{i}.mp4"), 5000)
        self.assertEqual(len(Handler.log), 12)

    def test_truncated_upload_detected(self):
        c = WebDavClient(self.url("/dav/"), USER, PASS, timeout=5)
        c.upload(self.local, "truncate.mp4")
        self.assertEqual(c.remote_size("truncate.mp4"), 4999)     # caller compares and keeps local

    def test_bad_credentials_and_missing_folder(self):
        c = WebDavClient(self.url("/dav/"), USER, "wrong", timeout=5)
        self.assertFalse(c.check())
        with self.assertRaises(WebDavError) as cm:
            c.upload(self.local, "x.mp4")
        self.assertIn("401", str(cm.exception))
        c = WebDavClient(self.url("/dav/missing/"), USER, PASS, timeout=5)
        with self.assertRaises(WebDavError) as cm:
            c.upload(self.local, "x.mp4")
        self.assertIn("404", str(cm.exception))
        self.assertIn("remote folder", str(cm.exception))

    def test_missing_after_upload_is_reported(self):
        c = WebDavClient(self.url("/dav/"), USER, PASS, timeout=5)
        with self.assertRaises(WebDavError) as cm:
            c.remote_size("never-uploaded.mp4")
        self.assertIn("not on the server", str(cm.exception))

    def test_upload_can_be_cut_short_mid_transfer(self):
        big = os.path.join(self.tmp.name, "big.mp4")
        with open(big, "wb") as fh:
            fh.write(b"\x00" * (4 * 1024 * 1024))
        c = WebDavClient(self.url("/dav/"), USER, PASS, timeout=5)
        c.ABORT_CHECK_INTERVAL = 0.0                     # poll the check on every chunk
        calls = {"n": 0}

        def abort():
            calls["n"] += 1
            return "battery 18% below floor 25%" if calls["n"] >= 2 else None

        with self.assertRaises(UploadAborted) as cm:
            c.upload(big, "big.mp4", should_abort=abort)
        self.assertIn("battery", str(cm.exception))
        self.assertNotIn("/dav/big.mp4", Handler.store)   # nothing truncated left on the server
        # The connection is torn down cleanly; the next upload just works.
        self.assertEqual(c.upload(self.local, "seg.mp4", should_abort=lambda: None), 201)
        self.assertEqual(c.remote_size("seg.mp4"), 5000)

    def test_file_drop_share(self):
        c = WebDavClient(self.url("/public.php/webdav/"), TOKEN, "", timeout=5)
        self.assertTrue(c.file_drop)
        self.assertTrue(c.check())                    # token accepted (HEAD -> 405, not 401)
        self.assertEqual(c.upload(self.local, "seg.mp4"), 201)
        self.assertIsNone(c.remote_size("seg.mp4"))  # cannot PROPFIND a file drop
        bad = WebDavClient(self.url("/public.php/webdav/"), "nottoken", "", timeout=5)
        self.assertFalse(bad.check())


if __name__ == "__main__":
    unittest.main()
