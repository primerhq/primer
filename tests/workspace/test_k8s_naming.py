from primer.workspace.k8s.naming import k8s_object_name


def test_short_id_kept_as_is():
    name = k8s_object_name("ws-abc-123")
    assert name == "primer-ws-ws-abc-123"


def test_long_id_hashed():
    long_id = "a" * 60
    name = k8s_object_name(long_id)
    assert len(name) <= 63
    assert name.startswith("primer-ws-")
    # Deterministic
    assert k8s_object_name(long_id) == name


def test_dns_label_safe():
    name = k8s_object_name("Ugly_Id_!@#")
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert not name.startswith("-") and not name.endswith("-")
