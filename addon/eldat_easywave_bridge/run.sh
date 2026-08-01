#!/usr/bin/env bashio
# shellcheck shell=bash
set -e

# The bridge reads /data/options.json itself, so only the log level is mapped
# here -- bashio gives us the add-on's configured level in Supervisor terms.
declare log_level
log_level="$(bashio::config 'log_level' 'info')"

case "${log_level}" in
    trace|debug) export ELDAT_LOG_LEVEL="DEBUG" ;;
    warning)     export ELDAT_LOG_LEVEL="WARNING" ;;
    error)       export ELDAT_LOG_LEVEL="ERROR" ;;
    *)           export ELDAT_LOG_LEVEL="INFO" ;;
esac

bashio::log.info "Starting the ELDAT Easywave bridge..."

cd /app
exec python3 -m bridge
