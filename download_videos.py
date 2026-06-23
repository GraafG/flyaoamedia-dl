"""
FlyAOA Media training downloader.

training.flyaoamedia.com is a Kajabi site that serves course lessons with
embedded Wistia videos. This tool:

1. Logs in with a plain Rails form POST (no browser automation needed).
2. Crawls the member library to discover products (courses) and their lessons.
3. Extracts the Wistia hashed id from each lesson page.
4. Downloads each video with yt-dlp.

Videos are organised per course:

    downloads/<Course Name>/<NN> - <Lesson Title>.mp4

Kodi/Jellyfin .nfo metadata and .jpg poster art are written alongside each video.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv()

# Make console output safe on Windows terminals that default to cp1252.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_URL = os.getenv("FLYAOA_BASE_URL", "https://training.flyaoamedia.com").rstrip("/")
LOGIN_URL = f"{BASE_URL}/login"
LIBRARY_URL = f"{BASE_URL}/library"

ROOT_DIR = Path(__file__).parent
USERNAME = os.getenv("FLYAOA_EMAIL", "")
PASSWORD = os.getenv("FLYAOA_PASSWORD", "")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", ROOT_DIR / "downloads"))
COOKIES_TXT = ROOT_DIR / "cookies.txt"
METADATA_FILE = ROOT_DIR / "metadata.json"
LINKS_FILE = ROOT_DIR / "video_links.txt"
FRAGMENT_CONCURRENCY = os.getenv("YTDLP_CONCURRENT_FRAGMENTS", "16")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

WISTIA_RE = re.compile(r"wistia_async_([a-z0-9]{8,})", re.I)
WISTIA_EMBED_RE = re.compile(r"(?:fast\.wistia\.(?:com|net)/embed/(?:iframe|medias)/|wvideo=)([a-z0-9]{8,})", re.I)
POST_HREF_RE = re.compile(r'href="([^"#?]*?/posts/[^"#?]+)"', re.I)
PRODUCT_HREF_RE = re.compile(r'href="([^"#?]*?/(?:library/)?products/[^"#?]+)"', re.I)
CATEGORY_HREF_RE = re.compile(r'href="([^"#?]*?/categories/[^"#?]+)"', re.I)
CATEGORY_LINK_RE = re.compile(
    r'<a[^>]+href="[^"]+/categories/(\d+)"[^>]*>(.*?)</a>', re.I | re.S
)
POST_CATEGORY_RE = re.compile(r"/categories/(\d+)/posts/")
PRODUCT_SLUG_RE = re.compile(r"/products/([^/]+)")


def session_factory():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _csrf_token(page_html):
    m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', page_html)
    if not m:
        m = re.search(r'name="csrf-token" content="([^"]+)"', page_html)
    return m.group(1) if m else None


def login(session):
    """Log into the Kajabi site. Returns True on success."""
    print("[*] Loading login page...")
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    token = _csrf_token(r.text)
    if not token:
        raise RuntimeError("Could not find the CSRF token on the login page.")

    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "Credentials are not configured. Set FLYAOA_EMAIL and FLYAOA_PASSWORD in .env."
        )

    print("[*] Submitting credentials...")
    payload = {
        "utf8": "\u2713",
        "authenticity_token": token,
        "member[email]": USERNAME,
        "member[password]": PASSWORD,
        "member[remember_me]": "1",
        "commit": "Submit",
    }
    r = session.post(LOGIN_URL, data=payload, headers={"Referer": LOGIN_URL}, timeout=30)
    r.raise_for_status()

    if "Invalid email or password" in r.text or (
        r.url.rstrip("/").endswith("/login") and "member[password]" in r.text
    ):
        print("[!] Login failed - check FLYAOA_EMAIL / FLYAOA_PASSWORD.")
        return False

    print("[*] Login successful!")
    return True


def _internal(url):
    """Normalise a possibly relative href to an absolute same-site URL, or None."""
    if not url:
        return None
    url = html.unescape(url)
    abs_url = urllib.parse.urljoin(BASE_URL + "/", url)
    parts = urllib.parse.urlparse(abs_url)
    base = urllib.parse.urlparse(BASE_URL)
    if parts.netloc != base.netloc:
        return None
    return urllib.parse.urldefrag(abs_url)[0]


def discover_products(session):
    """Return an ordered, de-duplicated list of product (course) URLs from the library."""
    products = []
    seen = set()
    for seed in (LIBRARY_URL, BASE_URL + "/"):
        try:
            r = session.get(seed, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[!] Could not load {seed}: {exc}")
            continue
        for href in PRODUCT_HREF_RE.findall(r.text):
            url = _internal(href)
            # Keep product landing pages, skip deep links (posts/categories handled later).
            if not url or "/posts/" in url or "/categories/" in url:
                continue
            if url not in seen:
                seen.add(url)
                products.append(url)
    print(f"[*] Found {len(products)} product(s).")
    return products


def discover_product_meta(session, product_url):
    """Return (product_title, {category_id: category_name}) for a product."""
    try:
        r = session.get(product_url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[!] Could not load {product_url}: {exc}")
        return "", {}
    page = r.text
    title = (
        _text(r'<h1[^>]*class="[^"]*(?:product|mini-dashboard)[^"]*title[^"]*"[^>]*>(.*?)</h1>', page)
        or _text(r"<h1[^>]*>(.*?)</h1>", page)
    )
    title = re.sub(r"<[^>]+>", "", title).strip()
    modules = {}
    for cid, label in CATEGORY_LINK_RE.findall(page):
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label)).strip()
        if name and cid not in modules:
            modules[cid] = name
    return title, modules


def discover_posts(session, product_url):
    """Crawl a product (and its category pages) and return ordered, unique lesson URLs."""
    posts = []
    seen = set()
    to_visit = [product_url]
    visited = set()

    while to_visit:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[!] Could not load {url}: {exc}")
            continue

        for href in POST_HREF_RE.findall(r.text):
            post = _internal(href)
            if post and post not in seen:
                seen.add(post)
                posts.append(post)

        # Follow category pages belonging to this product to reach more lessons.
        for href in CATEGORY_HREF_RE.findall(r.text):
            cat = _internal(href)
            if cat and cat not in visited and cat not in to_visit:
                to_visit.append(cat)

    return posts


def _text(pattern, page_html, default=""):
    m = re.search(pattern, page_html, re.I | re.S)
    return html.unescape(m.group(1)).strip() if m else default


def _strip_html(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def parse_post(session, url):
    """Fetch a lesson page and extract its title, description and Wistia hashed id."""
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[!] Could not load {url}: {exc}")
        return None

    page = r.text
    match = WISTIA_RE.search(page) or WISTIA_EMBED_RE.search(page)
    hashed_id = match.group(1) if match else None

    title = (
        _text(r'<h1[^>]*class="[^"]*post-body-title[^"]*"[^>]*>(.*?)</h1>', page)
        or _text(r'<h1[^>]*class="[^"]*(?:post|lesson)[^"]*title[^"]*"[^>]*>(.*?)</h1>', page)
        or _text(r"<h1[^>]*>(.*?)</h1>", page)
        or _text(r"<title>(.*?)</title>", page)
    )
    title = _strip_html(title)

    body_match = re.search(r'<div[^>]*class="[^"]*post-body[^"]*"[^>]*>(.*?)</div>\s*</div>', page, re.S)
    description = ""
    if body_match:
        plain = _strip_html(html.unescape(body_match.group(1)))
        # Trim the leading "Title View Time= 5:51" boilerplate Kajabi prints above the body.
        plain = re.sub(r"^.{0,200}?View Time\s*=\s*\d+:\d+\s*", "", plain, count=1, flags=re.I)
        # And strip the title itself if it leads.
        if title and plain.startswith(title):
            plain = plain[len(title):].lstrip(" -|:")
        description = plain.strip()

    cat_id_match = POST_CATEGORY_RE.search(url)
    return {
        "url": url,
        "title": title or "Untitled",
        "description": description,
        "hashed_id": hashed_id,
        "category_id": cat_id_match.group(1) if cat_id_match else "",
    }


def wistia_meta(hashed_id):
    """Fetch public Wistia media metadata (name, duration, thumbnail)."""
    if not hashed_id:
        return {}
    api = f"https://fast.wistia.com/embed/medias/{hashed_id}.json"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return {}
    media = data.get("media", {})
    assets = media.get("assets", [])
    thumb = ""
    for a in assets:
        if a.get("type") == "still_image" or "image" in a.get("content_type", ""):
            thumb = a.get("url", "")
            break
    if not thumb:
        thumb = media.get("embedOptions", {}).get("stillUrl", "") or media.get("thumbnail", {}).get("url", "")
    return {
        "name": media.get("name", ""),
        "duration": media.get("duration", ""),
        "thumbnail": thumb.split("?")[0] if thumb else "",
    }


def safe_filename(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(".")


def _xml(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_nfo(video, out_path):
    nfo_path = out_path.with_suffix(".nfo")
    if nfo_path.exists():
        return
    title = video.get("title", "")
    course = video.get("course", "")
    module = video.get("module", "")
    order = video.get("order", 0)
    plot = video.get("description", "")
    duration = video.get("duration", "")
    try:
        runtime = str(round(float(duration) / 60)) if duration else ""
    except (TypeError, ValueError):
        runtime = ""

    extra_lines = []
    if module:
        extra_lines.append(f"  <tag>{_xml(module)}</tag>")
    if runtime:
        extra_lines.append(f"  <runtime>{_xml(runtime)}</runtime>")
    extras = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    nfo = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>{_xml(title)}</title>
  <originaltitle>{_xml(title)}</originaltitle>
  <sorttitle>{order:03d} - {_xml(title)}</sorttitle>
  <plot>{_xml(plot)}</plot>
  <outline>{_xml(plot[:240])}</outline>
  <set><name>{_xml(course)}</name></set>
  <studio>FlyAOA Media</studio>
  <genre>Education</genre>
  <tag>{_xml(course)}</tag>
  <uniqueid type="wistia" default="true">{_xml(video.get("hashed_id", ""))}</uniqueid>
  <source>{_xml(video.get("url", ""))}</source>{extras}
</movie>
"""
    nfo_path.write_text(nfo, encoding="utf-8")


