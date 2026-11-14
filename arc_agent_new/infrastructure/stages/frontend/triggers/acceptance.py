"""Independent requirement acceptance, separate from implementation-owned tests."""

from hashlib import sha256
import yaml

from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.conventions import task_conventions
from infrastructure.shared.agent.context import SourceFiles
from infrastructure.shared.agent.discovery import project_index
from infrastructure.shared.agent.recovery import implementation_task
from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.stages.backend.context import requirement_context, related_schema, unknown_sql_tables
from infrastructure.stages.backend.testing import BackendChecks, require_green as require_backend_green
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.frontend.triggers.implementation import scenario_context, IMPLEMENT_INSTRUCTIONS, editable_runtime_path, test_run_status
from infrastructure.stages.frontend.triggers.e2e_runtime import FIXTURE
from infrastructure.stages.frontend.triggers.testing import E2EChecks, test_path
from infrastructure.stages.frontend.triggers.scope import TriggerCheckFailure
from infrastructure.stages.frontend.triggers.design import api_ids
from infrastructure.stages.frontend.ui.runtime import APPLICATION_ENTRYPOINTS
from infrastructure.stages.frontend.ui.testing import FrontendChecks, require_green, test_path as ui_test_path
from infrastructure.utils.files import save_json
from infrastructure.shared.project.git import checkpoint

INSTRUCTIONS = '''Write independent final acceptance tests for ONE original requirement.
Derive expected results from original_requirement and its Given/When/Then scenarios.
Interface details are supplied only to locate UI elements/routes and prepare valid Given
fixtures; they are not authority to weaken an original business requirement. Neither
application source nor implementation-owned tests are supplied. Do not infer expected
behavior from an observed implementation result or duplicate process smoke tests.

Write a small Playwright suite at test_path using the supplied test/expect/database
fixture. Drive When actions through the real browser without mocks or injected state.
Use UI IDs or accessible locators for actions. Accessible-name matching is case-insensitive
and substring-based, so a locator named "Male" also matches "Female": when one required name
contains another, pass { exact: true } or scope the locator to its fieldset/section. Never add
invisible characters or duplicate ARIA attributes to the application to disambiguate a
locator. Assert original required visible business
outcomes, not design-inferred internal labels or every DOM attribute. Cover main success,
critical rejection, correct identity/ownership, and refresh/persistence WHEN required.
Use two distinct identities where identity isolation matters; a test that only creates
one account cannot reject an implementation returning the first account for everyone.
For required rejection-without-writes, verify the relevant persisted result is unchanged.
Prefer user-visible outcomes over unspecified cookie/hash encodings or storage strategies.
For a When action requiring one API operation, observe the mutation the action issues and
require exactly one such request to succeed. Do NOT hard-code a path borrowed from another
requirement's API contract: several requirements may share one endpoint, so the path is an
implementation choice, not a business outcome. Assert that the action sends exactly one
mutating request to some /api path, and fail when it sends none, two, or the same submission
mirrored to a second path. Start observing before the action, wait for its response and
business completion, and exclude unrelated page-entry calls or explicitly required retries. Do
not count all requests globally.
There is no arbitrary test-count quota: cover the few important outcomes for this task.

The database fixture restores requirement-defined startup data before each test. Reuse
those records when required; do not assume an empty database or insert duplicate identities.
Use prerequisite records to isolate the current requirement from other requirements'
registration/creation implementations. Arrange scenario-specific absence or conflicting
Given states in the isolated test database; still drive the current When through the UI.
Seed additional minimal valid Given records using the documented database/test utilities,
with the exact physical table and column names from task.schema: INSERT INTO "Book", never a
guessed plural or lowercase variant such as books. A guessed name fails with 'no such table'
and is a test defect, not missing behavior. Application
sources, shared fixtures, original requirements and other suites are not editable.
During generation only TypeScript is checked; missing application behavior is a useful
acceptance failure, never a reason to weaken assertions. Use edit_file to create or repair only test_path under frontend/tests/e2e/acceptance.'''

