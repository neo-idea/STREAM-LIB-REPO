import os
import sys

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


if __name__ == "__main__":
    unittest.main()
