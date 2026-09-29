"""MongoDB implementation of the ``SampleSource`` port (ADR-0007).

Separate from the reconciler because the cold tier is still open (the rest of D6). If
Parquet on object storage lands later, a second implementation of this port lets
reconciliation read beyond the Mongo TTL without touching the logic that uses it.

The TTL is read from configuration rather than from MongoDB's index metadata. Both would
work, and the config is the value the deployment actually intends - an index that has
drifted from it is a deployment bug, not a new source of truth.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson

from fleet_common.settings import MongoSettings

logger = logging.getLogger(__name__)

#: Safety margin on the retention floor. MongoDB's TTL monitor runs about once a minute and
#: deletion is not instantaneous, so a document one second inside the window may already be
#: gone. Treating the last hour of the window as untrustworthy costs one extra
#: unreconcilable day at the boundary and avoids recomputing a segment from a set that was
#: being deleted while it was read - which would look current and be wrong.
TTL_SAFETY_MARGIN = timedelta(hours=1)


class MongoSampleSource:
    """Reads a vehicle-day's retained raw samples, in event-time order."""

    def __init__(self, settings: MongoSettings, *, client: Any = None) -> None:
        if client is None:
            from pymongo import MongoClient

            client = MongoClient(
                settings.mongo_uri,
                serverSelectionTimeoutMS=5_000,
                connectTimeoutMS=5_000,
                socketTimeoutMS=30_000,
            )
        self._client = client
        self._collection = client[settings.mongo_db][settings.mongo_raw_collection]
        self._ttl_seconds = settings.mongo_raw_ttl_seconds

    def close(self) -> None:
        self._client.close()

    def retention_floor(self) -> datetime:
        """The earliest event time whose day can still be assumed complete.

        Derived from ``ingest_ts``, which is what the TTL index is built on - not from event
        time. That distinction is deliberate and it is ADR-0004's: a backdated event must not
        be able to expire the moment it is written. It does mean the floor is approximate for
        replayed history, where ingest and event time diverge widely; the margin above
        absorbs the ordinary case and the unreconcilable count reports the rest.
        """
        return datetime.now(UTC) - timedelta(seconds=self._ttl_seconds) + TTL_SAFETY_MARGIN

    def samples_for(self, *, vin: str, day: date) -> list[bytes]:
        """Every retained sample for that vehicle-day, ordered by event time.

        Ordered here rather than trusting insertion order, because the state machine assumes
        event-time order and the whole reason these events exist is that they arrived out of
        it. ``(vin, ts)`` is indexed, so this is an index scan rather than a collection scan.
        """
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end = start + timedelta(days=1)

        cursor = self._collection.find(
            {"vin": vin, "ts": {"$gte": start.isoformat(), "$lt": end.isoformat()}},
            # Exclude Mongo's own bookkeeping: the processor re-decodes these as telemetry
            # events, and _id and ingest_ts are not part of the event contract, so a strict
            # model would reject them.
            projection={"_id": 0, "ingest_ts": 0, "driver_erased": 0},
        ).sort("ts", 1)

        samples = [orjson.dumps(document) for document in cursor]
        logger.debug("loaded %d sample(s) for %s on %s", len(samples), vin, day)
        return samples
