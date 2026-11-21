"""Discrete inference analysis for published survey aggregates.

Public survey results are aggregates: a set of anonymous response tokens, the
number of responses, the score total and/or a rounded mean.  Even when each
query only exposes an aggregate, several legal queries can pin an individual
score down.  This module treats every previously published aggregate as a
constraint over scores in the discrete range 1..5 and answers one question:
can any token's score still take at least two values?

The check is deliberately exact.  For each token it looks for concrete
satisfying assignments having two different scores; a token is reported as
inferred only when an exhaustive search *proves* no second value exists.  Two
guards keep that proof honest:

* a randomized witness search runs first, so large but harmless groups are
  cheap to clear (a real assignment is proof that a value is still possible);
* the exhaustive search is bounded.  If the bound runs out,
  :class:`InferenceUnverifiable` is raised and callers fail closed.

Means are compared at the precision that was actually published, never at the
internal exact value.
"""
import random
from decimal import Decimal, ROUND_HALF_UP

_SCORES = (1, 2, 3, 4, 5)


class InferenceUnverifiable(Exception):
    """The bounded exact search could not certify an answer."""


def round_mean(total, responses, precision):
    """Round a score mean half-up to the precision actually published."""
    if responses <= 0:
        raise ValueError("responses must be positive")
    if not 0 <= precision <= 4:
        raise ValueError("precision must be between 0 and 4")
    quantum = Decimal(1).scaleb(-precision)
    value = Decimal(total) / Decimal(responses)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _allowable_range(constraint):
    tokens = constraint["tokens"]
    n = len(tokens)
    if "min_total" in constraint or "max_total" in constraint:
        return (constraint.get("min_total", n), constraint.get("max_total", 5 * n))
    total = constraint.get("total")
    if total is not None:
        return total, total
    if constraint.get("mean") is None:
        return n, 5 * n
    mean = Decimal(str(constraint["mean"]))
    precision = constraint.get("precision", 2)
    allowed = [t for t in range(n, 5 * n + 1)
               if round_mean(t, n, precision) == mean]
    if not allowed:
        raise ValueError("mean constraint has no feasible total")
    return allowed[0], allowed[-1]


def _compile(tokens, constraints):
    index = {token: i for i, token in enumerate(tokens)}
    compiled = []
    for constraint in constraints:
        idxs = tuple(sorted(index[token] for token in constraint["tokens"]))
        lo, hi = _allowable_range(constraint)
        compiled.append((idxs, lo, hi, constraint))
    return compiled


def _component(constraints, token):
    """Constraints transitively connected to ``token`` by shared membership."""
    by_token = {}
    for constraint in constraints:
        for member in constraint["tokens"]:
            by_token.setdefault(member, []).append(constraint)
    seen = set()
    stack = [token]
    chosen = []
    chosen_ids = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for constraint in by_token.get(current, []):
            if id(constraint) in chosen_ids:
                continue
            chosen_ids.add(id(constraint))
            chosen.append(constraint)
            stack.extend(constraint["tokens"])
    return chosen


