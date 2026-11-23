from screentime.sync import _forget_removed_servers


def test_removed_servers_lose_their_queue_and_disabled_ones_keep_it():
    state = {
        "outbox": {"Production": [1], "Local": [2], "Old": [3]},
        "latest": {"Old": {"d": "x"}, "Production": {}},
        "server_health": {"Old": {}},
    }
    _forget_removed_servers(state, {"Production", "Local"})  # Local is disabled in the config, Old is gone
    assert state["outbox"] == {"Production": [1], "Local": [2]}
    assert state["latest"] == {"Production": {}}
    assert state["server_health"] == {}
