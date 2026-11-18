"""Единственная основная стратегия: обучение покупателей и оценка действий."""
from typing import Protocol

from .planning.controller import LearningController
from .planning.policy import QWeights
from .planning.schemas import EventMsg, StartMsg


class Strategy(Protocol):
    def decide(self, config: dict, payload: dict, memory: dict) -> list:
        ...


class LearningStrategy:
    name = 'Learning v1'

    def __init__(self, weights=None):
        self.weights = QWeights(dict(weights.w), weights.min_score) if weights else None

    def decide(self, config, payload, memory):
        if 'controller' not in memory:
            memory['controller'] = LearningController(
                StartMsg.from_wire(config), weights=self.weights
            )
        return memory['controller'].handle_event(EventMsg.from_wire(payload))
