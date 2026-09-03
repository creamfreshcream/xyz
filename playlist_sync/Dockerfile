# Standard library only — no pip install, no wheels to keep up to date.
FROM python:3.12-slim

WORKDIR /app
COPY jellyfin_playlist.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "jellyfin_playlist.py"]
