"""The attack lab: problem families as code, attacks as code, cost curves as evidence.

A candidate problem advances on the promotion ladder only through this
package: step 2 (well defined) needs a generator, forward map and verifier
that run; step 3 (not trivially broken) needs the standard attacks to fail,
or to show super-polynomial growth, on a size sweep.
"""

from .families import FAMILIES, Instance, ProblemFamily, get_family
from .attacks import ATTACKS, Attack, AttackResult, get_attack
from .sweep import CostCurve, sweep
from . import isomorphism  # noqa: E402,F401  (registers tensor_iso, atfe and their attacks)

__all__ = ["FAMILIES", "Instance", "ProblemFamily", "get_family", "ATTACKS", "Attack", "AttackResult",
           "get_attack", "CostCurve", "sweep"]
