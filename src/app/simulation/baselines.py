"""Baselines: importing historical results as calibration targets, and model-based analysis.

(a) **Historical import** — :func:`import_historical_results` turns a CSV of REAL
    municipality-level results (``municipality_code, party, votes``) plus a YAML mapping from real
    party names to the FICTIONAL model parties (weights, so one real party can be split over
    several model parties) into municipality target shares.  The output is labelled
    :data:`PROVENANCE_LABEL`: it is DERIVED from real results via an explicit, editable mapping and
    is *not* a result of this fictional system.  It is used only when a scenario references it in
    ``calibration.imported_baseline`` (path of the mapping YAML, relative to ``config/``).

(b) **Analysis helpers** answering the spec §13 questions from the structural model's
    expectations (FICTIONAL model, SIMULATED numbers): province lean, a municipality versus the
    rest of its province, the urban-coalition index, elasticity rankings and province
    competitiveness.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import config_path, parse_config
from app.core.constitution import DataCategory
from app.core.errors import ConfigError
from app.core.logging import get_logger
from app.geography.frame import GeographyFrame
from app.simulation.structural import StructuralModel

log = get_logger(__name__)

#: Provenance label attached to every imported baseline.
PROVENANCE_LABEL = "DERIVED from real election results via mapping — not a result of this fictional system"


# --------------------------------------------------------------------------- import
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaselineColumns(_Model):
    municipality: str = "municipality_code"
    party: str = "party"
    votes: str = "votes"


class BaselineMapping(_Model):
    """Mapping document (YAML) for a historical-results import."""

    description: str | None = None
    #: Human-readable source of the results (e.g. the electoral council's open data).
    source: str | None = None
    #: CSV with the results, relative to the mapping file (used by :func:`load_imported_baseline`).
    source_csv: str | None = None
    #: True for invented example data (never present it as real results).
    synthetic: bool = False
    columns: BaselineColumns = Field(default_factory=BaselineColumns)
    #: Real party name → {model party code: weight}; weights of a row are normalised.
    parties: dict[str, dict[str, float]]
    #: Votes of real parties missing from the mapping: dropped (default) or an error.
    unmapped: Literal["drop", "error"] = "drop"

    @field_validator("parties")
    @classmethod
    def _weights(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        bad = [f"{real} → {mp}: {w}" for real, ws in v.items() for mp, w in ws.items() if not w >= 0]
        if bad:
            raise ValueError(f"mapping weights must be non-negative numbers: {bad}")
        return v


@dataclass
class ImportedBaseline:
    """Municipality target shares over model parties (DERIVED; see :data:`PROVENANCE_LABEL`)."""

    municipality_codes: list[str]
    parties: list[str]
    shares: np.ndarray  # (n, P) rows sum to 1
    provenance: str = PROVENANCE_LABEL
    data_category: DataCategory = DataCategory.DERIVED
    source: str | None = None
    synthetic: bool = False
    unmapped_vote_share: float = 0.0
    unmapped_parties: list[str] = field(default_factory=list)
    unresolved_municipalities: list[str] = field(default_factory=list)

    def to_calibration_targets(self) -> dict[str, dict[str, float]]:
        """``{municipality_code: {party: share}}`` suitable for ``calibration.municipalities``."""
        return {
            m: {p: round(float(v), 6) for p, v in zip(self.parties, row, strict=True)}
            for m, row in zip(self.municipality_codes, self.shares, strict=True)
        }

    def to_frame(self) -> pd.DataFrame:
        df = pd.DataFrame(
            self.shares,
            index=pd.Index(self.municipality_codes, name="municipality_code"),
            columns=self.parties,
        )
        df.attrs["provenance"] = self.provenance
        df.attrs["data_category"] = self.data_category.value
        return df


def _read_mapping(mapping_yaml: str | Path) -> tuple[BaselineMapping, Path]:
    p = Path(mapping_yaml)
    if not p.is_absolute() and not p.exists():
        p = config_path(p)
    if not p.exists():
        raise ConfigError(f"baseline mapping not found: {mapping_yaml}")
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_config(data, BaselineMapping, source=str(p)), p


def import_historical_results(
    csv_path: str | Path,
    mapping_yaml: str | Path,
    frame: GeographyFrame | None = None,
) -> ImportedBaseline:
    """Convert municipality-level results + a party mapping into municipality target shares.

    Rows whose municipality code is not in ``frame`` (e.g. merged municipalities of an older
    vintage) are reported in ``unresolved_municipalities`` and skipped.
    """
    mapping, _ = _read_mapping(mapping_yaml)
    cp = Path(csv_path)
    if not cp.exists():
        raise ConfigError(f"results CSV not found: {csv_path}")
    df = pd.read_csv(cp, dtype={mapping.columns.municipality: str, mapping.columns.party: str})
    cols = mapping.columns
    missing = [c for c in (cols.municipality, cols.party, cols.votes) if c not in df.columns]
    if missing:
        raise ConfigError(f"{cp}: missing columns {missing}")
    df = df[[cols.municipality, cols.party, cols.votes]].rename(
        columns={cols.municipality: "municipality_code", cols.party: "party", cols.votes: "votes"}
    )
    df["municipality_code"] = df["municipality_code"].str.strip()
    df["party"] = df["party"].str.strip()
    incomplete = df["municipality_code"].isna() | (df["municipality_code"] == "") | df["party"].isna()
    if incomplete.any():
        log.warning(
            "historical import %s: %d row(s) without municipality or party skipped", cp, incomplete.sum()
        )
        df = df[~incomplete].copy()
    votes = pd.to_numeric(df["votes"], errors="coerce")
    bad_votes = votes.isna() | (votes < 0)
    if bad_votes.any():
        log.warning(
            "historical import %s: %d row(s) with missing, non-numeric or negative votes counted as 0",
            cp,
            bad_votes.sum(),
        )
    df["votes"] = votes.where(~bad_votes, 0.0).astype(float)
    unmapped = sorted(set(df["party"]) - set(mapping.parties))
    if unmapped and mapping.unmapped == "error":
        raise ConfigError(f"{cp}: parties without mapping: {unmapped}")
    total_votes = float(df["votes"].sum())
    unmapped_votes = float(df.loc[df["party"].isin(unmapped), "votes"].sum())
    model_parties = sorted({mp for w in mapping.parties.values() for mp in w})
    rows = []
    for real, weights in mapping.parties.items():
        wsum = sum(weights.values())
        if wsum <= 0:
            continue
        sub = df[df["party"] == real]
        for mp, w in weights.items():
            rows.append(
                pd.DataFrame(
                    {
                        "municipality_code": sub["municipality_code"],
                        "party": mp,
                        "votes": sub["votes"] * w / wsum,
                    }
                )
            )
    if not rows:
        raise ConfigError(f"{cp}: no rows matched the party mapping")
    long = pd.concat(rows, ignore_index=True)
    wide = long.pivot_table(
        index="municipality_code", columns="party", values="votes", aggfunc="sum", fill_value=0.0
    )
    wide = wide.reindex(columns=model_parties, fill_value=0.0)
    wide = wide[wide.sum(axis=1) > 0]
    unresolved: list[str] = []
    if frame is not None:
        known = set(frame.muni_codes)
        unresolved = sorted(set(wide.index) - known)
        wide = wide[wide.index.isin(known)]
        if unresolved:
            log.warning(
                "historical import: %d municipality codes not in frame (skipped): %s",
                len(unresolved),
                unresolved[:10],
            )
    shares = wide.to_numpy(dtype=float)
    shares = shares / shares.sum(axis=1, keepdims=True)
    return ImportedBaseline(
        municipality_codes=[str(c) for c in wide.index],
        parties=model_parties,
        shares=shares,
        source=mapping.source,
        synthetic=mapping.synthetic,
        unmapped_vote_share=unmapped_votes / total_votes if total_votes else 0.0,
        unmapped_parties=unmapped,
        unresolved_municipalities=unresolved,
    )


#: Alias used in docs/ARCHITECTURE.md.
import_historical_csv = import_historical_results


def load_imported_baseline(mapping_yaml: str | Path, frame: GeographyFrame | None = None) -> ImportedBaseline:
    """Import using the mapping's own ``source_csv`` (path relative to the mapping file)."""
    mapping, path = _read_mapping(mapping_yaml)
    if not mapping.source_csv:
        raise ConfigError(f"{path}: 'source_csv' is required to load an imported baseline")
    csv = Path(mapping.source_csv)
    if not csv.is_absolute():
        csv = path.parent / csv
    return import_historical_results(csv, path, frame)


