"""Сбор контента из интернета: DuckDuckGo поиск + RSS + извлечение статей."""
import html
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import aiohttp

from config import MEDIA_DIR, WEB_TIMEOUT, WEB_USER_AGENT

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=WEB_TIMEOUT)
_HEADERS = {"User-Agent": WEB_USER_AGENT}


async def fetch_text(url: str, timeout: int | None = None) -> str:
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    return ""
                return await r.text(errors="ignore")
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось загрузить %s: %s", url, e)
        return ""


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._href = attrs.get("href")
            self._anchor = []
        elif tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip > 0:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            text = " ".join("".join(self._anchor).split())
            if self._href and text:
                self.links.append((self._href, text))
            self._href = None

    def handle_data(self, data):
        if self._skip == 0 and self._href is not None:
            self._anchor.append(data)


def _clean_snippet(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


async def search_ddg(query: str, max_results: int = 8) -> list[dict]:
    """Ищет в DuckDuckGo (HTML). Возвращает [{title, url, snippet}]."""
    q = urllib.parse.quote_plus(query)
    page = await fetch_text(f"https://html.duckduckgo.com/html/?q={q}")
    if not page:
        return []
    results: list[dict] = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        url, title = m.group(1), _clean_snippet(m.group(2))
        if url.startswith("//"):
            url = "https:" + url
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= max_results:
            break
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', page, re.S)
    for i, s in enumerate(snips[:len(results)]):
        results[i]["snippet"] = _clean_snippet(s)
    return results


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


async def parse_rss(feed_url: str, max_items: int = 10) -> list[dict]:
    """Парсит RSS/Atom фид. Возвращает [{title, url, summary, pub_date}]."""
    xml_text = await fetch_text(feed_url)
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Не удалось распарсить RSS %s: %s", feed_url, e)
        return []
    items: list[dict] = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for child in item:
            ctag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if ctag == "title":
                d["title"] = text
            elif ctag == "link":
                href = child.get("href")
                d["url"] = href or text
            elif ctag in ("description", "summary", "content"):
                d.setdefault("summary", _strip_tags(text))
            elif ctag == "pubDate":
                d["pub_date"] = text
        if d.get("title") and d.get("url"):
            d.setdefault("summary", "")
            items.append(d)
        if len(items) >= max_items:
            break
    return items


_TITLE_PAT = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_OG_IMG_PAT = re.compile(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)', re.I)
_IMG_PAT = re.compile(r'<img[^>]+src=["\']([^"\']+)', re.I)


def _clean_title(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s).strip()
    return s[:500]


async def extract_article(url: str) -> dict:
    """Достаёт заголовок + картинку(og:image) со страницы."""
    page = await fetch_text(url)
    if not page:
        return {"title": "", "image_url": ""}
    title = ""
    m = _TITLE_PAT.search(page)
    if m:
        title = _clean_title(m.group(1))
    img = ""
    m = _OG_IMG_PAT.search(page)
    if m:
        img = m.group(1)
    if not img:
        m = _IMG_PAT.search(page)
        if m:
            img = m.group(1)
    return {"title": title, "image_url": img}


async def download_image(url: str) -> str:
    """Скачивает картинку в MEDIA_DIR и возвращает путь к файлу."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    fname = f"web_{int(time.time() * 1000)}.jpg"
    path = os.path.join(MEDIA_DIR, fname)
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    return ""
                data = await r.read()
                if len(data) < 1000:
                    return ""
                with open(path, "wb") as f:
                    f.write(data)
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось скачать картинку %s: %s", url, e)
        return ""
