from dataclasses import asdict

import pytest

from returnscope.review_desk.algorithm import (
    BATCH_APPLIED,
    BATCH_CONFLICT,
    BATCH_INERT,
    INSPECTING,
    RELEASED,
    SEATED,
    WAITING,
    WITHDRAWN,
    ReviewDesk,
    corrected_score,
)
from returnscope.review_desk.contracts import (
    DeskParcel,
    DeskSettings,
    RescanEvent,
    parse_event,
    parse_parcel,
    parse_settings,
    InvalidRow,
)

SETTINGS = DeskSettings(capacity=3, max_per_shop=2, threshold=.64)


def parcel(name='R1', shop='SX', **changes):
    base = dict(rma=name, original_shop=shop, listed_weight=1000, measured_weight=1000,
                score=.72, in_scope=True, amount=10000, arrival=100,
                priority=False, seal=True)
    base.update(changes)
    return DeskParcel(**base)


def event(rma, weight, revision):
    return RescanEvent(rma=rma, weight=weight, revision=revision)


def by_rma(desk):
    return {entry.parcel.rma: entry for entry in desk.entries.values()}


def slots(desk):
    return {row['rma']: row['desk_slot'] for row in desk.evidence()['records']
            if row['desk_slot'] is not None}


# ---------------------------------------------------------------- 规则 1/2：补扫重派
def test_rescan_reopens_dispatch_for_waiting_parcel():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    assert set(slots(desk)) == {'R1', 'R2', 'R3'}
    audit = desk.apply_rescan_batch([event('R4', 1300, 2)], batch_id='B1')
    assert audit['reason'] == BATCH_APPLIED and audit['applied_rmas'] == ['R4']
    # 重派后 R4 凭更正后的高暴露进入复核位
    assert set(slots(desk)) == {'R1', 'R2', 'R4'}
    assert by_rma(desk)['R3'].state == WAITING


def test_weight_correction_recomputes_score_and_queue_position():
    desk = ReviewDesk([parcel('R1', shop='S1', score=.65, amount=10000),
                       parcel('R2', shop='S2', score=.66, amount=10000),
                       parcel('R3', shop='S3', score=.90, amount=20000)], SETTINGS)
    assert slots(desk) == {'R1': 3, 'R2': 2, 'R3': 1}
    # R1 称重更正为严重偏差，错件概率应上升并抢到更靠前的顺位
    desk.apply_rescan_batch([event('R1', 1900, 3)], batch_id='B1')
    entries = by_rma(desk)
    assert entries['R1'].parcel.measured_weight == 1900
    assert entries['R1'].revision == 3
    assert entries['R1'].parcel.score > .65
    records = {row['rma']: row for row in desk.evidence()['records']}
    assert records['R1']['desk_slot'] < records['R2']['desk_slot']


def test_score_correction_is_monotonic_and_bounded():
    base = parcel('R1', listed_weight=1000, score=.5)
    small = corrected_score(base, 1010)
    large = corrected_score(base, 1900)
    assert .5 < small < large < 1
    zero = DeskParcel(rma='Z', original_shop='SX', listed_weight=1000, measured_weight=1000,
                      score=0.0, in_scope=True, amount=100, arrival=1, priority=False, seal=True)
    assert corrected_score(zero, 2000) > 0
    out_of_scope = DeskParcel(rma='O', original_shop='SX', listed_weight=1000, measured_weight=1000,
                              score=0.0, in_scope=False, amount=100, arrival=1, priority=False, seal=True)
    assert corrected_score(out_of_scope, 5000) == 0.0  # 范围外不回到自动评分


def test_correction_below_threshold_releases_slot_for_redispatch():
    desk = ReviewDesk([parcel('R1', shop='S1', score=.65, amount=30000, measured_weight=1900),
                       parcel('R2', shop='S2', score=.9), parcel('R3', shop='S3', score=.88)],
                      DeskSettings(capacity=2, max_per_shop=2, threshold=.64))
    assert set(slots(desk)) == {'R1', 'R2'}
    # R1 补扫回申报重量，偏差消失使概率回落到阈值以下，释放复核位给 R3
    desk.apply_rescan_batch([event('R1', 1000, 2)], batch_id='B1')
    entries = by_rma(desk)
    assert entries['R1'].parcel.score < .64
    assert entries['R1'].state == RELEASED and entries['R1'].desk_slot is None
    assert set(slots(desk)) == {'R2', 'R3'}


