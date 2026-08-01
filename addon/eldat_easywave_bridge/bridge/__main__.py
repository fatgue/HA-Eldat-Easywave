"""Entry point for the ELDAT Easywave bridge.

Reads Home Assistant add-on options from ``/data/options.json`` when present and
otherwise falls back to environment variables, so the same image runs under the
Supervisor and on a development machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .cp210x import ELDAT_PRODUCT_IDS, Cp210xError
from .server import BridgeConfig, serve

_OPTIONS_FILE = Path("/data/options.json")

_LOGGER = logging.getLogger("eldat_bridge")


def _load_options() -> dict:
    if _OPTIONS_FILE.is_file():
        try:
            return json.loads(_OPTIONS_FILE.read_text())
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("cannot read %s: %s", _OPTIONS_FILE, err)
    return {}


def _parse_product_ids(raw: object) -> tuple[int, ...]:
    """Accept a list or comma-separated string of hex ids; empty means all."""
    if raw is None or raw == "":
        return ELDAT_PRODUCT_IDS
    items: list[str]
    if isinstance(raw, str):
        items = [part for part in raw.replace(" ", "").split(",") if part]
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        return ELDAT_PRODUCT_IDS

    parsed: list[int] = []
    for item in items:
        try:
            parsed.append(int(item, 16))
        except ValueError:
            _LOGGER.warning("ignoring unparsable product id %r", item)
    return tuple(parsed) or ELDAT_PRODUCT_IDS


def build_config() -> BridgeConfig:
    options = _load_options()

    def option(name: str, env: str, default):
        if name in options and options[name] not in (None, ""):
            return options[name]
        return os.environ.get(env, default)

    return BridgeConfig(
        host=str(option("host", "ELDAT_HOST", "0.0.0.0")),
        port=int(option("port", "ELDAT_PORT", 5000)),
        product_ids=_parse_product_ids(option("product_ids", "ELDAT_PRODUCT_IDS", None)),
        prefer_kernel=str(option("prefer_kernel", "ELDAT_PREFER_KERNEL", "true")).lower()
        not in ("false", "0", "no"),
    )


async def _run() -> int:
    config = build_config()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, signal_name), stop.set)

    server_task = asyncio.create_task(serve(config))
    stop_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait(
        {server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if server_task in done and (error := server_task.exception()):
        raise error
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ELDAT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run())
    except Cp210xError as err:
        _LOGGER.error("%s", err)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
