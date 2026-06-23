def test_primectl_version_is_a_string():
    import primectl
    assert isinstance(primectl.__version__, str)
    assert primectl.__version__  # non-empty