class _Problem:
    """Constraint solver for one connected component."""

    def __init__(self, tokens, constraints):
        self.tokens = tokens
        self.compiled = _compile(tokens, constraints)
        self.n = len(tokens)
        self.token_cons = [[] for _ in range(self.n)]
        for j, (idxs, _, _, _) in enumerate(self.compiled):
            for i in idxs:
                self.token_cons[i].append(j)
        self.order = sorted(range(self.n), key=lambda i: -len(self.token_cons[i]))

    def _bounds_ok(self, j, sums, remaining):
        _, lo, hi, _ = self.compiled[j]
        return sums[j] + remaining[j] <= hi and sums[j] + remaining[j] * 5 >= lo

    def _value_range(self, i, sums, remaining):
        """Values for unassigned ``i`` that keep every constraint feasible."""
        low, high = 1, 5
        for j in self.token_cons[i]:
            _, lo, hi, _ = self.compiled[j]
            rest = remaining[j] - 1
            low = max(low, lo - sums[j] - 5 * rest)
            high = min(high, hi - sums[j] - rest)
        return low, high

    def _exact(self, sums):
        for j, (_, lo, hi, constraint) in enumerate(self.compiled):
            if not lo <= sums[j] <= hi:
                return False
            if "min_total" in constraint or "max_total" in constraint:
                continue
            total = constraint.get("total")
            if total is not None and sums[j] != total:
                return False
            if total is None and constraint.get("mean") is not None:
                if round_mean(sums[j], len(constraint["tokens"]),
                              constraint.get("precision", 2)) != Decimal(
                                  str(constraint["mean"])):
                    return False
        return True

    def solve(self, fixed, node_limit=1_500_000, randomize=False):
        """Return True iff a satisfying 1..5 assignment exists.

        Raises :class:`InferenceUnverifiable` when the node budget is spent
        before either a witness or an exhaustive refutation is found.
        """
        assigned = [None] * self.n
        sums = [0] * len(self.compiled)
        remaining = [len(c[0]) for c in self.compiled]
        for i, value in fixed.items():
            assigned[i] = value
            for j in self.token_cons[i]:
                sums[j] += value
                remaining[j] -= 1
        for j in range(len(self.compiled)):
            if not self._bounds_ok(j, sums, remaining):
                return False
        budget = [node_limit]
        order = [i for i in self.order if assigned[i] is None]
        rng = random.Random(0x5EED)
        values = []
        for _ in range(6):
            trial = list(_SCORES)
            rng.shuffle(trial)
            values.extend(v for v in trial if v not in values)
        attempt = [values]

        def walk(pos):
            budget[0] -= 1
            if budget[0] < 0:
                raise InferenceUnverifiable("inference search budget exhausted")
            if pos == len(order):
                return self._exact(sums)
            i = order[pos]
            low, high = self._value_range(i, sums, remaining)
            if low > high:
                return False
            for value in [v for v in attempt[0] if low <= v <= high]:
                assigned[i] = value
                ok = True
                for j in self.token_cons[i]:
                    sums[j] += value
                    remaining[j] -= 1
                    if not self._bounds_ok(j, sums, remaining):
                        ok = False
                if ok and walk(pos + 1):
                    return True
                for j in self.token_cons[i]:
                    sums[j] -= value
                    remaining[j] += 1
                assigned[i] = None
            return False

        return walk(0)

    def possible_values(self, index, witness_limit=20_000, proof_limit=1_500_000):
        """Values the token can take in some satisfying assignment.

        A bounded randomized search seeks a witness first; only when it runs
        out is the exhaustive search used, and only an exhaustive refutation
        is reported as impossible.
        """
        found = []
        for value in _SCORES:
            if len(found) >= 2:
                break
            if self.solve({index: value}, witness_limit):
                found.append(value)
                continue
            if self.solve({index: value}, proof_limit):
                found.append(value)
        return found

    def forced_values(self, index):
        values = self.possible_values(index)
        if not values:
            raise ValueError("published aggregates are inconsistent with current answers")
        return values


def analyze(requested_tokens, constraints, node_limit=1_500_000):
    """Compute which requested token scores are uniquely determined.

    Returns ``{"safe": bool, "forced": [...], "domains": {token: [scores]}}``.
    Only the connected component of constraints touching a token is searched;
    tokens absent from every constraint keep all five possible scores.
    """
    requested = list(dict.fromkeys(requested_tokens))
    active = [c for c in constraints if c.get("tokens")]
    domains = {}
    for token in requested:
        component = _component(active, token)
        if not component:
            domains[token] = list(_SCORES)
            continue
        tokens = sorted({member for c in component for member in c["tokens"]})
        index = tokens.index(token)
        problem = _Problem(tokens, component)
        domains[token] = problem.forced_values(index)
    forced = [token for token in requested if len(domains[token]) == 1]
    return {"safe": not forced, "forced": forced, "domains": domains}


def minimal_core(requested_tokens, constraints, targets=None):
    """Shrink the constraint set while the target tokens stay determined.

    ``targets`` defaults to every requested token.  Published constraints that
    can be dropped without letting a target token vary again are removed, so
    the remaining summary is a reviewable explanation of the inference.
    """
    targets = set(requested_tokens if targets is None else targets)
    core = list(constraints)
    index = 0
    while index < len(core):
        constraint = core[index]
        if constraint.get("kind") == "draft":
            index += 1
            continue
        trial = core[:index] + core[index + 1:]
        if targets and targets <= set(analyze(sorted(targets), trial)["forced"]):
            core = trial
        else:
            index += 1
    forced = analyze(sorted(targets), core)["forced"] if targets else []
    return core, forced


def summary(constraints):
    """Reviewable constraint summary; contains group tokens (internal only)."""
    out = []
    for constraint in constraints:
        out.append({
            "version": constraint.get("version"),
            "kind": constraint.get("kind"),
            "tokens": list(constraint.get("tokens", [])),
            "group_size": len(constraint.get("tokens", [])),
            "total": constraint.get("total"),
            "mean": constraint.get("mean"),
            "precision": constraint.get("precision"),
        })
    return out
