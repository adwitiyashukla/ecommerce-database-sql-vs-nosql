from ecomdb import ddb
from ecomdb.sizing import index_entry_size, item_size, value_size


def test_string_and_number_sizes():
    assert value_size({"S": "abc"}) == 3
    assert value_size({"S": "caf\u00e9"}) == 5
    assert value_size({"N": "12345"}) == 4
    assert value_size({"N": "1000"}) == 2
    assert value_size({"N": "-7"}) == 3
    assert value_size({"N": "0"}) == 2
    assert value_size({"BOOL": True}) == 1


def test_item_size_counts_names_and_values():
    item = {"PK": ddb.s("CUST#1"), "qty": ddb.n(3)}
    assert item_size(item) == len("PK") + len("CUST#1") + len("qty") + 2


def test_index_entry_only_counts_projected_attributes():
    item = ddb.order_item(5, 9, "paid", "2025-07-08T10:00:00", 2, 4598, "OH", "45220")
    entry = index_entry_size(item, ["GSI2PK", "GSI2SK"], ["status", "total"])
    assert 0 < entry < item_size(item)
