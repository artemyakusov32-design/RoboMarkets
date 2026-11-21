"""strategy.py — СТРАТЕГИЯ: кандидаты действий -> признаки φ(s,a) -> Q = w·φ -> жадный выбор.

Модель (market.py) отвечает на вопрос «что мы знаем о ценах», стратегия — «что делать».
Единственная рабочая политика оценивает кандидатов линейной суммой признаков.
Параметры определены явно, генератор учитывает ограничения склада и конца смены.
"""
from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Optional, Protocol, Sequence

from .lookahead import BuyerTimeline
from .market import MarketModel, RobotType, robot_type
from .schemas import Command, Item, StartMsg, item_matches, op_result_props

MAX_COMMANDS = 50


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ параметры
@dataclass
class Params:
    """Гиперпараметры модели/стратегии (не коэффициенты Q)."""
    stock_per_buyer: float = 1.5     # целевой запас робота «под покупателя»
    ucb_kappa: float = 0.25          # бонус неопределённости при выборе целевого типа
    diversify: float = 8.0           # штраф за совпадение целевых типов разных покупателей
    reserve_price: float = 10.0      # ниже этой цены не продаём (занимает слот покупателя)
    wait_ticks: int = 3              # ждать лучшего покупателя, если он освободится за столько тактов
    slot_planning_min_horizon: int = 1000
    slot_planning_known_buyers: int = 2
    slot_wait_cost: float = 4.0      # снижение ценности при ожидании сверх времени производства
    slot_uncertainty_bonus: float = 0.2
    slot_stock_target: float = 3.4
    slot_stock_sensitivity: float = 0.09
    hold_est: float = 6.0            # оценка числа тактов хранения робота при отложенной продаже
    pipeline_sheets: float = 3.0     # желаемый запас «листы + корпуса»
    pipeline_controls: float = 2.0   # желаемый запас элементов управления
    cooldown_inclusive: bool = False  # для прогноза покупатель освобождается в такт cooldownUntil
    cap_by_kind: dict[str, int] = field(default_factory=lambda: {"sheet": 4, "control": 4, "body": 5, "robot": 6})
    epsilon: float = 0.0             # ε-жадность для обучения (в бою = 0)
    time_safety: float = 0.5         # доля eventTimeoutMs, которую разрешаем тратить на решение


# Веса CMA-ES standard, принятые по финальной проверке на seed 1136–1235.
DEFAULT_Q: dict[str, float] = {
    # accept
    "acc_bias": 6.460646087746754,
    "acc_price": -0.744818097905831,
    "acc_need": 3.9080489813355537,
    "acc_crowd": -11.097763003529252,
    "acc_margin": 5.534227410033151,
    # start
    "st_bias": 3.257839880428274,
    "st_stage_cut": 1.3944633532086121,
    "st_stage_handles": -0.8188727308603809,
    "st_stage_paint": 0.08728537237925571,
    "st_stage_assemble": 3.6846899708706107,
    "st_stage_value": -0.1383131860917498,
    "st_progress": 1.1311520492760716,
    "st_deficit": 3.910089925924348,
    "st_stock": -2.2165974005162448,
    "st_free_gain": 1.4165965677071024,
    "st_cost": -0.7695392356595826,
    # offer
    "of_bias": -0.15508815045414567,
    "of_exp_price": 0.41239676472590786,
    "of_info": 7.015393826392922,
    "of_fee": -1.7180367697817365,
    "of_store": 0.8116550547251186,
    "of_wait_gap": -1.858778684042095,
    "of_below_reserve": -27.343548378214237,
    # discard
    "dc_bias": -14.544140887176384,
    "dc_pressure": 20.347280441067404,
    "dc_value": -1.1115620945671594,
    "dc_overstock": 0.15391434375990043,
}
STAGE_WEIGHT = {"cut": 0.3, "handles": 0.3, "paint": 0.6, "assemble": 1.0}


