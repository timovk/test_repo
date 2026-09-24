"""Helpers that turn a scenario + geography into engine :class:`~app.elections.types.RaceSpec` s.

Race codes follow docs/ARCHITECTURE.md §5: ``PRES`` (national parent), ``PRES-<PV>``,
``HOUSE-<PV>-<NN>``, ``SEN-<PV>-<1|2>``, ``GOV-<PV>``, ``MAYOR-<GMxxxx>``.  District plans and
Senate seat classes come from the districts subsystem; these helpers only assemble races from
them, so services and tests build races the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from app.core.constitution import ElectoralSystem, RaceType
from app.core.errors import ScenarioError
from app.elections.types import BallotLine, RaceSpec
from app.geography.frame import GeographyFrame
from app.scenarios.schema import ScenarioDocument
from app.simulation.candidates import RaceCandidates, RaceSlot


def _home_province(frame: GeographyFrame | None, muni_code: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if frame is None or muni_code is None:
        return None
    m = frame.muni_index_or_none(muni_code)
    return frame.province_codes[int(frame.muni_province[m])] if m is not None else None


def ticket_lines(doc: ScenarioDocument, frame: GeographyFrame | None = None) -> list[BallotLine]:
    """Presidential ballot lines (one per ticket; line key = presidential candidate key)."""
    lines = []
    for t in doc.president.tickets:
        try:
            p, vp = doc.candidate(t.president), doc.candidate(t.vice_president)
        except KeyError as exc:
            raise ScenarioError(f"ticket references unknown candidate {exc.args[0]}") from exc
        lines.append(
            BallotLine(
                key=p.key,
                party_code=t.party,
                candidate_key=p.key,
                running_mate_key=vp.key,
                label=f"{p.first_name} {p.last_name} / {vp.first_name} {vp.last_name}",
                quality=p.quality,
                incumbent=t.incumbent,
                home_province=_home_province(frame, p.home_municipality, p.home_province),
                home_municipality=p.home_municipality,
                running_mate_home_province=_home_province(frame, vp.home_municipality, vp.home_province),
                withdrawn=t.withdrawn,
            )
        )
    return lines


def presidential_races(
    frame: GeographyFrame,
    lines: Sequence[BallotLine],
    ev_by_province: Mapping[str, int] | None = None,
    *,
    include_national: bool = True,
    incumbent_party: str | None = None,
) -> list[RaceSpec]:
    """One winner-take-all ``PRES-<PV>`` contest per province (+ the national ``PRES`` parent)."""
    races = []
    for p, pv in enumerate(frame.province_codes):
        races.append(
            RaceSpec(
                key=f"PRES-{pv}",
                race_type=RaceType.PRESIDENT_PROVINCE,
                unit_index=frame.units_in_province(p),
                lines=list(lines),
                electoral_system=ElectoralSystem.WINNER_TAKE_ALL,
                electoral_votes=(ev_by_province or {}).get(pv),
                province_code=pv,
                incumbent_party=incumbent_party,
            )
        )
    if include_national:
        races.append(
            RaceSpec(
                key="PRES",
                race_type=RaceType.PRESIDENT,
                unit_index=np.arange(frame.n_units),
                lines=list(lines),
                electoral_system=ElectoralSystem.WINNER_TAKE_ALL,
                incumbent_party=incumbent_party,
            )
        )
    return races


def house_slots(frame: GeographyFrame, unit_district: Sequence[str] | np.ndarray) -> list[RaceSlot]:
    """Race slots ``HOUSE-<district>`` from a unit → district code mapping (e.g. ``"NB-07"``)."""
    codes = np.asarray(unit_district, dtype=object)
    if len(codes) != frame.n_units:
        raise ValueError("unit_district must have one entry per unit")
    slots = []
    for d in sorted(set(codes.tolist())):
        idx = np.flatnonzero(codes == d)
        pv = str(d).split("-")[0]
        slots.append(
            RaceSlot(
                key=f"HOUSE-{d}",
                race_type=RaceType.HOUSE,
                unit_index=idx,
                province_code=pv,
                office_label="the Tweede Kamer",
            )
        )
    return slots


def senate_slots(frame: GeographyFrame, seats: Sequence[tuple[str, int]]) -> list[RaceSlot]:
    """Race slots ``SEN-<PV>-<seat>`` for the (province, seat number) pairs up for election."""
    return [
        RaceSlot(
            key=f"SEN-{pv}-{seat}",
            race_type=RaceType.SENATE,
            unit_index=frame.units_in_province(frame.province_index(pv)),
            province_code=pv,
            office_label="the Eerste Kamer",
        )
        for pv, seat in seats
    ]


def governor_slots(frame: GeographyFrame, provinces: Sequence[str] | None = None) -> list[RaceSlot]:
    """``GOV-<PV>`` slots (all provinces by default)."""
    return [
        RaceSlot(
            key=f"GOV-{pv}",
            race_type=RaceType.GOVERNOR,
            unit_index=frame.units_in_province(frame.province_index(pv)),
            province_code=pv,
            office_label=f"Governor of {frame.province_names[frame.province_index(pv)]}",
        )
        for pv in (provinces or frame.province_codes)
    ]


def mayor_slots(frame: GeographyFrame, municipalities: Sequence[str] | None = None) -> list[RaceSlot]:
    """``MAYOR-<GMxxxx>`` slots (all municipalities by default)."""
    out = []
    for code in municipalities or frame.muni_codes:
        m = frame.muni_index(code)
        out.append(
            RaceSlot(
                key=f"MAYOR-{code}",
                race_type=RaceType.MAYOR,
                unit_index=frame.units_in_muni(m),
                province_code=frame.province_codes[int(frame.muni_province[m])],
                office_label=f"Mayor of {frame.muni_names[m]}",
            )
        )
    return out


def races_from_candidates(slots: Sequence[RaceSlot], fielded: Mapping[str, RaceCandidates]) -> list[RaceSpec]:
    """Single-member FPTP :class:`RaceSpec` s from slots and their generated candidates."""
    races = []
    for s in slots:
        rc = fielded[s.key]
        inc = next((ln for ln in rc.lines if ln.incumbent), None)
        district = s.key.split("-", 1)[1] if s.race_type == RaceType.HOUSE else None
        muni = s.key.split("-", 1)[1] if s.race_type == RaceType.MAYOR else None
        races.append(
            RaceSpec(
                key=s.key,
                race_type=s.race_type,
                unit_index=np.asarray(s.unit_index, dtype=np.int64),
                lines=list(rc.lines),
                electoral_system=ElectoralSystem.FPTP,
                province_code=s.province_code,
                district_code=district,
                municipality_code=muni,
                incumbent_party=inc.party_code if inc else None,
                is_open_seat=inc is None,
            )
        )
    return races
