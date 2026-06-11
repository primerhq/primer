def test_registry_built_from_fetched_spec(mock_session):
    reg = mock_session.session.registry
    assert reg.resolve("agent").plural == "agents"


def test_client_is_lazily_constructed(mock_session):
    # Accessing .client must not raise and must be reused.
    c1 = mock_session.session.client
    c2 = mock_session.session.client
    assert c1 is c2