# --------------------------------------------------------------------------- analysis (§13)
def _tag(df: pd.DataFrame, what: str) -> pd.DataFrame:
    df.attrs["data_category"] = DataCategory.SIMULATED.value
    df.attrs["description"] = f"{what} — expectation of the FICTIONAL political model (no shocks)"
    return df


def province_lean(
    model: StructuralModel, parties: Sequence[str] | None = None, *, environment: bool = True
) -> pd.DataFrame:
    """Province vs national expected shares (long format).

    Columns: ``province_code, province_name, party, province_share, national_share, lean_pp,
    province_margin_pp, national_margin_pp`` where the margins are the party's lead over the
    strongest other party (negative when trailing).
    """
    f = model.frame
    prov = model.aggregate_expected_shares("province", parties, environment=environment)
    nat = model.aggregate_expected_shares("national", parties, environment=environment).iloc[0]
    rows = []
    for i, pv in enumerate(f.province_codes):
        ps = prov.iloc[i]
        for party in prov.columns:
            others_p = ps.drop(party).max() if len(ps) > 1 else 0.0
            others_n = nat.drop(party).max() if len(nat) > 1 else 0.0
            rows.append(
                {
                    "province_code": pv,
                    "province_name": f.province_names[i],
                    "party": party,
                    "province_share": float(ps[party]),
                    "national_share": float(nat[party]),
                    "lean_pp": 100.0 * float(ps[party] - nat[party]),
                    "province_margin_pp": 100.0 * float(ps[party] - others_p),
                    "national_margin_pp": 100.0 * float(nat[party] - others_n),
                }
            )
    return _tag(pd.DataFrame(rows), "province lean")


