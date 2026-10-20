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
