"""Единственная основная стратегия: обучение покупателей и оценка действий."""
from typing import Protocol

from .planning.controller import LearningController
from .planning.schemas import EventMsg, StartMsg


class Strategy(Protocol):
    def decide(self, config: dict, payload: dict, memory: dict) -> list:
        ...


class LearningStrategy:
    name = 'Learning v1'

    def decide(self, config, payload, memory):
        if 'controller' not in memory:
            memory['controller'] = LearningController(StartMsg.from_wire(config))
        return memory['controller'].handle_event(EventMsg.from_wire(payload))