@dataclass
class QWeights:
    w: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_Q))
    min_score: float = 0.0   # действие с Q <= min_score не выполняется

    def names(self) -> list[str]:
        return sorted(self.w)

    def to_vector(self) -> list[float]:           # для генетического алгоритма / CMA-ES
        return [self.w[k] for k in self.names()]

    @classmethod
    def from_vector(cls, vec: Sequence[float], min_score: float = 0.0) -> "QWeights":
        names = sorted(DEFAULT_Q)
        return cls({k: float(v) for k, v in zip(names, vec)}, min_score)


# ------------------------------------------------------------------ стоимости цепочки
@dataclass
class ChainCosts:
    cut: float = 1.0
    handles: float = 2.0
    paint: float = 2.0
    assemble: float = 3.0
    sheet_price: float = 4.0
    control_price: float = 3.0
    cut_ticks: int = 2
    handles_ticks: int = 2
    paint_ticks: int = 3
    assemble_ticks: int = 3

    @classmethod
    def from_start(cls, cfg: StartMsg) -> "ChainCosts":
        c = cls()
        for m in cfg.machines:
            for op in m.ops:
                if op.id == "cut":
                    c.cut = op.cost
                    c.cut_ticks = op.duration
                elif op.id.startswith("add-handles"):
                    c.handles = op.cost
                    c.handles_ticks = op.duration
                elif op.id.startswith("paint"):
                    c.paint = op.cost
                    c.paint_ticks = op.duration
                elif op.id == "assemble":
                    c.assemble = op.cost
                    c.assemble_ticks = op.duration
        return c


def stage_of(op_id: str) -> str:
    if op_id.startswith("paint"):
        return "paint"
    if op_id.startswith("add-handles"):
        return "handles"
    return op_id  # cut, assemble


# ------------------------------------------------------------------ рабочее состояние такта
@dataclass
class WorkingState:
    """Локальная копия склада/станков/покупателей: команды одного ответа применяются сюда сразу,
    поэтому один itemId, станок или покупатель не используются дважды в одном ответе."""
    t: int
    free: int
    items: dict[str, Item]
    idle_machines: set[str]
    cooldown_left: dict[str, int]
    supply: Optional[Item] = None
    supply_price: float = 0.0
    busy_left: dict[str, int] = field(default_factory=dict)

    @property
    def ready_buyers(self) -> set[str]:
        return {b for b, left in self.cooldown_left.items() if left <= 0}


@dataclass
class Candidate:
    command: Command
    features: dict[str, float]
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


