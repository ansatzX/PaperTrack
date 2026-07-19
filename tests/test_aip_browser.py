from importlib.util import find_spec


def test_obsolete_aip_browser_subprocess_module_is_removed():
    assert find_spec("papertrack.aip_browser") is None
