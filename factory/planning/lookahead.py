"""Резервирование окон покупателей с учётом времени готовности роботов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .market import RobotType, robot_type

if TYPE_CHECKING:
    from .policy import Ctx


@dataclass(frozen=True)
class SaleStock:
    kind: RobotType
    ready_in: int


class BuyerTimeline:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.buyers = tuple(ctx.market.beliefs)
        self.prices = {kind: {b: ctx.market.expected_price(b, kind) for b in self.buyers}
                       for kind in ctx.market.types}
        self.cooldown = ctx.cfg.buyerCooldown
        self.wait_cost = ctx.params.slot_wait_cost
        self.remaining = ctx.cfg.horizon - ctx.ws.t
        self.assemblers = {m.id: op.duration for m in ctx.cfg.machines for op in m.ops
                           if op.id == "assemble"}

    def _stock(self) -> list[SaleStock]:
        """Готовые роботы и время, когда окрашенные корпуса смогут попасть на продажу."""
        ws = self.ctx.ws
        robots, bodies = [], []
        controls = sum(item.kind == "control" for item in ws.items.values())
        inventory = [(i.kind, i.props, 0) for i in ws.items.values()]
        inventory.extend((kind, props, ws.busy_left.get(machine, 0))
                         for machine, (kind, props) in self.ctx.wip.items())
        for kind, props, ready in inventory:
            if kind == "robot":
                robots.append(SaleStock(robot_type(props), ready))
            elif kind == "body" and props.get("color") is not None:
                bodies.append(SaleStock(robot_type(props), ready))
        availability = {m: ws.busy_left.get(m, 0) for m in self.assemblers}
        if not availability:
            return robots
        for index, body in enumerate(sorted(bodies, key=lambda item: item.ready_in)):
            machine = min(availability, key=availability.__getitem__)
            duration = self.assemblers[machine]
            # Недостающие элементы управления ожидаем с шагом длительности сборки.
            control_ready = max(0, index - controls + 1) * duration
            finish = max(availability[machine], body.ready_in, control_ready) + duration
            if finish <= self.remaining:
                robots.append(SaleStock(body.kind, finish))
            availability[machine] = finish
        return robots

    def _quote(self, stock: SaleStock, buyer: str, available: dict[str, int]) -> float:
        sale = max(stock.ready_in, available[buyer])
        if sale > self.remaining:
            return 0.0
        return self.prices[stock.kind][buyer] - self.wait_cost * (sale - stock.ready_in)

    def reserve_slots(self) -> dict[str, int]:
        available = self.ctx.ws.cooldown_left.copy()
        scarcity = {}
        for kind, estimates in self.prices.items():
            ranked = sorted(estimates.values(), reverse=True)
            scarcity[kind] = ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)
        for stock in sorted(self._stock(), key=lambda item: (item.ready_in, -scarcity[item.kind])):
            buyer = max(self.buyers, key=lambda b: self._quote(stock, b, available))
            if self._quote(stock, buyer, available) >= self.ctx.params.reserve_price:
                available[buyer] = max(stock.ready_in, available[buyer]) + self.cooldown
        return available
