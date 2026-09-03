# Needs psycopg2 to read AudioMuse-AI's mood data straight out of Postgres —
# everything else here is stdlib, same as playlist_sync's Dockerfile.
FROM python:3.12-slim

RUN pip install --no-cache-dir psycopg2-binary==2.9.9

WORKDIR /app
COPY dj.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "dj.py"]
