"""Polling configuration, fictional-pollster guard and poll-type vocabulary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.polling.config import PollingConfig, PollsterConfig, looks_like_real_pollster, resolve_pollsters
from app.polling.types import (
    POLL_TYPES,
    is_province_code,
    lookup_method,
    poll_group_for_race,
    province_of_geo,
    race_code_for,
    spec_poll_counts,
)
from app.scenarios.schema import PollingSpec, PollsterSpec


def test_config_file_has_fictional_pollsters(polling_config: PollingConfig) -> None:
    names = [p.name for p in polling_config.pollsters]
    assert 8 <= len(names) <= 10
    assert len(set(names)) == len(names)
    for p in polling_config.pollsters:
        assert p.fictional is True
        assert p.rating > 0
        assert p.rating_label
        assert not looks_like_real_pollster(p.name)
        assert p.typical_sample >= 300
    # house effects are small (percentage points) and use the demo party codes
    codes = {"PA", "SAP", "VLP", "DM", "CVU", "NVB", "PLB", "RV"}
    for p in polling_config.pollsters:
        assert set(p.house_effects) <= codes
        assert all(abs(v) <= 3.0 for v in p.house_effects.values())


@pytest.mark.parametrize(
    "name", ["Peil.nl", "Ipsos I&O", "Verian", "EenVandaag Opiniepanel", "Kantar Public", "YouGov NL"]
)
def test_real_pollster_names_are_rejected(name: str) -> None:
    assert looks_like_real_pollster(name)
    with pytest.raises(ValidationError):
        PollsterConfig(name=name)


@pytest.mark.parametrize("name", ["Polderpeil", "Kompas Research", "Deltametrie", "Duinzicht Data"])
def test_invented_names_are_accepted(name: str) -> None:
    assert not looks_like_real_pollster(name)
    assert PollsterConfig(name=name).name == name


def test_duplicate_pollsters_rejected() -> None:
    with pytest.raises(ValidationError):
        PollingConfig(pollsters=[PollsterConfig(name="Polderpeil"), PollsterConfig(name="Polderpeil")])


def test_invalid_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        PollsterConfig(name="Polderpeil", population="XX")
    with pytest.raises(ValidationError):
        PollsterConfig(name="Polderpeil", poll_types=["exit_poll"])
    with pytest.raises(ValidationError):
        PollsterConfig(name="Polderpeil", rating=-0.5)
    with pytest.raises(ValidationError):
        PollsterConfig(name="Polderpeil", rating=6.0)
    # rating 0 is a deliberate exclusion from the averages (e.g. an editor distrusts a pollster)
    assert PollsterConfig(name="Polderpeil", rating=0).rating == 0.0


def test_resolve_pollsters_prefers_scenario(polling_config: PollingConfig) -> None:
    assert resolve_pollsters([], polling_config) == polling_config.pollsters
    scen = [PollsterSpec(name="Scenario Peilers", rating=1.1)]
    assert resolve_pollsters(scen, polling_config) == scen
    with pytest.raises(ValueError):
        resolve_pollsters([PollsterSpec(name="Peil.nl")], polling_config)


def test_scenario_pollster_spec_is_pollster_like() -> None:
    spec = PollsterSpec(name="Scenario Peilers", house_effects={"PA": 1.0})
    for attr in ("name", "rating", "method", "typical_sample", "house_effects"):
        assert hasattr(spec, attr)


def test_race_code_round_trip() -> None:
    assert race_code_for("national_president", "NL") == "PRES"
    assert race_code_for("province_president", "NB") == "PRES-NB"
    assert race_code_for("house_district", "NB-07") == "HOUSE-NB-07"
    assert race_code_for("governor", "UT") == "GOV-UT"
    assert race_code_for("senate", "NB") is None
    assert race_code_for("senate", "NB-1") == "SEN-NB-1"
    assert race_code_for("generic_house", "NL") is None
    for code in ("PRES", "PRES-NB", "HOUSE-NB-07", "GOV-UT"):
        group = poll_group_for_race(code)
        assert group is not None
        assert race_code_for(*group) == code
    assert poll_group_for_race("SEN-NB-2") == ("senate", "NB")
    assert poll_group_for_race("MAYOR-GM0855") is None


def test_geo_helpers() -> None:
    assert province_of_geo("NB-07") == "NB"
    assert province_of_geo("NB") == "NB"
    assert province_of_geo("NL") is None
    assert is_province_code("ZH") and not is_province_code("NL") and not is_province_code("ZH-01")


def test_spec_poll_counts() -> None:
    counts = spec_poll_counts(PollingSpec())
    assert counts["national_president"] == 45
    assert counts["generic_house"] == 20
    assert set(counts) <= set(POLL_TYPES)
    assert spec_poll_counts({"senate": 3}) == {"senate": 3}
    with pytest.raises(ValueError):
        spec_poll_counts({"exit_poll": 3})


def test_lookup_method_tolerates_free_text_labels() -> None:
    table = {"phone": 1.0, "mixed": 0.98, "panel": 0.95, "online": 0.9, "ivr": 0.85}
    assert lookup_method("phone", table, 0.5) == 1.0
    assert lookup_method("IVR", table, 0.5) == 0.85
    assert lookup_method("probability panel", table, 0.5) == 0.95
    assert lookup_method("online opt-in", table, 0.5) == 0.9
    assert lookup_method("Mixed Mode", table, 0.5) == 0.98
    assert lookup_method("carrier pigeon", table, 0.5) == 0.5
    assert lookup_method(None, table, 0.5) == 0.5
