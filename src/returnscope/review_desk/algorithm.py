from __future__ import annotations

from .contracts import DeskParcel, DeskSettings, RescanEvent

from dataclasses import dataclass

# 工作台状态
SEATED = '已派复核'          # 占用复核位
INSPECTING = '已开验'        # 仓管已开包检验，复核位锁定
WAITING = '待派复核'         # 在等待队列，未占位
WITHDRAWN = '已撤回'         # 同版补扫矛盾，当班不再受理
RELEASED = '放行推荐'        # 无需复核

# 补扫批次处置原因
BATCH_APPLIED = 'RESCAN_APPLIED'             # 称重更正已生效并重派
BATCH_INERT = 'STALE_REVISION_IGNORED'       # 旧版本或同内容补扫，全部忽略
BATCH_CONFLICT = 'SAME_VERSION_CONFLICT'     # 同版补扫互相矛盾
BATCH_INVALID = 'BATCH_EMPTY'                # 批次内没有任何可识别事件

# 顺位/撤回原因
REASON_REDISPATCH = 'RESCAN_REDISPATCH'
REASON_SHOP = 'SHOP_CONCENTRATION'
REASON_FULL = 'DESK_FULL'
REASON_LOW = 'LOW_EXPECTED_LOSS'
REASON_WITHDRAWN = 'WITHDRAWN_AFTER_CONFLICT'


def corrected_score(parcel: DeskParcel, new_weight: int) -> float:
    """称重更正后重算错件概率。

    保留原模型先验，按新旧称重相对申报重量的偏差比调整对数几率：
    新称重大幅偏离则概率上升、回到申报重量则概率回落，结果压缩回 (0,1)。
    评分范围外（人工待核）的包裹不回到自动评分，仍返回原概率。
    """
    if not parcel.in_scope:
        return parcel.score
    prior_odds = parcel.score / (1 - parcel.score) if parcel.score < 1 else 1e12
    prior_odds = max(prior_odds, 1e-9)
    old_deviation = abs(parcel.measured_weight - parcel.listed_weight) / parcel.listed_weight
    new_deviation = abs(new_weight - parcel.listed_weight) / parcel.listed_weight
    adjusted = prior_odds * (1 + 1.2 * new_deviation) / (1 + 1.2 * old_deviation)
    return adjusted / (1 + adjusted)


def needs_review(parcel: DeskParcel, threshold: float) -> bool:
    if not parcel.in_scope:
        return True                       # 评分范围外必须人工验包（TRAP-19 口径）
    if not parcel.seal:
        return True
    if parcel.priority and parcel.score >= threshold * .75:
        return True
    return parcel.score >= threshold


def waiting_order(parcel: DeskParcel, settings: DeskSettings) -> tuple:
    """常规待派顺位：优先客户 > 拆封（范围内件）> 错件暴露（范围外按全额计提）> 早到优先。

    范围外件不在封签键上获得无条件插队权——它与高分件按错件暴露同池比较，
    仅当工作台只剩一个空位时由 ``_rank_waiting`` 启用人工强制优先。
    """
    exposure = int(parcel.amount * (1 if not parcel.in_scope else parcel.score) + .5)
    # 范围内拆封件（-1）先于封签件（1）；范围外件取中性最大值，封签键不参与其常规排序
    seal_key = 2 if not parcel.in_scope else (1 if parcel.seal else -1)
    return (
        not parcel.priority,              # 优先客户排前
        seal_key,
        -exposure,                        # 错件暴露高者排前
        parcel.arrival,                   # 早到者排前
        parcel.original_shop,
        parcel.rma,
    )