def municipality_vs_rest_of_province(
    model: StructuralModel,
    municipality: str,
    parties: Sequence[str] | None = None,
    *,
    environment: bool = True,
) -> pd.DataFrame:
    """A municipality versus the rest of its province (e.g. Tilburg vs rest of Noord-Brabant).

    ``municipality`` is a CBS code or a name.  Columns: ``party, municipality_share, rest_share,
    province_share, difference_pp``.
    """
    from app.simulation.regions import resolve_municipality

    f = model.frame
    m = resolve_municipality(f, municipality)
    if m is None:
        raise KeyError(f"unknown municipality {municipality!r}")
    p = int(f.muni_province[m])
    parties = list(parties) if parties is not None else model.party_codes
    shares, valid = model.expected_valid_weights(None, parties, environment=environment)
    votes = shares * valid[:, None]
    in_m = f.unit_muni == m
    in_p = f.unit_province == p
    mv = votes[in_m].sum(axis=0)
    rv = votes[in_p & ~in_m].sum(axis=0)
    pv = votes[in_p].sum(axis=0)
    df = pd.DataFrame(
        {
            "party": parties,
            "municipality_share": mv / max(mv.sum(), 1e-12),
            "rest_share": rv / max(rv.sum(), 1e-12),
            "province_share": pv / max(pv.sum(), 1e-12),
        }
    )
    df["difference_pp"] = 100.0 * (df["municipality_share"] - df["rest_share"])
    df.attrs["municipality"] = f.muni_names[m]
    df.attrs["province"] = f.province_names[p]
    return _tag(df, f"{f.muni_names[m]} vs rest of {f.province_names[p]}")


