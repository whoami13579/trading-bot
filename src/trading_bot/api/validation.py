"""Validation shared by REST components and streaming; errors never echo inputs."""

from datetime import datetime
import math
from urllib.parse import quote

RESOLUTIONS = frozenset(
    ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR", "HOUR_4", "DAY", "WEEK")
)
STOPS = frozenset(
    (
        "guaranteedStop",
        "trailingStop",
        "stopLevel",
        "stopDistance",
        "stopAmount",
        "profitLevel",
        "profitDistance",
        "profitAmount",
    )
)


def text(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("nonempty_text_required")
    return value


def segment(value: str) -> str:
    text(value)
    if value in (".", ".."):
        raise ValueError("invalid_path_segment")
    return quote(value, safe="")


def number(value, *, positive=True):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        raise ValueError(
            "finite_positive_number_required" if positive else "finite_number_required"
        )
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("integer_out_of_range")
    return value


def boolean(value):
    if type(value) is not bool:
        raise ValueError("boolean_required")
    return value


def date(value):
    if not isinstance(value, str):
        raise ValueError("utc_date_required")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        if parsed.strftime("%Y-%m-%dT%H:%M:%S") != value:
            raise ValueError()
    except ValueError:
        raise ValueError("date_format_YYYY_MM_DDTHH_MM_SS_required") from None
    return parsed


def dates(from_, to, *, max_days=None):
    start, end = (
        date(from_) if from_ is not None else None,
        date(to) if to is not None else None,
    )
    if (
        start
        and end
        and (
            end < start
            or (
                max_days is not None
                and (end - start).total_seconds() > max_days * 86400
            )
        )
    ):
        raise ValueError("invalid_date_range")
    return {"from": from_, "to": to}


def items(values, *, maximum=None):
    if isinstance(values, str):
        values = values.split(",")
    if (
        not isinstance(values, (list, tuple))
        or not values
        or (maximum and len(values) > maximum)
    ):
        raise ValueError("invalid_instrument_list")
    result = [text(x) for x in values]
    if any("," in x for x in result):
        raise ValueError("invalid_instrument_identifier")
    return list(dict.fromkeys(result))


def stops(payload):
    if payload.keys() - STOPS:
        raise ValueError("unsupported_stop_parameter")
    for key, value in payload.items():
        boolean(value) if key in ("guaranteedStop", "trailingStop") else number(
            value, positive=not key.endswith("Level")
        )
    if payload.get("guaranteedStop") and payload.get("trailingStop"):
        raise ValueError("guaranteed_and_trailing_stops_conflict")
    if payload.get("guaranteedStop") and not any(
        k in payload for k in ("stopLevel", "stopDistance", "stopAmount")
    ):
        raise ValueError("guaranteed_stop_requires_stop")
    if payload.get("trailingStop") and "stopDistance" not in payload:
        raise ValueError("trailing_stop_requires_distance")
    for prefix in ("stop", "profit"):
        if (
            sum(
                prefix + suffix in payload for suffix in ("Level", "Distance", "Amount")
            )
            > 1
        ):
            raise ValueError("conflicting_stop_or_profit_parameters")
    return payload
