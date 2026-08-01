#!/bin/sh
# Deliberately plain sh, not bashio.
#
# bashio reads the add-on configuration through the Supervisor API, which needs
# the `hassio_api` permission -- and without it every start logs "Unable to
# access the API, forbidden". The bridge already reads /data/options.json
# directly, so asking for API access just to fetch a log level would be a
# permission granted for nothing.
set -e

cd /app
exec python3 -m bridge
