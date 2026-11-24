"""Independent SDK install smoke — no server package required."""

def test_import_sdk():
    import dataagent_sdk
    from dataagent_sdk import Client

    assert dataagent_sdk.__version__
    tools = Client("http://localhost").as_tools("p")
    assert tools
