import math

OVERHEAD = 100


def value_size(value):
    if "S" in value:
        return len(value["S"].encode("utf-8"))
    if "N" in value:
        text = value["N"]
        digits = text.lstrip("-").replace(".", "").strip("0") or "0"
        return math.ceil(len(digits) / 2) + 1 + (1 if text.startswith("-") else 0)
    if "BOOL" in value or "NULL" in value:
        return 1
    if "B" in value:
        return len(value["B"])
    if "L" in value:
        return 3 + sum(value_size(v) + 1 for v in value["L"])
    if "M" in value:
        return 3 + sum(len(k.encode("utf-8")) + value_size(v) + 1 for k, v in value["M"].items())
    raise ValueError(f"unsupported attribute value {value!r}")


def item_size(item):
    return sum(len(name.encode("utf-8")) + value_size(value) for name, value in item.items())


def index_entry_size(item, keys, projected):
    names = {"PK", "SK", *keys, *projected}
    return sum(len(name.encode("utf-8")) + value_size(item[name]) for name in names if name in item)
