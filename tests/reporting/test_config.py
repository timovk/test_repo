"""Night configuration: YAML document, schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.reporting.config import (
    CallingConfig,
    NightConfig,
    PlaybackConfig,
    PollsCloseConfig,
    ReportingSpeedConfig,
    default_night_config,
    load_night_config,
    parse_hhmm,
)


def test_yaml_matches_schema_defaults() -> None:
    cfg = load_night_config()
    assert cfg.model_dump() == default_night_config().model_dump()
    assert cfg.polls_close.default == "21:00" and cfg.polls_close.timezone == "Europe/Amsterdam"
    assert cfg.playback.speeds == [1.0, 2.0, 5.0, 10.0, 25.0] and cfg.playback.base_rate == 60.0
    c = cfg.calling
    assert c.lean_threshold == 0.80 and c.projected_threshold == 0.995 and c.called_threshold == 0.9995
    assert c.retraction_threshold == 0.90 and c.call_at_poll_close is False
    assert cfg.data_category == "SIMULATED"
    assert len(cfg.fingerprint()) == 16


def test_validation_rejects_inconsistent_documents() -> None:
    with pytest.raises(ValidationError):
        CallingConfig(lean_threshold=0.999, projected_threshold=0.995)
    with pytest.raises(ValidationError):
        CallingConfig(retraction_threshold=0.996)
    with pytest.raises(ValidationError):
        CallingConfig(n_draws_min=5000, n_draws=4000)
    with pytest.raises(ValidationError):
        PollsCloseConfig(default="25:00")
    with pytest.raises(ValidationError):
        PollsCloseConfig(provinces={"NH": "9pm"})
    with pytest.raises(ValidationError):
        PollsCloseConfig(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        PlaybackConfig(speeds=[1, 2], default_speed=5)
    with pytest.raises(ValidationError):
        ReportingSpeedConfig(batches_by_size=[{"max_ballots": 100, "min": 1, "max": 1}])
    with pytest.raises(ValidationError):
        NightConfig.model_validate({"calling": {"unknown": 1}})


def test_parse_hhmm() -> None:
    t = parse_hhmm("21:00")
    assert (t.hour, t.minute) == (21, 0)
    with pytest.raises(ValueError):
        parse_hhmm("2100")


def test_timing_parameters_are_consistent() -> None:
    from app.reporting.config import FirstReportConfig

    with pytest.raises(ValidationError):
        FirstReportConfig(min_minutes=60, max_minutes=30)
    with pytest.raises(ValidationError):
        ReportingSpeedConfig(
            batches_by_size=[
                {"max_ballots": 100, "min": 1, "max": 1},
                {"max_ballots": 100, "min": 2, "max": 2},
                {"max_ballots": None, "min": 3, "max": 3},
            ]
        )
    rep = default_night_config().reporting
    # polls close at 21:00: the soft cap on the last batch is between 05:00 and 06:00
    assert 8 * 60 <= rep.latest_end_minutes <= 9 * 60
    assert rep.latest_end_minutes - rep.end_spread_minutes >= 7.5 * 60