# ---------------------------------------------------------------- 规则 1：已开验保留复核位
def test_opened_parcel_keeps_slot_and_position_through_redispatch():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    desk.open_inspection('R1')
    entries = by_rma(desk)
    assert entries['R1'].state == INSPECTING and entries['R1'].desk_slot == 1
    # 让 R4 带着极端称重补扫，顺位本应排到最前
    desk.apply_rescan_batch([event('R4', 5000, 2)], batch_id='B1')
    evidence = desk.evidence()
    records = {row['rma']: row for row in evidence['records']}
    assert records['R1']['state'] == INSPECTING and records['R1']['desk_slot'] == 1
    assert records['R1']['measured_weight'] == 1000  # 开验件未被补扫移动
    seated = [row['rma'] for row in evidence['records'] if row['desk_slot'] is not None]
    assert seated[0] == 'R1'


def test_cannot_open_inspection_for_waiting_parcel():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    with pytest.raises(ValueError, match='PARCEL_NOT_SEATED'):
        desk.open_inspection('R4')


# ---------------------------------------------------------------- 规则 3：同版矛盾连已开验一起撤回
def test_same_version_conflict_withdraws_even_opened_parcel_and_refills_slot():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    desk.open_inspection('R1')
    audit = desk.apply_rescan_batch([
        event('R1', 1100, 5),
        event('R1', 1200, 5),
    ], batch_id='B1')
    assert audit['reason'] == BATCH_CONFLICT
    assert audit['conflict_rmas'] == ['R1']
    entries = by_rma(desk)
    assert entries['R1'].state == WITHDRAWN
    assert entries['R1'].desk_slot is None and entries['R1'].opened is False
    # 空位当班补齐：等待中的 R4 顶上
    seated = sorted(slots(desk))
    assert 'R1' not in seated and len(seated) == 3
    assert set(seated) == {'R2', 'R3', 'R4'}


def test_withdrawn_parcel_never_returns_in_later_batches():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    desk.apply_rescan_batch([event('R1', 1100, 2), event('R1', 1200, 2)], batch_id='B1')
    assert by_rma(desk)['R1'].state == WITHDRAWN
    desk.apply_rescan_batch([event('R1', 1150, 3)], batch_id='B2')
    assert by_rma(desk)['R1'].state == WITHDRAWN
    assert by_rma(desk)['R1'].desk_slot is None


# ---------------------------------------------------------------- 规则 4：末位空位范围外优先
def test_last_open_slot_prefers_out_of_scope_over_high_scorer():
    lock = parcel('LOCK', score=.95, amount=90000)
    high = parcel('HIGH', score=.99, amount=90000)          # 暴露远高于范围外件
    forced = parcel('MANUAL', score=0.0, in_scope=False, amount=100)
    desk = ReviewDesk([lock, high, forced],
                      DeskSettings(capacity=2, max_per_shop=3, threshold=.64))
    assert set(slots(desk)) == {'LOCK', 'HIGH'}             # 常规顺位 HIGH 凭高暴露先派
    desk.open_inspection('LOCK')                            # 锁定一个复核位
    # LOCK 补扫回申报重量触发重派：只剩一个空位，范围外 MANUAL 压过高分 HIGH
    desk.apply_rescan_batch([event('LOCK', 1000, 2)], batch_id='B1')
    evidence = desk.evidence()
    seated = {row['rma']: row['desk_slot'] for row in evidence['records']
              if row['desk_slot'] is not None}
    assert seated == {'LOCK': 1, 'MANUAL': 2}
    records = {row['rma']: row for row in evidence['records']}
    assert records['HIGH']['state'] == WAITING
    assert records['HIGH']['reason'] == 'DESK_FULL'


def test_normal_dispatch_ranks_by_exposure_before_last_slot_rule():
    # 还有两个空位时不启用末位规则，高暴露的高分件应排在低额范围外件之前
    high = parcel('HIGH', score=.99, amount=90000)
    forced = parcel('MANUAL', score=0.0, in_scope=False, amount=100)
    other = parcel('OTHER', score=.8, amount=50000)
    desk = ReviewDesk([high, forced, other],
                      DeskSettings(capacity=3, max_per_shop=3, threshold=.64))
    seated = [row['rma'] for row in desk.evidence()['records'] if row['desk_slot'] is not None]
    assert seated.index('HIGH') < seated.index('MANUAL')


