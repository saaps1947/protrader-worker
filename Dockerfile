# Playwright's official image — Chromium and every system library it needs
# (libnss3, libatk-bridge2.0-0, fonts, etc.) are already baked in. This is
# what avoids the apt-get/root problem entirely: nothing needs to install
# system packages at Render's build stage, because they're already in the
# base image.
#
# VERSION MATTERS: this tag must match the playwright version pinned in
# requirements.txt exactly. The Python bindings and the browser build are
# tightly coupled — a mismatch causes a real runtime failure, not a build
# warning. If you ever bump one, bump both together.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# No `playwright install` step needed — Chromium is already in this image.

COPY worker.py .

# Unbuffered so print() output shows up in Render's Logs immediately,
# not batched/delayed.
ENV PYTHONUNBUFFERED=1

CMD ["python", "worker.py"]
