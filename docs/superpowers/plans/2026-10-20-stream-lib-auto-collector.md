# stream-lib-cat-auto.json 自动搜集定时任务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每日自动从 GitHub 发现并验证 TVBox JSON 配置，生成 `stream-lib-cat-auto.json` 并提交回仓库。

**Architecture:** 一个纯标准库 Python 脚本（发现渠道 = 代码搜索 + 仓库搜索 + 种子提取；验证 = 可达 + JSON + ≥2 个 TVBox 标记；多因素打分排序），由 GitHub Actions 每日 UTC 02:17 定时运行，有变化才 commit & push。

**Tech Stack:** Python 3 标准库（urllib / json / re / concurrent.futures）、unittest、GitHub REST API（`/search/code`、`/search/repositories`、`/repos/{}/contents`）、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-10-20-stream-lib-auto-collector-design.md`

## Global Constraints

- 纯 Python 标准库，禁止第三方依赖
- 输出格式与 `stream-lib-cat.json` 一致：`{"urls": [{"url": ..., "name": ...}]}`，`ensure_ascii=False`，2 空格缩进，文件末尾换行
- 手工文件 `stream-lib-cat.json` 不得改动
- 验证通过数为 0 时保留旧文件，退出码 0
- GitHub raw 链接发布时统一加 `https://gh-proxy.com/` 前缀；验证直连原始地址
- 硬性门槛：GET 10s 超时、8MB 响应上限、合法 JSON object、含 ≥2 个内容标记（sites/spider/lives/parses/wallpaper）
- 打分：标记数(0–5) + 主题相关(+1) + round(log10(stars+1)×2) + 陈旧>1年(−3)；Top 50
- 测试命令统一为 `python3 -m unittest discover -s tests -v`（在仓库根目录执行）

---

### Task 1: URL 工具函数（归一化 / 代理前缀 / 提取）

**Files:**
- Create: `scripts/collect_stream_lib.py`
- Create: `tests/test_collect_stream_lib.py`

**Interfaces:**
- Produces:
  - `normalize_url(url: str) -> str` — 剥离 gh-proxy 类前缀、scheme/host 小写、路径去尾部斜杠、去 fragment
  - `publish_url(url: str) -> str` — raw.githubusercontent.com 链接加 `https://gh-proxy.com/` 前缀，其余原样
  - `extract_config_urls(text: str) -> list[str]` — 从文本提取「.json / .bin 结尾或路径含 config」的 http(s) 链接

- [ ] **Step 1: 写失败测试**

创建 `tests/test_collect_stream_lib.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'collect_stream_lib'`

- [ ] **Step 3: 最小实现**

创建 `scripts/collect_stream_lib.py`：

