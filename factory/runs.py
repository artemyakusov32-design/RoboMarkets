"""Изоляция прогонов и идемпотентная обработка событий."""
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from time import perf_counter

from .strategy import BaselineStrategy


class ApiError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


@dataclass
class Run:
    config: dict
    memory: dict = field(default_factory=dict)
    responses: dict = field(default_factory=dict)
    latest: dict = field(default_factory=dict)
    log: list = field(default_factory=list)
    result: dict = field(default_factory=dict)
    finished: bool = False
    last_seq: int = -1
    lock: RLock = field(default_factory=RLock)


class RunStore:
    def __init__(self, strategy=None):
        self.strategy = strategy or BaselineStrategy()
        self.runs = {}
        self.lock = RLock()

    def get(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise ApiError("unknown_run", "Сначала отправьте start", 404)
            return self.runs[run_id]

    def start(self, run_id, config):
        if config.get("runId", run_id) != run_id:
            raise ApiError("bad_run", "runId тела и URL различаются")
        try:
            assert type(config["horizon"]) is int and config["horizon"] >= 0
            assert type(config["capacity"]) is int and config["capacity"] > 0
            assert isinstance(config["machines"], list) and config["machines"]
            assert isinstance(config["buyers"], list)
            for machine in config["machines"]:
                assert isinstance(machine["id"], str) and machine["ops"]
                for op in machine["ops"]:
                    assert op["duration"] > 0 and op["inputs"] and op["output"]["kind"]
        except (KeyError, TypeError, AssertionError):
            raise ApiError("bad_start", "Некорректная конфигурация прогона")
        with self.lock:
            if run_id in self.runs:
                if self.runs[run_id].config != config:
                    raise ApiError("run_exists", "runId уже используется с другой конфигурацией", 409)
                return {"ok": True}
            self.runs[run_id] = Run(deepcopy(config))
        return {"ok": True}

    def event(self, run_id, payload):
        run = self.get(run_id)
        seq = payload.get("seq")
        if type(seq) is not int or seq < 0:
            raise ApiError("bad_event", "Нужен неотрицательный целый seq")
        with run.lock:
            if seq in run.responses:
                return deepcopy(run.responses[seq])
            if run.finished:
                raise ApiError("run_finished", "Прогон уже завершён", 409)
            if seq <= run.last_seq:
                raise ApiError("out_of_order", "Новое событие должно иметь возрастающий seq", 409)
            try:
                assert type(payload["t"]) is int and 0 <= payload["t"] <= run.config["horizon"]
                event = payload["event"]
                assert event["type"] in ("supply", "machine_done", "sale_result", "idle")
                if event["type"] == "supply":
                    assert event["item"]["id"] and event["item"]["kind"]
                state = payload["state"]
                assert isinstance(state["storage"]["items"], list)
                assert type(state["storage"]["free"]) is int and state["storage"]["free"] >= 0
                assert isinstance(state["cash"], (int, float))
                for m in state["machines"]:
                    assert m["id"] and (m["busyUntil"] is None or type(m["busyUntil"]) is int)
                for b in state["buyers"]:
                    assert b["id"] and (b["cooldownUntil"] is None or type(b["cooldownUntil"]) is int)
                for item in state["storage"]["items"]:
                    assert item["id"] and item["kind"] and isinstance(item.get("props", {}), dict)
            except (KeyError, TypeError, AssertionError):
                raise ApiError("bad_state", "Некорректное событие или снимок состояния")
            memory = deepcopy(run.memory)
            started = perf_counter()
            response = {"commands": self.strategy.decide(run.config, payload, memory)}
            elapsed = (perf_counter() - started) * 1000
            run.memory = memory
            run.responses[seq] = deepcopy(response)
            run.last_seq = seq
            run.latest = deepcopy(payload)
            run.log.append(dict(seq=seq, t=payload["t"], type=event["type"],
                commands=deepcopy(response["commands"]), report=deepcopy(payload.get("report", [])),
                decisionMs=round(elapsed, 3)))
            return response

    def finish(self, run_id, result):
        run = self.get(run_id)
        with run.lock:
            run.finished = True
            # Скрытые параметры покупателей из finish не используются стратегией.
            run.result = {k: deepcopy(v) for k, v in result.items() if k != "buyers"}
        return {"ok": True}

    def snapshot(self, run_id):
        run = self.get(run_id)
        with run.lock:
            return deepcopy(dict(runId=run_id, status="finished" if run.finished else "running",
                horizon=run.config["horizon"], events=len(run.responses),
                latest=run.latest, result=run.result, log=run.log[-100:]))

    def summaries(self):
        with self.lock:
            ids = list(self.runs)
        return [dict(runId=id_, status=self.snapshot(id_)["status"]) for id_ in ids]
