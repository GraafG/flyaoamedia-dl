# Contributing to flyaoamedia-dl

Contributions are welcome!

## How to contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-improvement`)
3. Commit your changes (`git commit -m 'Add X'`)
4. Push to your branch (`git push origin feature/my-improvement`)
5. Open a Pull Request

## Guidelines

- **No credentials** — Never commit passwords, tokens, cookies, or `.env` files
- **Test your changes** — Make sure the script works with your own account before opening a PR
- **Keep it simple** — This is a small project; keep changes focused and easy to review
- **English only** — Please use English for issues, PRs, and comments

## Ideas for contributions

- Better error handling for expired sessions
- Resume/skip by course or date
- Faster resume/logging for large downloads
- Improved progress reporting

## Development

Run the offline checks without an account or `.env` file:

```bash
python -m pip install -r requirements.txt
python -m pip install ruff
python -m py_compile download_videos.py
python -m unittest discover -s tests -v
ruff check download_videos.py tests
python -m pip check
```

The regression tests mock network and subprocess calls, isolate environment
variables, and replace dotenv loading. They use only synthetic data and temporary
files. CI runs these checks on Python 3.11 and 3.12.

Only for a separate, explicitly intended live run with your own account:

```bash
pip install -r requirements.txt
cp .env.example .env
python -u download_videos.py --list
```