class Ctx:
    """Всё, что нужно для оценки действий в текущем такте (читает модель, не меняет её)."""

    def __init__(self, cfg: StartMsg, ws: WorkingState, market: MarketModel, chain: ChainCosts,
                 params: Params, wip: dict[str, tuple[str, dict]], last_price: dict[str, float]):
        self.cfg, self.ws, self.market, self.chain, self.params = cfg, ws, market, chain, params
        self.wip = wip
        self.chain.sheet_price = last_price.get("sheet", chain.sheet_price)
        self.chain.control_price = last_price.get("control", chain.control_price)
        self.colors = list(cfg.attributes["color"])
        self.desired: Counter = Counter()
        self.plan_buyer_slots = cfg.horizon >= params.slot_planning_min_horizon or (
            sum(belief.ideal_type() is not None for belief in market.beliefs.values())
            >= params.slot_planning_known_buyers
        )
        if self.plan_buyer_slots:
            self.V = self._plan_buyer_slots()
        else:
            self.V = market.type_values()
            assigned_types = market.assign_types(params.ucb_kappa, params.diversify)
            for robot in assigned_types.values():
                self.desired[robot] += params.stock_per_buyer

    def _plan_buyer_slots(self) -> dict[RobotType, float]:
        """Оценить следующую сборку по свободным окнам после текущих запасов."""
        params = self.params
        timeline = BuyerTimeline(self)
        available = timeline.reserve_slots()
        stock = self.have()

        production_ticks = self.chain.paint_ticks + self.chain.assemble_ticks
        values = {}
        for robot, by_buyer in timeline.prices.items():
            options = []
            for buyer, price in by_buyer.items():
                low, high = self.market.interval(buyer, robot)
                wait = max(0, available[buyer] - production_ticks)
                options.append(price + params.slot_uncertainty_bonus * (high - low)
                               - params.slot_wait_cost * wait)
            values[robot] = max(options)

        best = max(values.values())
        for robot, value in values.items():
            stock_adjustment = params.slot_stock_target + (value - best) * params.slot_stock_sensitivity
            self.desired[robot] = stock[robot] + max(-3.0, stock_adjustment)
        return values

    # ценность предмета = ожидаемая прибыль от его доведения до продажи (за вычетом оставшихся затрат)
    def potential(self, kind: str, props: dict) -> float:
        c, fee = self.chain, self.cfg.offerFee
        if kind == "robot":
            return self.V[robot_type(props)] - fee
        tail = c.assemble + c.control_price + fee
        if kind == "body":
            color, h = props.get("color"), bool(props.get("handles"))
            if color is not None:
                return self.V[(color, h)] - tail
            best = max(self.V[(col, h)] - c.paint for col in self.colors)
            if not h:
                best = max(best, max(self.V[(col, True)] - c.paint - c.handles for col in self.colors))
            return best - tail
        if kind == "sheet":
            return self.potential("body", {"color": None, "handles": False}) - c.cut
        if kind == "control":
            return c.control_price
        return 0.0

    def remaining_ticks(self, kind: str, props: dict) -> int:
        """Нижняя оценка времени до робота по цепочке v1, длительности из start."""
        c = self.chain
        if kind == 'robot':
            return 0
        if kind == 'control':
            return c.assemble_ticks
        if kind == 'body':
            return c.assemble_ticks + (c.paint_ticks if props.get('color') is None else 0)
        if kind == 'sheet':
            return c.cut_ticks + c.paint_ticks + c.assemble_ticks
        return self.cfg.horizon + 1

    def have(self) -> Counter:
        h: Counter = Counter()
        pool = [(i.kind, i.props) for i in self.ws.items.values()] + list(self.wip.values())
        for kind, props in pool:
            if kind == "robot" or (kind == "body" and props.get("color") is not None):
                h[robot_type(props)] += 1
        return h

    def count(self, kind: str, props: Optional[dict] = None) -> int:
        pool = [(i.kind, i.props) for i in self.ws.items.values()] + list(self.wip.values())
        return sum(1 for k, p in pool if k == kind and (props is None or p == props))


