"""Formatting helpers for the interactive flight viewer."""

from __future__ import annotations

import datetime


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}b"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}kb"
    return f"{size_bytes / (1024 * 1024):.2f}mb"


def format_timestamp(ts: float | None) -> str:
    if not ts:
        return "n/a"
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%B %d, %Y %I:%M %p (UTC)")


def format_duration(seconds: float) -> str:
    total_sec = int(round(seconds))
    mins, secs = divmod(total_sec, 60)
    hours, mins = divmod(mins, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins > 0 or hours > 0:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


def packet_field_name(field: str) -> str:
    mapping = {
        "latitude": "Latitude/Longitude",
        "longitude": "Latitude/Longitude",
        "altitude": "Altitude",
        "speed": "Speeds",
        "heading": "Velocities",
        "vertical_speed": "Velocities",
    }
    return mapping.get(field, field.capitalize())
