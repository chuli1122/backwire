#!/bin/sh
# Container startup template. Set BACKWIRE_URL, BACKWIRE_TOKEN, BACKWIRE_NAME,
# and BACKWIRE_WORKDIR (for example /tmp/backwire-work) in the container environment.
# BACKWIRE_WORKDIR limits file APIs; it is not an OS sandbox for shell commands.
exec python3 /opt/backwire/backwire_client.py "$@"
