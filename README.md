# smugmug-usage

![tests](https://github.com/YOUR-GITHUB-USERNAME/smugmug-usage/actions/workflows/tests.yml/badge.svg)

A read-only command-line tool that totals how much storage a SmugMug account uses, broken down by album.

I run a portrait photography business on SmugMug and wanted to know which galleries take up the most space. SmugMug doesn't show this, so this script walks every album through the SmugMug API v2 and adds up the original file sizes.

## What it does

- Signs in with OAuth 1.0a, asking for **read-only** permission. Every request is an HTTP GET, so it can't change, move, or delete anything.
- Pages through every album and image, following `NextPage` links.
- Uses each photo's `ArchivedSize` (the original upload). For videos, it gets the size from the `LargestVideo` endpoint, because `ArchivedSize` is only the poster image.
- Counts each image once, even when it's collected into more than one album.
- Retries on rate limits (HTTP 429), server errors (5xx), and network drops, honoring `Retry-After`.
- Writes `smugmug_usage_by_album.csv`, largest album first. If that file is open in Excel, it writes a timestamped copy instead.

## Setup

1. Get an API key and secret from SmugMug (apply for one in your SmugMug account's developer/API settings).
2. Install dependencies:

   ```
   python -m pip install -r requirements.txt
   ```

3. Set your credentials as environment variables. They are never stored in the code.

   Windows (Command Prompt, then open a new window):

   ```
   setx SMUGMUG_API_KEY "your-key"
   setx SMUGMUG_API_SECRET "your-secret"
   ```

   macOS/Linux:

   ```
   export SMUGMUG_API_KEY="your-key"
   export SMUGMUG_API_SECRET="your-secret"
   ```

4. Run it:

   ```
   python smugmug_usage.py
   ```

   The first run prints a URL. Approve access, paste the PIN, and the login is saved to `.smugmug_tokens.json` in your home folder.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | All albums read |
| 1 | Finished, but some albums couldn't be read (listed in the output) |
| 2 | API key or secret not set |
| 3 | Sign-in failed |
| 4 | Network or SmugMug error |
| 130 | Stopped with Ctrl+C |

## Tests

The tests never touch the network. SmugMug responses are faked with small stub objects, and retry delays are patched out.

```
python -m pip install -r requirements-dev.txt
python -m pytest -v
```

They cover:

- Retry logic: 429/5xx/network retries, `Retry-After` in seconds and HTTP-date form, the 120-second cap, exponential backoff, giving up after the retry limit, and no retry on 401 or 404
- Pagination: following `NextPage`, stopping on a repeated page, empty albums
- Counting: duplicates across albums, video sizes, files with missing or invalid sizes
- CSV output: UTF-8 BOM for Excel, header row, fallback name when the file is locked
- Saved logins: missing, valid, and corrupt token files
- Credentials: read from the environment only, nothing hardcoded

GitHub Actions runs the suite on every push, on Windows and Ubuntu with Python 3.10, 3.12, and 3.13.

## License

MIT. See [LICENSE](LICENSE).
