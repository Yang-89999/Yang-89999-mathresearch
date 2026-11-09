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


def test_target_flags_render_full_definitions_and_accept_overrides():
    base = yaml.safe_load(DEFAULT_CHARTER)
    text = Charter.from_dict(base).to_prompt()
    assert "wants_trapdoor" not in text and "Key generation outputs a public instance" in text
    assert "Homomorphic structure (not required)." in text and "Shor" in text
    custom = {**base, "target": {**base["target"], "wants_trapdoor": {"wanted": True, "description": "My own  words."},
                                 "wants_group_action": False}}
    text = Charter.from_dict(custom).to_prompt()
    assert "- A trapdoor (wanted): My own words." in text and "A group action (not required)." in text
    for bad in ({"wants_trapdor": True}, {"wants_trapdoor": "yes"}, {"wants_trapdoor": {"wanted": True, "why": "x"}}):
        with pytest.raises(CharterError):
            Charter.from_dict({**base, "target": bad})
