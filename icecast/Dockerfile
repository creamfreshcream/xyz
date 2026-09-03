# Debian's icecast2 with the config rendered from environment variables at start.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        icecast2 gettext-base media-types ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/icecast2 \
    && chown -R icecast2:icecast /var/log/icecast2

COPY icecast.xml.template /etc/icecast2/icecast.xml.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Icecast refuses to run as root, and as its own user it can still write the
# container's stdout/stderr for logging.
USER icecast2

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
