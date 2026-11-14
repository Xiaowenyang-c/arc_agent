"""Bounded file editing with failure attribution and real acceptance checks."""

import json
import re

from infrastructure.shared.agent.discovery import DISCOVERY_RULES, DiscoverySession
from infrastructure.shared.agent.conventions import task_conventions

from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.agent.tools.protocol import FILE_TOOL_RULES
from infrastructure.shared.agent.proposals import RESPONSE_ERRORS, response_feedback
from infrastructure.shared.project.git import checkpoint
from infrastructure.utils.files import read_source


class AgentExhausted(ValueError):
    """A recoverable task failure with the last real repair feedback."""

    def __init__(self, label, turns, feedback):
        super().__init__(f"Agent exhausted {turns} rounds for {label}")
        self.turns = turns
        self.feedback = feedback


_VOLATILE_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"']+|/(?:[^\s\"'/]+/){2}[^\s\"']*")
_VOLATILE_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?\b")
_VOLATILE_DURATION = re.compile(r"\b\d+(\.\d+)?\s?(ms|sec|seconds|s)\b")
_VOLATILE_NUMBER = re.compile(r"elapsed\D{0,12}\d+(\.\d+)?")


def feedback_signature(feedback) -> str:
    """Equivalence key for repair feedback, ignoring ordering and volatile details.

    The same failing check re-reported with a different timestamp, duration,
    temp path or list ordering is the same observed failure; only a genuinely
    different message means the repair attempts are changing something.
    """
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return sorted((normalize(item) for item in value), key=repr)
        if isinstance(value, str):
            for pattern in (_VOLATILE_PATH, _VOLATILE_TIME, _VOLATILE_DURATION, _VOLATILE_NUMBER):
                value = pattern.sub("<volatile>", value)
            return value
        return value
    return json.dumps(normalize(feedback), sort_keys=True, ensure_ascii=False, default=str)