# ------------------------------------------------------------------ генерация кандидатов
class CandidateGenerator:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def generate(self) -> list[Candidate]:
        return self._accept() + self._start() + self._offer() + self._discard()

    # ---- accept
    def _accept(self) -> list[Candidate]:
        ctx, ws = self.ctx, self.ctx.ws
        it = ws.supply
        if it is None or ws.free <= 0:
            return []
        if ctx.remaining_ticks(it.kind, it.props) > ctx.cfg.horizon - ws.t:
            return []
        p = ctx.params
        if it.kind == "sheet":
            have = ctx.count("sheet") + ctx.count("body")
            need = p.pipeline_sheets - have
        else:
            need = p.pipeline_controls - ctx.count("control")
        margin = ctx.potential("sheet", {}) - ctx.chain.sheet_price
        f = {"acc_bias": 1.0, "acc_price": ws.supply_price, "acc_need": clip(need, -3, 3),
             "acc_crowd": (ctx.cfg.capacity - (ws.free - 1)) / ctx.cfg.capacity,
             "acc_margin": clip(margin, -20, 60) / 50.0}
        return [Candidate(Command(cmd="accept", itemId=it.id), f)]

    # ---- start
    def _start(self) -> list[Candidate]:
        ctx, ws = self.ctx, self.ctx.ws
        have = ctx.have()
        out: list[Candidate] = []
        for m in ctx.cfg.machines:
            if m.id not in ws.idle_machines:
                continue
            for op in m.ops:
                pools = [[i for i in ws.items.values() if item_matches(tpl, i)] for tpl in op.inputs]
                if any(not pl for pl in pools):
                    continue
                seen: set = set()
                for combo in product(*pools):
                    ids = [i.id for i in combo]
                    if len(set(ids)) != len(ids):
                        continue
                    sig = tuple((i.kind, tuple(sorted(i.props.items()))) for i in combo)
                    if sig in seen:      # одинаковые предметы взаимозаменяемы
                        continue
                    seen.add(sig)
                    props = op_result_props(op, list(combo))
                    if op.duration + ctx.remaining_ticks(op.output.kind, props) > ctx.cfg.horizon - ws.t:
                        continue
                    out.append(self._start_candidate(m.id, op, list(combo), have))
        return out

    def _start_candidate(self, machine_id: str, op, combo: list[Item], have: Counter) -> Candidate:
        ctx = self.ctx
        props = op_result_props(op, combo)
        kind = op.output.kind
        stage = stage_of(op.id)
        pot_out = ctx.potential(kind, props)
        pot_in = sum(ctx.potential(i.kind, i.props) for i in combo)
        color, h = props.get("color"), bool(props.get("handles"))
        if kind in ("body", "robot") and color is not None:
            deficit = ctx.desired[(color, h)] - have[(color, h)]
        elif kind == "body" and stage == "handles":
            d = lambda hh: max(ctx.desired[(c, hh)] - have[(c, hh)] for c in ctx.colors)
            deficit = d(True) - d(False)
        else:
            deficit = 0.0
        f = {"st_bias": 1.0, f"st_stage_{stage}": 1.0,
             "st_stage_value": STAGE_WEIGHT.get(stage, 0.5) * max(pot_out, 0.0) / 10.0,
             "st_progress": clip(pot_out - pot_in - op.cost, -20, 20) / 10.0,
             "st_deficit": clip(deficit, -3, 3),
             "st_stock": float(min(ctx.count(kind, props), 6)),
             "st_free_gain": float(len(combo) - 1),
             "st_cost": float(op.cost)}
        cmd = Command(cmd="start", machineId=machine_id, opId=op.id, itemIds=[i.id for i in combo])
        return Candidate(cmd, f, meta={"machine": machine_id, "kind": kind, "props": props})

    # ---- offer
    def _offer(self) -> list[Candidate]:
        ctx, ws, p = self.ctx, self.ctx.ws, self.ctx.params
        if not ws.ready_buyers:
            return []
        cfg, market = ctx.cfg, ctx.market
        out: list[Candidate] = []
        seen: set[RobotType] = set()
        for it in list(ws.items.values()):
            if it.kind != "robot":
                continue
            t = robot_type(it.props)
            if t in seen:
                continue
            seen.add(t)
            E = {b: market.expected_price(b, t) for b in market.beliefs}
            for b in sorted(ws.ready_buyers):
                m = market.suggest_min_price(b, t, reserve=p.reserve_price)
                if m is None:
                    continue
                lo, hi = market.interval(b, t)
                if ctx.plan_buyer_slots:
                    alternatives = [price - p.slot_wait_cost * ws.cooldown_left[buyer]
                                    for buyer, price in E.items() if buyer != b
                                    and ws.cooldown_left[buyer] <= cfg.horizon - ws.t]
                else:
                    alternatives = [price for buyer, price in E.items()
                                    if buyer != b and ws.cooldown_left[buyer] <= p.wait_ticks]
                f = {"of_bias": 1.0, "of_exp_price": E[b],
                     "of_info": (hi - lo) / cfg.priceCeiling * (1.0 - ws.t / max(cfg.horizon, 1)),
                     "of_fee": cfg.offerFee,
                     "of_store": cfg.storageFee.get("robot", 0.5) * p.hold_est,
                     "of_wait_gap": max(0.0, max(alternatives, default=0.0) - E[b]),
                     "of_below_reserve": max(0.0, p.reserve_price - E[b]) / p.reserve_price}
                out.append(Candidate(Command(cmd="offer", buyerId=b, itemId=it.id, minPrice=m), f,
                                     meta={"rtype": t, "min_price": m}))
        return out

    # ---- discard
    def _discard(self) -> list[Candidate]:
        ctx, ws, p = self.ctx, self.ctx.ws, self.ctx.params
        if ws.free > 3:
            return []
        out, seen = [], set()
        for it in ws.items.values():
            sig = (it.kind, tuple(sorted(it.props.items())))
            if sig in seen:
                continue
            seen.add(sig)
            over = max(0, ctx.count(it.kind) - p.cap_by_kind.get(it.kind, 99))
            pressure = ((ctx.cfg.capacity - ws.free) / ctx.cfg.capacity) ** 2
            f = {"dc_bias": 1.0, "dc_pressure": pressure,
                 "dc_value": max(ctx.potential(it.kind, it.props), 0.0), "dc_overstock": float(over)}
            out.append(Candidate(Command(cmd="discard", itemId=it.id), f))
        return out

    # ---- применение к рабочему состоянию
    def apply(self, c: Candidate) -> None:
        ws, cmd = self.ctx.ws, c.command
        if cmd.cmd == "accept" and ws.supply is not None:
            ws.items[ws.supply.id] = ws.supply
            ws.free -= 1
            ws.supply = None
        elif cmd.cmd == "start":
            for iid in cmd.itemIds or []:
                ws.items.pop(iid, None)
            ws.free += len(cmd.itemIds or []) - 1   # входы уходят со склада, станок резервирует 1 место
            ws.idle_machines.discard(cmd.machineId)
            self.ctx.wip[c.meta["machine"]] = (c.meta["kind"], c.meta["props"])
        elif cmd.cmd == "offer":
            ws.items.pop(cmd.itemId, None)           # предмет «в работе» до sale_result
            ws.cooldown_left[cmd.buyerId] = self.ctx.cfg.buyerCooldown
        elif cmd.cmd == "discard":
            if ws.items.pop(cmd.itemId, None) is not None:
                ws.free += 1


