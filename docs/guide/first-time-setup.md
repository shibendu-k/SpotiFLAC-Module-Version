<!-- New onboarding guide -->

[← Back to the README](../../README.md)

# First-Time Setup

This page is the shortest path from a fresh install to your first successful download.

## 1) Install

```bash
pip install SpotiFLAC
```

## 2) Add at least one extension registry

SpotiFLAC ships with no built-in download provider, so add your own registry source first:

```bash
spotiflac --registries https://example.com/my-registry.json --help
```

`--registries` is persisted, so you usually do this once.

## 3) Verify which services are available

Open one interface and check provider availability:

```bash
spotiflac --gui
# or
spotiflac --tui
```

Both surfaces read the same installed extensions.

## 4) Run a first download

```bash
spotiflac "https://open.spotify.com/track/TRACK_ID" ./out --service ext:tidal-web
```

If your configured extension IDs differ, use those IDs instead.

## 5) Save your preferred defaults

```bash
spotiflac "https://open.spotify.com/album/ALBUM_ID" ./out \
  --service ext:tidal-web ext:qobuz-web \
  --quality LOSSLESS \
  --save-profile default-lossless
```

Next time:

```bash
spotiflac "https://open.spotify.com/playlist/PLAYLIST_ID" ./out --profile default-lossless
```

## 6) Optional: first Telegram bot run

```bash
spotiflac --telegram-bot \
  --telegram-token "<your-bot-token>" \
  --telegram-admin-id 123456789
```

Or set:

- `URMUSICAPI_TELEGRAM_TOKEN`
- `URMUSICAPI_TELEGRAM_ADMINS`
- `URMUSICAPI_TELEGRAM_DAILY_LIMIT`

## Next reads

- [Examples](examples.md)
- [Configuration](configuration.md)
- [Extensions](extensions.md)
