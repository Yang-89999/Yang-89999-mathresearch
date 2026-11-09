"""The research charter: the one input the operator provides.

The charter states the global objective, the properties a candidate problem
should have, the domains to start from, and how effort is split between
lanes. Everything else the system decides for itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

LANES = ("harvest", "deepen", "open")


class CharterError(ValueError):
    pass


@dataclass
class TargetProperties:
    """What a useful post-quantum hardness assumption should offer.

    One-wayness alone is not the goal: one-way functions already give
    post-quantum signatures. The charter names the extra structure wanted.
    """

    average_case: bool = True
    efficient_verification: bool = True
    wants_trapdoor: bool = True
    wants_group_action: bool = True
    wants_homomorphism: bool = False
    finite_field_formulation: bool = True
    quantum_check: str = "reject problems whose instances hide an abelian group structure (period, hidden subgroup)"


@dataclass
class Charter:
    objective: str
    domains: list[str]
    target: TargetProperties = field(default_factory=TargetProperties)
    excluded_domains: list[str] = field(default_factory=list)
    lanes: dict[str, float] = field(default_factory=lambda: {"harvest": 0.6, "deepen": 0.25, "open": 0.15})
    max_concurrent_runs: int = 3
    reflect_every_runs: int = 20
    notes: str = ""
    hidden_attacks: list[str] = field(default_factory=list)   # lab attacks withheld from workers (retrodiction)
    catalog_max_year: int | None = None                       # ignore catalog entries published after this year

    @classmethod
    def from_dict(cls, data: dict) -> "Charter":
        if not isinstance(data, dict):
            raise CharterError("charter must be a mapping")
        for key in ("objective", "domains"):
            if key not in data:
                raise CharterError(f"charter is missing required key: {key}")
        if not isinstance(data["domains"], list) or not data["domains"]:
            raise CharterError("charter.domains must be a non-empty list")
        target = TargetProperties(**(data.get("target") or {}))
        lanes = dict(data.get("lanes") or {"harvest": 0.6, "deepen": 0.25, "open": 0.15})
        unknown = set(lanes) - set(LANES)
        if unknown:
            raise CharterError(f"unknown lanes: {sorted(unknown)}; allowed: {list(LANES)}")
        total = sum(lanes.values())
        if total <= 0:
            raise CharterError("lane shares must sum to a positive number")
        lanes = {k: v / total for k, v in lanes.items()}
        for lane in LANES:
            lanes.setdefault(lane, 0.0)
        return cls(
            objective=str(data["objective"]).strip(),
            domains=[str(d) for d in data["domains"]],
            target=target,
            excluded_domains=[str(d) for d in data.get("excluded_domains", [])],
            lanes=lanes,
            max_concurrent_runs=int(data.get("max_concurrent_runs", 3)),
            reflect_every_runs=int(data.get("reflect_every_runs", 20)),
            notes=str(data.get("notes", "")),
            hidden_attacks=[str(a) for a in data.get("hidden_attacks", []) or []],
            catalog_max_year=int(data["catalog_max_year"]) if data.get("catalog_max_year") else None,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Charter":
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return cls.from_dict(data)

    def to_prompt(self) -> str:
        """Render the charter as the stable prefix of every worker prompt."""
        t = self.target
        wants = [
            name
            for flag, name in (
                (t.wants_trapdoor, "a trapdoor (for encryption)"),
                (t.wants_group_action, "a group action or similar structure (for key exchange)"),
                (t.wants_homomorphism, "homomorphic structure"),
            )
            if flag
        ]
        lines = [
            "# Research charter",
            "",
            self.objective,
            "",
            "## Properties a candidate problem must offer",
            f"- Hardness on average over an efficiently samplable distribution: {'required' if t.average_case else 'not required'}",
            f"- Efficient verification of a solution: {'required' if t.efficient_verification else 'not required'}",
            f"- Extra structure wanted: {', '.join(wants) if wants else 'none specified'}",
            f"- Finite-field or finite-structure formulation: {'preferred' if t.finite_field_formulation else 'not required'}",
            f"- Quantum check: {t.quantum_check}",
            "",
            "## Starting domains",
        ]
        lines += [f"- {d}" for d in self.domains]
        if self.excluded_domains:
            lines += ["", "## Excluded as initial directions"] + [f"- {d}" for d in self.excluded_domains]
        if self.notes:
            lines += ["", "## Notes", self.notes]
        return "\n".join(lines)


DEFAULT_CHARTER = """\
# MathResearch charter. The operator writes this once; the system takes it
# from here. Steer by editing this file, not by supplying research questions.

objective: >
  Discover mathematical computational problems that admit efficient forward
  computation but resist inversion, reconstruction or classification, and
  develop the promising ones into rigorous candidate hardness assumptions for
  post-quantum cryptography. Learn the mathematics first; attack every
  candidate before believing in it; preserve unexpected discoveries even when
  their cryptographic use is unclear.

domains:
  - computational algebraic geometry (polynomial maps, varieties, Groebner bases, elimination, singularities, curves)
  - computational number theory (finite fields, arithmetic geometry, Diophantine problems)
  - commutative algebra (ideals, syzygies, free resolutions, Hilbert functions, local rings)
  - computational topology and geometry (homology, fundamental groups, reconstruction, invariants)
  - computational representation theory (group and quiver representations, orbit and classification problems)
  - category theory and higher algebra (categorical invariants, moduli, derived structures)
  - combinatorics and discrete mathematics (graphs, codes, discrete optimization, counting)

excluded_domains:
  - dynamical systems (not an initial cryptographic research direction)

target:
  average_case: true
  efficient_verification: true
  wants_trapdoor: true
  wants_group_action: true
  wants_homomorphism: false
  finite_field_formulation: true

# Share of worker runs per lane. harvest: many cheap runs that read and
# propose candidates. deepen: few long runs on candidates that passed the
# computational filters. open: exploration not tied to the objective.
lanes:
  harvest: 0.6
  deepen: 0.25
  open: 0.15

max_concurrent_runs: 3
reflect_every_runs: 20
"""
