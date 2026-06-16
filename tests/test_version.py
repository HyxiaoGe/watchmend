# tests/test_version.py
def test_version_string_present():
    from sentinel import __version__

    assert isinstance(__version__, str)
    assert __version__  # 非空
