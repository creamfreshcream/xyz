FROM python:3.12-slim

# ffmpeg does the decoding, libopus is what discord.py encodes with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jellyfin_bot ./jellyfin_bot

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "jellyfin_bot"]
