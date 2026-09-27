from prism_core.position_peak import ratchet_highest_price


def test_missing_peak_is_initialised_and_must_be_persisted():
    scenario = {}
    assert ratchet_highest_price(scenario, 100.0, 113.0) == (113.0, True)
    assert scenario["highest_price"] == 113.0
    # Next run with a lower price keeps the stored peak and needs no write.
    assert ratchet_highest_price(scenario, 100.0, 110.0) == (113.0, False)


def test_peak_only_rises():
    scenario = {"highest_price": 120.0}
    assert ratchet_highest_price(scenario, 100.0, 125.0) == (125.0, True)
    assert ratchet_highest_price(scenario, 100.0, 90.0) == (125.0, False)


def test_below_entry_initialises_to_buy_price_and_bad_values_reset():
    scenario = {}
    assert ratchet_highest_price(scenario, 100.0, 95.0) == (100.0, True)
    assert ratchet_highest_price({"highest_price": "x"}, 100.0, 105.0) == (105.0, True)
    assert ratchet_highest_price({"highest_price": 0}, 0, 0) == (0.0, False)
