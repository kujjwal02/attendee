# Self-hosting this fork (ARM64 + Google Drive)

This is a fork of [Attendee](https://github.com/attendee-labs/attendee) adapted to:

1. **Run natively on ARM64** (e.g. an Oracle Ampere A1 VM) by swapping Google Chrome for
   Chromium, and
2. **Store recordings on Google Drive** via an **rclone FUSE mount** + Django's plain
   filesystem storage - **no MinIO, no S3, no Azure**.

Native Zoom support has been **removed** (see [Zoom](#zoom-removed) below). Google Meet and
Microsoft Teams work as upstream.

> **Not fully testable off the target host.** The image must be built on the ARM VM and
> validated against a **live Google Meet call** - Chromium-on-Meet and the in-container FUSE
> mount can only be confirmed there. See [Validation on the VM](#validation-on-the-vm).

---

## What changed vs upstream

| Area | Change | Why |
|---|---|---|
| `Dockerfile` | `debian:bookworm-slim` base (was `--platform=linux/amd64 ubuntu:22.04`); Google Chrome -> `chromium` + `chromium-driver`; multi-stage builder -> lean runtime; `uv` instead of pip | Google ships no arm64 Linux Chrome; Debian has real arm64 Chromium debs (Ubuntu's are snap shims). Multi-stage keeps the compiler toolchain out of the final image. |
| `requirements.txt` | removed `zoom-meeting-sdk` | x86_64-only wheel; would break the arm build |
| `bots/bot_controller/bot_controller.py` | Zoom launch path raises `NotImplementedError` | native SDK is gone |
| `attendee/settings/base.py` | new `STORAGE_PROTOCOL=filesystem` branch | write recordings to a local dir (the Drive mount) |
| `bots/models.py`, `bots/storage.py` | `url` helpers only build S3 presigned URLs when `STORAGE_PROTOCOL=s3` | boto3 calls crash on non-S3 backends |

---

## Build

Build **on the ARM VM** (native arm64 - no buildx/QEMU needed):

```bash
git clone https://github.com/kujjwal02/attendee.git
cd attendee
git checkout selfhost-arm-gdrive
docker build --target final -t attendee-selfhost:arm64 .
```

Sanity-check the browser inside the image (versions must match, or Selenium won't start):

```bash
docker run --rm attendee-selfhost:arm64 bash -lc 'google-chrome --version && chromedriver --version'
```

### Image-size / memory notes
- The **build toolchain and all `-dev` headers live only in the `builder` stage** and are
  not present in the final image.
- `--no-install-recommends` everywhere; apt lists are cleaned in each layer.
- **`uv`** replaces pip for the dependency install (much faster resolves/builds).
- PyAV (`av`) is still compiled from source **on purpose** - it must link against the system
  ffmpeg (which includes `libavdevice`); the PyPI wheel omits it.
- `python3-gi` (PyGObject) is installed **explicitly**. Upstream got it implicitly as an apt
  *Recommends*; with `--no-install-recommends` it must be named, or the GStreamer recording
  pipeline (`bots/bot_controller/gstreamer_pipeline.py`) breaks at runtime.

### Runtime memory (per bot)
Each concurrent bot is one Chromium + Xvfb + ffmpeg, so **size the VM for peak concurrency**
and prefer running one meeting at a time on a small VM. Relevant knobs:
- `shm_size: 2gb` on the worker service (Chromium needs it), or the adapter's
  `--disable-dev-shm-usage` is honoured.
- `ENABLE_CHROME_SANDBOX=false` (the adapter already adds `--no-sandbox`).
- Keep worker concurrency at 1 for a single small VM.
- Chromium (vs Chrome) is already the lighter engine; it is **required** - Meet needs a
  Blink/WebRTC browser and the whole adapter is Chrome/CDP-specific, so Firefox / a
  headless-shell are not drop-in options.

---

## Environment variables

```dotenv
# --- storage: local filesystem = the rclone Google Drive mount ---
STORAGE_PROTOCOL=filesystem
RECORDING_STORAGE_ROOT=/recordings              # bind-mounted to the Drive mount (see below)
USE_REMOTE_STORAGE_FOR_AUDIO_CHUNKS=false       # audio chunks stay in Postgres -> no object store at all

# --- single-VM bot execution ---
LAUNCH_BOT_METHOD=celery                         # bots run in-process in the worker
ENABLE_CHROME_SANDBOX=false                      # + --no-sandbox (handled in web_bot_adapter)
```

`RECORDING_STORAGE_ROOT` defaults to `/recordings` if unset. Leave the storage `base_url`
unset - Attendee's own recording-download URL isn't used; the file is retrieved from Drive.

---

## Deploy on Dokploy

A ready-to-use compose file is included: **`docker-compose.dokploy.yaml`**. Point a Dokploy
**Compose** service at this fork/branch and select that file. It builds the image once (on the
Arm VM) and runs three roles - `web` (gunicorn), `worker` (celery bots), `scheduler` - plus
`postgres` and `redis`, with a one-shot `init` service that runs `migrate` + `collectstatic`
before the app starts.

Set these in Dokploy's **Environment** UI:

| Var | Notes |
|---|---|
| `DJANGO_SECRET_KEY` | required - long random string |
| `CREDENTIALS_ENCRYPTION_KEY` | required - Fernet key; generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `POSTGRES_PASSWORD` | required |
| `ALLOWED_HOSTS` | your domain, e.g. `attendee.example.com` |
| `RECORDING_MOUNT` | host path of the rclone Drive mount (default `/home/ubuntu/attendee-recordings`) |
| `DJANGO_SSL_REQUIRE` | keep `true` (default) behind Traefik - it sets `SECURE_PROXY_SSL_HEADER` so Django trusts `X-Forwarded-Proto=https`. Setting it `false` makes Django treat every request as http → CSRF "Origin checking failed" on POST (Google login) and an `http://` OAuth `redirect_uri`. |

Attach the Dokploy domain/Traefik route to the **`web`** service on port `8000`. The `worker`
service carries the Drive bind-mount (`:rshared`) and `shm_size: 2gb`; make sure the rclone
mount (below) exists on the host first.

---

## Google Drive via an rclone FUSE mount

Recordings are written to a local directory that is an **rclone mount of Google Drive**
(personal `kujjwal02@gmail.com`, ~1 TB). Attendee itself only ever sees a local path.

### Why a user OAuth token (not a service account)
A personal Gmail has no Workspace, so **no domain-wide delegation and no Shared Drives**, and
a service account has **no Drive quota of its own**. You must use a **user OAuth token**.

### 1. Create your own OAuth client + **publish the app**
1. [Google Cloud Console](https://console.cloud.google.com) -> new project.
2. **APIs & Services -> Enable APIs -> Google Drive API.**
3. **OAuth consent screen -> User type: External** (a personal Gmail can't be "Internal"),
   add scope `.../auth/drive`, add your Gmail as a test user.
4. **IMPORTANT - Publish the app** (consent screen -> "Publish app"). If it is left in
   **"Testing"**, Google **expires the refresh token after ~7 days** and the mount silently
   stops uploading. Publishing avoids that.
5. **Credentials -> Create OAuth client ID -> Application type: Desktop app.** A Desktop
   client has a client *secret*; a bare "native" client without one fails to refresh in
   rclone. Copy `client_id` + `client_secret`.

### 2. Authorize on a machine with a browser (your laptop)
```bash
rclone authorize "drive" "<client_id>" "<client_secret>"
# sign in -> approve -> it prints a token JSON blob; copy it
```

### 3. Configure the remote on the VM and make the folder
```ini
# ~/.config/rclone/rclone.conf
[gdrive]
type = drive
client_id = <client_id>
client_secret = <client_secret>
scope = drive
token = {"access_token":"...","refresh_token":"...","expiry":"..."}
# optional: pin to one folder
# root_folder_id = <folder-id-from-the-drive-url>
```
```bash
rclone mkdir gdrive:attendee-recordings && rclone lsd gdrive:
```

### 4. Mount it (systemd unit)
```ini
# /etc/systemd/system/attendee-recordings.mount ... or an ExecStart wrapper:
# rclone mount gdrive:attendee-recordings /home/ubuntu/attendee-recordings \
#   --vfs-cache-mode writes --allow-other --dir-cache-time 1000h
```
`--vfs-cache-mode writes` uses **transient local disk** while each file uploads (bounded,
auto-evicted) - disk isn't literally zero during an upload, but stays small. `--allow-other`
is required so the container's non-root user can read/write the mount.

### 5. Bind the mount into the worker with shared propagation
The container must see the FUSE mount, which means **shared mount propagation**:

```yaml
# docker-compose (worker service)
services:
  worker:
    volumes:
      - /home/ubuntu/attendee-recordings:/recordings:rshared
    # RECORDING_STORAGE_ROOT=/recordings
```

You may need to mark the host mount shared first:
```bash
mount --make-rshared /home/ubuntu/attendee-recordings
```
Without `rshared`, the container captures the mountpoint at start time and never sees files
rclone writes afterwards. This is host-level config, like the existing Syncthing/gitwatch
units on the VM.

---

## Zoom removed

The native Zoom SDK (`zoom-meeting-sdk`) is **x86_64-only**, so it would block the arm build,
and Zoom is not needed here. It's removed from `requirements.txt`, and
`BotController.get_bot_adapter()` raises `NotImplementedError` for Zoom URLs. The Zoom
models / migrations / OAuth API endpoints are left in place but inert (removing them buys no
size and risks the DB schema). **Only Google Meet and Microsoft Teams are supported.**

---

## Validation on the VM

These cannot be checked off a laptop:

1. **Build on the VM** (native arm64). Confirm `google-chrome` and `chromedriver` **major
   versions match**.
2. Set `LAUNCH_BOT_METHOD=celery`, `STORAGE_PROTOCOL=filesystem`, the Drive mount live and
   bind-mounted `rshared`.
3. `POST /api/v1/bots` for a **real Google Meet link** (closed-caption transcription), admit
   the bot, hold a short meeting, end it.
4. **Verify:**
   - an MP4 appears under `attendee-recordings/` in Google Drive;
   - the transcript is retrievable via `GET /bots/{id}/transcript`;
   - everything ran on Chromium/arm64;
   - a **full-size** recording uploads cleanly through the `--vfs-cache-mode writes` cache
     (not just a 2-minute test).
5. Watch the **first token refresh after 7+ days** to confirm the published OAuth app keeps
   the refresh token alive.

### Known risks
- **Chromium-on-Meet** is not the upstream-tested config - this is the #1 gate.
- **FUSE-in-container propagation** + `--vfs-cache-mode writes` behaviour under large writes.
- **arm64 wheel availability** for the compiled Python deps (numpy, opencv-python, grpcio,
  aiortc, psycopg2, av-from-source) - all expected to resolve on arm64, but confirmed only
  by a real build on the VM.
