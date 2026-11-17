"""bot.py — LearningController: парсинг состояния -> обновление модели -> решение. Один экземпляр = один runId."""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from .market import MarketModel, RobotType, robot_type
from .schemas import EventMsg, StartMsg
from .policy import (ChainCosts, Ctx, DecisionEngine, LinearPolicy, Params, Policy,
                       QWeights, WorkingState)

@dataclass
class Pending:
    buyer: str
    rtype: RobotType
    min_price: float
    t: int


class LearningController:
    def __init__(self, cfg: StartMsg, policy: Policy | None = None, params: Params | None = None,
                 weights: QWeights | None = None):
        self.cfg = cfg
        self.params = params or Params()
        self.market = MarketModel(cfg.buyers, cfg.attributes, cfg.priceCeiling)  # новая модель на каждый runId
        self.chain = ChainCosts.from_start(cfg)
        qw = weights or QWeights()
        self.engine = DecisionEngine(policy or LinearPolicy(qw), qw.min_score)
        self.last_price: dict[str, float] = {}
        self.pending: dict[tuple[str, str], Pending] = {}  # (buyerId, itemId) -> предложение
        self.wip: dict[str, tuple[str, dict]] = {}     # machineId -> (kind, props) результата в работе
        self.stats: Counter = Counter()
        self.last_commands: list[dict] = []

    def handle_event(self, msg: EventMsg) -> list[dict]:
        # Кэш и блокировка целого прогона принадлежат RunStore: старые seq не забываются.
        # Исключение не подавляется: RunStore не сохранит частично изменённую память.
        started = time.perf_counter()
        deadline = started + self.params.time_safety * self.cfg.eventTimeoutMs / 1000.0
        wire = self._process(msg, deadline)
        self.stats['max_ms'] = max(self.stats['max_ms'], (time.perf_counter() - started) * 1000)
        self.last_commands = wire
        return wire

    # ------------------------------------------------------------ внутреннее
    def _process(self, msg: EventMsg, deadline: float) -> list[dict]:
        self._ingest(msg)
        ws = self._working_state(msg)
        ctx = Ctx(self.cfg, ws, self.market, self.chain, self.params, self.wip, self.last_price)
        obs = []  # Линейная оценка использует признаки кандидата действия.
        chosen = self.engine.decide(ctx, obs, deadline)
        for c in chosen:                            # запоминаем, что ждём в ответ
            if c.command.cmd == "offer":
                self.pending[(c.command.buyerId, c.command.itemId)] = Pending(c.command.buyerId, c.meta["rtype"],
                                                         c.meta["min_price"], msg.t)
        return [c.command.to_wire() for c in chosen]

    def _ingest(self, msg: EventMsg) -> None:
        ev, st = msg.event, msg.state
        for r in msg.report:
            self.stats["cmd_ok" if r.ok else f"cmd_fail:{r.reason}"] += 1
            if not r.ok and 0 <= r.cmd < len(self.last_commands):
                failed = self.last_commands[r.cmd]
                if failed['cmd'] == 'offer':
                    self.pending.pop((failed['buyerId'], failed['itemId']), None)
                elif failed['cmd'] == 'start':
                    self.wip.pop(failed['machineId'], None)
        if ev.type == "supply" and ev.item is not None and ev.price is not None:
            self.last_price[ev.item.kind] = ev.price
        elif ev.type == "machine_done" and ev.machineId:
            self.wip.pop(ev.machineId, None)
        elif ev.type == "sale_result" and ev.buyerId:
            pend = self.pending.pop((ev.buyerId, ev.itemId or ""), None)
            props = ev.item.props if ev.item else None
            rtype = robot_type(props) if props is not None else (pend.rtype if pend else None)
            if rtype is not None and ev.outcome:
                self.market.on_sale_result(ev.buyerId, rtype, ev.outcome, ev.price, ev.reason,
                                           pend.min_price if pend else 0.0)
                self.stats[f"sale_{ev.outcome}"] += 1
        busy = {m.id for m in st.machines if m.busyUntil is not None}
        for mid in [m for m in self.wip if m not in busy]:   # команда start отклонена или станок освободился
            del self.wip[mid]
        for k in [k for k, p in self.pending.items() if msg.t - p.t > 3]:
            del self.pending[k]

    def _working_state(self, msg: EventMsg) -> WorkingState:
        st, t, p = msg.state, msg.t, self.params
        left: dict[str, int] = {}
        for b in st.buyers:
            u: Optional[int] = b.cooldownUntil
            left[b.id] = 0 if u is None else max(1, u - t + (1 if p.cooldown_inclusive else 0))
        for b in self.cfg.buyers:
            left.setdefault(b, self.cfg.buyerCooldown)
        supply = msg.event.item if msg.event.type == "supply" else None
        pending_items = {item_id for _, item_id in self.pending}
        return WorkingState(
            t=t, free=st.storage.free, items={i.id: i for i in st.storage.items if i.id not in pending_items},
            idle_machines={m.id for m in st.machines if m.busyUntil is None},
            cooldown_left=left, supply=supply,
            supply_price=float(msg.event.price or 0.0) if supply is not None else 0.0)
