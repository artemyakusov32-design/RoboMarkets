"""Локальная модель v1. Скрытый рынок принадлежит только симулятору.

Это собственный учебный эмулятор, не копия официального движка: одинаковые seed
здесь и в арене не обязаны означать одинаковый мир.
"""
from collections import deque
from copy import deepcopy
from random import Random

from .config import demo_config
from .domain import matches, operation_output


class Simulator:
    def __init__(self, seed=1, horizon=600):
        self.config = demo_config(horizon)
        self.supply_rng = Random(seed)
        self.market_rng = Random(seed + 100000)
        self.items = {}
        self.jobs = {}
        self.cooldowns = {}
        self.market = {b: dict(color=self.market_rng.choice(["red", "green", "blue"]),
            handles=self.market_rng.choice([True, False]), Pmax=self.market_rng.uniform(45, 75),
            f=self.market_rng.uniform(.3, .6)) for b in self.config["buyers"]}
        self.next_supply = {"sheet": 0, "control": 0}
        self.cash = 0.
        self.sold = self.refused = self.invalid = self.seq = self.item_seq = 0
        self.t = 0
        self.report = []
        self.pending = deque()

    def new_item(self, kind, props=None):
        self.item_seq += 1
        return dict(id="i-" + str(self.item_seq), kind=kind, props=props or {})

    def state(self):
        return deepcopy(dict(cash=round(self.cash, 6), storage=dict(capacity=self.config["capacity"],
            free=self.config["capacity"] - len(self.items) - len(self.jobs), items=list(self.items.values())),
            machines=[dict(id=m["id"], busyUntil=self.jobs[m["id"]]["until"] if m["id"] in self.jobs else None)
                for m in self.config["machines"]],
            buyers=[dict(id=b, cooldownUntil=self.cooldowns[b] if self.cooldowns.get(b, 0) > self.t else None)
                for b in self.config["buyers"]]))

    def execute(self, command, event, accepted):
        cmd = command.get("cmd")
        id_ = command.get("itemId")
        if cmd == "accept":
            if event["type"] != "supply" or event["item"]["id"] != id_ or id_ in accepted:
                return "not_offered"
            if self.state()["storage"]["free"] <= 0:
                return "storage_full"
            accepted.add(id_)
            self.items[id_] = deepcopy(event["item"])
            self.cash -= event["price"]
        elif cmd == "start":
            machine = next((m for m in self.config["machines"] if m["id"] == command.get("machineId")), None)
            if machine is None:
                return "unknown_machine"
            op = next((o for o in machine["ops"] if o["id"] == command.get("opId")), None)
            if op is None:
                return "unknown_op"
            if machine["id"] in self.jobs:
                return "machine_busy"
            ids = command.get("itemIds", [])
            if len(ids) != len(op["inputs"]) or len(set(ids)) != len(ids):
                return "bad_inputs"
            inputs = [self.items.get(id_) for id_ in ids]
            if any(i is None or not matches(i, template) for i, template in zip(inputs, op["inputs"])):
                return "bad_inputs"
            output_id = self.new_item(op["output"]["kind"])["id"]
            output = operation_output(op, inputs, output_id)
            for id_ in ids:
                del self.items[id_]
            self.jobs[machine["id"]] = dict(until=self.t + op["duration"], item=output, opId=op["id"])
            self.cash -= op["cost"]
        elif cmd == "offer":
            robot = self.items.get(id_)
            if robot is None or robot["kind"] != "robot":
                return "no_such_robot"
            buyer = command.get("buyerId")
            if buyer not in self.market:
                return "unknown_buyer"
            self.cash -= self.config["offerFee"]
            result = dict(type="sale_result", buyerId=buyer, itemId=id_, item=deepcopy(robot), outcome="refused")
            if self.cooldowns.get(buyer, 0) > self.t:
                result["reason"] = "cooldown"
            else:
                self.cooldowns[buyer] = self.t + self.config["buyerCooldown"]
                target = self.market[buyer]
                mismatches = sum(robot["props"].get(k) != target[k] for k in ("color", "handles"))
                price = target["Pmax"] * (1 if mismatches == 0 else target["f"] if mismatches == 1 else 0)
                price *= self.market_rng.uniform(.98, 1.02)
                if price >= command.get("minPrice", 0):
                    self.cash += price
                    del self.items[id_]
                    result.update(outcome="sold", price=price)
                    self.sold += 1
                else:
                    result["reason"] = "below_min_price"
            if result["outcome"] == "refused":
                self.refused += 1
            self.pending.append(result)
        elif cmd == "discard":
            if id_ not in self.items:
                return "no_such_item"
            del self.items[id_]
        else:
            return "unknown_command"
        return None

    def emit(self, event, decide):
        self.pending.append(event)
        while self.pending:
            current = self.pending.popleft()
            payload = dict(seq=self.seq, t=self.t, event=current, state=self.state(), report=self.report)
            self.seq += 1
            response = decide(deepcopy(payload))
            self.report = []
            accepted = set()
            for index, command in enumerate(response.get("commands", [])[:50]):
                reason = self.execute(command, current, accepted)
                report = dict(cmd=index, ok=reason is None)
                if reason:
                    report["reason"] = reason
                    self.invalid += 1
                self.report.append(report)

    def run(self, decide):
        for self.t in range(self.config["horizon"] + 1):
            had_event = False
            for machine in self.config["machines"]:
                job = self.jobs.get(machine["id"])
                if job and job["until"] == self.t:
                    del self.jobs[machine["id"]]
                    self.items[job["item"]["id"]] = job["item"]
                    self.emit(dict(type="machine_done", machineId=machine["id"], opId=job["opId"], item=job["item"]), decide)
                    had_event = True
            for kind in self.next_supply:
                if self.t == self.next_supply[kind]:
                    self.next_supply[kind] += self.supply_rng.randint(2, 4)
                    self.emit(dict(type="supply", item=self.new_item(kind), price=4 if kind == "sheet" else 3), decide)
                    had_event = True
            if not had_event and self.t % self.config["idleEvery"] == 0:
                self.emit(dict(type="idle"), decide)
            self.cash -= sum(self.config["storageFee"][i["kind"]] for i in self.items.values())
        return dict(profit=round(self.cash, 2), sold=self.sold, refused=self.refused,
            invalidCommands=self.invalid, events=self.seq)
