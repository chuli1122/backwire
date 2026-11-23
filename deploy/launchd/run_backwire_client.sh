#!/bin/sh
set -eu

# Parse credentials as data; never source this file as shell code.
env_file="${BACKWIRE_ENV_FILE:-$HOME/.config/backwire/client.env}"
if [ -L "$env_file" ] || [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
    echo "backwire: environment file must be a readable regular non-symlink file" >&2
    exit 1
fi
if [ "$(stat -f '%Su' "$env_file")" != "$(id -un)" ]; then
    echo "backwire: environment file must be owned by the current user" >&2
    exit 1
fi
if [ "$(stat -f '%Lp' "$env_file")" != "600" ]; then
    echo "backwire: environment file permissions must be 600" >&2
    exit 1
fi
while IFS='=' read -r key value || [ -n "$key" ]; do
    case "$key" in
        BACKWIRE_URL|BACKWIRE_TOKEN|BACKWIRE_NAME|BACKWIRE_KIND|BACKWIRE_WORKDIR)
            case "$value" in *[\\\`\$\(\)\;\|\&]*) echo "backwire: invalid environment value" >&2; exit 1;; esac
            export "$key=$value" ;;
        ""|\#*) : ;;
        *) echo "backwire: unknown environment key: $key" >&2; exit 1 ;;
    esac
done < "$env_file"
exec /usr/bin/python3 "$HOME/bin/backwire_client.py"
