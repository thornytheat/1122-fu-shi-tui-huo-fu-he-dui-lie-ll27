from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .algorithm import ReviewDesk
from .contracts import (
    DeskSettings,
    InvalidRow,
    RescanEvent,
    parse_event,
    parse_parcel,
    parse_settings,
)


@dataclass(frozen=True, slots=True)
class ReviewDeskRejection:
    line: int
    field: str
    code: str


def validate_parcels(rows: Iterable[Mapping[str, Any]]):
    accepted = []
    rejected = []
    for line, raw in enumerate(rows, 1):
        try:
            accepted.append(parse_parcel(raw))
        except InvalidRow as issue:
            rejected.append(ReviewDeskRejection(line, issue.field, issue.code))
    return accepted, rejected


def validate_events(rows: Iterable[Mapping[str, Any]]):
    accepted = []
    rejected = []
    for line, raw in enumerate(rows, 1):
        try:
            accepted.append(parse_event(raw))
        except InvalidRow as issue:
            rejected.append(ReviewDeskRejection(line, issue.field, issue.code))
    return accepted, rejected


def validate_settings(values: Mapping[str, Any]) -> DeskSettings:
    return parse_settings(values)


def _rejections(errors: list[ReviewDeskRejection]):
    return [asdict(error) for error in errors]


def open_desk(parcels: Iterable[Mapping[str, Any]], settings: Mapping[str, Any]) -> dict:
    """开班：校验排队包裹与工作台设置，输出首次派位证据。"""
    accepted, rejected = validate_parcels(parcels)
    config = validate_settings(settings)
    desk = ReviewDesk(accepted, config)
    return desk.evidence() | {
        'accepted_count': len(accepted),
        'rejected_count': len(rejected),
        'errors': _rejections(rejected),
    }


def replay(
    parcels: Iterable[Mapping[str, Any]],
    settings: Mapping[str, Any],
    batches: Iterable[Mapping[str, Any]],
    *,
    opened_rmas: Iterable[str] = (),
) -> dict:
    """一次性重放开班与全部补扫批次；批次按到达顺序处理，重复批次幂等。

    每个批次形如 ``{"batch_id": "B1", "events": [...]}``；opened_rmas 中的包裹
    在对应批次处理前标记为已开验，复核位随即锁定。
    """
    accepted, rejected = validate_parcels(parcels)
    config = validate_settings(settings)
    desk = ReviewDesk(accepted, config)

    batch_errors = []
    pending_opens = list(opened_rmas)
    for line, raw in enumerate(batches, 1):
        if not isinstance(raw, Mapping) or not isinstance(raw.get('batch_id'), str) \
                or not raw['batch_id'].strip() or not isinstance(raw.get('events'), list):
            batch_errors.append(ReviewDeskRejection(line, 'review_desk_batch', 'EXPECTED_BATCH_OBJECT'))
            continue
        events, event_errors = validate_events(raw['events'])
        for item in event_errors:
            batch_errors.append(ReviewDeskRejection(
                item.line, item.field, item.code,
            ))
        # 首批补扫处理前完成开验；批次也可用 open_before 声明自己的开验时机
        if line == 1:
            for rma in pending_opens:
                if rma in desk.entries:
                    desk.open_inspection(rma)
        for rma in raw.get('open_before', ()):
            if rma in desk.entries:
                desk.open_inspection(rma)
        desk.apply_rescan_batch(events, batch_id=raw['batch_id'].strip())

    evidence = desk.evidence()
    evidence['accepted_count'] = len(accepted)
    evidence['rejected_count'] = len(rejected)
    evidence['errors'] = _rejections(rejected)
    evidence['batch_errors'] = _rejections(batch_errors)
    return evidence


def evaluate(rows: Iterable[Mapping[str, Any]], **options) -> dict:
    """与其他模块同形的便捷入口：仅开班派位。"""
    return open_desk(rows, options.get('settings') or {
        'capacity': options.get('capacity', 9),
        'max_per_shop': options.get('max_per_shop', 3),
        'threshold': options.get('threshold', .64),
    })
