"""Receiver for readsb (wiedehopf/readsb), the dump1090 fork.

readsb's Beast (``--net-bo-port``, 30005) and AVR (``--net-ro-port``, 30002)
outputs are dump1090's, so this is the ``dump1090`` receiver plus readsb's
``receiver.json`` locations (see ``positions.py``) and a guard against its
BeastReduce output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pyaerial.receivers import register_receiver
from pyaerial.receivers.dump1090 import Dump1090Receiver

# readsb's --net-beast-reduce-out-port has no default; 30006 is where
# sdr-enthusiasts/docker-adsb-ultrafeeder always serves it.
REDUCE_PORT = 30006


@register_receiver("readsb")
class ReadsbReceiver(Dump1090Receiver):
    known_options = Dump1090Receiver.known_options | {"allow_reduced"}
    feeder = "readsb"

    @classmethod
    def validate_options(
        cls, options: dict[str, Any], *, config_dir: Path
    ) -> list[str]:
        problems = super().validate_options(options, config_dir=config_dir)
        if problems:
            return problems
        allow = options.get("allow_reduced", False)
        if not isinstance(allow, bool):
            return [f"options.allow_reduced: must be true or false, not {allow!r}"]
        try:
            endpoint = cls.endpoint(options)
        except ValueError:
            return []  # a non-numeric port fails when the receiver is built
        if (
            endpoint.port == REDUCE_PORT
            and endpoint.wire_format == "beast"
            and not allow
        ):
            return [
                f"port {REDUCE_PORT} is readsb's BeastReduce output in common setups "
                "(ultrafeeder): rate-limited and deduplicated, so the integrity "
                "checks would miss most frames. Use the full Beast output "
                "(--net-bo-port, usually 30005), or set options.allow_reduced: true "
                f"if port {REDUCE_PORT} serves full Beast."
            ]
        return []
