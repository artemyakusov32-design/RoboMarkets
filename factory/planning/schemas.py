"""Внутренние типы планировщика. HTTP-валидация и seq находятся в RunStore.

Адаптация присланного алгоритма к стандартной библиотеке Python без Pydantic/FastAPI.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from types import SimpleNamespace
from typing import Optional


@dataclass
class Item:
    id: str
    kind: str
    props: dict


@dataclass
class Template:
    kind: str
    props: dict


@dataclass
class Command:
    cmd: str
    itemId: Optional[str] = None
    machineId: Optional[str] = None
    opId: Optional[str] = None
    itemIds: Optional[list[str]] = None
    buyerId: Optional[str] = None
    minPrice: Optional[float] = None

    def to_wire(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


def item(raw):
    return Item(raw['id'], raw['kind'], dict(raw.get('props', {})))


class StartMsg(SimpleNamespace):
    @classmethod
    def from_wire(cls, raw):
        values = dict(eventTimeoutMs=2000, offerFee=1, buyerCooldown=8,
                      priceCeiling=100, storageFee={})
        values.update(raw)
        machines = []
        for machine in raw['machines']:
            ops = []
            for op in machine['ops']:
                data = dict(cost=0, inherit=None)
                data.update(op)
                data['inputs'] = [Template(t['kind'], dict(t.get('props', {}))) for t in op['inputs']]
                data['output'] = Template(op['output']['kind'], dict(op['output'].get('props', {})))
                ops.append(SimpleNamespace(**data))
            machines.append(SimpleNamespace(id=machine['id'], ops=ops))
        values['machines'] = machines
        return cls(**values)


class EventMsg(SimpleNamespace):
    @classmethod
    def from_wire(cls, raw):
        event = dict(item=None, price=None, machineId=None, buyerId=None,
                     itemId=None, outcome=None, reason=None)
        event.update(raw['event'])
        if event['item'] is not None:
            event['item'] = item(event['item'])
        st = raw['state']
        storage = SimpleNamespace(**dict(st['storage'], items=[item(i) for i in st['storage']['items']]))
        state = SimpleNamespace(cash=st['cash'], storage=storage,
            machines=[SimpleNamespace(**m) for m in st['machines']],
            buyers=[SimpleNamespace(**b) for b in st['buyers']])
        report = [SimpleNamespace(**dict(dict(cmd=-1, ok=True, reason=None), **r)) for r in raw.get('report', [])]
        return cls(seq=raw['seq'], t=raw['t'], event=SimpleNamespace(**event), state=state, report=report)


def item_matches(template, value):
    if value.kind != template.kind:
        return False
    for key, want in template.props.items():
        have = value.props.get(key)
        if want == '*':
            if have is None:
                return False
        elif have != want:
            return False
    return True


def op_result_props(op, inputs):
    props = dict(inputs[op.inherit].props) if op.inherit is not None else {}
    props.update(op.output.props)
    return props
