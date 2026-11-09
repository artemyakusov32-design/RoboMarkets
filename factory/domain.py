"""Общие правила предметов. Здесь нет HTTP и знаний о покупателях."""
from copy import deepcopy
from itertools import permutations


def matches(item, template):
    if item["kind"] != template["kind"]:
        return False
    for key, value in template.get("props", {}).items():
        actual = item.get("props", {}).get(key)
        if value == "*":
            if actual is None:
                return False
        elif actual != value:
            return False
    return True


def find_inputs(items, templates):
    for group in permutations(items, len(templates)):
        if all(matches(item, template) for item, template in zip(group, templates)):
            return list(group)
    return None


def operation_output(op, inputs, item_id):
    props = {}
    if "inherit" in op:
        props.update(deepcopy(inputs[op["inherit"]].get("props", {})))
    props.update(deepcopy(op["output"].get("props", {})))
    return dict(id=item_id, kind=op["output"]["kind"], props=props)
