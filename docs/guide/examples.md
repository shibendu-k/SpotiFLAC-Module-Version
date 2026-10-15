<!-- New practical examples guide -->

[← Back to the README](../../README.md)

# Examples

## CLI: single track

```bash
spotiflac "https://open.spotify.com/track/TRACK_ID" ./out --service ext:tidal-web
```

## CLI: album with provider fallback order

```bash
spotiflac "https://open.spotify.com/album/ALBUM_ID" ./out \
  --service ext:qobuz-web ext:tidal-web ext:amazon \
  --quality HI_RES_LOSSLESS \
  --fallback
```

## CLI: playlist with organization + profile save

```bash
spotiflac "https://open.spotify.com/playlist/PLAYLIST_ID" ./library \
  --use-artist-subfolders \
  --use-album-subfolders \
  --filename-format "{year} - {album}/{track}. {title}" \
  --save-profile library-layout
```

## CLI: run from a saved profile

```bash
spotiflac "https://open.spotify.com/artist/ARTIST_ID" ./library --profile library-layout
```

## Python: sync API

```python
from SpotiFLAC import SpotiFLAC

SpotiFLAC(
    url="https://open.spotify.com/track/TRACK_ID",
    output_dir="./out",
    services=["ext:tidal-web", "ext:qobuz-web"],
    quality="LOSSLESS",
)
```

## Python: async API

```python
import asyncio
from SpotiFLAC import AsyncSpotiFLAC

async def main():
    async with AsyncSpotiFLAC(
        output_dir="./out",
        services=["ext:tidal-web", "ext:qobuz-web"],
        quality="LOSSLESS",
    ) as client:
        await client.download_track("https://open.spotify.com/track/TRACK_ID")

asyncio.run(main())
```

## Web mode (browser GUI)

```bash
spotiflac --web --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

## Telegram bot mode

```bash
spotiflac --telegram-bot \
  --telegram-token "<your-bot-token>" \
  --telegram-admin-id 123456789 \
  --telegram-daily-limit 20
```

The bot supports inline search selection, quality selection, and per-user limits.
