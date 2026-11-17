"""market.py — МОДЕЛЬ: знания о покупателях внутри одного runId.

Для каждого покупателя ведётся набор гипотез «образец (6 типов) × Pmax × f».
Каждая гипотеза хранит интервалы (боксы) для Pmax, f и их произведения q = Pmax·f:
    0 отличий -> цена = Pmax,  1 отличие -> цена = q = Pmax·f,  2 отличия -> 0.
Одно наблюдение по любому типу сужает интервалы гипотезы и поэтому сразу
уточняет цены по остальным типам этого же покупателя (связь цен).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional

RobotType = tuple[Optional[str], bool]  # (color, handles)
EPS = 1e-6
INF = math.inf


def robot_type(props: Mapping[str, Any]) -> RobotType:
    return (props.get("color"), bool(props.get("handles")))


def type_distance(a: RobotType, b: RobotType) -> int:
    return int(a[0] != b[0]) + int(a[1] != b[1])


@dataclass(frozen=True)
class MarketPriors:
    """Диапазоны из спецификации v1 (Pmax 45..75, f 0.30..0.60) с небольшим запасом."""
    pmax: tuple[float, float] = (42.0, 78.0)
    f: tuple[float, float] = (0.27, 0.63)
    noise: float = 0.02      # поправка сделки ±2%
    noise_slack: float = 0.005  # запас на округление цен


class Hypothesis:
    __slots__ = ("sample", "p_lo", "p_hi", "f_lo", "f_hi", "q_lo", "q_hi", "alive")

    def __init__(self, sample: RobotType, p: tuple[float, float], f: tuple[float, float]):
        self.sample = sample
        self.p_lo, self.p_hi = p
        self.f_lo, self.f_hi = f
        self.q_lo, self.q_hi = self.p_lo * self.f_lo, self.p_hi * self.f_hi
        self.alive = True

    def interval(self, t: RobotType) -> tuple[float, float]:
        d = type_distance(t, self.sample)
        if d == 0:
            return self.p_lo, self.p_hi
        if d == 1:
            return self.q_lo, self.q_hi
        return 0.0, 0.0

    def mid(self, t: RobotType) -> float:
        lo, hi = self.interval(t)
        return (lo + hi) / 2.0

    def constrain(self, t: RobotType, a: float, b: float) -> None:
        """Наблюдение: базовая цена типа t лежит в [a, b] (b может быть inf)."""
        d = type_distance(t, self.sample)
        if d == 2:
            if a > EPS:           # ненулевая цена там, где по гипотезе должен быть 0
                self.alive = False
            return
        if d == 0:
            self.p_lo, self.p_hi = max(self.p_lo, a), min(self.p_hi, b)
        else:
            self.q_lo, self.q_hi = max(self.q_lo, a), min(self.q_hi, b)
        if self.p_hi <= EPS or self.q_hi <= EPS:
            self.alive = False
            return
        self._propagate()

    def _propagate(self) -> None:
        for _ in range(2):
            self.p_lo = max(self.p_lo, self.q_lo / self.f_hi)
            self.p_hi = min(self.p_hi, self.q_hi / self.f_lo)
            self.f_lo = max(self.f_lo, self.q_lo / self.p_hi)
            self.f_hi = min(self.f_hi, self.q_hi / self.p_lo)
            self.q_lo = max(self.q_lo, self.p_lo * self.f_lo)
            self.q_hi = min(self.q_hi, self.p_hi * self.f_hi)
        if (self.p_lo > self.p_hi + EPS or self.f_lo > self.f_hi + EPS
                or self.q_lo > self.q_hi + EPS):
            self.alive = False


class BuyerBelief:
    def __init__(self, buyer_id: str, types: list[RobotType], priors: MarketPriors, ceiling: float):
        self.id, self.types, self.priors, self.ceiling = buyer_id, types, priors, ceiling
        self.resets = 0
        self.hyps: list[Hypothesis] = []
        self._init_hyps()

    def _init_hyps(self) -> None:
        p = (self.priors.pmax[0], min(self.priors.pmax[1], self.ceiling))
        self.hyps = [Hypothesis(s, p, self.priors.f) for s in self.types]

    def alive(self) -> list[Hypothesis]:
        return [h for h in self.hyps if h.alive]

    def observe(self, t: RobotType, sold: bool, price: float, min_price: float) -> None:
        n = self.priors.noise + self.priors.noise_slack
        if sold:
            a, b = price / (1 + n), price / (1 - n)
        else:
            if min_price <= 0:       # отказ при minPrice=0 информации о цене не даёт
                return
            a, b = 0.0, min_price / (1 - n)   # цена < minPrice
        for h in self.hyps:
            if h.alive:
                h.constrain(t, a, b)
        if not self.alive():         # противоречие (дрейф желаний / сбой модели) -> сброс
            self.resets += 1
            self._init_hyps()

    # ---- запросы к модели ----
    def price_interval(self, t: RobotType) -> tuple[float, float]:
        ivs = [h.interval(t) for h in self.alive()]
        return min(i[0] for i in ivs), max(i[1] for i in ivs)

    def expected_price(self, t: RobotType) -> float:
        hs = self.alive()
        return sum(h.mid(t) for h in hs) / len(hs)

    def ucb(self, t: RobotType, kappa: float) -> float:
        lo, hi = self.price_interval(t)
        return self.expected_price(t) + kappa * (hi - lo)

    def ideal_type(self) -> Optional[RobotType]:
        """Образец известен точно, если жива одна гипотеза."""
        hs = self.alive()
        return hs[0].sample if len(hs) == 1 else None


class MarketModel:
    def __init__(self, buyers: list[str], attributes: Mapping[str, list[Any]],
                 ceiling: float, priors: MarketPriors | None = None):
        self.priors = priors or MarketPriors()
        self.ceiling = ceiling
        self.types: list[RobotType] = [(c, bool(h)) for c in attributes["color"]
                                       for h in attributes["handles"]]
        self.beliefs = {b: BuyerBelief(b, self.types, self.priors, ceiling) for b in buyers}

    # ---- обновление по sale_result ----
    def on_sale_result(self, buyer_id: str, rtype: RobotType, outcome: str,
                       price: Optional[float], reason: Optional[str], min_price: float) -> None:
        bel = self.beliefs.get(buyer_id)
        if bel is None:
            return
        if outcome == "sold" and price is not None:
            bel.observe(rtype, True, price, min_price)
        elif outcome == "refused" and reason != "cooldown":
            bel.observe(rtype, False, 0.0, min_price)
        # refused/cooldown: про цену ничего не известно

    # ---- запросы ----
    def expected_price(self, b: str, t: RobotType) -> float:
        return self.beliefs[b].expected_price(t)

    def interval(self, b: str, t: RobotType) -> tuple[float, float]:
        return self.beliefs[b].price_interval(t)

    def type_values(self) -> dict[RobotType, float]:
        return {t: max(self.expected_price(b, t) for b in self.beliefs) for t in self.types}

    def assign_types(self, kappa: float, diversify: float) -> dict[str, RobotType]:
        """Целевой тип робота для каждого покупателя (UCB + штраф за дубли типов)."""
        used: Counter[RobotType] = Counter()
        res: dict[str, RobotType] = {}
        order = sorted(self.beliefs, key=lambda b: -max(self.beliefs[b].ucb(t, kappa) for t in self.types))
        for b in order:
            t = max(self.types, key=lambda t: self.beliefs[b].ucb(t, kappa) - diversify * used[t])
            res[b] = t
            used[t] += 1
        return res

    def suggest_min_price(self, b: str, t: RobotType, *, alpha_explore: float, alpha_exploit: float,
                          tol_rel: float, reserve: float) -> Optional[float]:
        """Бинарный поиск: minPrice внутри вилки [lo, hi]. Вилка узкая -> почти продаём (alpha_exploit),
        широкая -> делим по alpha_explore (0.5 = деление пополам). None — покупатель не заплатит >= reserve."""
        lo, hi = self.interval(b, t)
        if hi < reserve:
            return None
        rel = (hi - lo) / max(hi, EPS)
        alpha = alpha_exploit if rel <= tol_rel else alpha_explore
        m = max(lo + alpha * (hi - lo), reserve)
        return round(min(m, self.ceiling), 2)
