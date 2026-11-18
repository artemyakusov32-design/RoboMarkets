"""Формат весов для передачи из локального обучения в сервис."""

import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile

from .policy import DEFAULT_Q, QWeights


def load_weights(path: Path) -> QWeights:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Ожидается файл весов Learning v1 с version=1")
    weights = data.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(DEFAULT_Q):
        raise ValueError("Файл должен содержать ровно 27 коэффициентов Learning v1")
    if not all(type(value) in (int, float) and math.isfinite(value) for value in weights.values()):
        raise ValueError("Все коэффициенты должны быть конечными числами")
    return QWeights({name: float(value) for name, value in weights.items()})


def save_weights(path: Path, weights: QWeights, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump({"version": 1, "weights": weights.w, "training": report},
                      file, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
