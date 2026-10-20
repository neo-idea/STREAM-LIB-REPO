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
        url = match.group(0).rstrip(".,;:，。；、）】」』》 ")
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            continue
        path = (parts.path or "").lower()
        if path.endswith(".json") or path.endswith(".bin") or "config" in path:
            out.append(url)
    return out


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