```python
#!/usr/bin/env python3
"""Collect TVBox JSON config URLs from GitHub into stream-lib-cat-auto.json.

Discovery channels:
  1. GitHub code search (ASCII keyword queries, .json files)
  2. GitHub repository search (incl. CJK keywords) + root *.json probing
  3. Seeds (scripts/seeds.json): each seed contributes its own URL plus any
     config-looking URLs extracted from its content.

Candidates are validated (reachable, JSON object, >=2 TVBox markers),
scored (markers + topic relevance + stars - staleness), deduped by
normalized URL, and the top N are written to stream-lib-cat-auto.json.
GitHub raw URLs are published behind https://gh-proxy.com/ for direct
use by TVBox clients in mainland China.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

OUTPUT_FILE = "stream-lib-cat-auto.json"
SEEDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds.json")
TOP_N = 50
FETCH_TIMEOUT = 10
MAX_BYTES = 8 * 1024 * 1024
MAX_WORKERS = 16
CONTENT_MARKERS = ("sites", "spider", "lives", "parses", "wallpaper")
TOPIC_KEYWORDS = ("tvbox", "影视", "ott", "box", "配置")
PROXY_PREFIXES = (
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://mirror.ghproxy.com/",
)
GH_PROXY_PREFIX = "https://gh-proxy.com/"
STALE_AFTER_DAYS = 365
SEARCH_SLEEP_SECONDS = 3
USER_AGENT = "stream-lib-collector/1.0"
API_BASE = "https://api.github.com"

CODE_SEARCH_QUERIES = (
    '"spider" "sites" extension:json',
    '"lives" "sites" extension:json',
    '"spider" "lives" extension:json',
    '"parses" "sites" extension:json',
    'tvbox extension:json',
)
REPO_SEARCH_QUERIES = (
    "tvbox",
    "tvbox 配置",
    "影视 接口",
    "影视配置",
)

_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]\x60]+")

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def normalize_url(url: str) -> str:
    """Strip proxy prefixes, lowercase scheme/host, drop trailing slash & fragment."""
    u = url.strip()
    for prefix in PROXY_PREFIXES:
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    parts = urllib.parse.urlsplit(u)
    host = (parts.hostname or "").lower()
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, path, parts.query, "")
    )


def publish_url(url: str) -> str:
    """Wrap raw.githubusercontent.com URLs with the gh-proxy prefix."""
    u = normalize_url(url)
    if urllib.parse.urlsplit(u).hostname == "raw.githubusercontent.com":
        return GH_PROXY_PREFIX + u
    return u


def extract_config_urls(text: str) -> list[str]:
    """Extract config-looking http(s) URLs from free text."""
    out = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:，。；、")
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            continue
        path = (parts.path or "").lower()
        if path.endswith(".json") or path.endswith(".bin") or "config" in path:
            out.append(url)
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS（11 个测试）

- [ ] **Step 5: Commit**

```bash
git add scripts/collect_stream_lib.py tests/test_collect_stream_lib.py
git commit -m "feat: add URL utilities for stream-lib collector"
```

---

### Task 2: 内容校验 / 打分 / 命名 / 输出渲染

**Files:**
- Modify: `scripts/collect_stream_lib.py`（追加函数）
- Modify: `tests/test_collect_stream_lib.py`（追加测试）

**Interfaces:**
- Consumes: Task 1 的常量 `CONTENT_MARKERS`、`TOPIC_KEYWORDS`、`STALE_AFTER_DAYS`
- Produces:
  - `parse_config_markers(raw: bytes) -> int | None` — 解析 JSON object 并返回命中的内容标记数（0–5）；解析失败或非 object 返回 `None`
  - `score_candidate(markers: int, repo_meta: dict | None) -> int` — 多因素打分
  - `build_name(repo_full_name: str, file_path: str, multi_file: bool) -> str` — 「仓库名 + 线路」，同仓库多文件时追加文件名
  - `render_output(entries: list[dict]) -> str` — 最终 JSON 文本

- [ ] **Step 1: 写失败测试**

在 `tests/test_collect_stream_lib.py` 的 `if __name__ == "__main__":` 之前追加：

```python
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
```

同时在文件顶部 import 区确认已有 `from datetime import datetime, timedelta, timezone`（Task 1 已加），测试文件顶部无需新 import。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'collect_stream_lib' has no attribute 'parse_config_markers'`

- [ ] **Step 3: 最小实现**

在 `scripts/collect_stream_lib.py` 的 `extract_config_urls` 之后追加：

