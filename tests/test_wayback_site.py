import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import wayback_site as w  # noqa: E402

BASE = "http://example.com"
HEADER = ["timestamp", "original", "mimetype", "statuscode"]


def run_main(argv):
    """Запускает main(), возвращает (код выхода, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            w.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class PathTests(unittest.TestCase):
    def test_directories_and_extensionless_urls_become_index(self):
        expected = os.path.join("out", "a", "index.html")
        self.assertEqual(w.local_path(BASE + "/a/", "out"), expected)
        self.assertEqual(w.local_path(BASE + "/a", "out"), expected)
        self.assertEqual(w.local_path(BASE + "/", "out"), os.path.join("out", "index.html"))

    def test_regular_file(self):
        self.assertEqual(w.local_path(BASE + "/a/b.php", "out"), os.path.join("out", "a", "b.php"))

    def test_query_is_kept_in_file_name(self):
        self.assertEqual(w.local_path(BASE + "/page.php?id=3", "out"),
                         os.path.join("out", "page__id_3.php"))

    def test_windows_unsafe_characters(self):
        self.assertEqual(w.local_path(BASE + "/a/b:c*.html", "out"), os.path.join("out", "a", "b_c_.html"))
        self.assertEqual(w.local_path(BASE + "/a/x./y.html", "out"), os.path.join("out", "a", "x", "y.html"))

    def test_long_name_is_shortened_and_keeps_extension(self):
        path = w.local_path(BASE + "/" + "a" * 300 + ".html", "out")
        name = os.path.basename(path)
        self.assertLessEqual(len(name), 200)
        self.assertTrue(name.endswith(".html"))

    def test_cannot_escape_output_folder(self):
        path = w.local_path(BASE + "/a/../../etc/passwd.html", "out")
        self.assertTrue(path.startswith("out" + os.sep))
        self.assertNotIn("..", path.split(os.sep))


class UrlTests(unittest.TestCase):
    def test_normalize_host(self):
        self.assertEqual(w.normalize_host("example.com"), "example.com")
        self.assertEqual(w.normalize_host("https://WWW.Example.com/path?x=1"), "example.com")
        self.assertEqual(w.normalize_host("Пример.РФ"), "xn--e1afmkfd.xn--p1ai")

    def test_pick_latest_merges_www_and_scheme(self):
        rows = [("20200101000000", "http://example.com/a.php", "text/html"),
                ("20240101000000", "https://www.example.com/a.php", "text/html"),
                ("20220101000000", "http://example.com/b.php", "text/html")]
        latest = w.pick_latest(rows)
        self.assertEqual(len(latest), 2)
        self.assertEqual(latest["example.com/a.php"][0], "20240101000000")

    def test_host_regex_rewrites_only_own_host(self):
        rx = w.host_regex("example.com")
        html = (b'<a href="http://example.com/a.php">1</a> <img src="//www.example.com/i.png"> '
                b'<a href="https://example.com:8080/x">2</a> <a href="http://notexample.com/">3</a> '
                b'<a href="http://example.com.evil.net/">4</a> <a href="http://sub.example.com/">5</a>')
        result = rx.sub(b"", html)
        self.assertIn(b'href="/a.php"', result)
        self.assertIn(b'src="/i.png"', result)
        self.assertIn(b'href="/x"', result)
        self.assertIn(b"http://notexample.com/", result)
        self.assertIn(b"http://example.com.evil.net/", result)
        self.assertIn(b"http://sub.example.com/", result)


class CdxTests(unittest.TestCase):
    def setUp(self):
        w.LAST_ERROR = ""

    def test_status_filter_revisits_and_pagination(self):
        page1 = [HEADER,
                 ["20240101000000", BASE + "/a.php", "text/html", "200"],
                 ["20240102000000", BASE + "/img/x.png", "warc/revisit", "-"],
                 ["20240103000000", BASE + "/gone.php", "text/html", "404"],
                 ["20240104000000", BASE + "/moved.php", "text/html", "301"],
                 ["20240105000000", BASE + "/odd", "unk", "-"],
                 [], ["RESUMEKEY"]]
        page2 = [HEADER, ["20240106000000", BASE + "/b.png", "image/png", "200"]]
        calls = []

        def fake(url, retries=5, timeout=60):
            calls.append(url)
            return json.dumps(page2 if "resumeKey" in url else page1).encode()

        with mock.patch.object(w, "http_get", fake), contextlib.redirect_stderr(io.StringIO()):
            rows = w.fetch_cdx("example.com", "20250101")
        self.assertEqual([r[1] for r in rows], [BASE + "/a.php", BASE + "/img/x.png", BASE + "/b.png"])
        self.assertEqual(len(calls), 2)
        self.assertIn("to=20250101", calls[0])
        self.assertIn("url=example.com%2F%2A", calls[0])

    def test_falls_back_to_simple_query(self):
        legacy = [["timestamp", "original", "mimetype"], ["20240101000000", BASE + "/a.php", "text/html"]]

        def fake(url, retries=5, timeout=60):
            if "filter=statuscode" in url:
                return json.dumps(legacy).encode()
            w.LAST_ERROR = "HTTP 429"
            return None

        with mock.patch.object(w, "http_get", fake), contextlib.redirect_stderr(io.StringIO()) as err:
            rows = w.fetch_cdx("example.com", None)
        self.assertEqual(rows, [("20240101000000", BASE + "/a.php", "text/html")])
        self.assertIn("HTTP 429", err.getvalue())

    def test_failure_returns_none(self):
        with mock.patch.object(w, "http_get", lambda *a, **k: None), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(w.fetch_cdx("example.com", None))

    def test_non_json_answer_is_a_failure(self):
        with mock.patch.object(w, "http_get", lambda *a, **k: b"<html>busy</html>"):
            self.assertIsNone(w.cdx_query("example.com", None, legacy=False))
        self.assertIn("JSON", w.LAST_ERROR)


class CharsetTests(unittest.TestCase):
    def test_detect_charset(self):
        with tempfile.TemporaryDirectory() as d:
            utf8 = os.path.join(d, "u.html")
            cp = os.path.join(d, "c.html")
            ascii_ = os.path.join(d, "a.html")
            with open(utf8, "wb") as f:
                f.write("Привет".encode("utf-8"))
            with open(cp, "wb") as f:
                f.write("Привет".encode("cp1251"))
            with open(ascii_, "wb") as f:
                f.write(b"hello")
            self.assertEqual(w.detect_charset(utf8), "utf-8")
            self.assertEqual(w.detect_charset(cp), "windows-1251")
            self.assertEqual(w.detect_charset(ascii_), "utf-8")
            self.assertEqual(w.detect_charset(os.path.join(d, "missing.html")), "utf-8")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        wrong_meta = '<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
        with open(os.path.join(d, "utf8.php"), "wb") as f:
            f.write((wrong_meta + "<p>Привет</p>").encode("utf-8"))
        with open(os.path.join(d, "old.html"), "wb") as f:
            f.write("<p>Привет</p>".encode("cp1251"))
        with open(os.path.join(d, "koi.html"), "wb") as f:
            f.write("<p>Привет</p>".encode("koi8-r"))
        with open(os.path.join(d, "pic.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, name, legacy="windows-1251"):
        srv = w.create_server(self.tmp.name, 0, legacy)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/{name}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # без прокси из окружения
            with opener.open(url, timeout=10) as r:
                return r.headers, r.read()
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(5)

    def test_utf8_page_with_wrong_meta_is_served_as_utf8(self):
        headers, _ = self.request("utf8.php")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_legacy_page_uses_fallback_encoding(self):
        headers, _ = self.request("old.html")
        self.assertEqual(headers["Content-Type"], "text/html; charset=windows-1251")

    def test_custom_fallback_encoding(self):
        headers, _ = self.request("koi.html", legacy="koi8-r")
        self.assertEqual(headers["Content-Type"], "text/html; charset=koi8-r")

    def test_binary_files_are_untouched(self):
        headers, body = self.request("pic.png")
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))


class MainTests(unittest.TestCase):
    ROWS = [HEADER,
            ["20240101000000", BASE + "/", "text/html", "200"],
            ["20240101000000", BASE + "/docs/a.php", "text/html", "200"],
            ["20240101000000", BASE + "/docs/big.zip", "application/zip", "200"],
            ["20240102000000", BASE + "/img/x.png", "warc/revisit", "-"],
            ["20240103000000", BASE + "/gone.html", "text/html", "404"]]

    def setUp(self):
        w.LAST_ERROR = ""
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "copy")

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, cdx_ok=True):
        def fake(url, retries=5, timeout=60):
            if "cdx/search/cdx" in url:
                if not cdx_ok:
                    w.LAST_ERROR = "HTTP 503"
                    return None
                return json.dumps(self.ROWS).encode()
            if url.endswith("/docs/a.php"):
                return '<a href="http://example.com/docs/b.php">б</a>'.encode("utf-8")
            if url.endswith("/img/x.png"):
                return b"\x89PNG"
            return b"<html></html>"
        return mock.patch.object(w, "http_get", fake)

    def args(self, *extra):
        return ["example.com", "--out", self.out, "--delay", "0", "--workers", "1", *extra]

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.out).replace(os.sep, "/")
                      for d, _, fs in os.walk(self.out) for f in fs)

    def test_download_rewrites_links_and_skips_bad_records(self):
        with self.archive():
            code, out, _ = run_main(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), ["_cdx_latest.json", "docs/a.php", "docs/big.zip",
                                        "img/x.png", "index.html"])
        with open(os.path.join(self.out, "docs", "a.php"), encoding="utf-8") as f:
            self.assertIn('href="/docs/b.php"', f.read())
        self.assertIn("К скачиванию: 4 файлов", out)

    def test_include_and_exclude(self):
        with self.archive():
            code, _, _ = run_main(self.args("--include", "^/docs/", "--exclude", r"\.zip$"))
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), ["_cdx_latest.json", "docs/a.php"])

    def test_second_run_skips_and_falls_back_to_saved_list(self):
        with self.archive():
            run_main(self.args())
        with self.archive(cdx_ok=False):
            code, out, err = run_main(self.args())
        self.assertEqual(code, 0)
        self.assertIn("сохранённый в прошлый раз", err)
        self.assertIn("ok=0 skip=4 fail=0", out)

    def test_archive_down_without_cache_reports_reason(self):
        with self.archive(cdx_ok=False):
            code, _, _ = run_main(self.args())
        self.assertIn("HTTP 503", str(code))

    def test_list_shows_sections(self):
        with self.archive():
            code, out, _ = run_main(["example.com", "--out", self.out, "--list"])
        self.assertEqual(code, 0)
        self.assertIn("/docs/", out)
        self.assertFalse(os.path.exists(self.out))

    def test_ctrl_c_stops_quickly_and_leaves_resumable_state(self):
        with self.archive(), mock.patch.object(w, "download", side_effect=KeyboardInterrupt):
            code, out, _ = run_main(self.args())
        self.assertEqual(code, 130)
        self.assertIn("Остановлено", out)

    def test_domain_is_required_for_download(self):
        code, _, _ = run_main(["--out", self.out])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
