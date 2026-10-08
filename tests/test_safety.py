import pytest
from stemble_mcp import server as s


def test_own_path_accepts_relative():
    assert s._own_path("/courses/1/assignments") == "/courses/1/assignments"


@pytest.mark.parametrize("bad", ["//evil.com/x", "https://evil.com/", "courses/1", "/a\\b", "/x://y"])
def test_own_path_rejects(bad):
    with pytest.raises(ValueError):
        s._own_path(bad)


def test_safe_confines_to_output(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "OUTPUT", tmp_path)
    assert s._safe("lab1") == (tmp_path / "lab1").resolve()
    assert s._safe("") == tmp_path.resolve()
    for bad in ["../x", "/etc/passwd", "a/../../x", "~/.zshrc"]:
        with pytest.raises(ValueError):
            s._safe(bad)


def test_public_https_blocks_private():
    assert not s._public_https("http://example.com/a.png")
    assert not s._public_https("https://127.0.0.1/a.png")
    assert not s._public_https("https://169.254.169.254/")
    assert not s._public_https("https://localhost/")


def test_strip_keeps_exponents():
    assert s._strip("10<sup>-3</sup> M H<sub>2</sub>O") == "10^(-3) M H_2O"


def test_slim_noise_top_level_only():
    out = s._slim({"user": {"x": 1}, "task": {"config": "keep", "errors": ["e"]}})
    assert out == {"task": {"config": "keep", "errors": ["e"]}}