```python
def parse_config_markers(raw: bytes) -> int | None:
    """Return how many TVBox content markers a JSON payload contains.

    Returns None when the payload is not a JSON object.
    """
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return sum(1 for marker in CONTENT_MARKERS if marker in data)


def score_candidate(markers: int, repo_meta: dict | None) -> int:
    """Multi-factor score: markers + topic relevance + stars - staleness."""
    score = markers
    if repo_meta:
        text = f"{repo_meta.get('name') or ''} {repo_meta.get('description') or ''}".lower()
        if any(keyword in text for keyword in TOPIC_KEYWORDS):
            score += 1
        stars = repo_meta.get("stargazers_count") or 0
        score += round(math.log10(stars + 1) * 2)
        pushed_at = repo_meta.get("pushed_at")
        if pushed_at:
            try:
                pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - pushed
                if age > timedelta(days=STALE_AFTER_DAYS):
                    score -= 3
            except ValueError:
                pass
    return score


def build_name(repo_full_name: str, file_path: str, multi_file: bool) -> str:
    """Name a discovered config: repo name + 线路 (+ file stem when repo has several)."""
    repo_name = repo_full_name.split("/")[-1]
    name = f"{repo_name}线路"
    if multi_file:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        name = f"{name}-{stem}"
    return name


def render_output(entries: list[dict]) -> str:
    """Render the final stream-lib-cat-auto.json content."""
    return json.dumps({"urls": entries}, ensure_ascii=False, indent=2) + "\n"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS（21 个测试）

- [ ] **Step 5: Commit**

```bash
git add scripts/collect_stream_lib.py tests/test_collect_stream_lib.py
git commit -m "feat: add validation, scoring, naming and rendering for collector"
```

---

### Task 3: 网络层 + 发现渠道 + main 组装 + seeds.json

**Files:**
- Modify: `scripts/collect_stream_lib.py`（追加网络与主流程）
- Modify: `tests/test_collect_stream_lib.py`（追加测试）
- Create: `scripts/seeds.json`

**Interfaces:**
- Consumes: Task 1 的 `normalize_url` / `extract_config_urls`、Task 2 的 `parse_config_markers` / `score_candidate` / `build_name` / `render_output`、常量 `CODE_SEARCH_QUERIES` / `REPO_SEARCH_QUERIES` / `SEEDS_FILE` 等
- Produces:
  - `fetch(url: str) -> bytes | None` — 带 UA、10s 超时、8MB 上限的 GET，失败返回 None
  - `github_api(path: str, params: dict | None, token: str | None) -> dict | list | None` — GitHub REST 调用，403/429 退避重试 1 次
  - `load_seeds() -> list[dict]` — 读取 `scripts/seeds.json` 的 `seeds` 数组
  - `validate_candidates(candidates: dict) -> list[tuple[str, int]]` — 并发验证，返回 `(normalized_url, markers)` 列表
  - `main() -> int` — 组装全流程，返回进程退出码

- [ ] **Step 1: 写失败测试**

在 `tests/test_collect_stream_lib.py` 的 `if __name__ == "__main__":` 之前追加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'collect_stream_lib' has no attribute 'load_seeds'`

- [ ] **Step 3: 实现**

创建 `scripts/seeds.json`：

```json
{
  "seeds": [
    {
      "url": "https://gh-proxy.com/https://raw.githubusercontent.com/xmbjm/svip/refs/heads/main/svip.json",
      "name": "svip线路",
      "comment": "已知配置。种子自身作为候选，并从内容中提取其它配置链接。可自由增删 seed：url 必填，name 可选（作为该 url 的固定线路名）。"
    }
  ]
}
```

在 `scripts/collect_stream_lib.py` 的 `render_output` 之后追加：