def urban_coalition_index(
    model: StructuralModel, parties: Sequence[str] | None = None, *, environment: bool = True
) -> pd.DataFrame:
    """How urban each party's coalition is.

    Columns: ``party, share_urban`` (CBS urbanity classes 1–2), ``share_suburban`` (3),
    ``share_rural`` (4–5), ``urban_vote_fraction`` (share of the party's votes cast in classes
    1–2), ``index`` = ln(share_urban / share_rural) (> 0: urban coalition) and
    ``density_correlation`` (vote-weighted correlation of unit share with log density).
    """
    f = model.frame
    parties = list(parties) if parties is not None else model.party_codes
    shares, valid = model.expected_valid_weights(None, parties, environment=environment)
    votes = shares * valid[:, None]
    cls = f.unit_urbanity_class
    groups = {"urban": np.isin(cls, [1, 2]), "suburban": cls == 3, "rural": np.isin(cls, [4, 5])}
    out = {"party": parties}
    for g, mask in groups.items():
        v = votes[mask].sum(axis=0)
        out[f"share_{g}"] = v / max(v.sum(), 1e-12)
    total = votes.sum(axis=0)
    out["urban_vote_fraction"] = votes[groups["urban"]].sum(axis=0) / np.maximum(total, 1e-12)
    df = pd.DataFrame(out)
    df["index"] = np.log(np.maximum(df["share_urban"], 1e-9) / np.maximum(df["share_rural"], 1e-9))
    ld = np.log1p(f.unit_population / np.maximum(f.unit_land_km2, 1e-6))
    w = valid / max(valid.sum(), 1e-12)
    corr = []
    for j in range(len(parties)):
        x, y = ld, shares[:, j]
        mx, my = (w * x).sum(), (w * y).sum()
        cov = (w * (x - mx) * (y - my)).sum()
        sx, sy = np.sqrt((w * (x - mx) ** 2).sum()), np.sqrt((w * (y - my) ** 2).sum())
        corr.append(float(cov / (sx * sy)) if sx > 0 and sy > 0 else 0.0)
    df["density_correlation"] = corr
    return _tag(df.sort_values("index", ascending=False).reset_index(drop=True), "urban coalition index")


def elasticity(
    model: StructuralModel, level: Literal["unit", "municipality", "province"] = "municipality"
) -> pd.Series:
    """Eligible-weighted mean elasticity per unit / municipality / province (national mean 1)."""
    f = model.frame
    e, w = model.elasticity, model.eligible
    if level == "unit":
        return pd.Series(e, index=f.unit_codes, name="elasticity")
    if level == "municipality":
        num, den, idx = f.to_munis(e * w), f.to_munis(w), f.muni_codes
    elif level == "province":
        num, den, idx = f.to_provinces(e * w), f.to_provinces(w), f.province_codes
    else:
        raise ValueError(f"unknown level {level!r}")
    return pd.Series(num / np.maximum(den, 1e-12), index=idx, name="elasticity")


def elasticity_ranking(model: StructuralModel, top: int | None = None) -> pd.DataFrame:
    """Municipalities ranked from most to least elastic (swingy)."""
    f = model.frame
    el = elasticity(model, "municipality")
    df = pd.DataFrame(
        {
            "municipality_code": f.muni_codes,
            "municipality_name": f.muni_names,
            "province_code": [f.province_codes[int(p)] for p in f.muni_province],
            "elasticity": el.to_numpy(),
            "eligible": f.to_munis(model.eligible),
        }
    ).sort_values(["elasticity", "municipality_code"], ascending=[False, True])
    df = df.reset_index(drop=True)
    return _tag(df.head(top) if top else df, "elasticity ranking")


def province_competitiveness(
    model: StructuralModel, parties: Sequence[str] | None = None, *, environment: bool = True
) -> pd.DataFrame:
    """Expected top-two margin per province (most competitive first).

    ``parties`` restricts the ballot (e.g. the presidential tickets' parties); absent parties'
    support is transferred by affinity.
    """
    f = model.frame
    prov = model.aggregate_expected_shares("province", parties, environment=environment)
    rows = []
    for i, pv in enumerate(f.province_codes):
        s = prov.iloc[i].sort_values(ascending=False)
        rows.append(
            {
                "province_code": pv,
                "province_name": f.province_names[i],
                "leader": s.index[0],
                "leader_share": float(s.iloc[0]),
                "runner_up": s.index[1] if len(s) > 1 else None,
                "runner_up_share": float(s.iloc[1]) if len(s) > 1 else 0.0,
                "margin_pp": 100.0 * float(s.iloc[0] - (s.iloc[1] if len(s) > 1 else 0.0)),
            }
        )
    df = pd.DataFrame(rows).sort_values(["margin_pp", "province_code"]).reset_index(drop=True)
    return _tag(df, "province competitiveness")
