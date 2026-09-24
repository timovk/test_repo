"""Named political regions and the municipality-name resolver."""

from __future__ import annotations

import dataclasses

import pytest

from app.simulation.regions import (
    MunicipalityRef,
    load_regions_config,
    normalize_name,
    regions_config_from_dict,
    resolve_municipality,
    resolve_regions,
)


def test_normalize_name():
    assert normalize_name("'s-Gravenhage") == "s gravenhage"
    assert normalize_name("Súdwest-Fryslân") == "sudwest fryslan"
    assert normalize_name("Bergen (NH.)") == "bergen"
    assert normalize_name("Hengelo (O.)") == "hengelo"
    assert normalize_name("  NUENEN, Gerwen en Nederwetten ") == "nuenen gerwen en nederwetten"


def test_resolver_with_names_codes_and_disambiguation(frame):
    names = list(frame.muni_names)
    names[0] = "Bergen (NH.)"
    names[1] = "Bergen (L.)"
    names[2] = "'s-Gravenhage"
    names[3] = "Súdwest-Fryslân"
    f = dataclasses.replace(frame, muni_names=names)
    p0 = f.province_codes[int(f.muni_province[0])]
    p1 = f.province_codes[int(f.muni_province[1])]
    assert p0 == p1  # both in the first synthetic province → ambiguous without a usable province
    assert resolve_municipality(f, "bergen") is None
    assert resolve_municipality(f, "Den Haag") == 2
    assert resolve_municipality(f, "sudwest friesland") == 3
    assert (
        resolve_municipality(
            f, MunicipalityRef(name="Súdwest Fryslan", province=f.province_codes[int(f.muni_province[3])])
        )
        == 3
    )
    assert resolve_municipality(f, MunicipalityRef(name="Den Haag", province="LI")) is None
    assert resolve_municipality(f, f.muni_codes[5]) == 5
    cfg = regions_config_from_dict(
        {
            "aliases": {"The Capital": "'s-Gravenhage"},
            "regions": {
                "r1": {
                    "label": "R1",
                    "municipalities": ["The Capital", {"name": "Nowhere", "province": "NB"}],
                    "codes": [f.muni_codes[10]],
                },
                "r2": {
                    "label": "R2",
                    "provinces": ["NB"],
                    "exclude": [f.muni_codes[f.munis_in_province(10)[0]]],
                },
            },
        }
    )
    r = resolve_regions(f, cfg)
    assert r.muni_mask("r1")[2] and r.muni_mask("r1")[10]
    assert r.unresolved["r1"] == ["Nowhere (NB)"]
    assert r.resolution_rate == pytest.approx(0.5)
    nb = f.munis_in_province(10)
    assert r.muni_mask("r2")[nb[1:]].all() and not r.muni_mask("r2")[nb[0]]
    assert r.muni_mask("r2").sum() == len(nb) - 1
    assert r.unit_mask(f, "r2").sum() == sum(len(f.units_in_muni(m)) for m in nb[1:])


def test_region_ids_must_be_identifiers():
    with pytest.raises(Exception, match="snake_case"):
        regions_config_from_dict({"regions": {"Bible Belt": {"label": "x"}}})


def test_shipped_regions_config_loads():
    cfg = load_regions_config()
    for rid in (
        "bible_belt",
        "catholic_south",
        "randstad_core",
        "university_towns",
        "gooi_wassenaar",
        "groningen_gasfield",
        "frisian",
        "wadden",
        "veluwe",
        "twente",
        "achterhoek",
        "zeeland_islands",
        "flevopolder",
        "rotterdam_port",
    ):
        assert rid in cfg.regions
    assert len(cfg.regions["bible_belt"].municipalities) >= 15