```python
def fetch(url: str) -> bytes | None:
    """GET a URL with timeout and a size cap. Returns None on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT, context=_SSL_CTX) as resp:
            data = resp.read(MAX_BYTES + 1)
            return data if len(data) <= MAX_BYTES else None
    except Exception:
        return None


def github_api(path: str, params: dict | None = None, token: str | None = None):
    """Call the GitHub REST API. Returns parsed JSON or None. One retry on rate limit."""
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in (1, 2):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt == 1:
                time.sleep(10)
                continue
            return None
        except Exception:
            if attempt == 1:
                time.sleep(3)
                continue
            return None
    return None


def load_seeds() -> list[dict]:
    """Load seed sources from scripts/seeds.json; tolerate a missing/broken file."""
    try:
        with open(SEEDS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("seeds", [])
    except (OSError, json.JSONDecodeError):
        return []


def discover_seeds() -> tuple[dict, dict]:
    """Candidates from seeds: each seed URL itself plus URLs extracted from its content."""
    candidates: dict = {}
    names: dict = {}
    for seed in load_seeds():
        url = seed.get("url")
        if not url:
            continue
        key = normalize_url(url)
        candidates[key] = {"url": url, "repo": None, "path": None, "repo_meta": None}
        if seed.get("name"):
            names[key] = seed["name"]
        content = fetch(url)
        if not content:
            continue
        text = content.decode("utf-8", errors="ignore")
        for found in extract_config_urls(text):
            found_key = normalize_url(found)
            if found_key not in candidates:
                candidates[found_key] = {
                    "url": found, "repo": None, "path": None, "repo_meta": None,
                }
    return candidates, names


def discover_code_search(token: str) -> dict:
    """Find TVBox-looking .json files via the GitHub code search API."""
    candidates: dict = {}
    for query in CODE_SEARCH_QUERIES:
        data = github_api("/search/code", {"q": query, "per_page": 30}, token)
        time.sleep(SEARCH_SLEEP_SECONDS)
        if not isinstance(data, dict):
            continue
        for item in data.get("items", []):
            repo = item.get("repository") or {}
            full_name = repo.get("full_name")
            path = item.get("path", "")
            if not full_name or not path.endswith(".json"):
                continue
            url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{path}"
            candidates[normalize_url(url)] = {
                "url": url, "repo": full_name, "path": path, "repo_meta": repo,
            }
    return candidates


def discover_repo_search(token: str) -> dict:
    """Find candidate repos (CJK-friendly) and probe their root .json files."""
    candidates: dict = {}
    repos: dict = {}
    for query in REPO_SEARCH_QUERIES:
        data = github_api(
            "/search/repositories", {"q": query, "sort": "updated", "per_page": 10}, token
        )
        time.sleep(SEARCH_SLEEP_SECONDS)
        if not isinstance(data, dict):
            continue
        for repo in data.get("items", []):
            full_name = repo.get("full_name")
            if full_name:
                repos[full_name] = repo
    for full_name, repo in repos.items():
        contents = github_api(f"/repos/{full_name}/contents", None, token)
        if not isinstance(contents, list):
            continue
        for item in contents:
            name = item.get("name", "")
            size = item.get("size") or 0
            if item.get("type") == "file" and name.endswith(".json") and size <= MAX_BYTES:
                url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{name}"
                candidates[normalize_url(url)] = {
                    "url": url, "repo": full_name, "path": name, "repo_meta": repo,
                }
    return candidates


def validate_candidates(candidates: dict) -> list[tuple[str, int]]:
    """Concurrently validate candidates; return [(normalized_url, markers)] for valid ones."""
    results: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch, cand["url"]): key
            for key, cand in candidates.items()
        }
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            raw = future.result()
            if raw is None:
                continue
            markers = parse_config_markers(raw)
            if markers is None or markers < 2:
                continue
            results.append((key, markers))
    return results


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    all_candidates: dict = {}
    seed_names: dict = {}
    try:
        seed_candidates, seed_names = discover_seeds()
        all_candidates.update(seed_candidates)
    except Exception as exc:
        print(f"[warn] seed discovery failed: {exc}")
    if token:
        try:
            all_candidates.update(discover_code_search(token))
        except Exception as exc:
            print(f"[warn] code search failed: {exc}")
        try:
            all_candidates.update(discover_repo_search(token))
        except Exception as exc:
            print(f"[warn] repo search failed: {exc}")
    else:
        print("[warn] no GH_TOKEN/GITHUB_TOKEN in env; search APIs skipped")
    print(f"[info] {len(all_candidates)} candidates collected")

    valid = validate_candidates(all_candidates)
    if not valid:
        print("[warn] no valid configs found; keeping existing file")
        return 0

    repo_file_count: dict = {}
    for key, _ in valid:
        repo = all_candidates[key].get("repo")
        if repo:
            repo_file_count[repo] = repo_file_count.get(repo, 0) + 1

    entries = []
    for key, markers in valid:
        cand = all_candidates[key]
        if key in seed_names:
            name = seed_names[key]
        elif cand.get("repo"):
            name = build_name(
                cand["repo"],
                cand.get("path") or "",
                repo_file_count.get(cand["repo"], 1) > 1,
            )
        else:
            name = urllib.parse.urlsplit(key).hostname or key
        entries.append({
            "url": publish_url(cand["url"]),
            "name": name,
            "_score": score_candidate(markers, cand.get("repo_meta")),
        })
    entries.sort(key=lambda e: (-e["_score"], e["name"]))
    for entry in entries:
        del entry["_score"]
    entries = entries[:TOP_N]

    output = render_output(entries)
    print(f"[info] {len(entries)} valid configs after scoring")
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as handle:
            existing = handle.read()
    except OSError:
        existing = None
    if existing == output:
        print("[info] output unchanged")
        return 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
        handle.write(output)
    print(f"[info] wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS（23 个测试）

- [ ] **Step 5: Commit**

```bash
git add scripts/collect_stream_lib.py scripts/seeds.json tests/test_collect_stream_lib.py
git commit -m "feat: add discovery channels, validation pipeline and main entrypoint"
```

---

### Task 4: 本地端到端验证 + GitHub Actions 定时任务

**Files:**
- Create: `.github/workflows/collect-stream-lib.yml`
- Create（脚本产物）: `stream-lib-cat-auto.json`

**Interfaces:**
- Consumes: Task 3 的 `scripts/collect_stream_lib.py`（`GH_TOKEN` 环境变量驱动搜索）
- Produces: 每日自动更新 `stream-lib-cat-auto.json` 的云端定时任务

- [ ] **Step 1: 本机端到端运行（无 token，只验证种子链路 + 防清空）**

Run: `python3 scripts/collect_stream_lib.py`
Expected: 输出 `[warn] no GH_TOKEN/GITHUB_TOKEN ...`、`[info] N candidates collected`、`[info] wrote stream-lib-cat-auto.json`（至少包含 svip 线路）；退出码 0

- [ ] **Step 2: 校验产物格式**

Run: `python3 -c "import json; d=json.load(open('stream-lib-cat-auto.json')); print(len(d['urls']), d['urls'][0])"`
Expected: 数字 ≥1，且元素含 `url`/`name` 键；`git status` 显示 `stream-lib-cat.json` 未被改动

- [ ] **Step 3: 本机带 token 全链路（可选，若有 GitHub token）**

Run: `GH_TOKEN=<token> python3 scripts/collect_stream_lib.py`
Expected: 候选数明显增多（数十到数百），产出条目含搜索发现的线路

- [ ] **Step 4: 创建 workflow**

创建 `.github/workflows/collect-stream-lib.yml`：

```yaml
name: Collect stream-lib configs

