"""Alerter that publishes alerts to a Kafka topic (topic == alert level)."""
from __future__ import annotations

import json

import kafka
from kafka.errors import KafkaError, NoBrokersAvailable

from pyaerial.alerters import Alerter, register_alerter
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_PAYLOAD,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_CALLSIGN,
    STORE_ICAO,
)

KAFKA_ARGUMENT_SERVER = "server"


@register_alerter("kafka")
class KafkaAlerter(Alerter):
    def configure(self, arguments: dict) -> None:
        if KAFKA_ARGUMENT_SERVER not in arguments:
            raise KeyError("kafka alerter requires a 'server' argument")
        self.server = arguments[KAFKA_ARGUMENT_SERVER]
        self._producer: kafka.KafkaProducer | None = None

    def _get_producer(self) -> kafka.KafkaProducer | None:
        # Reuse a single producer instead of recreating one per alert.
        if self._producer is None:
            try:
                self._producer = kafka.KafkaProducer(bootstrap_servers=[self.server])
            except NoBrokersAvailable:
                self.log.error("No Kafka brokers available at %s; alert dropped.", self.server)
                return None
        return self._producer

    def alert(self, meta: dict, payload: dict) -> None:
        producer = self._get_producer()
        if producer is None:
            return
        data = {
            STORE_CALLSIGN: meta.get(STORE_CALLSIGN),
            ALERT_CAT_TYPE: meta[ALERT_CAT_TYPE],
            ALERT_CAT_ZONE: meta[ALERT_CAT_ZONE],
            ALERT_CAT_ETA: meta[ALERT_CAT_ETA],
            ALERT_CAT_PAYLOAD: payload,
        }
        try:
            # send() is asynchronous; the producer's background thread delivers
            # buffered records and close() flushes on shutdown. Avoiding a
            # per-alert flush() keeps the alert dispatch pool non-blocking.
            producer.send(meta[ALERT_CAT_TYPE],
                          key=meta[STORE_ICAO].encode("utf-8"),
                          value=json.dumps(data).encode("utf-8"))
        except KafkaError as exc:
            self.log.error("Failed to send Kafka alert for %s: %s", meta[STORE_ICAO], exc)
            self._producer = None  # force reconnect next time

    def close(self) -> None:
        if self._producer is not None:
            self._producer.close()
            self._producer = None
