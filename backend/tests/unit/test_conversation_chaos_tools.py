from scripts.llm_fault_server import _fault_for_index


def test_llm_fault_schedule_is_exactly_twenty_percent_and_cycles_types():
    faults = [_fault_for_index(index) for index in range(20)]

    assert sum(fault != "success" for fault in faults) == 4
    assert {fault for fault in faults if fault != "success"} == {
        "timeout", "http_429", "http_500", "bad_json",
    }


def test_llm_fault_schedule_remains_twenty_percent_for_larger_runs():
    faults = [_fault_for_index(index) for index in range(100)]

    assert sum(fault != "success" for fault in faults) == 20
