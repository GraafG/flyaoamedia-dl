import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import IncompleteRead
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError


def load_downloader():
    """Import without reading .env, inheriting credentials, or changing console encoding."""
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = Mock()
    path = Path(__file__).resolve().parents[1] / "download_videos.py"
    spec = importlib.util.spec_from_file_location("offline_downloader", path)
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(sys.modules, {"dotenv": dotenv}),
        patch.dict(os.environ, {}, clear=True),
        redirect_stdout(io.StringIO()),
        redirect_stderr(io.StringIO()),
        patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")),
        patch("subprocess.run", side_effect=AssertionError("Subprocesses are forbidden")),
    ):
        spec.loader.exec_module(module)
    return module


downloader = load_downloader()


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))
        self.urlopen = self.enterContext(patch.object(
            downloader.urllib.request, "urlopen",
            side_effect=AssertionError("Unexpected network request"),
        ))
        self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP request"),
        ))
        self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("Network is forbidden"),
        ))
        self.process = self.enterContext(patch.object(
            downloader.subprocess, "run",
            side_effect=AssertionError("Unexpected subprocess"),
        ))

    def test_logging_appends_mirrors_and_closes_on_exit(self):
        path = self.root / "run.log"
        path.write_text("previous run\n", encoding="utf-8")
        with downloader._enable_file_logging(path):
            handle = sys.stdout._fh
            print("console output")
            print("console error", file=sys.stderr)
            self.assertFalse(handle.closed)
        self.assertTrue(handle.closed)
        self.assertIs(sys.stdout, self.stdout)
        self.assertIs(sys.stderr, self.stderr)
        self.assertEqual(self.stdout.getvalue(), "console output\n")
        self.assertEqual(self.stderr.getvalue(), "console error\n")
        contents = path.read_text(encoding="utf-8")
        self.assertTrue(contents.startswith("previous run\n\n=== RUN "))
        self.assertIn("console output\nconsole error\n", contents)

    def test_logging_restores_streams_on_system_exit(self):
        with (
            self.assertRaises(SystemExit),
            downloader._enable_file_logging(self.root / "run.log"),
        ):
            handle = sys.stdout._fh
            raise SystemExit(1)
        self.assertTrue(handle.closed)
        self.assertIs(sys.stdout, self.stdout)
        self.assertIs(sys.stderr, self.stderr)

    def test_unavailable_log_reports_error_and_keeps_console(self):
        with downloader._enable_file_logging(self.root / "missing" / "run.log"):
            self.assertIs(sys.stdout, self.stdout)
            print("still running")
        self.assertIn("Could not open log file", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "still running\n")

    def test_cli_runs_inside_logging_context(self):
        path = self.root / "run.log"

        def run(args):
            self.assertEqual(args.limit, 2)
            self.assertTrue(args.attachments_only)
            self.assertIsInstance(sys.stdout, downloader._Tee)
            print("mocked run")

        with (
            patch.object(downloader, "OUTPUT_DIR", self.root / "downloads"),
            patch.object(downloader, "LOG_FILE", path),
            patch.object(downloader, "_run", side_effect=run) as execute,
            patch.object(sys, "argv", ["download_videos.py", "--limit", "2", "--attachments-only"]),
        ):
            downloader.main()
        execute.assert_called_once()
        self.assertIn("mocked run", path.read_text(encoding="utf-8"))
        self.assertIs(sys.stdout, self.stdout)

    def test_metadata_success_preserves_fields_and_request(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps({
            "media": {
                "name": "Sample", "duration": 120,
                "assets": [{"type": "still_image", "url": "https://example.test/art.jpg?size=1"}],
            },
        }).encode())
        self.assertEqual(downloader.wistia_meta("abc12345"), {
            "name": "Sample", "duration": 120, "thumbnail": "https://example.test/art.jpg",
        })
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://fast.wistia.com/embed/medias/abc12345.json")
        self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 20})

    def test_expected_metadata_failures_report_and_continue(self):
        for error in (URLError("offline"), IncompleteRead(b""), ValueError("invalid URL")):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                self.assertEqual(downloader.wistia_meta("abc12345"), {})
        self.assertEqual(self.stdout.getvalue().count("Could not load Wistia metadata"), 3)

    def test_malformed_metadata_json_reports_and_continues(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value = io.BytesIO(b"not json")
        self.assertEqual(downloader.wistia_meta("abc12345"), {})
        self.assertIn("Could not load Wistia metadata", self.stdout.getvalue())

    def test_unexpected_metadata_errors_are_not_silenced(self):
        self.urlopen.side_effect = TypeError("programming error")
        with self.assertRaises(TypeError):
            downloader.wistia_meta("abc12345")

    def test_thumbnail_success_and_existing_file_skip(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value = io.BytesIO(b"synthetic image")
        path = self.root / "lesson.mp4"
        video = {"thumbnail": "https://example.test/art.jpg"}
        downloader.download_thumb(video, path)
        self.assertEqual(path.with_suffix(".jpg").read_bytes(), b"synthetic image")
        self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 15})
        downloader.download_thumb(video, path)
        self.urlopen.assert_called_once()

    def test_thumbnail_failures_report_and_continue(self):
        for error in (URLError("offline"), IncompleteRead(b""), ValueError("invalid URL")):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                downloader.download_thumb(
                    {"thumbnail": "https://example.test/art.jpg"}, self.root / "lesson.mp4",
                )
        self.assertEqual(self.stdout.getvalue().count("Could not save lesson.jpg"), 3)

    def test_thumbnail_write_failure_reports_and_continues(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value = io.BytesIO(b"synthetic image")
        with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
            downloader.download_thumb(
                {"thumbnail": "https://example.test/art.jpg"}, self.root / "lesson.mp4",
            )
        self.assertIn("disk full", self.stdout.getvalue())

    def test_unexpected_thumbnail_errors_are_not_silenced(self):
        self.urlopen.side_effect = TypeError("programming error")
        with self.assertRaises(TypeError):
            downloader.download_thumb(
                {"thumbnail": "https://example.test/art.jpg"}, self.root / "lesson.mp4",
            )

    def test_missing_media_skips_network(self):
        self.assertEqual(downloader.wistia_meta(""), {})
        downloader.download_thumb({}, self.root / "lesson.mp4")
        self.urlopen.assert_not_called()

    def test_ytdlp_failure_uses_cookie_fallback_without_raising(self):
        path = self.root / "lesson.mp4"
        video = {"hashed_id": "abc12345", "url": "https://example.test/lesson"}

        def run(command, *, check):
            self.assertFalse(check)
            if command[-1] == video["url"]:
                path.write_bytes(b"synthetic video")
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=1)

        self.process.side_effect = run
        self.assertTrue(downloader.download_with_ytdlp(video, path))
        self.assertEqual(self.process.call_count, 2)
        first, second = self.process.call_args_list
        self.assertEqual(first.args[0][-1], "https://fast.wistia.com/embed/iframe/abc12345")
        self.assertEqual(second.args[0][-3:], ["--cookies", str(downloader.COOKIES_TXT), video["url"]])

    def test_ytdlp_failed_attempts_still_return_false(self):
        self.process.side_effect = None
        self.process.return_value = SimpleNamespace(returncode=1)
        self.assertFalse(downloader.download_with_ytdlp(
            {"hashed_id": "abc12345", "url": "https://example.test/lesson"},
            self.root / "lesson.mp4",
        ))
        self.assertEqual(self.process.call_count, 2)

    def test_regex_flags_preserve_case_and_multiline_matching(self):
        self.assertEqual(downloader.WISTIA_RE.search("WISTIA_ASYNC_abc12345").group(1), "abc12345")
        self.assertEqual(downloader._text(r"<h1>(.*?)</h1>", "<H1>First\nSecond</H1>"), "First\nSecond")
        page = (
            '<A class="downloads-link" href="https://example.test/sample.pdf">\n'
            '<DIV class="media-body">Sample\n PDF</DIV></A>'
        )
        self.assertEqual(downloader._extract_attachments(page), [
            {"url": "https://example.test/sample.pdf", "name": "Sample PDF"},
        ])


if __name__ == "__main__":
    unittest.main()
