import json
from datetime import datetime

from .bench import PATTERNS
from .isolation import LEVELS

ORDER = ("load", "storage", "check", "bench", "flash", "pgbench", "isolation")
ENGINE_NAMES = {"postgres": "PostgreSQL", "mysql": "MySQL", "dynamodb": "DynamoDB"}


def save(cfg, name, data, merge=True):
    folder = cfg.app.results_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    if merge and path.exists() and isinstance(data, dict):
        previous = json.loads(path.read_text(encoding="utf-8")).get("data", {})
        if isinstance(previous, dict):
            data = {**previous, **data}
    payload = {
        "name": name,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "scale": cfg.data.scale,
        "data": data,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def saved(cfg):
    found = {}
    for name in ORDER:
        path = cfg.app.results_dir / f"{name}.json"
        if path.exists():
            found[name] = json.loads(path.read_text(encoding="utf-8"))["data"]
    return found


def fmt(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def render(cfg):
    data = saved(cfg)
    parts = [f"# ecomdb results (scale {cfg.data.scale})"]
    if "load" in data:
        rows = []
        for engine, timings in data["load"].items():
            rows.append(
                [ENGINE_NAMES[engine], fmt(timings.get("total"), 1), fmt(timings.get("items"))]
            )
        parts += ["## Load", table(["engine", "seconds", "items"], rows)]
    if "storage" in data:
        rows = []
        for engine, info in data["storage"].items():
            rows.append(
                [
                    ENGINE_NAMES[engine],
                    fmt(info["total_bytes"] / 2**20, 1),
                    fmt(info["index_bytes"] / 2**20, 1),
                    fmt(info["bytes_per_order"], 0),
                ]
            )
        parts += [
            "## Storage",
            table(["engine", "total MiB", "index MiB", "bytes per order"], rows),
        ]
    if "check" in data:
        report = data["check"]
        reads = sum(v["calls"] for v in report["reads"].values())
        writes = sum(v["calls"] for v in report["writes"].values())
        state = sum(v["calls"] for v in report["state"].values())
        parts += [
            "## Cross-engine check",
            f"{reads} reads, {writes} writes, {state} state comparisons, "
            f"{len(report['mismatches'])} mismatches",
        ]
    if "bench" in data:
        engines = list(data["bench"])
        header = ["access pattern"]
        for engine in engines:
            header += [
                f"{ENGINE_NAMES[engine]} p50 ms",
                f"{ENGINE_NAMES[engine]} p99 ms",
                "requests",
            ]
        rows = []
        for pattern in PATTERNS:
            row = [pattern]
            for engine in engines:
                result = data["bench"][engine].get(pattern, {})
                row += [
                    fmt(result.get("p50_ms")),
                    fmt(result.get("p99_ms")),
                    fmt(result.get("requests_per_call")),
                ]
            rows.append(row)
        parts += ["## Access patterns", table(header, rows)]
    if "flash" in data:
        rows = []
        for engine, strategies in data["flash"].items():
            for strategy, result in strategies.items():
                rows.append(
                    [
                        ENGINE_NAMES[engine],
                        strategy,
                        fmt(result["sold"]),
                        fmt(result["oversold"]),
                        fmt(result["per_second"], 0),
                        fmt(result["p99_ms"]),
                        ", ".join(f"{k} {v}" for k, v in sorted(result["retries"].items())) or "0",
                    ]
                )
        header = ["engine", "strategy", "sold", "oversold", "orders per s", "p99 ms", "retries"]
        parts += ["## Flash sale", table(header, rows)]
    if "pgbench" in data and data["pgbench"]:
        check = data["pgbench"]
        tps = check["pgbench_tps"]
        parts += [
            "## pgbench cross-check",
            f"pgbench {tps:.0f} tps, harness {check['harness_tps']:.0f} orders per s",
        ]
    if "isolation" in data:
        for engine, matrix in data["isolation"].items():
            if engine == "dynamodb":
                modes = ["plain writes", "conditional writes"]
                rows = [[name, *(row[m] for m in modes)] for name, row in matrix.items()]
                parts += ["## Isolation, DynamoDB", table(["scenario", *modes], rows)]
            else:
                rows = [[name, *(row[level] for level in LEVELS)] for name, row in matrix.items()]
                parts += [
                    f"## Isolation, {ENGINE_NAMES[engine]}",
                    table(["scenario", *(level.lower() for level in LEVELS)], rows),
                ]
    return "\n\n".join(parts) + "\n"


def write(cfg):
    text = render(cfg)
    cfg.app.results_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.app.results_dir / "report.md"
    path.write_text(text, encoding="utf-8")
    return path, text
