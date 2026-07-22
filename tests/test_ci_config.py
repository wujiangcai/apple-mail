from copy import deepcopy
from pathlib import Path
import re

import pytest
import yaml


class _GitHubActionsLoader(yaml.SafeLoader):
    """Safe YAML loader with YAML 1.2 boolean semantics."""


_BOOL_TAG = "tag:yaml.org,2002:bool"
_GitHubActionsLoader.yaml_implicit_resolvers = {
    first_character: [
        resolver for resolver in resolvers if resolver[0] != _BOOL_TAG
    ]
    for first_character, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_GitHubActionsLoader.add_implicit_resolver(
    _BOOL_TAG,
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _load_yaml(path: str) -> dict:
    parsed = yaml.load(
        Path(path).read_text(encoding="utf-8"),
        Loader=_GitHubActionsLoader,
    )
    assert isinstance(parsed, dict)
    return parsed


def _named_steps(job: dict) -> dict[str, dict]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    named = {step["name"]: step for step in steps}
    assert len(named) == len(steps)
    return named


def _validate_required_conditions(workflow: dict) -> None:
    triggers = workflow.get("on")
    assert isinstance(triggers, dict)
    for name in ("push", "pull_request", "workflow_dispatch"):
        assert name in triggers
        configuration = triggers[name]
        assert configuration is None or isinstance(configuration, dict)

    jobs = workflow["jobs"]
    for name in ("python-unit", "postgres-integration", "frontend", "secrets"):
        job = jobs[name]
        assert "if" not in job
        for step in job["steps"]:
            if name == "python-unit" and step["name"] == "Audit Python dependencies":
                assert step.get("if") == "matrix.python-version == '3.13'"
            else:
                assert "if" not in step


def test_github_actions_loader_preserves_on_and_required_triggers():
    workflow = _load_yaml(".github/workflows/ci.yml")

    assert "on" in workflow
    assert set(workflow["on"]) >= {"push", "pull_request", "workflow_dispatch"}
    assert True not in workflow


def test_required_jobs_and_steps_have_no_bypass_conditions():
    _validate_required_conditions(_load_yaml(".github/workflows/ci.yml"))


def test_condition_validator_rejects_in_memory_job_bypass():
    workflow = deepcopy(_load_yaml(".github/workflows/ci.yml"))
    workflow["jobs"]["frontend"]["if"] = False

    with pytest.raises(AssertionError):
        _validate_required_conditions(workflow)


def test_condition_validator_rejects_in_memory_step_bypass():
    workflow = deepcopy(_load_yaml(".github/workflows/ci.yml"))
    steps = _named_steps(workflow["jobs"]["python-unit"])
    steps["Run unit tests"]["if"] = False

    with pytest.raises(AssertionError):
        _validate_required_conditions(workflow)


def test_condition_validator_rejects_disabled_required_trigger():
    workflow = deepcopy(_load_yaml(".github/workflows/ci.yml"))
    workflow["on"] = {
        "push": False,
        "pull_request": None,
        "workflow_dispatch": None,
    }

    with pytest.raises(AssertionError):
        _validate_required_conditions(workflow)


def test_condition_validator_rejects_wrong_python_audit_condition():
    workflow = deepcopy(_load_yaml(".github/workflows/ci.yml"))
    steps = _named_steps(workflow["jobs"]["python-unit"])
    steps["Audit Python dependencies"]["if"] = "always()"

    with pytest.raises(AssertionError):
        _validate_required_conditions(workflow)


def test_ci_covers_supported_python_node_and_postgres_versions():
    jobs = _load_yaml(".github/workflows/ci.yml")["jobs"]

    assert jobs["python-unit"]["strategy"]["matrix"]["python-version"] == [
        "3.12",
        "3.13",
    ]
    node_setup = _named_steps(jobs["frontend"])["Set up Node.js"]
    assert node_setup["uses"] == "actions/setup-node@v4"
    assert node_setup["with"]["node-version"] == 24
    postgres = jobs["postgres-integration"]
    assert postgres["services"]["postgres"]["image"] == "postgres:17"
    assert postgres["env"]["TEST_DATABASE_URL"].startswith("postgresql://")


def test_ci_runs_each_release_gate_in_the_correct_job():
    jobs = _load_yaml(".github/workflows/ci.yml")["jobs"]

    python_steps = _named_steps(jobs["python-unit"])
    python_install = python_steps["Install locked dependencies"]["run"].splitlines()
    assert python_install == [
        "python -m pip install --require-hashes -r requirements.lock",
        "python -m pip install --no-build-isolation --no-deps -e .",
    ]
    assert python_steps["Lint Python"]["run"] == "python -m ruff check ."
    assert python_steps["Compile Python"]["run"] == (
        "python -m compileall -q mail_manager tests"
    )
    assert python_steps["Run unit tests"]["run"] == (
        'python -m pytest tests -m "not postgres" '
        "--cov=mail_manager --cov-fail-under=85"
    )
    assert python_steps["Audit Python dependencies"]["run"] == (
        "python -m pip_audit -r requirements.lock"
    )

    postgres_steps = _named_steps(jobs["postgres-integration"])
    assert postgres_steps["Run PostgreSQL integration tests"]["run"] == (
        "python -m pytest tests/test_postgres_integration.py -v"
    )

    frontend_steps = _named_steps(jobs["frontend"])
    assert frontend_steps["Install locked dependencies"]["run"] == "npm ci"
    assert frontend_steps["Run Vitest"]["run"] == "npm test"
    assert frontend_steps["Audit npm dependencies"]["run"] == (
        "npm audit --audit-level=high"
    )

    secret_steps = _named_steps(jobs["secrets"])
    assert secret_steps["Check out full history"]["with"]["fetch-depth"] == 0
    assert secret_steps["Scan committed history"]["uses"] == (
        "gitleaks/gitleaks-action@v2"
    )


def test_ci_uses_versioned_public_actions_only():
    jobs = _load_yaml(".github/workflows/ci.yml")["jobs"]
    actions = [
        step["uses"]
        for job in jobs.values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert actions
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@v\d+", action)
        for action in actions
    )


def test_dependabot_checks_all_dependency_ecosystems_weekly():
    dependabot = _load_yaml(".github/dependabot.yml")
    updates = dependabot["updates"]
    by_ecosystem = {update["package-ecosystem"]: update for update in updates}

    assert dependabot["version"] == 2
    assert set(by_ecosystem) == {"pip", "npm", "github-actions"}
    for update in by_ecosystem.values():
        assert update["directory"] == "/"
        assert update["schedule"] == {"interval": "weekly"}