def test_last_slot_rule_still_respects_shop_cap():
    locked = parcel('LOCK', shop='SX', score=.95, amount=90000)
    forced_sx = parcel('M1', shop='SX', score=0.0, in_scope=False, amount=100)
    high_sy = parcel('H1', shop='SY', score=.99, amount=90000)
    desk = ReviewDesk([locked, forced_sx, high_sy],
                      DeskSettings(capacity=2, max_per_shop=1, threshold=.64))
    assert set(slots(desk)) == {'LOCK', 'H1'}
    desk.open_inspection('LOCK')  # SX 门店额度被锁定件占满
    # 重派后只剩一个空位：范围外的 SX 件受门店上限挡住，SY 的高分件保留位置
    desk.apply_rescan_batch([event('LOCK', 1000, 2)], batch_id='B1')
    evidence = desk.evidence()
    seated = {row['rma'] for row in evidence['records'] if row['desk_slot'] is not None}
    assert seated == {'LOCK', 'H1'}
    records = {row['rma']: row for row in evidence['records']}
    assert records['M1']['reason'] == 'SHOP_CONCENTRATION'


# ---------------------------------------------------------------- 规则 5：乱序旧版本不覆盖
def test_out_of_order_older_revision_never_overwrites_new_weight():
    desk = ReviewDesk([parcel('R1')], SETTINGS)
    desk.apply_rescan_batch([event('R1', 1300, 4)], batch_id='B1')
    assert by_rma(desk)['R1'].parcel.measured_weight == 1300
    # 修订号 3 的补扫乱序晚到，属于旧版本
    audit = desk.apply_rescan_batch([event('R1', 900, 3)], batch_id='B2')
    assert audit['reason'] == BATCH_INERT and audit['inert_rmas'] == ['R1']
    entry = by_rma(desk)['R1']
    assert entry.parcel.measured_weight == 1300 and entry.revision == 4


def test_mixed_order_batch_uses_only_highest_revision():
    desk = ReviewDesk([parcel('R1')], SETTINGS)
    audit = desk.apply_rescan_batch([
        event('R1', 900, 2),
        event('R1', 1400, 5),
        event('R1', 800, 3),
    ], batch_id='B1')
    assert audit['reason'] == BATCH_APPLIED
    entry = by_rma(desk)['R1']
    assert entry.parcel.measured_weight == 1400 and entry.revision == 5


def test_same_revision_identical_weight_is_inert_not_conflict():
    desk = ReviewDesk([parcel('R1')], SETTINGS)
    audit = desk.apply_rescan_batch([event('R1', 1000, 1), event('R1', 1000, 1)], batch_id='B1')
    assert audit['reason'] == BATCH_INERT
    assert by_rma(desk)['R1'].revision == 1


# ---------------------------------------------------------------- 规则 6：重跑幂等
def test_rerun_same_batch_does_not_change_slots_or_weight():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    desk.apply_rescan_batch([event('R4', 1700, 2)], batch_id='B1')
    first = desk.evidence()
    audit_again = desk.apply_rescan_batch([event('R4', 1700, 2)], batch_id='B1')
    second = desk.evidence()
    assert audit_again['reason'] == BATCH_APPLIED  # 回放原审计
    assert first == second
    assert len(desk.batch_audit) == 1
    assert sum(1 for row in second['records'] if row['desk_slot'] is not None) == 3


def test_unknown_rma_is_flagged_without_touching_desk():
    desk = ReviewDesk([parcel('R1')], SETTINGS)
    audit = desk.apply_rescan_batch([event('GHOST', 1200, 2)], batch_id='B1')
    assert audit['unknown_rmas'] == ['GHOST']
    assert desk.evidence()['occupied_slots'] == 1


# ---------------------------------------------------------------- 边界与组合场景
def test_empty_batch_is_reported_and_does_not_consume_a_batch_id_slot():
    desk = ReviewDesk([parcel('R1')], SETTINGS)
    audit = desk.apply_rescan_batch([], batch_id='B0')
    assert audit['reason'] == 'BATCH_EMPTY' and audit['event_count'] == 0
    # 同一 batch_id 的有效批次仍可正常处理（空批次只记账不锁号）
    follow = desk.apply_rescan_batch([event('R1', 1300, 2)], batch_id='B0')
    assert follow['reason'] == BATCH_APPLIED


