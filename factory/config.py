"""Открытая конфигурация v1 для локальных прогонов; арена присылает свою в start."""
from copy import deepcopy


def demo_config(horizon=600):
    def op(id_, inputs, output, duration, cost, inherit=None):
        result = dict(id=id_, inputs=inputs, output=output, duration=duration, cost=cost)
        if inherit is not None:
            result["inherit"] = inherit
        return result

    body = {"kind": "body", "props": {"color": None}}
    machines = [
        {"id": "m-cut", "ops": [op("cut", [{"kind": "sheet"}],
         {"kind": "body", "props": {"color": None, "handles": False}}, 2, 1)]},
        {"id": "m-handles", "ops": [op("add-handles", [{"kind": "body", "props":
         {"color": None, "handles": False}}], {"kind": "body", "props": {"handles": True}}, 2, 2, 0)]},
        {"id": "m-paint", "ops": [op("paint-" + color, [body],
         {"kind": "body", "props": {"color": color}}, 3, 2, 0)
         for color in ["red", "green", "blue"]]},
        {"id": "m-assemble", "ops": [op("assemble", [{"kind": "body", "props":
         {"color": "*"}}, {"kind": "control"}], {"kind": "robot", "props": {}}, 3, 3, 0)]},
    ]
    return deepcopy(dict(specVersion="v1", mode="practice", horizon=horizon,
        eventTimeoutMs=2000, idleEvery=10, capacity=12,
        storageFee={"sheet": .1, "control": .1, "body": .2, "robot": .5},
        offerFee=1, buyerCooldown=8, priceCeiling=100, buyers=["b1", "b2", "b3", "b4"],
        attributes={"color": ["red", "green", "blue"], "handles": [True, False]},
        machines=machines))
