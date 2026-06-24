# flyaoamedia-dl

Download your [FlyAOA Media](https://training.flyaoamedia.com) training videos — **and the PDF handouts, checklists, study guides and other downloadable files attached to each lesson** — so you can watch and use them offline.

Requires an active FlyAOA Media training membership with access to the courses.

## How does it work?

training.flyaoamedia.com runs on **Kajabi**, which serves course lessons with embedded **Wistia** videos. This tool uses plain HTTP requests (no browser automation) to:

1. Log in to the training site
2. Crawl your **Library** to discover every product (course) and its lessons
3. Read each lesson page and find the embedded Wistia video
4. Download the video with `yt-dlp`
5. Mirror any downloadable files (PDFs, handouts, checklists, ZIPs) attached to the lesson

Videos and their lesson files are organised per course:

```text
downloads/
  Course Name/
    001 - First Lesson.mp4
    001 - First Lesson - Study_Guide.pdf
    002 - Second Lesson.mp4
```

Lesson metadata is saved to `metadata.json`.

## Requirements

- **Python 3.11+**
- **A FlyAOA Media account** with access to the training
- `ffmpeg` on your `PATH` (used by `yt-dlp` to merge video + audio)

## Installation

1. Clone the repository and install dependencies:

   ```powershell
   git clone https://github.com/GraafG/flyaoamedia-dl.git
   cd flyaoamedia-dl
   pip install -r requirements.txt
   ```

2. Copy the example configuration:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Fill in `.env` with your account email and password.

## Usage

Download everything:

```powershell
python -u download_videos.py
```

List the courses and lessons it finds (no downloads):

```powershell
python -u download_videos.py --list
```

Download only a limited number of videos:

```powershell
python -u download_videos.py --limit 5
```

Only download a specific course (matches against the product URL):

```powershell
python -u download_videos.py --product zero-to-finish
```

Refresh metadata only:

```powershell
python -u download_videos.py --metadata-only
```

By default the downloader **reuses the cached `metadata.json`** when it exists, so
resuming an interrupted run does not re-crawl every lesson page (which can trip the
site's rate limiting). Force a fresh crawl with `--refresh`:

```powershell
python -u download_videos.py --refresh
```

The tool will:

- Log in to the training site
- Save lesson metadata in `metadata.json`
- Save lesson URLs in `video_links.txt`
- Download videos into `downloads\<Course Name>\`
- Mirror all console output to `download.log`
- Skip previously downloaded `.mp4` files
- Resume safely when you run it again

## Configuration

All settings can be configured in `.env`:

| Variable | Description | Default |
|---|---|---|
| `FLYAOA_EMAIL` | Your FlyAOA Media login email | empty |
| `FLYAOA_PASSWORD` | Your FlyAOA Media password | empty |
| `FLYAOA_BASE_URL` | Base URL of the training site | `https://training.flyaoamedia.com` |
| `OUTPUT_DIR` | Folder for downloaded videos | `./downloads` |
| `YTDLP_CONCURRENT_FRAGMENTS` | Parallel HLS/DASH fragment downloads | `16` |
| `FLYAOA_REQUEST_DELAY` | Seconds to wait between crawl requests (avoids HTTP 429) | `1.0` |
| `LOG_FILE` | Path for the mirrored run log | `./download.log` |

## Speed

The main speed setting is `YTDLP_CONCURRENT_FRAGMENTS`.

```powershell
$env:YTDLP_CONCURRENT_FRAGMENTS='32'
python -u download_videos.py
```

Use `16` or `32` as a practical range. Higher values may be faster, but can also trigger throttling or more retries.

## Output

```
downloads/
  Course Name/
    001 - First Lesson.mp4
    001 - First Lesson.nfo
    001 - First Lesson.jpg
    001 - First Lesson - Study_Guide.pdf
    002 - Second Lesson.mp4
    002 - Second Lesson.nfo
    002 - Second Lesson.jpg
```

Each video gets:

- `.mp4` — the video
- `.nfo` — Kodi/Jellyfin metadata (title, course, ordering, duration)
- `.jpg` — Wistia thumbnail / poster art

Lessons that have files in their Kajabi **downloads** dropdown (PDFs, handouts,
checklists, ZIPs, etc.) also get those mirrored next to the video, named
`NNN - Lesson Title - Original Filename.ext`. Lessons with downloadable files
but no video (for example a course "Study Guide") are mirrored too.

To skip NFO generation:

```powershell
python -u download_videos.py --no-nfo
```

To control attachment mirroring:

```powershell
# Skip downloadable attachments entirely
python -u download_videos.py --no-attachments

# Only mirror attachments (skip videos) — handy when videos are already downloaded
python -u download_videos.py --attachments-only
```

## Jellyfin

Add `downloads/` as a **Movies** library in Jellyfin and group by folder. The `.nfo` files are read automatically, and the `<set>` tag keeps lessons grouped by course.

## Troubleshooting

- **Login failed** — Check `FLYAOA_EMAIL` / `FLYAOA_PASSWORD` in `.env`. The site requires your account **email**, not a username.
- **No products found** — Kajabi occasionally changes its library layout. Run with `--list` to see what is discovered.
- **A lesson has no video** — Some lessons are text/PDF only; these are listed in `metadata.json` with no `hashed_id` and are skipped.
- **Download stops halfway** — Run the script again. Completed files are skipped.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Report bugs or suggest features via [GitHub Issues](../../issues).

Security issues? See [SECURITY.md](SECURITY.md).

## Disclaimer

This tool is intended for personal use by paying FlyAOA Media members to watch their own accessible training videos offline. Do not share downloaded files — respect FlyAOA Media's copyright and terms.

## License

MIT
