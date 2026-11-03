"""Tiny in-process stand-in for the redis-py calls RedisLiveStore makes."""

from __future__ import annotations

from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, float] = {}  # key -> seconds from the last SET EX / EXPIRE
        self.calls: list[str] = []
        self.fail: set[str] = set()  # command names that raise
        self.down = False
        self.closed = False
        self._in_pipeline = False

    def _hit(self, name: str) -> None:
        if not self._in_pipeline:
            self.calls.append(name)  # one entry per network round trip
        if self.down or name in self.fail:
            raise RedisConnectionError(f"fake {name} failure")

    # connection
    def ping(self) -> bool:
        self._hit("ping")
        return True

    def close(self) -> None:
        self.closed = True

    def publish(self, channel: str, data: str) -> int:
        self._hit("publish")
        return 0

    # strings
    def get(self, key: str) -> str | None:
        self._hit("get")
        return self.kv.get(key)

    def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool:
        self._hit("set")
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        else:
            self.ttls.pop(key, None)
        return True

    def expire(self, key: str, seconds: int) -> bool:
        self._hit("expire")
        if not any(
            key in store for store in (self.kv, self.sets, self.hashes, self.zsets)
        ):
            return False
        self.ttls[key] = seconds
        return True

    def ttl(self, key: str) -> int:
        self._hit("ttl")
        if not any(
            key in store for store in (self.kv, self.sets, self.hashes, self.zsets)
        ):
            return -2
        return int(self.ttls.get(key, -1))

    def lapse(self, *keys: str) -> None:
        """Simulate the TTL running out (no round trip)."""
        for key in keys:
            self.ttls.pop(key, None)
            for store in (self.kv, self.sets, self.hashes, self.zsets):
                store.pop(key, None)

    def delete(self, *keys: str) -> int:
        self._hit("delete")
        n = 0
        for key in keys:
            self.ttls.pop(key, None)
            for store in (self.kv, self.sets, self.hashes, self.zsets):
                if store.pop(key, None) is not None:
                    n += 1
        return n

    # sets
    def sadd(self, key: str, *members: str) -> int:
        self._hit("sadd")
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def srem(self, key: str, *members: str) -> int:
        self._hit("srem")
        for member in members:
            self.sets.get(key, set()).discard(member)
        return len(members)

    def smembers(self, key: str) -> set[str]:
        self._hit("smembers")
        return set(self.sets.get(key, set()))

    def scard(self, key: str) -> int:
        self._hit("scard")
        return len(self.sets.get(key, set()))

    # hashes
    def hset(self, key: str, field: str, value: str) -> int:
        self._hit("hset")
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hget(self, key: str, field: str) -> str | None:
        self._hit("hget")
        return self.hashes.get(key, {}).get(field)

    def hdel(self, key: str, *fields: str) -> int:
        self._hit("hdel")
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)
        return len(fields)

    def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        self._hit("hmget")
        stored = self.hashes.get(key, {})
        return [stored.get(field) for field in fields]

    def hvals(self, key: str) -> list[str]:
        self._hit("hvals")
        return list(self.hashes.get(key, {}).values())

    def hgetall(self, key: str) -> dict[str, str]:
        self._hit("hgetall")
        return dict(self.hashes.get(key, {}))

    # sorted sets
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._hit("zadd")
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def _sorted(self, key: str) -> list[tuple[str, float]]:
        return sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        self._hit("zrange")
        items = [m for m, _ in self._sorted(key)]
        return items[start : None if end == -1 else end + 1]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        self._hit("zrevrange")
        items = [m for m, _ in reversed(self._sorted(key))]
        return items[start : None if end == -1 else end + 1]

    @staticmethod
    def _bound(text: str | float) -> tuple[float, bool]:
        """(value, exclusive) from a Redis score bound like '(1.5' or '+inf'."""
        text = str(text)
        exclusive = text.startswith("(")
        return float(text.lstrip("(")), exclusive

    def _in_range(self, score: float, low: str, high: str) -> bool:
        lo, lo_ex = self._bound(low)
        hi, hi_ex = self._bound(high)
        above = score > lo if lo_ex else score >= lo
        below = score < hi if hi_ex else score <= hi
        return above and below

    def zrangebyscore(
        self,
        key: str,
        low: str,
        high: str,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        self._hit("zrangebyscore")
        items = [
            m for m, score in self._sorted(key) if self._in_range(score, low, high)
        ]
        if start is not None and num is not None:
            items = items[start : start + num]
        return items

    def zrevrangebyscore(
        self,
        key: str,
        high: str,
        low: str,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        self._hit("zrevrangebyscore")
        items = [
            m
            for m, score in reversed(self._sorted(key))
            if self._in_range(score, low, high)
        ]
        if start is not None and num is not None:
            items = items[start : start + num]
        return items

    def zremrangebyscore(self, key: str, low: str, high: float) -> int:
        self._hit("zremrangebyscore")
        zs = self.zsets.get(key, {})
        doomed = [m for m, score in zs.items() if score <= float(high)]
        for member in doomed:
            zs.pop(member)
        return len(doomed)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        self._hit("pipeline")
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.ops: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Any:
        def queue(*args: Any, **kwargs: Any) -> FakePipeline:
            self.ops.append((name, args, kwargs))
            return self

        return queue

    def execute(self) -> list[Any]:
        self.client._hit("execute")
        self.client._in_pipeline = True
        try:
            return [getattr(self.client, name)(*a, **kw) for name, a, kw in self.ops]
        finally:
            self.client._in_pipeline = False