# ------------------------------------------------------------------ политики (заменяемый блок)
class Policy(Protocol):
    def score(self, obs: list[float], cands: list[Candidate]) -> list[float]: ...


class LinearPolicy:
    """Q(s,a) = Σ w_i·φ_i(s,a). Веса: вручную, генетика, CMA-ES, градиент."""
    def __init__(self, weights: QWeights | None = None):
        self.weights = weights or QWeights()

    def score(self, obs: list[float], cands: list[Candidate]) -> list[float]:
        w = self.weights.w
        return [sum(w.get(k, 0.0) * v for k, v in c.features.items()) for c in cands]


# ------------------------------------------------------------------ движок решений
class DecisionEngine:
    def __init__(self, policy: Policy, min_score: float = 0.0, rng: random.Random | None = None):
        self.policy, self.min_score = policy, min_score
        self.rng = rng or random.Random(0)

    def decide(self, ctx: Ctx, obs: list[float], deadline: float) -> list[Candidate]:
        gen, chosen = CandidateGenerator(ctx), []
        while len(chosen) < MAX_COMMANDS and time.perf_counter() < deadline:
            cands = gen.generate()
            if not cands:
                break
            for c, s in zip(cands, self.policy.score(obs, cands)):
                c.score = s
            explore = ctx.params.epsilon > 0 and self.rng.random() < ctx.params.epsilon
            best = self.rng.choice(cands) if explore else max(cands, key=lambda c: c.score)
            if best.score <= self.min_score and not explore:
                break
            gen.apply(best)
            chosen.append(best)
        return chosen
