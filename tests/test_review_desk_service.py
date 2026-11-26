import pytest

from returnscope.review_desk.service import evaluate, open_desk, replay


def parcel(name='R1', shop='SX', **changes):
    base = dict(rma=name, original_shop=shop, listed_weight=1000, measured_weight=1000,
                score=.72, in_scope=True, amount=10000, arrival=100,
                priority=False, seal=True)
    base.update(changes)
    return base


SETTINGS = {'capacity': 3, 'max_per_shop': 3, 'threshold': .64}


def test_open_desk_validates_rows_and_reports_initial_seating():
    good = [parcel('R1'), parcel('R2'), parcel('R3'), parcel('R4')]
    bad = [parcel('BAD') | {'measured_weight': 0}]
    result = open_desk(good + bad, SETTINGS)
    assert result['accepted_count'] == 4 and result['rejected_count'] == 1
    assert result['errors'][0]['field'] == 'review_desk.measured_weight'
    assert result['occupied_slots'] == 3 and result['open_slots'] == 0
    assert result['state_counts']['已派复核'] == 3
    assert result['state_counts']['待派复核'] == 1


def test_evaluate_entry_matches_other_module_shape():
    result = evaluate([parcel('R1')], capacity=2)
    assert result['accepted_count'] == 1
    assert result['records'][0]['state'] == '已派复核'
    assert result['settings']['capacity'] == 2


def test_replay_applies_batches_in_arrival_order():
    parcels = [parcel('R1', shop='S1'), parcel('R2', shop='S2'),
               parcel('R3', shop='S3'), parcel('R4', shop='S4')]
    batches = [
        {'batch_id': 'B1', 'events': [
            {'rma': 'R4', 'weight': 1700, 'revision': 2},
            {'rma': 'R4', 'weight': 900, 'revision': 1},   # 乱序旧版本同批到达
        ]},
        {'batch_id': 'B2', 'events': [
            {'rma': 'R1', 'weight': 1100, 'revision': 3},
            {'rma': 'R1', 'weight': 1200, 'revision': 3},   # 同版矛盾
        ]},
    ]
    result = replay(parcels, SETTINGS, batches, opened_rmas=['R2'])
    records = {row['rma']: row for row in result['records']}
    # B1 只取最高修订号，R4 凭更正后顺位进入复核位
    assert records['R4']['measured_weight'] == 1700 and records['R4']['revision'] == 2
    # B2 同版矛盾撤回 R1；R2 已开验不受 B1 重派影响
    assert records['R1']['state'] == '已撤回'
    assert records['R2']['state'] == '已开验' and records['R2']['desk_slot'] == 1
    audit = {item['batch_id']: item for item in result['batch_audit']}
    assert audit['B1']['reason'] == 'RESCAN_APPLIED'
    assert audit['B2']['reason'] == 'SAME_VERSION_CONFLICT'


def test_replay_rerun_of_same_batch_is_idempotent():
    parcels = [parcel('R1', shop='S1'), parcel('R2', shop='S2'), parcel('R3', shop='S3')]
    batches = [
        {'batch_id': 'B1', 'events': [{'rma': 'R3', 'weight': 1800, 'revision': 2}]},
        {'batch_id': 'B1', 'events': [{'rma': 'R3', 'weight': 1800, 'revision': 2}]},
        {'batch_id': 'B1', 'events': [{'rma': 'R3', 'weight': 1800, 'revision': 2}]},
    ]
    result = replay(parcels, SETTINGS, batches)
    assert len(result['batch_audit']) == 1          # 重跑不重复占位、不重复记账
    seated = [row['rma'] for row in result['records'] if row['desk_slot'] is not None]
    assert len(seated) == 3 and len(set(seated)) == 3


def test_replay_isolates_malformed_batches_and_events():
    parcels = [parcel('R1')]
    batches = [
        {'events': []},                                            # 缺 batch_id
        {'batch_id': 'B1', 'events': [{'rma': 'R1', 'weight': 0, 'revision': 2}]},
        {'batch_id': 'B2', 'events': [{'rma': 'R1', 'weight': 1200, 'revision': 2}]},
    ]
    result = replay(parcels, SETTINGS, batches)
    assert result['batch_errors']
    assert {error['code'] for error in result['batch_errors']} >= {'EXPECTED_BATCH_OBJECT', 'OUT_OF_RANGE'}
    # 合法批次仍被处理
    records = {row['rma']: row for row in result['records']}
    assert records['R1']['measured_weight'] == 1200


def test_invalid_settings_are_rejected():
    with pytest.raises(Exception):
        open_desk([], {'capacity': 0, 'max_per_shop': 1, 'threshold': .5})
    with pytest.raises(Exception):
        open_desk([], {'capacity': 2, 'max_per_shop': 1, 'threshold': 1.5})
