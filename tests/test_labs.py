from stockpile import flashsale, isolation, storage, workload


def test_engines_return_identical_results(loaded):
    report = workload.run_check(loaded, list(loaded.app.engines), log=lambda message: None)
    assert report["mismatches"] == []
    assert sum(entry["calls"] for entry in report["reads"].values()) > 0
    assert report["writes"]["checkout"]["calls"] > 0


def test_guarded_strategies_sell_exactly_the_stock(loaded, flash_case):
    engine, strategy = flash_case
    result = flashsale.run_strategy(loaded, engine, strategy)
    assert result["sold"] == loaded.flash.stock
    assert result["client_sold"] == result["sold"]
    assert result["left"] == 0
    assert result["oversold"] == 0


def test_naive_strategy_counts_its_oversell(loaded, engine):
    result = flashsale.run_strategy(loaded, engine, "naive")
    taken = loaded.flash.stock - result["left"]
    assert result["sold"] >= taken
    assert result["oversold"] == result["sold"] - taken
    assert result["client_sold"] == result["sold"]


def test_isolation_cell(loaded, isolation_case):
    engine, name, level, expected = isolation_case
    scenario = next(s for s in isolation.scenarios() if s.name == name)
    outcome = isolation.run_scenario(loaded, engine, scenario, level, loaded.isolation.step_wait)
    assert outcome["cell"] == expected


def test_dynamodb_guards_stop_each_anomaly(loaded):
    if "dynamodb" not in loaded.app.engines:
        return
    matrix = isolation.run_dynamo_matrix(loaded, log=lambda message: None)
    for row in matrix.values():
        assert row == {"plain writes": "anomaly", "conditional writes": "condition failed"}


def test_storage_reports_every_engine(loaded):
    result = storage.run_storage(loaded, list(loaded.app.engines), log=lambda message: None)
    for info in result.values():
        assert info["orders"] > 0
        assert info["bytes_per_order"] > 0
        assert info["index_bytes"] > 0
