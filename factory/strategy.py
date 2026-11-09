"""Заменяемая базовая стратегия. Вход: конфигурация, событие, память одного прогона."""
from copy import deepcopy
from typing import Protocol

from .domain import find_inputs


class Strategy(Protocol):
    def decide(self, config: dict, payload: dict, memory: dict) -> list:
        ...


class BaselineStrategy:
    """Конвейер v1, чередование типов и продажа свободным покупателям.

    Не обучается рынку. Все изменения виртуального склада внутри ответа учитываются,
    чтобы один предмет не оказался в двух командах. Новые результаты станков
    появляются только на следующих событиях, а не в этом ответе.
    """

    def decide(self, config, payload, memory):
        state = payload["state"]
        items = deepcopy(state["storage"]["items"])
        free = state["storage"]["free"]
        t = payload["t"]
        remaining = config["horizon"] - t
        commands = []
        busy = {m["id"] for m in state["machines"] if m["busyUntil"] is not None}
        available = [b["id"] for b in state["buyers"] if b["cooldownUntil"] is None]

        # Не считаем место свободным после offer: покупатель может отказать.
        for robot in [i for i in items if i["kind"] == "robot"]:
            if not available:
                break
            buyer = available.pop(0)
            commands.append(dict(cmd="offer", buyerId=buyer, itemId=robot["id"], minPrice=1))

        # Сначала поздние стадии конвейера; длительности берем из start.
        stage = {"robot": 0, "body": 1}
        machines = sorted(config["machines"], key=lambda m: min(
            stage.get(op["output"]["kind"], 2) for op in m["ops"]))
        variant = memory.get("variant", 0)
        colors = config.get("attributes", {}).get("color", ["red"])
        want_handles = bool((variant // len(colors)) % 2)
        for machine in machines:
            if machine["id"] in busy:
                continue
            for op in machine["ops"]:
                output = op["output"]
                if op["duration"] > remaining:
                    continue
                # Профиль базового планировщика — v1; описание входов общее.
                if output["kind"] == "body":
                    color = output.get("props", {}).get("color")
                    if color is not None and color != colors[variant % len(colors)]:
                        continue
                    if op["id"] == "add-handles" and not want_handles:
                        continue
                    if op["id"] == "cut" and remaining < 8:
                        continue
                    if color is not None and remaining < op["duration"] + 3:
                        continue
                group = find_inputs(items, op["inputs"])
                if group is None:
                    continue
                if output["kind"] == "body" and output.get("props", {}).get("color") is not None:
                    handles_machine = any(m["id"] == "m-handles" for m in config["machines"])
                    if want_handles and handles_machine and not group[0].get("props", {}).get("handles"):
                        continue
                ids = [item["id"] for item in group]
                commands.append(dict(cmd="start", machineId=machine["id"], opId=op["id"], itemIds=ids))
                items = [i for i in items if i["id"] not in ids]
                free += len(ids) - 1  # входы ушли, одно место зарезервировано
                busy.add(machine["id"])
                if output["kind"] == "robot":
                    variant += 1
                    memory["variant"] = variant
                break

        event = payload["event"]
        if event["type"] == "supply" and free > 0:
            kind = event["item"]["kind"]
            count = sum(i["kind"] == kind for i in items)
            if kind in ("sheet", "control") and count < 2 and remaining >= (8 if kind == "sheet" else 3):
                commands.append(dict(cmd="accept", itemId=event["item"]["id"]))
        return commands[:50]
