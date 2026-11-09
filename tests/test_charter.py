import pytest
import yaml

from mathresearch.charter import DEFAULT_CHARTER, Charter, CharterError


def test_default_charter_loads_and_normalises_lanes(tmp_path):
    p = tmp_path / "charter.yaml"
    p.write_text(DEFAULT_CHARTER, encoding="utf-8")
    c = Charter.load(p)
    assert "post-quantum" in c.objective
    assert len(c.domains) == 7
    assert c.excluded_domains == [] and "Excluded" not in c.to_prompt()
    assert abs(sum(c.lanes.values()) - 1.0) < 1e-9
    assert c.lanes["harvest"] > c.lanes["deepen"] > c.lanes["open"]


def test_prompt_rendering_mentions_target_properties():
    c = Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER))
    text = c.to_prompt()
    assert text.startswith("# Research charter")
    assert "trapdoor" in text and "group action" in text
    assert "Excluded as initial directions" not in text
    assert "Excluded as initial directions" in Charter.from_dict({**yaml.safe_load(DEFAULT_CHARTER), "excluded_domains": ["x"]}).to_prompt()


def test_missing_domains_rejected():
    with pytest.raises(CharterError):
        Charter.from_dict({"objective": "x"})


def test_unknown_lane_rejected():
    with pytest.raises(CharterError):
        Charter.from_dict({"objective": "x", "domains": ["a"], "lanes": {"harvest": 1, "bogus": 1}})


def test_lanes_fill_missing_with_zero():
    c = Charter.from_dict({"objective": "x", "domains": ["a"], "lanes": {"harvest": 2, "deepen": 2}})
    assert c.lanes == {"harvest": 0.5, "deepen": 0.5, "open": 0.0}
