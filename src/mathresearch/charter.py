"""The research charter: the one input the operator provides.

The charter states the global objective, the properties a candidate problem
should have, the domains to start from, and how effort is split between
lanes. Everything else the system decides for itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .pricing import Budget, Price

LANES = ("harvest", "deepen", "open")


class CharterError(ValueError):
    pass


# What each target flag means, as workers read it. The charter may replace any of these texts.
TARGET_FLAGS = ("average_case", "efficient_verification", "wants_trapdoor", "wants_group_action",
                "wants_homomorphism", "finite_field_formulation")

TARGET_TITLES = {
    "average_case": "Average-case hardness",
    "efficient_verification": "Efficient verification",
    "wants_trapdoor": "A trapdoor",
    "wants_group_action": "A group action",
    "wants_homomorphism": "Homomorphic structure",
    "finite_field_formulation": "A finite formulation",
}

TARGET_DESCRIPTIONS = {
    "average_case": (
        "There is an efficient randomized sampler of instances (with a planted solution where the problem needs "
        "one) such that every efficient classical or quantum algorithm solves a sampled instance with only "
        "negligible probability. Keys are chosen at random, so worst-case hardness (NP-hardness) is not evidence. "
        "State the distribution exactly (field size, number of variables, how secrets are drawn), give a "
        "worst-case to average-case reduction if one exists (as for LWE and SIS), and otherwise attack random "
        "instances in the lab across growing sizes."),
    "efficient_verification": (
        "Given an instance and a claimed solution, correctness can be checked in time polynomial in the instance "
        "size (the search problem is in NP). Signatures and identification schemes need this, and the lab scores "
        "attacks by it. For a decision or indistinguishability problem, say how a distinguisher's advantage is "
        "measured instead."),
    "wants_trapdoor": (
        "Key generation outputs a public instance and a secret trapdoor: with the trapdoor, inversion is efficient "
        "for (almost) every image; without it, the public instance should be as hard as a random instance of the "
        "problem. Public-key encryption and hash-and-sign signatures need this. Examples: the hidden central map "
        "of UOV or HFE behind affine changes of variables, a short basis for an LWE/SIS lattice, the Goppa code "
        "hidden in McEliece. The usual failure is a trapdoor that leaves visible structure in the public key (a "
        "rank defect, an invariant subspace, a linear relation): Patarin's break of Matsumoto-Imai, MinRank attacks "
        "on HFE and Kipnis-Shamir on balanced Oil and Vinegar all exploit exactly that, so say why the public key "
        "looks generic and test it."),
    "wants_group_action": (
        "A group G acts on a set X, g.x is efficient to compute, and given x and g.x it is hard to find g "
        "(vectorization); ideally, given x, g.x and h.x it is also hard to find gh.x (parallelization, the analogue "
        "of computational Diffie-Hellman). A commutative action gives non-interactive key exchange, as CSIDH does; "
        "non-abelian actions (tensor isomorphism, code equivalence, isomorphism of polynomials) give signatures "
        "through sigma protocols and Fiat-Shamir. For an abelian group, Kuperberg's quantum algorithm finds g in "
        "subexponential time, so name the group, say whether it is abelian, and size parameters accordingly."),
    "wants_homomorphism": (
        "The hard map respects an algebraic operation, f(x + y) = f(x) * f(y), as homomorphic encryption needs. "
        "Such structure is also a frequent source of attacks."),
    "finite_field_formulation": (
        "Instances are finite objects (over a finite field F_q or another finite structure) with explicit size "
        "parameters, so the lab can sample instances and run exact attacks (Groebner bases, linear algebra, "
        "exhaustive search). A problem over the reals or the complex numbers needs a discretisation first."),
}

DEFAULT_QUANTUM_CHECK = (
    "Shor's algorithm solves the hidden subgroup problem over finite abelian groups, which breaks factoring, "
    "discrete logarithms and anything that hides a period. Explain why the candidate does not reduce to an abelian "
    "hidden subgroup problem; if it reduces to a dihedral one, Kuperberg's subexponential algorithm applies.")


@dataclass
class TargetProperties:
    """What a useful post-quantum hardness assumption should offer.

    One-wayness alone is not the goal: one-way functions already give
    post-quantum signatures. The charter names the extra structure wanted.
    Each flag is rendered with its full description (TARGET_DESCRIPTIONS), which the charter may replace.
    """

    average_case: bool = True
    efficient_verification: bool = True
    wants_trapdoor: bool = True
    wants_group_action: bool = True
    wants_homomorphism: bool = False
    finite_field_formulation: bool = True
    quantum_check: str = DEFAULT_QUANTUM_CHECK
    descriptions: dict[str, str] = field(default_factory=dict)  # operator's replacements, by flag

    @classmethod
    def from_dict(cls, data) -> "TargetProperties":
        """Each flag is true/false, or a mapping {wanted: true/false, description: "..."}."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise CharterError("charter.target must be a mapping")
        unknown = set(data) - set(TARGET_FLAGS) - {"quantum_check"}
        if unknown:
            raise CharterError(f"unknown target properties {sorted(unknown)}; allowed: {list(TARGET_FLAGS)} "
                               f"and quantum_check")
        kw: dict = {}
        descriptions: dict[str, str] = {}
        for name in TARGET_FLAGS:
            if name not in data:
                continue
            value = data[name]
            if isinstance(value, dict):
                extra = set(value) - {"wanted", "description"}
                if extra:
                    raise CharterError(f"charter.target.{name}: unknown keys {sorted(extra)}; use wanted, description")
                kw[name] = bool(value.get("wanted", True))
                if value.get("description"):
                    descriptions[name] = " ".join(str(value["description"]).split())
            elif isinstance(value, bool):
                kw[name] = value
            else:
                raise CharterError(f"charter.target.{name} must be true, false or a mapping with wanted/description")
        if data.get("quantum_check"):
            kw["quantum_check"] = " ".join(str(data["quantum_check"]).split())
        return cls(**kw, descriptions=descriptions)

    def description(self, name: str) -> str:
        return self.descriptions.get(name) or TARGET_DESCRIPTIONS[name]

    def to_prompt_lines(self) -> list[str]:
        labels = {"average_case": "required", "efficient_verification": "required",
                  "finite_field_formulation": "preferred"}
        wanted, other = [], []
        for name in TARGET_FLAGS:
            on = getattr(self, name)
            level = labels.get(name, "wanted") if on else "not required"
            line = f"- {TARGET_TITLES[name]} ({level})"
            (wanted if on else other).append(line + f": {self.description(name)}" if on else line + ".")
        return wanted + other + [f"- Quantum check (always): {self.quantum_check}"]


