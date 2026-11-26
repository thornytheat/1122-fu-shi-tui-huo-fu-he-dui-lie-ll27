from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any


class InvalidRow(ValueError):
    def __init__(self, field: str, code: str):
        super().__init__(f'{field}: {code}')
        self.field = field
        self.code = code


def integer(row: Mapping, key: str, low: int, high: int, field: str) -> int:
    value = row.get(key)
    if type(value) is not int:
        raise InvalidRow(field, 'EXPECTED_INTEGER')
    if not low <= value <= high:
        raise InvalidRow(field, 'OUT_OF_RANGE')
    return value


def number(row: Mapping, key: str, low: float, high: float, field: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise InvalidRow(field, 'EXPECTED_NUMBER')
    if not low <= value <= high or not math.isfinite(value):
        raise InvalidRow(field, 'OUT_OF_RANGE')
    return float(value)


def text(row: Mapping, key: str, field: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRow(field, 'EXPECTED_NONEMPTY_TEXT')
    if len(value.strip()) > 80:
        raise InvalidRow(field, 'TEXT_TOO_LONG')
    return value.strip()


def flag(row: Mapping, key: str, field: str) -> bool:
    value = row.get(key)
    if type(value) is not bool:
        raise InvalidRow(field, 'EXPECTED_FLAG')
    return value


@dataclass(frozen=True, slots=True)
class DeskParcel:
    """当班排队的退货包裹，沿用复核配额的概率与门店额度口径。"""
    rma: str
    original_shop: str
    listed_weight: int
    measured_weight: int
    score: float
    in_scope: bool
    amount: int
    arrival: int
    priority: bool
    seal: bool


@dataclass(frozen=True, slots=True)
class RescanEvent:
    """一次补扫称重事件；同批相同 rma 可携带多个修订版本。"""
    rma: str
    weight: int
    revision: int


@dataclass(frozen=True, slots=True)
class DeskSettings:
    capacity: int
    max_per_shop: int
    threshold: float


def parse_parcel(values: Mapping[str, Any]) -> DeskParcel:
    if not isinstance(values, Mapping):
        raise InvalidRow('review_desk', 'EXPECTED_OBJECT')
    rma = text(values, 'rma', 'review_desk.rma')
    original_shop = text(values, 'original_shop', 'review_desk.original_shop')
    listed_weight = integer(values, 'listed_weight', 1, 90000, 'review_desk.listed_weight')
    measured_weight = integer(values, 'measured_weight', 1, 90000, 'review_desk.measured_weight')
    score = number(values, 'score', 0, 1, 'review_desk.score')
    in_scope = flag(values, 'in_scope', 'review_desk.in_scope')
    amount = integer(values, 'amount', 1, 1000000, 'review_desk.amount')
    arrival = integer(values, 'arrival', 0, 10000000, 'review_desk.arrival')
    priority = flag(values, 'priority', 'review_desk.priority')
    seal = flag(values, 'seal', 'review_desk.seal')
    return DeskParcel(
        rma=rma,
        original_shop=original_shop,
        listed_weight=listed_weight,
        measured_weight=measured_weight,
        score=score,
        in_scope=in_scope,
        amount=amount,
        arrival=arrival,
        priority=priority,
        seal=seal,
    )


def parse_event(values: Mapping[str, Any]) -> RescanEvent:
    if not isinstance(values, Mapping):
        raise InvalidRow('review_desk_event', 'EXPECTED_OBJECT')
    rma = text(values, 'rma', 'review_desk_event.rma')
    weight = integer(values, 'weight', 1, 90000, 'review_desk_event.weight')
    revision = integer(values, 'revision', 1, 200, 'review_desk_event.revision')
    return RescanEvent(rma=rma, weight=weight, revision=revision)


def parse_settings(values: Mapping[str, Any]) -> DeskSettings:
    if not isinstance(values, Mapping):
        raise InvalidRow('review_desk_settings', 'EXPECTED_OBJECT')
    capacity = integer(values, 'capacity', 1, 100, 'review_desk_settings.capacity')
    max_per_shop = integer(values, 'max_per_shop', 1, 100, 'review_desk_settings.max_per_shop')
    threshold = number(values, 'threshold', 0, 1, 'review_desk_settings.threshold')
    if threshold in (0.0, 1.0):
        raise InvalidRow('review_desk_settings.threshold', 'OUT_OF_RANGE')
    return DeskSettings(capacity=capacity, max_per_shop=max_per_shop, threshold=threshold)
