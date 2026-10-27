# Video Catalog Browser

A small, single-file web app for browsing a personal video catalog stored in
[DuckDB](https://duckdb.org/). It groups clips by capture date, collapses
related files (Insta360 lens pairs, LRV sidecars, proxies) into one logical
clip, and previews them in the browser, converting formats the browser can't
play with ffmpeg.

- Filter by storage root (drive); offline drives are marked.
- Clip times are taken from the filename (`VID_`, `PXL_`, `DJI_` ...) or from
  the file's modified time.
- NAS-backup and duplicate flags, plus project notes.
- In-browser preview: plays the lightest playable file (proxy → sidecar →
  original). Unsupported codecs are converted once to a small H.264 copy and
  cached.

The data is read live from the DuckDB file on every request, opened read-only.

## Run with Docker (recommended)

```bash
cp .env.example .env      # then edit the paths in .env
docker compose up -d --build
```

Open <http://127.0.0.1:8000>.

`.env` settings:

| Variable         | Meaning                                                        |
|------------------|----------------------------------------------------------------|
| `CATALOG_DIR`    | Folder containing `videos_catalog.duckdb` (mounted read-only)  |
| `MEDIA_1`        | Drive/folder holding videos, mounted at `/media/1`             |
| `MEDIA_2`        | Optional second drive/folder, mounted at `/media/2`            |
| `VB_PATH_MAP`    | Maps catalog roots to mounts, e.g. `H:/=/media/1;F:/=/media/2` |
| `VB_ROOT_LABELS` | Display names, e.g. `H:/=H: Processed;F:/=F: Raw`              |
| `VB_PORT`        | Host port (default `8000`)                                     |

Mounted drives must be connected when the container starts. In the container,
the **Play** and **Show in folder** buttons are unavailable; use the preview.

## Run directly (Windows, macOS, Linux)

```bash
pip install "duckdb>=2.0"
python video_browser.py
```

Put `videos_catalog.duckdb` next to the script or one folder up, or set
`VB_DB`. Install ffmpeg/ffprobe on `PATH` for converted previews
(Windows: `winget install Gyan.FFmpeg`). Other settings: `VB_HOST`, `VB_PORT`,
`VB_CACHE`, `VB_ROOT_LABELS`, `VB_PATH_MAP`.

## Catalog schema

The app expects a DuckDB 2 database with a `videos` table:

| Column         | Type      | Notes                                       |
|----------------|-----------|---------------------------------------------|
| `filename`     | VARCHAR   |                                             |
| `ext`          | VARCHAR   | e.g. `.mp4`; `.nev` / `.gyro` never previewed |
| `kind`         | VARCHAR   | `video`, `sidecar` or `proxy`               |
| `device`       | VARCHAR   | camera / phone name                         |
| `capture_date` | DATE      |                                             |
| `size_bytes`   | BIGINT    |                                             |
| `modified`     | TIMESTAMP | file modified time                          |
| `rel_path`     | VARCHAR   | unique path relative to the storage root    |
| `full_path`    | VARCHAR   | absolute path, e.g. `H:/Processed/...`      |
| `location`     | VARCHAR   | optional                                    |
| `project`      | VARCHAR   | optional                                    |
| `collection`   | VARCHAR   | e.g. `Processed`, `Raw`                     |
| `storage_root` | VARCHAR   | e.g. `H:/`                                  |
| `nas_path`     | VARCHAR   | set when a NAS backup exists                |
| `dup_of`       | VARCHAR   | set when the file duplicates another        |

An optional `projects` table (`project`, `resolve_project`, `editing_media`,
`notes`) adds hover notes to project tags.

## Security

There is no authentication. Anyone who can reach the port can browse the
catalog and stream any file in it. Keep it bound to `127.0.0.1` (the default,
and what `compose.yaml` does). POST endpoints require a custom header and
media requests reject cross-site fetches, so other websites can't drive it.

Never commit your catalog database or `.env`; both are git-ignored.

## License

[MIT](LICENSE)