REVIEW_INSTRUCTIONS = '''Review this independent acceptance test against the original requirement.
This is a separate test review, not implementation repair. Actual failure evidence is
provided. Diagnose test_requirement_mismatch, test_overconstraint, environment, or
implementation. Change ONLY test_path if specific original requirement or fixture evidence
shows the test is wrong. Preserve every required business outcome. If the implementation
or environment is wrong, return tool_calls=[] and explain it; the failed acceptance remains
failed. Compilation passing is enough to finish this review; you are NOT asked to turn a
business failure GREEN. Never adjust expected values just to match observed wrong behavior.
Use original requirements as authority; no application source or process tests are available.
A strict-mode violation (one locator resolving to several elements) is a test defect, not an
implementation failure: disambiguate that locator with exact matching or scoping and keep every
required business outcome.'''


def readable_note(readable):
    """State the readable paths up front instead of letting a run discover them by rejection.

    Acceptance tasks keep asking for application source they are not given; each request is
    rejected and burns one of the few repair rounds. Naming the list in the prompt removes
    that loop at the source, where a feedback message after the fact cannot.
    """
    return ('\n\nREADABLE FILES: exactly these paths can be opened — ' + ', '.join(readable) +
        '. Application source (frontend/src, backend/src), other suites, package files and '
        'fixtures are NOT readable; a request for them is rejected and costs one of your '
        'limited rounds. The task payload already contains the UI ids, routes, API signatures '
        'and schema fields you need, so do not try to read them from source.')


def require_acceptance(result):
    require_green(result)
    if result.get('skipped') or result.get('flaky'):
        raise ValueError('Independent acceptance includes skipped or unstable tests')


def locator_defect(result):
    """Report whether the failure is a selector that resolved to several elements.

    Repair rounds may only edit the application, so spending them on this failure class cannot
    succeed: the ambiguity lives in the test's own locator. The test review does own the suite,
    so it gets the extra round instead of the implementation loop replaying a gate it cannot fix.
    """
    text = str((result or {}).get('output') or '')
    return 'strict mode violation' in text


def acceptance_path(rid):
    return 'frontend/tests/e2e/acceptance/' + sha256(rid.encode()).hexdigest()[:12] + '.spec.ts'


def original_requirements(input_dir):
    """Read the original YAML again; no generated design is used as the test oracle."""
    path = input_dir / 'requirements.yaml'
    if not path.is_file():
        raise ValueError('Independent acceptance requires original input/requirements.yaml')
    return RequirementTree(yaml.safe_load(path.read_text(encoding='utf-8-sig')))


