"""Resolve the requirement tree, database symbols, and requirement obligations."""

import re
from pathlib import Path

from infrastructure.utils.files import read_artifact

from infrastructure.stages.database.obligations.records import normalize_obligations, check_symbols
from infrastructure.stages.requirements.tree import RequirementTree

SQL_TABLE_REFERENCE = re.compile(
    r"(?:insert\s+into|delete\s+from)\s+[`\"'\[<]?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def unknown_sql_tables(path: Path, names) -> list[str]:
    """Physical tables a suite writes to that the generated schema does not define."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return sorted({name for name in SQL_TABLE_REFERENCE.findall(text) if name not in names})


def load_context(arc_dir: Path):
    """Load the requirement tree, schema, and normalized obligations."""
    tree = RequirementTree(read_artifact(arc_dir, "requirements.json"))
    schema = read_artifact(arc_dir, "db_schema.json")
    obligations = normalize_obligations(read_artifact(arc_dir, "db_obligations.json"), tree)
    check_symbols([*obligations["requirements"].values(), *obligations["steps"].values()], schema)
    return tree, schema, obligations


def requirement_context(tree, rid: str) -> dict:
    """Return one requirement with its own structured steps and their concatenated teaching/model context."""
    return {**tree.requirements[rid], "steps": [tree.steps[sid] for sid in tree.own_steps[rid]]}


def api_requirement_uses(tree, allocations: dict, aid: str) -> list[dict]:
    """Supply every API owner once, with structured steps as the sole scenario representation."""
    return [{**tree.requirements[rid], "steps": [tree.steps[sid] for sid in tree.own_steps[rid]]}
        for rid, ids in allocations.items() if aid in ids]


def supplied_sources(output: Path, api: dict, schema: dict) -> dict:
    """The caller selects context paths; the agent never explores the codebase."""
    entities = {symbol.split(".")[0] for mode in ("read", "write") for symbol in api[mode]}
    pending = list(entities)
    while pending:
        name = pending.pop()
        for field in schema["entities"][name]["fields"].values():
            if field["references"]:
                target = field["references"].split(".")[0]
                if target not in entities:
                    entities.add(target)
                    pending.append(target)
    paths = ["backend/src/api/types.ts", "backend/src/db/client.ts", "backend/tests/support/context.ts"]
    paths.extend(f"backend/src/db/tables/{name}.sql" for name in sorted(entities))
    return {relative: (output / relative).read_text(encoding="utf-8-sig") for relative in paths}


def related_schema(schema, records):
    """Project explicitly used entities; keep their complete definitions for SQL fixtures."""
    names = {field.split(".")[0] for record in records for mode in ("read", "write")
        for field in record.get(mode, [])}
    return {name: value for name, value in schema["entities"].items() if name in names}