@dataclass
class ReadingPolicy:
    """How references steer the agenda."""
    seed_operator_references: bool = True   # every reference the operator ingests gets a seed-read question
    max_prior_share: float = 0.5            # when ideas from the model's prior alone exceed this share of the open
                                            # ideas (vs. ones sparked by references or experiments), reflection
                                            # lowers their priority


@dataclass
class ReviewPolicy:
    """Timely reviews of candidates through lessons from experts' published work."""
    enabled: bool = True
    max_per_reflection: int = 2        # review questions the reflection step may open at once
    from_status: str = "well_defined"  # first ladder status at which a candidate is reviewed


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
    budget: Budget = field(default_factory=Budget)
    pricing: dict[str, Price] = field(default_factory=dict)   # $ per million tokens, per model id
    reading: ReadingPolicy = field(default_factory=ReadingPolicy)
    reviews: ReviewPolicy = field(default_factory=ReviewPolicy)

    @classmethod
    def from_dict(cls, data: dict) -> "Charter":
        if not isinstance(data, dict):
            raise CharterError("charter must be a mapping")
        for key in ("objective", "domains"):
            if key not in data:
                raise CharterError(f"charter is missing required key: {key}")
        if not isinstance(data["domains"], list) or not data["domains"]:
            raise CharterError("charter.domains must be a non-empty list")
        target = TargetProperties.from_dict(data.get("target"))
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
            budget=_budget(data.get("budget")),
            pricing=_pricing(data.get("pricing")),
            reading=_reading(data.get("reading")),
            reviews=_reviews(data.get("reviews")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Charter":
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return cls.from_dict(data)

    def to_prompt(self) -> str:
        """Render the charter as the stable prefix of every worker prompt."""
        lines = [
            "# Research charter",
            "",
            self.objective,
            "",
            "## Properties a candidate problem must offer",
            *self.target.to_prompt_lines(),
            "",
            "## Starting domains",
        ]
        lines += [f"- {d}" for d in self.domains]
        if self.excluded_domains:
            lines += ["", "## Excluded as initial directions"] + [f"- {d}" for d in self.excluded_domains]
        if self.notes:
            lines += ["", "## Notes", self.notes]
        return "\n".join(lines)


def _budget(data) -> Budget:
    if data is not None and not isinstance(data, dict):
        raise CharterError("charter.budget must be a mapping")
    try:
        return Budget.from_dict(data)
    except ValueError as exc:
        raise CharterError(f"charter.budget: {exc}") from exc


def _reading(data) -> ReadingPolicy:
    if not data:
        return ReadingPolicy()
    if not isinstance(data, dict):
        raise CharterError("charter.reading must be a mapping")
    unknown = set(data) - {"seed_operator_references", "max_prior_share"}
    if unknown:
        raise CharterError(f"charter.reading: unknown fields {sorted(unknown)}")
    share = float(data.get("max_prior_share", 0.5))
    if not 0.0 <= share <= 1.0:
        raise CharterError("charter.reading.max_prior_share must be between 0 and 1")
    return ReadingPolicy(seed_operator_references=bool(data.get("seed_operator_references", True)),
                         max_prior_share=share)


def _reviews(data) -> ReviewPolicy:
    if not data:
        return ReviewPolicy()
    if not isinstance(data, dict):
        raise CharterError("charter.reviews must be a mapping")
    unknown = set(data) - {"enabled", "max_per_reflection", "from_status"}
    if unknown:
        raise CharterError(f"charter.reviews: unknown fields {sorted(unknown)}")
    statuses = ("idea", "well_defined", "not_trivially_broken", "not_known", "useful_structure")
    from_status = str(data.get("from_status", "well_defined"))
    if from_status not in statuses:
        raise CharterError(f"charter.reviews.from_status must be one of {list(statuses)}")
    return ReviewPolicy(enabled=bool(data.get("enabled", True)),
                        max_per_reflection=max(0, int(data.get("max_per_reflection", 2))), from_status=from_status)


def _pricing(data) -> dict[str, Price]:
    if not data:
        return {}
    if not isinstance(data, dict):
        raise CharterError("charter.pricing must map model ids to prices")
    out = {}
    for model, price in data.items():
        if not isinstance(price, dict):
            raise CharterError(f"charter.pricing.{model} must be a mapping")
        try:
            out[str(model)] = Price.from_dict(price)
        except ValueError as exc:
            raise CharterError(f"charter.pricing.{model}: {exc}") from exc
    return out


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

# What a candidate problem should offer. Workers never see these short names:
# each flag that is true becomes a full definition in every worker prompt
# (what it means precisely, why cryptography needs it, examples, the usual way
# it fails). Run `mathresearch charter show` to read exactly what they see.
#   average_case: hard on random instances (crypto samples keys at random)
#   efficient_verification: a proposed solution can be checked quickly
#   wants_trapdoor: a secret makes inversion easy (public-key encryption, signatures)
#   wants_group_action: a group acting on a set (key exchange, as in isogenies)
#   wants_homomorphism: structure preserved under operations (not needed at first)
#   finite_field_formulation: instances live over finite fields or finite structures
# To use your own definition, write a flag as a mapping, for example:
#   wants_trapdoor:
#     wanted: true
#     description: >
#       Your own wording of what the trapdoor must achieve.
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

# Workers that run at the same time (each one a model conversation plus its tools).
max_concurrent_runs: 3
reflect_every_runs: 20

# Spending caps, enforced by the loop. A run that reaches its cap is asked to
# write back immediately; no new run starts when it could break the daily or
# total cap. Set a value to null to disable that cap.
budget:
  max_usd_per_run: 2.0
  max_tokens_per_run: 600000
  max_usd_per_day: 20.0
  max_usd_total: null

# Prices in US dollars per million tokens, by model id. Current Claude models
# and OpenAI's gpt-6-astra, gpt-6.1-sol, gpt-6.1-terra and gpt-6-luna are built
# in. Add any other model here (from its provider's pricing page), or dollar
# caps cannot be enforced for it; an entry here overrides the built-in price:
# pricing:
#   your-openai-model-id: {input: 0.0, cached_input: 0.0, output: 0.0}
pricing: {}
# References you ingest (mathresearch ingest fetch / file) are seeds: each gets
# a seed-read question that must open at least one question sparked by it.
# Reflection lowers the priority of questions that come from the model's prior
# alone once they exceed max_prior_share of the open questions that came from
# ideas (the rest were sparked by your references or by experiments).
reading:
  seed_operator_references: true
  max_prior_share: 0.5
# Lessons from the published work of Regev, Peikert, Ducas, Lyubashevsky and
# Bai (catalog/expert_lessons.yaml) appear in every worker prompt. When a
# candidate reaches a new ladder status (from from_status on), the reflection
# step opens a review of it through one expert's lessons, rotating experts.
reviews:
  enabled: true
  max_per_reflection: 2
  from_status: well_defined
"""
