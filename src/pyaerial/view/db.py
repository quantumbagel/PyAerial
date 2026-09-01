"""Mongo helpers for the interactive flight viewer."""

from __future__ import annotations

import pymongo

_VIEW_DB_NAME: str | None = None


def set_view_db_name(name: str | None) -> None:
    global _VIEW_DB_NAME
    _VIEW_DB_NAME = name


def get_mongo_db(
    client: pymongo.MongoClient | None,
) -> pymongo.database.Database | None:
    if client is None:
        return None
    try:
        if hasattr(client, "admin"):
            client.admin.command("ping")
        if _VIEW_DB_NAME:
            return client.get_database(_VIEW_DB_NAME)
        try:
            return client.get_default_database()
        except Exception:
            return client.get_database("pyaerial")
    except Exception:
        return None
