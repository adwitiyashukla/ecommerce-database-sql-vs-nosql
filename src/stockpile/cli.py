import argparse
import sys

from . import (
    bench,
    config,
    flashsale,
    generate,
    isolation,
    load,
    report,
    servers,
    storage,
    workload,
)

COMMANDS = (
    "up",
    "down",
    "status",
    "generate",
    "load",
    "storage",
    "check",
    "bench",
    "flash",
    "isolation",
    "report",
    "all",
)


def log(message):
    print(message, flush=True)


def ensure_data(cfg):
    manifest = generate.generate(cfg.data, cfg.app.low_stock)
    rows = sum(info["rows"] for info in manifest["tables"].values())
    log(f"data ready in {cfg.data.out_dir}: {rows:,} rows")


def cmd_up(cfg, engines, args):
    for engine in engines:
        if servers.managed(cfg, engine):
            started = servers.server_for(cfg, engine).start()
            log(f"{engine}: {'started' if started else 'already running'}")
        else:
            state = "reachable" if servers.reachable(cfg, engine) else "not reachable"
            log(f"{engine}: external, {state}")


def cmd_down(cfg, engines, args):
    for engine in engines:
        if servers.managed(cfg, engine):
            stopped = servers.server_for(cfg, engine).stop()
            log(f"{engine}: {'stopped' if stopped else 'not running'}")


def cmd_status(cfg, engines, args):
    failed = 0
    for engine in engines:
        ok = servers.reachable(cfg, engine)
        failed += 0 if ok else 1
        log(f"{engine}: {'reachable' if ok else 'not reachable'}")
    return 1 if failed else 0


def cmd_generate(cfg, engines, args):
    manifest = generate.generate(cfg.data, cfg.app.low_stock, force=args.force)
    for name, info in manifest["tables"].items():
        log(f"{name}: {info['rows']:,} rows")


def cmd_load(cfg, engines, args):
    ensure_data(cfg)
    results = {}
    for engine in engines:
        timings = load.LOADERS[engine](cfg)
        results[engine] = timings
        log(f"load {engine}: {timings['total']:.1f} s")
    report.save(cfg, "load", results)


def cmd_storage(cfg, engines, args):
    report.save(cfg, "storage", storage.run_storage(cfg, engines, log))


def cmd_check(cfg, engines, args):
    result = workload.run_check(cfg, engines, log)
    report.save(cfg, "check", result, merge=False)
    log(f"check {'passed' if result['ok'] else 'FAILED'}: {len(result['mismatches'])} mismatches")
    return 0 if result["ok"] else 1


def cmd_bench(cfg, engines, args):
    report.save(cfg, "bench", bench.run_bench(cfg, engines, log))


def cmd_flash(cfg, engines, args):
    report.save(cfg, "flash", flashsale.run_flash(cfg, engines, log))
    if "postgres" in engines:
        result = flashsale.pgbench_check(cfg)
        if result and "error" in result:
            log(f"pgbench cross-check skipped: {result['error']}")
        elif result:
            report.save(cfg, "pgbench", result, merge=False)
            tps = result["pgbench_tps"]
            log(f"pgbench {tps:.0f} tps, harness {result['harness_tps']:.0f} per s")


def cmd_isolation(cfg, engines, args):
    report.save(cfg, "isolation", isolation.run_isolation(cfg, engines, log))


def cmd_report(cfg, engines, args):
    path, text = report.write(cfg)
    print(text)
    log(f"written to {path}")


def cmd_all(cfg, engines, args):
    cmd_load(cfg, engines, args)
    cmd_storage(cfg, engines, args)
    status = cmd_check(cfg, engines, args)
    cmd_bench(cfg, engines, args)
    cmd_flash(cfg, engines, args)
    cmd_isolation(cfg, engines, args)
    cmd_report(cfg, engines, args)
    return status


HANDLERS = {name: globals()[f"cmd_{name}"] for name in COMMANDS}


def parse(argv):
    parser = argparse.ArgumentParser(
        prog="stockpile",
        description="One e-commerce marketplace on PostgreSQL, MySQL and DynamoDB",
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help="override one config value, can repeat",
    )
    parser.add_argument("--engines", default=None, help="comma separated list of engines")
    parser.add_argument("--force", action="store_true", help="regenerate data that is up to date")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(argv)
    try:
        cfg = config.load(args.config, args.overrides)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    engines = list(cfg.app.engines)
    if args.engines:
        engines = [e.strip() for e in args.engines.split(",") if e.strip()]
        unknown = [e for e in engines if e not in config.ENGINES]
        if unknown:
            print(f"unknown engines: {', '.join(unknown)}", file=sys.stderr)
            return 2
    return HANDLERS[args.command](cfg, engines, args) or 0
