from primer.workspace.files import FileResolvers


def test_file_resolvers_defaults_none():
    fr = FileResolvers()
    assert fr.document_resolver is None
    assert fr.secret_resolver is None


def test_file_resolvers_holds_callables():
    async def doc(_fm):
        return b"d"

    async def sec(_fm):
        return b"s"

    fr = FileResolvers(document_resolver=doc, secret_resolver=sec)
    assert fr.document_resolver is doc
    assert fr.secret_resolver is sec