@dataclass(slots=True)
class DeskEntry:
    parcel: DeskParcel
    state: str = WAITING
    reason: str = REASON_REDISPATCH
    desk_slot: int | None = None
    revision: int = 1
    opened: bool = False

    def evidence(self) -> dict:
        return {
            'rma': self.parcel.rma,
            'original_shop': self.parcel.original_shop,
            'listed_weight': self.parcel.listed_weight,
            'measured_weight': self.parcel.measured_weight,
            'score': round(self.parcel.score, 6),
            'in_scope': self.parcel.in_scope,
            'amount': self.parcel.amount,
            'arrival': self.parcel.arrival,
            'priority': self.parcel.priority,
            'seal': self.parcel.seal,
            'revision': self.revision,
            'opened': self.opened,
            'state': self.state,
            'reason': self.reason,
            'desk_slot': self.desk_slot,
        }


class ReviewDesk:
    """有状态的退货复核工作台：跨补扫批次维护复核位与开验锁定。"""

    def __init__(self, parcels: list[DeskParcel], settings: DeskSettings):
        if len({parcel.rma for parcel in parcels}) != len(parcels):
            raise ValueError('DUPLICATE_DESK_PARCEL')
        self.settings = settings
        self.entries: dict[str, DeskEntry] = {
            parcel.rma: DeskEntry(parcel=parcel) for parcel in parcels
        }
        self.seen_batches: set[str] = set()
        self.batch_audit: list[dict] = []
        self._dispatch_initial()

    # ------------------------------------------------------------------ 初派
    def _dispatch_initial(self):
        for entry in self.entries.values():
            if not needs_review(entry.parcel, self.settings.threshold):
                entry.state = RELEASED
                entry.reason = REASON_LOW
        self._fill_slots(initial=True)

    def _fill_slots(self, *, initial: bool):
        """按顺位把等待件填入空位；初派不启用末位空位的人工强制插队。"""
        shop_count: dict[str, int] = {}
        for entry in self.entries.values():
            if entry.desk_slot is not None:
                shop_count[entry.parcel.original_shop] = shop_count.get(entry.parcel.original_shop, 0) + 1
        used = sum(1 for entry in self.entries.values() if entry.desk_slot is not None)
        waiting = [entry for entry in self.entries.values() if entry.state == WAITING]
        while used < self.settings.capacity and waiting:
            ranked = self._rank_waiting(waiting, used, last_slot_rule=not initial)
            entry = next((candidate for candidate in ranked
                          if shop_count.get(candidate.parcel.original_shop, 0)
                          < self.settings.max_per_shop), None)
            if entry is None:
                break  # 剩下的等待件全部撞门店上限，保留空位
            shop = entry.parcel.original_shop
            used += 1
            shop_count[shop] = shop_count.get(shop, 0) + 1
            entry.desk_slot = used
            entry.state = INSPECTING if entry.opened else SEATED
            if initial:
                entry.reason = self._initial_reason(entry.parcel)
            waiting.remove(entry)
        for entry in waiting:
            shop = entry.parcel.original_shop
            entry.reason = (REASON_SHOP if shop_count.get(shop, 0) >= self.settings.max_per_shop
                            else REASON_FULL)

    def _rank_waiting(self, waiting: list[DeskEntry], used: int, *, last_slot_rule: bool) -> list[DeskEntry]:
        ordered = sorted(waiting, key=lambda entry: waiting_order(entry.parcel, self.settings))
        free = self.settings.capacity - used
        if last_slot_rule and free == 1:
            # 重派只剩一个空位：评分范围外的包裹优先于有高分的包裹，仍受原门店上限约束
            forced = [entry for entry in ordered if not entry.parcel.in_scope]
            scoped = [entry for entry in ordered if entry.parcel.in_scope]
            return forced + scoped
        return ordered

    def _initial_reason(self, parcel: DeskParcel) -> str:
        if not parcel.in_scope:
            return 'PARCEL_VERIFICATION'
        if not parcel.seal:
            return 'UNSEALED'
        if parcel.priority and parcel.score >= self.settings.threshold * .75:
            return 'PRIORITY_CUSTOMER'
        return 'MODEL_RISK'

    # ------------------------------------------------------------------ 开验
    def open_inspection(self, rma: str) -> DeskEntry:
        """仓管开包检验：复核位锁定，之后的重派不得移动该单。"""
        entry = self._require(rma)
        if entry.desk_slot is None:
            raise ValueError(f'PARCEL_NOT_SEATED:{rma}')
        entry.opened = True
        entry.state = INSPECTING
        return entry

    def _require(self, rma: str) -> DeskEntry:
        try:
            return self.entries[rma]
        except KeyError:
            raise ValueError(f'UNKNOWN_RMA:{rma}') from None

    # ------------------------------------------------------------------ 补扫
    def apply_rescan_batch(self, events: list[RescanEvent], *, batch_id: str) -> dict:
        """处理一批乱序到达的补扫称重，返回批次审计证据。

        同批重跑（batch_id 已处理）直接回放原审计、不改变任何占位。
        """
        if batch_id in self.seen_batches:
            audit = next(item for item in self.batch_audit if item['batch_id'] == batch_id)
            return audit
        if not events:
            # 空批次没有任何处置，不占用 batch_id，后续同号有效批次仍可受理
            return self._record(batch_id, BATCH_INVALID, events, [], [], [], [], lock_id=False)

        latest = self._latest_per_rma(events)
        unknown = [rma for rma in latest if rma not in self.entries]
        known = {rma: event for rma, event in latest.items() if rma in self.entries}

        conflict_rmas, inert_rmas, applied_rmas = [], [], []
        for rma, event in known.items():
            entry = self.entries[rma]
            if entry.state == WITHDRAWN:
                # 同版矛盾撤回的单当班不再受理，任何后续补扫均无效
                inert_rmas.append(rma)
                continue
            same_version = [row for row in events if row.rma == rma and row.revision == event.revision]
            if len({row.weight for row in same_version}) > 1:
                conflict_rmas.append(rma)
            elif event.revision < entry.revision or (
                event.revision == entry.revision and event.weight == entry.parcel.measured_weight
            ):
                inert_rmas.append(rma)
            else:
                applied_rmas.append(rma)

        if conflict_rmas:
            # 同版矛盾只撤回涉事单；同批其他包裹的补扫仍须生效，互不连坐
            for rma in conflict_rmas:
                self._withdraw(rma)
        if applied_rmas:
            for rma in applied_rmas:
                self._correct_weight(rma, known[rma])
        if conflict_rmas:
            reason = BATCH_CONFLICT
        elif applied_rmas:
            reason = BATCH_APPLIED
        else:
            reason = BATCH_INERT
        self._redispatch()

        audit = self._record(batch_id, reason, events, sorted(known), sorted(conflict_rmas),
                             sorted(inert_rmas), sorted(applied_rmas), unknown=sorted(unknown))
        return audit

    @staticmethod
    def _latest_per_rma(events: list[RescanEvent]) -> dict[str, RescanEvent]:
        latest: dict[str, RescanEvent] = {}
        for event in events:
            current = latest.get(event.rma)
            if current is None or event.revision > current.revision:
                latest[event.rma] = event
        return latest

    def _withdraw(self, rma: str):
        """同版补扫矛盾：连已开验的单也撤回，腾出复核位。"""
        entry = self.entries[rma]
        entry.state = WITHDRAWN
        entry.reason = REASON_WITHDRAWN
        entry.desk_slot = None
        entry.opened = False

    def _correct_weight(self, rma: str, event: RescanEvent):
        entry = self.entries[rma]
        new_score = corrected_score(entry.parcel, event.weight)
        entry.parcel = DeskParcel(
            rma=entry.parcel.rma,
            original_shop=entry.parcel.original_shop,
            listed_weight=entry.parcel.listed_weight,
            measured_weight=event.weight,
            score=new_score,
            in_scope=entry.parcel.in_scope,
            amount=entry.parcel.amount,
            arrival=entry.parcel.arrival,
            priority=entry.parcel.priority,
            seal=entry.parcel.seal,
        )
        entry.revision = event.revision
        # 已开验的包裹保留复核位；未开验的回到等待池参与重派
        if not entry.opened and entry.desk_slot is not None:
            entry.desk_slot = None
            entry.state = WAITING
            entry.reason = REASON_REDISPATCH
            if not needs_review(entry.parcel, self.settings.threshold):
                entry.state = RELEASED
                entry.reason = REASON_LOW

    def _redispatch(self):
        """重排顺位并重编号复核位；已开验件原地保留。"""
        # 更正后概率回落至阈值以下的未开验件直接放行，腾出复核位
        for entry in self.entries.values():
            if entry.state in (SEATED, WAITING) and not entry.opened \
                    and not needs_review(entry.parcel, self.settings.threshold):
                entry.state = RELEASED
                entry.reason = REASON_LOW
                entry.desk_slot = None
        locked = sorted((entry for entry in self.entries.values() if entry.opened),
                        key=lambda entry: entry.desk_slot)
        # 其余曾占位但未开验的件先回到等待池，统一按新顺位重派
        for entry in self.entries.values():
            if entry.desk_slot is not None and not entry.opened:
                entry.desk_slot = None
                if entry.state == SEATED:
                    entry.state = WAITING
                    entry.reason = REASON_REDISPATCH
        slot = 1
        for entry in locked:
            entry.desk_slot = slot
            entry.state = INSPECTING
            slot += 1
        shop_count: dict[str, int] = {}
        for entry in locked:
            shop = entry.parcel.original_shop
            shop_count[shop] = shop_count.get(shop, 0) + 1
        used = slot - 1
        waiting = [entry for entry in self.entries.values() if entry.state == WAITING]
        while used < self.settings.capacity and waiting:
            ordered = self._rank_waiting(waiting, used, last_slot_rule=True)
            entry = next((candidate for candidate in ordered
                          if shop_count.get(candidate.parcel.original_shop, 0)
                          < self.settings.max_per_shop), None)
            if entry is None:
                break
            shop = entry.parcel.original_shop
            used += 1
            shop_count[shop] = shop_count.get(shop, 0) + 1
            entry.desk_slot = used
            entry.state = SEATED
            entry.reason = REASON_REDISPATCH
            waiting.remove(entry)
        for entry in waiting:
            shop = entry.parcel.original_shop
            entry.reason = (REASON_SHOP if shop_count.get(shop, 0) >= self.settings.max_per_shop
                            else REASON_FULL)

    def _record(self, batch_id, reason, events, touched, conflicts, inert, applied, unknown=(), lock_id=True):
        audit = {
            'batch_id': batch_id,
            'reason': reason,
            'event_count': len(events),
            'touched_rmas': touched,
            'conflict_rmas': conflicts,
            'inert_rmas': inert,
            'applied_rmas': applied,
            'unknown_rmas': list(unknown),
        }
        if lock_id:
            self.seen_batches.add(batch_id)
        self.batch_audit.append(audit)
        return audit

    # ------------------------------------------------------------------ 证据
    def evidence(self) -> dict:
        records = sorted((entry.evidence() for entry in self.entries.values()),
                         key=lambda row: (row['desk_slot'] is None, row['desk_slot'] or 0,
                                          row['original_shop'], row['rma']))
        seated = [row for row in records if row['desk_slot'] is not None]
        shop_load: dict[str, int] = {}
        for row in seated:
            shop_load[row['original_shop']] = shop_load.get(row['original_shop'], 0) + 1
        return {
            'settings': {
                'capacity': self.settings.capacity,
                'max_per_shop': self.settings.max_per_shop,
                'threshold': self.settings.threshold,
            },
            'records': records,
            'batch_audit': list(self.batch_audit),
            'occupied_slots': len(seated),
            'open_slots': self.settings.capacity - len(seated),
            'shop_load': dict(sorted(shop_load.items())),
            'state_counts': {
                SEATED: sum(1 for row in records if row['state'] == SEATED),
                INSPECTING: sum(1 for row in records if row['state'] == INSPECTING),
                WAITING: sum(1 for row in records if row['state'] == WAITING),
                WITHDRAWN: sum(1 for row in records if row['state'] == WITHDRAWN),
                RELEASED: sum(1 for row in records if row['state'] == RELEASED),
            },
        }
