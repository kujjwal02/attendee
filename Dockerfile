# syntax=docker/dockerfile:1
#
# Self-hosted ARM64 fork of Attendee (see SELFHOST.md).
#
# Differences from upstream (which is amd64-only and ships Google Chrome):
#   - Native arm64 build (no `--platform=linux/amd64` pin).
#   - Google Chrome -> Chromium. Google publishes no arm64 Linux Chrome; Chromium is the
#     same Blink/WebRTC engine Meet uses. debian:bookworm ships REAL arm64 `chromium` +
#     `chromium-driver` debs (snap-free, version-matched) - Ubuntu's `chromium`/
#     `chromium-browser` are snap shims that cannot run in Docker.
#   - Multi-stage builder -> lean runtime: the compiler toolchain and -dev headers live
#     only in the `builder` stage and never ship in the final image.
#   - Zoom native SDK removed (x86_64-only wheel; would block the arm build).
#
# NOTE: this file can only be fully validated by building on the Arm VM and running a live
# Google Meet call. See SELFHOST.md "Validation on the VM".

########################################################################################
# Stage 1: base - lean runtime foundation (shared by builder and final)
########################################################################################
FROM debian:bookworm-slim AS base

SHELL ["/bin/bash", "-c"]

ENV project=attendee
ENV cwd=/$project
WORKDIR $cwd

ARG DEBIAN_FRONTEND=noninteractive

# Runtime-only OS dependencies. Every package here is needed at RUN time (not just to
# build). --no-install-recommends keeps the image small; because of it we must name the
# things that were previously pulled in implicitly as Recommends (notably python3-gi, on
# the Meet/Teams recording path via bots/bot_controller/gstreamer_pipeline.py).
#
#   chromium, chromium-driver  the meeting browser + matching WebDriver (its apt Depends
#                              pull the whole X/GL/nss/pango stack automatically)
#   xvfb, xauth                virtual display the bot renders into
#   xterm                      spawned to degrade video to a black frame (screen_and_audio_recorder)
#   xclip                      Teams chat paste (teams_bot_adapter)
#   pulseaudio*                audio capture (see entrypoint.sh)
#   ffmpeg                     muxing + provides the libav* shared libs PyAV links against
#   gstreamer1.0-*             recording pipeline runtime plugins
#   python3-gi, python3-gst-1.0, gir1.2-*  PyGObject bindings + typelibs for `gi.repository.Gst`
#   libpq5                     psycopg2 runtime
#   libgl1, libglib2.0-0, libsm6, libxext6, libxrender1   opencv-python runtime
#   fonts-liberation           text rendering in the headless browser
#   tini                       PID 1 init (apt package = arch-correct, unlike the amd64 GitHub binary)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      tini \
      chromium \
      chromium-driver \
      xvfb \
      xauth \
      xterm \
      xclip \
      pulseaudio \
      pulseaudio-utils \
      ffmpeg \
      gstreamer1.0-tools \
      gstreamer1.0-alsa \
      gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly \
      gstreamer1.0-libav \
      python3 \
      python3-gi \
      python3-gst-1.0 \
      gir1.2-glib-2.0 \
      gir1.2-gstreamer-1.0 \
      gir1.2-gst-plugins-base-1.0 \
      libpq5 \
      libgl1 \
      libglib2.0-0 \
      libsm6 \
      libxext6 \
      libxrender1 \
      fonts-liberation \
 && ln -sf "$(command -v chromium)" /usr/bin/google-chrome \
 && ln -sf "$(command -v chromedriver)" /usr/local/bin/chromedriver \
 && ln -sf /usr/bin/python3 /usr/bin/python \
 && google-chrome --version \
 && chromedriver --version \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* /usr/share/info/*

########################################################################################
# Stage 2: builder - compile the Python deps into a venv, then throw the toolchain away
########################################################################################
FROM base AS builder

# Full build toolchain + -dev headers. Present ONLY in this stage; none of it ships in the
# final image. Kept generous on purpose - builder size does not matter, build failures do.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      pkgconf \
      gfortran \
      git \
      python3-dev \
      libpq-dev \
      libssl-dev \
      libffi-dev \
      libavdevice-dev \
      libavfilter-dev \
      libavformat-dev \
      libavcodec-dev \
      libswscale-dev \
      libswresample-dev \
      libavutil-dev \
 && rm -rf /var/lib/apt/lists/*

# uv: a much faster resolver/installer than pip (static, multi-arch binary). Replaces pip
# for the build; UV_LINK_MODE=copy avoids cross-filesystem hardlink warnings in Docker.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy

# Isolated virtualenv, but --system-site-packages so it can still see the apt-installed
# PyGObject (`gi`), which is not pip-installable here. Also sidesteps Debian's PEP 668
# externally-managed-environment block entirely.
RUN uv venv --system-site-packages /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN uv pip install -r requirements.txt \
 # PyAV must be compiled against the system ffmpeg (which includes libavdevice) so that
 # webpage streaming via PyAV works; the PyPI wheel omits avdevice.
 && uv pip uninstall av \
 && uv pip install --no-binary av "av==12.0.0" \
 # Cython is only needed to build sdists at this point; drop it from the runtime venv.
 && uv pip uninstall cython

########################################################################################
# Stage 3: final - lean runtime image (base + venv + app code, no toolchain)
########################################################################################
FROM base AS final

# Bring over the fully-built virtualenv (all Python deps incl. source-compiled av/psycopg2).
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash app

ENV project=attendee
ENV cwd=/$project
WORKDIR $cwd

# Copy only what we need; set ownership/perm at copy time
COPY --chown=app:app --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=app:app . .

# Make STATIC_ROOT writeable for the non-root user so collectstatic can run at startup
RUN mkdir -p "$cwd/staticfiles" && chown -R app:app "$cwd/staticfiles"

# The app dynamically writes the Chrome managed-policy file, but only if the
# /etc/opt/chrome/... symlink exists (web_bot_adapter.py:578). Chromium, however, READS
# managed policy from /etc/chromium/.... Keep the original symlink AND add the Chromium one
# so signed-in-bot policies actually apply. Both point at a /tmp file the app can write.
RUN mkdir -p /etc/opt/chrome/policies/managed /etc/chromium/policies/managed \
  && ln -s /tmp/attendee-chrome-policies.json /etc/opt/chrome/policies/managed/attendee-chrome-policies.json \
  && ln -s /tmp/attendee-chrome-policies.json /etc/chromium/policies/managed/attendee-chrome-policies.json

# Switch to non-root AFTER copies to avoid permission flakiness
USER app

# Use tini + entrypoint; CMD can be overridden by compose
ENTRYPOINT ["/usr/bin/tini","--","/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