on:
  schedule:
    - cron: "17 2 * * *" # daily 02:17 UTC = 10:17 Beijing
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: collect-stream-lib
  cancel-in-progress: false

jobs:
  collect:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run collector
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: python3 scripts/collect_stream_lib.py

      - name: Commit and push if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add stream-lib-cat-auto.json
          if git diff --cached --quiet; then
            echo "No changes to commit"
          else
            git commit -m "chore(auto): update stream-lib-cat-auto.json"
            git push
          fi
```

- [ ] **Step 5: 运行全部单元测试**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS（23 个测试）

- [ ] **Step 6: Commit 并推送**

```bash
git add .github/workflows/collect-stream-lib.yml stream-lib-cat-auto.json
git commit -m "feat: add daily GitHub Actions workflow for stream-lib collection"
git push
```

- [ ] **Step 7: 手动触发云端验证**

浏览器打开 `https://github.com/talentneo/STREAM-LIB-REPO/actions/workflows/collect-stream-lib.yml` → 点击 "Run workflow" → 等待运行完成（绿色）→ 确认远端 `stream-lib-cat-auto.json` 有搜索发现的线路。

---

## Self-Review 结论

- **Spec 覆盖**：发现渠道三路（Task 3）、多因素硬门槛与打分（Task 2）、去重/gh-proxy/Top 50（Task 1+3）、防清空（Task 3 main）、workflow 每日 UTC 02:17 + 手动触发（Task 4）、不动手工文件（Task 4 Step 2 显式验证）——全部有对应任务
- **占位符**：无 TBD/TODO，所有步骤含完整代码与命令
- **类型一致性**：`validate_candidates` 返回 `list[tuple[str, int]]` 与 main 中用法一致；`discover_seeds` 返回 `(dict, dict)` 与 main 解包一致；`build_name(repo_full_name: str, file_path: str, multi_file: bool)` 定义与调用一致