def download_thumb(video, out_path):
    thumb_path = out_path.with_suffix(".jpg")
    if thumb_path.exists():
        return
    thumb_url = video.get("thumbnail", "")
    if not thumb_url:
        return
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            thumb_path.write_bytes(resp.read())
        print(f"  [thumb] Saved {thumb_path.name}")
    except Exception:
        pass


def export_cookies(session, filepath):
    """Write the requests session cookies to a Netscape cookie file for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in session.cookies:
        domain = c.domain
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.secure else "FALSE"
        expires = int(c.expires) if c.expires else 0
        lines.append(f"{domain}\t{flag}\t{c.path}\t{secure}\t{expires}\t{c.name}\t{c.value}")
    filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_with_ytdlp(video, out_path):
    """Download a Wistia video with yt-dlp. Falls back to the lesson page URL."""
    if out_path.exists() and out_path.stat().st_size > 1_000_000:
        print(f"  [skip] Already exists: {out_path.name}")
        return True

    out_template = str(out_path.with_suffix("")) + ".%(ext)s"
    base_cmd = [
        sys.executable, "-m", "yt_dlp",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", out_template,
        "--no-playlist",
        "--concurrent-fragments", FRAGMENT_CONCURRENCY,
        "--retries", "5",
        "--fragment-retries", "10",
        "--user-agent", USER_AGENT,
        "--referer", BASE_URL,
    ]

    targets = []
    if video.get("hashed_id"):
        targets.append(f"https://fast.wistia.com/embed/iframe/{video['hashed_id']}")
    # Fallback: let yt-dlp's generic extractor find the embed on the lesson page.
    targets.append(("--cookies", str(COOKIES_TXT), video["url"]))

    for target in targets:
        cmd = list(base_cmd)
        if isinstance(target, tuple):
            cmd += list(target[:-1]) + [target[-1]]
        else:
            cmd.append(target)
        if subprocess.run(cmd).returncode == 0 and out_path.exists():
            return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Download your FlyAOA Media training videos for offline personal use."
    )
    parser.add_argument("--limit", type=int, default=0, help="Download at most N videos.")
    parser.add_argument("--product", default="", help="Only process products whose URL contains this text.")
    parser.add_argument("--metadata-only", action="store_true", help="Only refresh metadata.json / video_links.txt.")
    parser.add_argument("--no-nfo", action="store_true", help="Skip writing .nfo and .jpg files.")
    parser.add_argument("--list", action="store_true", help="List discovered products and lessons, then exit.")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=== FlyAOA Media Training Downloader ===\n")

    session = session_factory()
    if not login(session):
        sys.exit(1)

    products = discover_products(session)
    if args.product:
        products = [p for p in products if args.product in p]
    if not products:
        print("[!] No products found. The library layout may have changed.")
        sys.exit(1)

    all_videos = []
    for product_url in products:
        product_title, modules = discover_product_meta(session, product_url)
        slug = PRODUCT_SLUG_RE.search(product_url)
        slug = slug.group(1) if slug else product_url.rstrip("/").split("/")[-1]
        course = safe_filename(product_title or slug.replace("-", " ").title())
        print(f"\n[*] Product: {course} ({product_url})")
        posts = discover_posts(session, product_url)
        print(f"    {len(posts)} lesson(s) across {len(modules)} module(s)")
        for order, post_url in enumerate(posts, 1):
            info = parse_post(session, post_url)
            if not info:
                continue
            info["course"] = course
            info["product_slug"] = slug
            info["module"] = modules.get(info.get("category_id", ""), "")
            info["order"] = order
            info.update({k: v for k, v in wistia_meta(info["hashed_id"]).items() if v})
            all_videos.append(info)

    # Persist metadata + links.
    METADATA_FILE.write_text(json.dumps(all_videos, indent=2, ensure_ascii=False), encoding="utf-8")
    LINKS_FILE.write_text("\n".join(v["url"] for v in all_videos), encoding="utf-8")
    print(f"\n[*] Saved metadata for {len(all_videos)} lessons -> {METADATA_FILE.name}")
    export_cookies(session, COOKIES_TXT)

    if args.list:
        for v in all_videos:
            flag = "video" if v.get("hashed_id") else "no-video"
            mod = f" [{v['module']}]" if v.get("module") else ""
            print(f"  [{flag}] {v['course']}{mod} / {v['order']:03d} - {v['title']}")
        return

    if args.metadata_only:
        print("[*] Metadata refreshed; no downloads requested.")
        return

    videos = [v for v in all_videos if v.get("hashed_id")]
    if args.limit > 0:
        videos = videos[:args.limit]

    success = 0
    failed = []
    for i, video in enumerate(videos, 1):
        course_dir = OUTPUT_DIR / video["course"]
        course_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{video['order']:03d} - {safe_filename(video['title'])}"
        out_path = course_dir / f"{filename}.mp4"

        mod = f" [{video['module']}]" if video.get("module") else ""
        print(f"\n[{i}/{len(videos)}] {video['course']}{mod} / {filename}")
        if download_with_ytdlp(video, out_path):
            success += 1
            if not args.no_nfo:
                write_nfo(video, out_path)
                download_thumb(video, out_path)
            print("  \u2713 Downloaded")
        else:
            failed.append(video["url"])
            print("  \u2717 Failed")

    print(f"\n=== Done: {success}/{len(videos)} downloaded ===")
    if failed:
        failed_file = ROOT_DIR / "failed_downloads.txt"
        failed_file.write_text("\n".join(failed), encoding="utf-8")
        print(f"[!] {len(failed)} failed - saved to {failed_file.name}")


if __name__ == "__main__":
    main()