def test_conflict_batch_still_applies_other_parcels_corrections():
    desk = ReviewDesk([parcel('R1', shop='S1'), parcel('R2', shop='S2'),
                       parcel('R3', shop='S3'), parcel('R4', shop='S4')], SETTINGS)
    audit = desk.apply_rescan_batch([
        event('R1', 1100, 4), event('R1', 1200, 4),   # R1 同版矛盾撤回
        event('R4', 1800, 2),                          # R4 正常更正
    ], batch_id='B1')
    assert audit['reason'] == BATCH_CONFLICT
    assert audit['conflict_rmas'] == ['R1'] and audit['applied_rmas'] == ['R4']
    entries = by_rma(desk)
    assert entries['R1'].state == WITHDRAWN
    assert entries['R4'].parcel.measured_weight == 1800
    # R1 撤回与 R4 更正同时发生，空位当班补齐，占位数不变
    assert desk.evidence()['occupied_slots'] == 3


def test_last_slot_falls_back_to_scoped_when_forced_parcel_hits_shop_cap():
    locked = parcel('LOCK', shop='SX', score=.95, amount=90000)
    forced_sx = parcel('M1', shop='SX', score=0.0, in_scope=False, amount=100)
    high_sy = parcel('H1', shop='SY', score=.99, amount=90000)
    other_sz = parcel('H2', shop='SZ', score=.9, amount=80000)
    desk = ReviewDesk([locked, forced_sx, high_sy, other_sz],
                      DeskSettings(capacity=4, max_per_shop=1, threshold=.64))
    desk.open_inspection('LOCK')
    # 重派后剩三个空位：SX 强制件撞门店上限，两个外店高分件按顺位补位
    desk.apply_rescan_batch([event('LOCK', 1000, 2)], batch_id='B1')
    evidence = desk.evidence()
    seated = {row['rma'] for row in evidence['records'] if row['desk_slot'] is not None}
    assert seated == {'LOCK', 'H1', 'H2'}
    records = {row['rma']: row for row in evidence['records']}
    assert records['M1']['reason'] == 'SHOP_CONCENTRATION'
    assert evidence['open_slots'] == 1  # 门店上限挡住强制件后，空位保留不强行占用


def test_priority_and_unsealed_lead_the_waiting_order():
    urgent = parcel('U', score=.66, priority=True)
    open_p = parcel('O', score=.66, seal=False)
    plain = parcel('P', score=.66)
    desk = ReviewDesk([urgent, open_p, plain],
                      DeskSettings(capacity=3, max_per_shop=3, threshold=.64))
    seated = [row['rma'] for row in desk.evidence()['records'] if row['desk_slot'] is not None]
    assert seated == ['U', 'O', 'P']


def test_released_parcel_never_occupies_a_slot():
    low = parcel('LOW', score=.1)
    desk = ReviewDesk([low], DeskSettings(capacity=2, max_per_shop=2, threshold=.64))
    row = desk.evidence()['records'][0]
    assert row['state'] == RELEASED and row['desk_slot'] is None
    assert desk.evidence()['occupied_slots'] == 0 and desk.evidence()['open_slots'] == 2


# ---------------------------------------------------------------- 契约校验
def test_contract_validation_ranges():
    with pytest.raises(InvalidRow):
        parse_parcel(asdict(parcel('R1')) | {'measured_weight': 0})
    with pytest.raises(InvalidRow):
        parse_event({'rma': 'R1', 'weight': 100, 'revision': 0})
    with pytest.raises(InvalidRow):
        parse_settings({'capacity': 0, 'max_per_shop': 1, 'threshold': .5})
    with pytest.raises(InvalidRow):
        parse_settings({'capacity': 2, 'max_per_shop': 1, 'threshold': 1.0})


def test_duplicate_parcel_rma_is_rejected():
    with pytest.raises(ValueError, match='DUPLICATE_DESK_PARCEL'):
        ReviewDesk([parcel('R1'), parcel('R1')], SETTINGS)