def run_independent_acceptance(input_dir, arc_dir, schema, backend, ui, design, model, console,
        max_turns=10, *, process_report, review_turns=2, repair_turns=2):
    """Generate, review and repair one acceptance suite per leaf requirement.

    Acceptance repair is the most expensive loop in the pipeline: it converges within two
    rounds when it converges at all, and the later rounds were observed only restating a
    failure the agent could not fix. It therefore keeps its own small budgets instead of
    inheriting the stage-wide edit budget, and the review runs once because its only job is
    to decide whether the test or the implementation is wrong.
    """
    output = arc_dir.parent.resolve()
    tree = original_requirements(input_dir)
    checks = E2EChecks(output)
    ui_checks = FrontendChecks(output)
    backend_checks = BackendChecks(output)
    selection = SourceFiles(output, ui)
    backend_selection = SourceFiles(output, root='backend/src', tests_root='backend/tests')
    context_index = project_index(output,
        kinds=('schema', 'api', 'page', 'ui_element', 'ui_data', 'trigger', 'requirement',
            'test_requirement', 'convention', 'file'),
        file_roots=('frontend/src', 'frontend/tests', 'backend/src', 'backend/tests'))
    accepted = []
    verified_process = set()
    pending_verification = any(check['status'] == 'FAILED'
        for check in process_report.get('verification', {}).values())
    report = {'status': 'RUNNING', 'requirements': {}}
    save_json(arc_dir / 'independent_acceptance.json', report)
    for rid in tree.leaves:
        path = acceptance_path(rid)
        entry = {'status': 'GENERATING', 'test_path': path}
        report['requirements'][rid] = entry
        label = 'Independent acceptance / ' + rid
        try:
            context = scenario_context(ui, design, rid)
            aids = set(backend['requirements'].get(rid, []))
            for trigger in context['triggers'].values():
                aids.update(api_ids(trigger))
            apis = {aid: backend['apis'][aid] for aid in aids}
            task = {'original_requirement': requirement_context(tree, rid), 'test_path': path,
                'ui_interfaces': {pid: {key: page[key] for key in ('route', 'root', 'elements')}
                    for pid, page in context['page_subtrees'].items()},
                'api_interfaces': {aid: {key: value for key, value in api.items()
                    if key in ('signature', 'request', 'response')} for aid, api in apis.items()},
                'schema': related_schema(schema, apis.values())}
            if not task['ui_interfaces']:
                entry.update(status='FAILED', error='No UI interface available for independent requirement acceptance; not verified')
                continue
            task['shared_conventions'] = task_conventions(output, {'requirement': task['original_requirement']})
            readable = [path, FIXTURE, 'backend/tests/support/context.ts']
            workspace = FileTools.for_target(output, 'acceptance_test', rid, fixed_path=path,
                references=[FIXTURE, 'backend/tests/support/context.ts'], readable=readable)

            def compile_suite():
                if not (output / path).is_file():
                    raise ValueError('Create the supplied acceptance test_path')
                known = set(schema['entities'])
                unknown = unknown_sql_tables(output / path, known)
                if unknown:
                    raise ValueError(
                        f"Test SQL writes to unknown table(s): {', '.join(unknown)}. "
                        f"Rewrite them with the exact physical names from task.schema: "
                        f"{', '.join(sorted(known))}.")
                result = checks.typecheck([path], full_source=False)
                if result['returncode']:
                    raise ValueError(result['output'])
                return result

            if not (output / path).is_file():
                agent = run_file_agent(model, INSTRUCTIONS + readable_note(readable), task, workspace, compile_suite,
                    max_turns, console, label + ' / Generation')
                entry['generation_turns'] = agent['turns']
            result = checks.tests([path])
            entry["result"] = result
            try:
                require_acceptance(result)
            except ValueError:
                # A separate, requirement-grounded review may correct the test, but its
                # gate intentionally does not demand behavioral GREEN.
                entry['initial_failure'] = result
                save_json(arc_dir / 'independent_acceptance.json', report)
                checkpoint(output, stage="12", task=rid, action="Record independent acceptance before test review",
                    status="FAILED", details={"test_path": path, "checks": result})
                review = run_file_agent(model, REVIEW_INSTRUCTIONS + readable_note(readable), task, workspace, compile_suite,
                    review_turns + (1 if locator_defect(result) else 0), console, label + ' / Test review', feedback=result)
                entry['review_turns'] = review['turns']
                result = checks.tests([path])
                entry["result"] = result
            if test_run_status(result) != 'GREEN' or pending_verification:
                entry['status'] = 'REPAIRING'
                process_ids = [key for key, value in process_report['requirements'].items()
                    if pending_verification or value.get('status') == 'GREEN' or key == rid]
                process_files = [test_path(key, output) for key in process_ids
                    if (output / test_path(key, output)).is_file()]
                acceptance_files = [acceptance_path(key) for key in [*accepted, rid]]
                ui_files = [ui_test_path(pid) for pid in ui['pages']
                    if (output / ui_test_path(pid)).is_file()]
                repair_task = {**task, **context, 'requirement': task['original_requirement'],
                    'backend_apis': apis, 'target_trigger_ids': sorted(context['triggers']),
                    'repair_reason': 'final_verification_and_acceptance' if pending_verification else 'independent_acceptance',
                    'application_entrypoints': list(APPLICATION_ENTRYPOINTS),
                    'backend_entrypoints': {'registry': 'backend/src/api/register.ts',
                        'app': 'backend/src/app.ts', 'server': 'backend/src/server.ts'}}
                repair_workspace = selection.workspace(
                    paths=selection.paths('page', context['page_subtrees']),
                    references=[path, *ui_files, FIXTURE, 'frontend/src/ui-runtime.ts',
                        *APPLICATION_ENTRYPOINTS, 'frontend/package.json', 'frontend/tsconfig.json'])
                repair_workspace.allow_write = lambda relative: editable_runtime_path(output, relative)
                backend_selection.include(repair_workspace, backend_selection.paths('api', aids))

                def repair_gate():
                    evidence = {}
                    try:
                        evidence['typecheck'] = checks.typecheck([*process_files, *acceptance_files])
                        if evidence['typecheck']['returncode']:
                            raise ValueError(evidence['typecheck']['output'])
                        # Repair rounds first run only this requirement's suite. The other
                        # accepted suites are regression, and replaying them on every failed
                        # round doubled the cost of a loop that usually fails for the same
                        # reason; they run once this requirement is GREEN.
                        evidence['acceptance'] = checks.tests([path])
                        require_acceptance(evidence['acceptance'])
                        regression = [other for other in acceptance_files if other != path]
                        if regression:
                            evidence['regression'] = checks.tests(regression)
                            require_acceptance(evidence['regression'])
                        if process_files:
                            evidence['smoke'] = checks.tests(process_files)
                            require_acceptance(evidence['smoke'])
                        if ui_files:
                            evidence['ui'] = ui_checks.tests(ui_files)
                            require_green(evidence['ui'])
                        if pending_verification or any(relative.startswith('backend/') for relative in repair_workspace.changed_paths):
                            evidence['backend_typecheck'] = backend_checks.typecheck()
                            if evidence['backend_typecheck']['returncode']:
                                raise ValueError(evidence['backend_typecheck']['output'])
                            suites = [selected for aid in backend['apis']
                                if (selected := selected_path(output, 'backend_test', aid))
                                and (output / selected).is_file()]
                            if suites:
                                evidence['backend'] = backend_checks.tests(suites)
                                require_backend_green(evidence['backend'])
                        evidence['build'] = checks.build()
                        if evidence['build']['returncode']:
                            raise ValueError(evidence['build']['output'])
                    except (ValueError, RuntimeError) as error:
                        raise TriggerCheckFailure(str(error), evidence) from error
                    return evidence

                def repair_feedback(value):
                    return selection.feedback(repair_workspace, backend_selection.feedback(repair_workspace, value))

                with implementation_task(output, entry, console, label + ' / Repair'):
                    repair = run_file_agent(model, IMPLEMENT_INSTRUCTIONS + '\n\n'
                        'Independent acceptance suites are read-only for implementation repair. '
                        'Preserve their original requirement outcomes and repair the real application. '
                        'Test review has already been performed separately. Failures in supplied final '
                        'regression/build checks are also repair targets: retrieve the failing requirement '
                        'and source evidence on demand, and preserve other accepted behavior.',
                        repair_task, repair_workspace, repair_gate, repair_turns, console,
                        label + ' / Repair', feedback={'acceptance': result,
                            'verification': process_report.get('verification', {}) if pending_verification else {}},
                        feedback_filter=repair_feedback,
                        context_index=context_index, stop_on_repeat=2)
                    entry.update(status='GREEN', repair_turns=repair['turns'],
                        result=repair['checks']['acceptance'], repair_checks=repair['checks'])
                if entry['status'] == 'FAILED':
                    continue
                result = entry['result']
                verified_process.update(key for key in process_ids if test_path(key, output) in process_files)
                pending_verification = False
            require_acceptance(result)
            process_entry = process_report['requirements'].get(rid)
            if process_entry and process_entry.get('status') == 'FAILED' and rid in verified_process:
                process_entry['previous_failure'] = dict(process_entry)
                process_entry.update(status='GREEN', recovery='independent_acceptance_repair')
                save_json(arc_dir / 'frontend_e2e.json', process_report)
            entry.update(status='GREEN', result=result)
            accepted.append(rid)
        except (ValueError, RuntimeError) as error:
            entry.update(status='FAILED', error=str(error))
        finally:
            save_json(arc_dir / 'independent_acceptance.json', report)
            checkpoint(output, stage="12", task=rid, action="Publish independent requirement acceptance",
                status=entry['status'], details=entry)
            console.show(label + ' | ' + entry['status'], entry.get('error', 'Real acceptance checks completed'))
    report['status'] = 'GREEN' if report['requirements'] and all(
        entry['status'] == 'GREEN' for entry in report['requirements'].values()) else 'COMPLETED_WITH_FAILURES'
    save_json(arc_dir / 'independent_acceptance.json', report)
    checkpoint(output, stage="12", task="independent acceptance", action="Publish final independent acceptance report",
        status=report['status'], details=report)
    return report
