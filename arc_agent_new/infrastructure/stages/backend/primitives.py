"""Requirement and scenario coverage over declared API database effects."""

from copy import deepcopy
from infrastructure.stages.database.obligations.rules import difference, union


class ObligationCoverageError(ValueError):
    def __init__(self, rid, required, combined, gaps):
        self.feedback = {"requirement_id": rid, "required_obligations": required,
            "api_obligation_union": combined, **gaps}
        super().__init__(f"Obligation Propagation failed for {rid}: {self.feedback}")


def coverage(required, apis, permitted=None):
    combined = union(apis)
    # Required reads are minimum capabilities, not a prohibition on supporting
    # lookups, joins or reusable response projections. Writes retain their boundary, but
    # that boundary is the union of every requirement that shares these APIs: a second
    # requirement legitimately extends one endpoint, and measuring excess against only the
    # current requirement used to make that extension impossible and force a fork.
    allowed = required if permitted is None else permitted
    return {"missing": difference(required, combined),
        "excess": {"read": [], "write": sorted(set(combined["write"]) - set(allowed["write"]))}}


def shared_scope(design, rid):
    """Return the requirement ids whose APIs this requirement also lists, plus itself.

    Sharing is decided by the requirement-to-API mapping, not by which requirements have
    already been processed. A gate that hands over only the current requirement's entry hides
    its co-owners, so the allowance silently degrades to "own obligations only" and rejects
    the very extension it exists to permit.
    """
    owned = set(design["requirements"].get(rid, []))
    ids = {rid} | {other for other, aids in design["requirements"].items()
        if other != rid and owned & set(aids)}
    return sorted(ids)


def shared_allowance(design, obligations, rid):
    """Return the writes any requirement sharing this requirement's APIs also requires."""
    owners = [other for other in shared_scope(design, rid) if other in obligations["requirements"]]
    return union(obligations["requirements"][other] for other in owners) if owners else None


def validate_design(design, obligations, complete=True, tree=None, only=None):
    """Check required reads, exact possible writes, and each scenario's API coverage.

    `only` restricts the requirements reported on while `design` still supplies every owner,
    which is what an upstream pre-check needs: it audits one requirement, but must see the
    co-owners that justify a shared endpoint's wider write set.
    """
    expected = obligations["requirements"] if complete else design["requirements"]
    for rid in (expected if only is None else only):
        if tree is not None and tree.children.get(rid):
            continue
        apis = [design["apis"][aid] for aid in design["requirements"][rid]]
        required = obligations["requirements"][rid]
        gaps = coverage(required, apis, shared_allowance(design, obligations, rid))
        if any(fields for gap in gaps.values() for fields in gap.values()):
            raise ObligationCoverageError(rid, required, union(apis), gaps)
        if tree is None:
            continue
        combined = union(apis)
        for sid in tree.own_steps[rid]:
            step_required = obligations["steps"][sid]
            missing = difference(step_required, combined)
            if any(missing.values()):
                raise ObligationCoverageError(sid, step_required, combined, {"missing": missing})


def hyphenated_variant(first, second):
    """Report whether one endpoint path is the other plus a hyphenated suffix."""
    short, long = sorted((first, second), key=len)
    return len(short) < len(long) and long.startswith(short + "-")


def check_endpoint_uniqueness(apis):
    """Reject a second endpoint that shadows an existing one under a hyphenated path.

    Cross-requirement compilation used to emit /api/register and /api/register-traveler for
    one business operation. Both handlers then existed, each requirement's tests depended on
    whichever path the UI happened to call, and several repair rounds were spent only
    reconciling the pair. One business operation keeps one endpoint, which may serve several
    requirements: pass 06 registers the same API id for each of them and the coverage check
    accepts the union of their obligations.

    Deliberately narrow: path shape alone decides. Guessing from write sets would also catch
    legitimate pairs such as /api/register and /api/sign-out, which both write Session.id, or
    /api/register and /api/login, which both create a session.
    """
    items = sorted(apis.items())
    for index, (aid, api) in enumerate(items):
        for bid, other in items[index + 1:]:
            left, right = api["signature"], other["signature"]
            if left["method"] != right["method"]:
                continue
            if left["path"] == right["path"]:
                raise ValueError(f"{aid} and {bid} register the same {left['method']} {left['path']}; "
                    "keep one API and reuse it instead of defining it twice")
            if not hyphenated_variant(left["path"], right["path"]):
                continue
            if not set(api["write"]) & set(other["write"]):
                continue
            raise ValueError(
                f"{aid} ({left['method']} {left['path']}) duplicates {bid} "
                f"({right['method']} {right['path']}) under a hyphen-extended path and writes "
                "overlapping data. One business operation owns one endpoint: keep the existing path, "
                "extend that API's contract with the fields the later requirement needs, and list the "
                "same API id in requirement_apis for BOTH requirements — the coverage check accepts "
                "the union of the obligations of every requirement that shares an API. If the "
                "operation is genuinely different, choose a path that is not a hyphen-extended "
                "variant of an existing one.")


def apply_decomposition(design, response, rid, obligations, tree=None):
    result = deepcopy(design)
    result["apis"].update(deepcopy(response["apis"]))
    result["requirements"][rid] = response["requirement_apis"]
    validate_design(result, obligations, complete=False, tree=tree)
    check_endpoint_uniqueness(result["apis"])
    return result
