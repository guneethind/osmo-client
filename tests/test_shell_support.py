"""Checks for what the packaged app depends on: the request guard of the local server, the stream and
data-folder helpers, and the quit path. Run with:  python -m unittest discover -s tests"""

from __future__ import annotations

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camlink import util  # noqa: E402
from client import lifecycle, server  # noqa: E402
from client.app import App  # noqa: E402


class RequestGuard(unittest.TestCase):
    def test_own_address_is_allowed(self) -> None:
        self.assertTrue(server.request_allowed("127.0.0.1:8091", None, 8091))
        self.assertTrue(server.request_allowed("localhost:8091", "http://localhost:8091", 8091))
        self.assertTrue(server.request_allowed("LOCALHOST:8091", None, 8091))

    def test_other_names_and_origins_are_refused(self) -> None:
        self.assertFalse(server.request_allowed("evil.example:8091", None, 8091))        # DNS rebinding
        self.assertFalse(server.request_allowed(None, None, 8091))
        self.assertFalse(server.request_allowed("127.0.0.1:8091", "https://evil.example", 8091))   # cross-site post
        self.assertFalse(server.request_allowed("127.0.0.1:8091", "null", 8091))
        self.assertFalse(server.request_allowed("127.0.0.1:9999", None, 8091))


class LiveServer(unittest.TestCase):
    """The real handler on a free port, with the app object stubbed to what the routes under test use."""

    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch("client.app.App.__init__", lambda self: None):
            cls.app = App()
        cls.quits: list[int] = []
        cls.http = server.start_server(cls.app, 0, on_quit=lambda: cls.quits.append(1))
        cls.port = cls.http.server_address[1]
        # start_server compares against the port it was given: restart with the real one
        cls.http.shutdown()
        cls.http.server_close()
        cls.http = server.start_server(cls.app, cls.port, on_quit=lambda: cls.quits.append(1))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.http.shutdown()
        cls.http.server_close()

    def request(self, method: str, path: str, headers: dict | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_page_is_served(self) -> None:
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Osmo", body)
        self.assertEqual(self.request("GET", "/monitor.js")[0], 200)

    def test_foreign_host_and_origin_get_403(self) -> None:
        self.assertEqual(self.request("GET", "/", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/quit", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.quits, [])

    def test_quit_calls_back(self) -> None:
        before = len(self.quits)
        status, body = self.request("POST", "/api/quit", {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(len(self.quits), before + 1)


class StreamsAndFolders(unittest.TestCase):
    def test_streams_without_a_console_go_to_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(util, "data_dir", return_value=Path(folder)), \
                mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            util.setup_streams()
            util.log("hello from a windowless run")
            sys.stdout.flush()
            self.assertIn("hello", (Path(folder) / "logs" / "osmo-client.log").read_text(encoding="utf-8"))

    def test_streams_with_a_console_are_kept(self) -> None:
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream):
            util.setup_streams()
            self.assertEqual(sys.stdout.encoding, "utf-8")

    def test_portable_mode_needs_the_marker_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(util, "frozen", return_value=True), \
                    mock.patch.object(util, "app_dir", return_value=Path(folder)):
                self.assertFalse(util.portable())
                (Path(folder) / "portable.txt").write_text("", encoding="utf-8")
                self.assertTrue(util.portable())
                self.assertEqual(util.data_dir(), Path(folder) / "data")
                self.assertTrue((Path(folder) / "data").is_dir())

    def test_not_portable_from_source(self) -> None:
        self.assertFalse(util.portable())


class QuitPath(unittest.TestCase):
    def test_request_quit_cancels_and_waits(self) -> None:
        called = threading.Event()
        lifecycle.install(called.set, lambda: Path("."))
        lifecycle.request_quit()
        self.assertTrue(called.is_set())
        self.assertTrue(lifecycle.quit_asked())
        self.assertEqual(lifecycle.downloads_folder(), Path("."))
        self.assertFalse(lifecycle.wait_stopped(0.01))
        lifecycle.mark_stopped()
        self.assertTrue(lifecycle.wait_stopped(0.01))


if __name__ == "__main__":
    unittest.main()
