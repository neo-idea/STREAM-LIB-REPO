import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import unittest

import collect_stream_lib as csl


class TestNormalizeUrl(unittest.TestCase):
    def test_strips_gh_proxy_prefix(self):
        self.assertEqual(
            csl.normalize_url(
                "https://gh-proxy.com/https://raw.githubusercontent.com/xmbjm/svip/refs/heads/main/svip.json"
            ),
            "https://raw.githubusercontent.com/xmbjm/svip/refs/heads/main/svip.json",
        )

    def test_lowercases_host_and_strips_trailing_slash(self):
        self.assertEqual(
            csl.normalize_url("HTTPS://Example.COM/path/"),
            "https://example.com/path",
        )

    def test_root_urls_normalize_equal(self):
        self.assertEqual(
            csl.normalize_url("http://feimao.pro"),
            csl.normalize_url("http://feimao.pro/"),
        )

    def test_drops_fragment_keeps_query(self):
        self.assertEqual(
            csl.normalize_url("https://a.com/c.json?v=1#frag"),
            "https://a.com/c.json?v=1",
        )


class TestPublishUrl(unittest.TestCase):
    def test_wraps_raw_githubusercontent(self):
        self.assertEqual(
            csl.publish_url("https://raw.githubusercontent.com/xmbjm/svip/refs/heads/main/svip.json"),
            "https://gh-proxy.com/https://raw.githubusercontent.com/xmbjm/svip/refs/heads/main/svip.json",
        )

    def test_does_not_wrap_other_hosts(self):
        self.assertEqual(
            csl.publish_url("http://feimao.pro/fmys.json"),
            "http://feimao.pro/fmys.json",
        )

    def test_does_not_double_wrap(self):
        url = "https://gh-proxy.com/https://raw.githubusercontent.com/a/b/HEAD/c.json"
        self.assertEqual(csl.publish_url(url), url)


class TestExtractConfigUrls(unittest.TestCase):
    def test_extracts_json_bin_and_config(self):
        text = (
            "线路1 https://a.com/c.json 线路2 http://b.com/d.bin "
            "线路3 https://e.com/getconfig 线路4 https://f.com/ignore.txt"
        )
        self.assertEqual(
            csl.extract_config_urls(text),
            ["https://a.com/c.json", "http://b.com/d.bin", "https://e.com/getconfig"],
        )

    def test_ignores_plain_domains_and_emails(self):
        self.assertEqual(csl.extract_config_urls("see abc.com or mail@x.com"), [])

    def test_stops_at_delimiters(self):
        text = "（https://a.com/c.json）"
        self.assertEqual(csl.extract_config_urls(text), ["https://a.com/c.json"])


class TestParseConfigMarkers(unittest.TestCase):
    def test_full_config(self):
        raw = b'{"sites": [], "spider": "x", "lives": [], "parses": [], "wallpaper": "w"}'
        self.assertEqual(csl.parse_config_markers(raw), 5)

    def test_two_markers_pass_threshold_caller_side(self):
        raw = b'{"sites": [], "lives": []}'
        self.assertEqual(csl.parse_config_markers(raw), 2)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(csl.parse_config_markers(b"not json"))

    def test_non_object_returns_none(self):
        self.assertIsNone(csl.parse_config_markers(b"[1, 2]"))

    def test_tolerates_bom(self):
        raw = b'\xef\xbb\xbf{"sites": [], "spider": "x"}'
        self.assertEqual(csl.parse_config_markers(raw), 2)


class TestScoreCandidate(unittest.TestCase):
    def test_markers_only(self):
        self.assertEqual(csl.score_candidate(3, None), 3)

    def test_topic_bonus(self):
        meta = {"name": "Awesome-TVBox", "description": "", "stargazers_count": 0,
                "pushed_at": datetime.now(timezone.utc).isoformat()}
        self.assertEqual(csl.score_candidate(2, meta), 3)  # 2 + topic

    def test_stars_bonus(self):
        meta = {"name": "r", "description": "", "stargazers_count": 99,
                "pushed_at": datetime.now(timezone.utc).isoformat()}
        # 1 marker + no topic + round(log10(100)*2) = 1 + 4
        self.assertEqual(csl.score_candidate(1, meta), 5)

    def test_stale_penalty(self):
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        meta = {"name": "r", "description": "", "stargazers_count": 0, "pushed_at": old}
        self.assertEqual(csl.score_candidate(2, meta), -1)  # 2 - 3, no topic

    def test_repo_without_pushed_at_is_not_penalized(self):
        meta = {"name": "r", "description": "", "stargazers_count": 0}
        self.assertEqual(csl.score_candidate(2, meta), 2)


class TestBuildName(unittest.TestCase):
    def test_single_file(self):
        self.assertEqual(csl.build_name("xmbjm/svip", "svip.json", False), "svip线路")

    def test_multi_file_appends_stem(self):
        self.assertEqual(csl.build_name("a/b", "config.json", True), "b线路-config")


class TestRenderOutput(unittest.TestCase):
    def test_format_matches_manual_file(self):
        text = csl.render_output([{"url": "http://fmys.top/fmys.json", "name": "驸马线路"}])
        self.assertEqual(
            text,
            '{\n  "urls": [\n    {\n      "url": "http://fmys.top/fmys.json",\n'
            '      "name": "驸马线路"\n    }\n  ]\n}\n',
        )


class TestLoadSeeds(unittest.TestCase):
    def test_reads_seeds_file(self):
        seeds = csl.load_seeds()
        self.assertIsInstance(seeds, list)
        self.assertTrue(all("url" in s for s in seeds))


class TestValidateCandidates(unittest.TestCase):
    def test_validates_and_skips_failures(self):
        good = b'{"sites": [], "spider": "x"}'
        candidates = {
            "https://a.com/good.json": {"url": "https://a.com/good.json"},
            "https://a.com/bad.json": {"url": "https://a.com/bad.json"},
        }
        original = csl.fetch
        try:
            csl.fetch = lambda url: good if "good" in url else None
            results = csl.validate_candidates(candidates)
        finally:
            csl.fetch = original
        self.assertEqual(results, [("https://a.com/good.json", 2)])


if __name__ == "__main__":
    unittest.main()