def run_file_agent(model, instructions: str, task: dict, files: FileTools, gate,
        max_turns: int, console, label: str, feedback=None, feedback_filter=None, context_index=None,
        allow_discovery=True, stop_on_repeat: int = 2) -> dict:
    """Refresh files and paths; optionally select failure-related files before repair.

    The stage-owned feedback filter may add writable sources/read-only references
    and condense check reports. It cannot change the acceptance gate or its result.
    stop_on_repeat > 0 ends the loop after that many consecutive rejections carrying
    identical feedback: the same observed failure means the loop is not converging. It
    defaults to 2 because feedback signatures are compared semantically (see
    feedback_signature), so a second identical rejection already proves the previous
    round changed nothing; a third would only spend tokens on the same failure.
    """
    if max_turns < 1:
        raise ValueError("--max-turns must be positive")
    index = context_index if allow_discovery else None
    allow_discovery = index is not None
    discovery, context_result = DiscoverySession(index), None
    read_files = {}
    read_paths = set()
    feedback = [] if feedback is None else [feedback_filter(feedback) if feedback_filter else feedback]
    check_feedback = list(feedback)
    last_signature, repeats = None, 0
    for turn in range(1, max_turns + 1):
        # Keep only the latest round's excerpts and demote older reads to bare paths: a
        # task that reads widely would otherwise re-send the same evidence every round.
        for path in [path for path, entry in read_files.items() if entry["round"] < turn - 1]:
            read_paths.add(path)
            read_files.pop(path)
        console.show(f"{label} | Edit round {turn}/{max_turns}",
            "Generate file edits, then run the stage's compilation and test acceptance checks.")
        response, applied, checking = None, False, False
        try:
            def request_model():
                # Gates may regenerate wiring outside edit_file; discard stale excerpts too.
                for path, entry in list(read_files.items()):
                    target = files.path(path)
                    if not target.is_file():
                        read_files.pop(path)
                        continue
                    lines = read_source(target).splitlines(keepends=True)
                    for excerpt in entry["excerpts"]:
                        content = "".join(lines[excerpt["start_line"] - 1:excerpt["end_line"]])
                        if len(lines) != excerpt["total_lines"] or content != excerpt["content"]:
                            read_files.pop(path)
                            break
                # Order keys stable-first, volatile-last: repair rounds repeat the same task
                # with the same tools and mostly the same files, so a long unchanged prefix
                # can hit provider prefix caching. Feedback changes every rejected round and
                # must therefore stay at the end, after everything the model can cache.
                target_path = getattr(getattr(files, "target", None), "path", None)
                files_state = files.state(incremental=turn > 1, target=target_path)
                request = {"task": files.task_state(task),
                    "context_kinds": list(index.records()) if index is not None else [],
                    "tools": files.schemas(),
                    "files": files_state}
                # A full body in files already covers the same lines; sending the excerpt
                # again duplicates evidence the model can read in place.
                excerpts = {path: entry["excerpts"] for path, entry in read_files.items()
                    if "content" not in files_state.get(path, {})}
                if excerpts:
                    request["read_files"] = excerpts
                remembered = read_paths | (set(read_files) - set(excerpts))
                if remembered:
                    request["read_paths"] = sorted(remembered)
                if discovery.evidence:
                    request["context_records"] = discovery.evidence
                if any(discovery.candidates.values()):
                    request["context_candidates"] = discovery.candidates
                conventions = task_conventions(files.output, task)
                if conventions:
                    request["task"]["shared_conventions"] = conventions
                if context_result is not None:
                    request["context_result"] = context_result
                request["feedback"] = feedback
                rules = instructions + "\n\n" + FILE_TOOL_RULES
                if allow_discovery:
                    rules += "\n\n" + DISCOVERY_RULES
                else:
                    rules += "\nSemantic discovery is disabled for this task. Use supplied evidence and read_file only."
                return model.generate_json(rules, request)
            response = request_model()
            while isinstance(response, dict):
                if "context_request" in response:
                    if not allow_discovery:
                        raise ValueError("Semantic discovery is disabled for this task; use the supplied task evidence and read_file within its allowed scope")
                    query = response["context_request"]
                    console.show(label + " | Context query", json.dumps(query, ensure_ascii=False))
                    if isinstance(query, dict) and query.get("kind") == "file" and "ids" in query:
                        raise ValueError('Use read_file for file content, for example '
                            '{"tool_calls":[{"tool":"read_file","path":"backend/src/db/initialize.ts"}]}. '
                            'context_request discovers semantic records or file candidates.')
                    else:
                        context_result = discovery.resolve(query)
                elif "tool_calls" in response and files.effect(response["tool_calls"]) == "read":
                    calls = response["tool_calls"]
                    console.show(label + " | Read files", str([call["path"] for call in calls]))
                    results = files.execute(calls)
                    observation = {"tool_results": results}
                    for item in results:
                        record = item["result"]
                        entry = read_files.setdefault(record["path"], {"round": turn, "excerpts": []})
                        entry["round"] = turn
                        # Keep distinct ranges; a wider read replaces excerpts it covers.
                        entry["excerpts"] = [old for old in entry["excerpts"] if not (
                            record["start_line"] <= old["start_line"] and record["end_line"] >= old["end_line"])]
                        entry["excerpts"].append(record)
                    context_result = {"read_paths": [item["result"]["path"] for item in results]}
                    observed = discovery.observe(observation)
                    if observed.get("next_action"):
                        context_result["next_action"] = observed["next_action"]
                else:
                    break
                response = request_model()
            if isinstance(response, dict) and response.get("diagnosis"):
                console.show(label + " | Failure diagnosis", json.dumps(response["diagnosis"], ensure_ascii=False, indent=2))
            calls = response["tool_calls"]
            results = files.execute(calls)
            for change in files.last_changes:
                read_files.pop(change["path"], None)
            context_result = {"tool_results": results}
            changes = [change for change in files.last_changes if "/tests/" in change["path"]
                and change["before"] is not None and change["before"] != change["after"]]
            if changes:
                audit = files.output / ".arc/test_repairs.jsonl"
                audit.parent.mkdir(parents=True, exist_ok=True)
                with audit.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"task": label, "turn": turn, "diagnosis": response.get("diagnosis"),
                        "feedback": feedback, "changes": changes}, ensure_ascii=False) + "\n")
            applied = True
            if files.pending_target:
                raise ValueError("Create task.file_target with edit_file before running acceptance checks")
            console.show(f"{label} | Edit round {turn}/{max_turns}",
                str([change["path"] for change in files.last_changes]))
            checkpoint(files.output, task=label, action=f"Apply file edits in round {turn}", status="PROPOSED",
                details={"round": turn, "files": [change["path"] for change in files.last_changes],
                    "diagnosis": response.get("diagnosis")})
            checking = True
            checks = gate()
            checkpoint(files.output, task=label, action=f"Accept edit round {turn}",
                details={"round": turn, "checks": checks})
            return {"turns": turn, "checks": checks}
        except (*RESPONSE_ERRORS, RuntimeError, OSError) as error:
            message = str(error)
            console.show(f"{label} | Repair feedback", message)
            rejected = (getattr(error, "feedback", {"error": message}) if applied
                else response_feedback(error, response))
            if isinstance(response, dict) and response.get("diagnosis") and isinstance(rejected, dict):
                rejected = {**rejected, "previous_diagnosis": response["diagnosis"]}
            rejected = feedback_filter(rejected) if feedback_filter else rejected
            signature = feedback_signature(rejected)
            repeats = repeats + 1 if signature == last_signature else 1
            last_signature = signature
            if checking:
                check_feedback = [rejected]
                feedback = list(check_feedback)
            else:
                feedback = [*check_feedback, rejected]
            checkpoint(files.output, task=label, action=f"Reject edit round {turn}", status="REJECTED",
                details={"round": turn, "applied": applied, "feedback": feedback})
            if stop_on_repeat and repeats >= stop_on_repeat:
                console.show(f"{label} | Not converging",
                    f"Stopping after {repeats} rejections with identical feedback; another round would "
                    "repeat this failure instead of changing it.")
                raise AgentExhausted(label, turn, feedback)
    raise AgentExhausted(label, max_turns, feedback)
