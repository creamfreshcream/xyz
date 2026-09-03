#!/bin/sh
set -eu

: "${ICECAST_SOURCE_PASSWORD:=hackme}"
: "${ICECAST_RELAY_PASSWORD:=$ICECAST_SOURCE_PASSWORD}"
: "${ICECAST_ADMIN_USER:=admin}"
: "${ICECAST_ADMIN_PASSWORD:=hackme}"
: "${ICECAST_HOSTNAME:=localhost}"
: "${ICECAST_MAX_LISTENERS:=200}"
: "${ICECAST_LOCATION:=Earth}"
: "${ICECAST_ADMIN_EMAIL:=admin@example.com}"
export ICECAST_SOURCE_PASSWORD ICECAST_RELAY_PASSWORD ICECAST_ADMIN_USER \
    ICECAST_ADMIN_PASSWORD ICECAST_HOSTNAME ICECAST_MAX_LISTENERS \
    ICECAST_LOCATION ICECAST_ADMIN_EMAIL

# Substitute only the variables we know about, so nothing else in the template
# can be eaten by accident. The container runs unprivileged, hence /tmp.
envsubst '$ICECAST_SOURCE_PASSWORD $ICECAST_RELAY_PASSWORD $ICECAST_ADMIN_USER $ICECAST_ADMIN_PASSWORD $ICECAST_HOSTNAME $ICECAST_MAX_LISTENERS $ICECAST_LOCATION $ICECAST_ADMIN_EMAIL' \
    < /etc/icecast2/icecast.xml.template > /tmp/icecast.xml

exec icecast2 -c /tmp/icecast.xml "$@"
