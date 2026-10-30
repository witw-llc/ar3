"""Exact-commit certification authority and publication boundaries."""
import base64
import copy
import importlib.util
import io
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verification", ROOT / "tools/verification.py")
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)
HEAD = "a" * 40
MERGED = "b" * 40
TREE = "c" * 40
REPO = "example/project"


class API:
    repo = REPO

    def __init__(self):
        self.run = {"id": 100, "run_attempt": 1, "head_sha": HEAD,
                    "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
                    "workflow_id": 10, "path": verification.WORKFLOW, "event": "workflow_dispatch",
                    "status": "completed", "conclusion": "success"}
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                     for name in verification.REQUIRED | {"complete"}]
        self.statuses = []
        self.posts = []
        self.pr = {"state": "open", "merged": False, "head": {"sha": HEAD, "ref": "0.1.123",
                   "repo": {"full_name": REPO}}, "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.merged_pr = dict(self.pr, state="closed", merged=True, merged_at="2026-01-01",
                              merge_commit_sha=MERGED)
        self.trees = {HEAD: TREE, MERGED: TREE}
        self.main = MERGED
        self.associated = [self.merged_pr]

    def request(self, path, data=None):
        if data is not None:
            assert path == f"statuses/{HEAD}"
            self.posts.append((path, copy.deepcopy(data)))
            self.statuses.insert(0, dict(data, creator={"login": "github-actions[bot]"}))
            return data
        if path == "actions/workflows/test.yml":
            return {"id": 10}
        if path.startswith("actions/runs/"):
            return copy.deepcopy(self.run)
        if path == "pulls/1":
            return copy.deepcopy(self.pr)
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if path.startswith("git/commits/"):
            return {"tree": {"sha": self.trees[path.split("/")[-1]]}}
        if path.startswith("contents/VERSION"):
            return {"content": base64.b64encode(b"0.1.123\n").decode()}
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path.startswith("actions/workflows/"):
            return [] if self.run is None else [copy.deepcopy(self.run)]
        if path.startswith("actions/runs/"):
            assert f"/attempts/{self.run['run_attempt']}/jobs" in path
            return copy.deepcopy(self.jobs)
        if path.endswith("/statuses"):
            return copy.deepcopy(self.statuses)
        if path.endswith("/pulls"):
            return copy.deepcopy(self.associated)
        raise AssertionError(path)


def update(api, mode, run_id=100, attempt=1):
    return verification.update_status(api, mode, HEAD, "1", run_id, attempt, "refs/heads/0.1.123")


def test_completed_evidence_and_squash_tree_match():
    api = API()
    assert verification.evidence(api, HEAD)["id"] == 100
    proof = verification.release_evidence(api, MERGED, "refs/heads/main", "push", "")
    assert proof["sha"] != proof["verified_sha"]
    assert proof["tree"] == TREE


@pytest.mark.parametrize("field,value", [
    ("event", "push"), ("path", ".github/workflows/other.yml"), ("workflow_id", 11),
    ("repository", {"full_name": "other/project"}),
    ("head_repository", {"full_name": "other/project"}),
    ("conclusion", "failure"), ("status", "in_progress"),
])
def test_wrong_or_incomplete_run_is_not_evidence(field, value):
    api = API()
    api.run[field] = value
    with pytest.raises(verification.Refused):
        verification.evidence(api, HEAD)


def test_missing_or_wrong_tip_run_is_not_evidence():
    for run in (None, dict(API().run, head_sha="d" * 40)):
        api = API()
        api.run = run
        with pytest.raises(verification.Refused, match="no manual"):
            verification.evidence(api, HEAD)


@pytest.mark.parametrize("name", sorted(verification.REQUIRED | {"complete"}))
@pytest.mark.parametrize("outcome", ["absent", "failure", "skipped", "cancelled", "neutral"])
def test_every_mandatory_job_must_actually_pass(name, outcome):
    api = API()
    if outcome == "absent":
        api.jobs = [job for job in api.jobs if job["name"] != name]
    else:
        next(job for job in api.jobs if job["name"] == name)["conclusion"] = outcome
    with pytest.raises(verification.Refused):
        verification.evidence(api, HEAD)


def test_duplicate_job_names_fail_closed():
    api = API()
    api.jobs.append(dict(api.jobs[0]))
    with pytest.raises(verification.Refused, match="duplicate"):
        verification.evidence(api, HEAD)


def test_initializer_rejects_spoofed_success_without_run():
    api = API()
    api.run = None
    api.statuses = [{"context": verification.CONTEXT, "state": "success",
                     "creator": {"login": "someone"}, "target_url": "https://example.com"}]
    update(api, "initialize")
    assert api.posts[-1][1]["state"] == "failure"


def test_terminal_writer_then_late_initializer_before_run_completion():
    api = API()
    api.run.update(status="in_progress", conclusion=None)
    next(job for job in api.jobs if job["name"] == "complete").update(status="in_progress", conclusion=None)
    update(api, "complete")
    assert api.posts[-1][1]["state"] == "success"
    update(api, "initialize")
    assert api.posts[-1][1]["state"] == "success"
    with pytest.raises(verification.Refused, match="not completed"):
        verification.evidence(api, HEAD)


@pytest.mark.parametrize("bad", ["creator", "url", "attempt"])
def test_finishing_race_requires_authenticated_current_attempt_status(bad):
    api = API()
    api.run.update(status="in_progress", conclusion=None)
    next(job for job in api.jobs if job["name"] == "complete").update(status="in_progress", conclusion=None)
    update(api, "complete")
    if bad == "creator":
        api.statuses[0]["creator"]["login"] = "someone"
    elif bad == "url":
        api.statuses[0]["target_url"] = "https://example.com"
    else:
        api.run["run_attempt"] = 2
    update(api, "initialize")
    assert api.posts[-1][1]["state"] == "failure"


def test_stale_final_writer_cannot_overwrite_new_run_or_attempt():
    for change in ({"id": 101}, {"run_attempt": 2}):
        api = API()
        api.run.update(change)
        assert "superseded" in update(api, "complete")
        assert api.posts == []


def test_announce_uses_direct_run_when_list_still_shows_an_older_dispatch():
    api = API()
    pages = api.pages
    def stale_list(path, key=None):
        if path.startswith("actions/workflows/"):
            return [dict(api.run, id=99)]
        return pages(path, key)
    api.pages = stale_list
    update(api, "announce")
    assert api.posts[-1][1]["state"] == "pending"
    assert "/runs/100/" in api.posts[-1][1]["target_url"]


def test_final_writer_rechecks_attempt_after_reading_jobs():
    api = API()
    pages = api.pages
    def advance_attempt(path, key=None):
        result = pages(path, key)
        if path.endswith("/jobs"):
            api.run["run_attempt"] = 2
        return result
    api.pages = advance_attempt
    assert "authority changed" in update(api, "complete")
    assert not api.posts


def test_initializer_sees_newer_run_through_authenticated_status_despite_list_lag():
    api = API()
    api.run.update(status="in_progress", conclusion=None)
    update(api, "announce")
    pages = api.pages
    def empty_list(path, key=None):
        return [] if path.startswith("actions/workflows/") else pages(path, key)
    api.pages = empty_list
    update(api, "initialize")
    assert api.posts[-1][1]["state"] == "failure"
    assert "/runs/100/" in api.posts[-1][1]["target_url"]


def test_cancellation_callback_revokes_success_without_relying_on_final_job():
    api = API()
    update(api, "complete")
    api.run.update(run_attempt=2, status="completed", conclusion="cancelled")
    next(job for job in api.jobs if job["name"] == "complete")["conclusion"] = "cancelled"
    update(api, "reconcile", attempt=2)
    assert api.posts[-1][1]["state"] == "failure"


def test_rerun_callback_revokes_old_green_before_any_suite_executes():
    api = API()
    update(api, "complete")
    api.run.update(run_attempt=2, status="in_progress", conclusion=None)
    api.jobs = []
    update(api, "reconcile", attempt=2)
    assert api.posts[-1][1]["state"] == "pending"


def test_failed_new_attempt_revokes_old_success():
    api = API()
    update(api, "complete")
    api.run.update(run_attempt=2, status="in_progress", conclusion=None)
    update(api, "announce", attempt=2)
    assert api.posts[-1][1]["state"] == "pending"
    next(job for job in api.jobs if job["name"] == "policy")["conclusion"] = "failure"
    update(api, "complete", attempt=2)
    assert api.posts[-1][1]["state"] == "failure"
    update(api, "complete", attempt=1)
    assert api.posts[-1][1]["state"] == "failure"


def test_moved_pr_never_receives_prior_tip_evidence():
    api = API()
    api.pr["head"]["sha"] = "d" * 40
    assert "stale" in update(api, "initialize")
    assert not api.posts
    update(api, "complete")
    assert api.posts[-1][0] == f"statuses/{HEAD}"


@pytest.mark.parametrize("change", ["fork", "closed", "branch", "base"])
def test_dispatch_subject_rejects_wrong_pr_selection(change):
    api = API()
    if change == "fork":
        api.pr["head"]["repo"]["full_name"] = "other/project"
    elif change == "closed":
        api.pr["state"] = "closed"
    elif change == "branch":
        api.pr["head"]["ref"] = "different"
    else:
        api.pr["base"]["ref"] = "different"
    with pytest.raises(verification.Refused):
        update(api, "announce")
    assert not api.posts


@pytest.mark.parametrize("change", ["tree", "branch", "version", "new-main", "unrelated-pr", "ambiguous-pr"])
def test_release_rejects_unverified_publication(change):
    api = API()
    ref, version = "refs/heads/main", "0.1.123"
    if change == "tree":
        api.trees[MERGED] = "e" * 40
    elif change == "branch":
        ref = "refs/heads/0.1.123"
    elif change == "version":
        version = "0.1.122"
    elif change == "new-main":
        api.main = "d" * 40
    elif change == "unrelated-pr":
        api.associated[0]["merge_commit_sha"] = "d" * 40
    else:
        api.associated.append(copy.deepcopy(api.associated[0]))
    with pytest.raises(verification.Refused):
        verification.release_evidence(api, MERGED, ref, "workflow_dispatch", version)


def test_api_paginates_instead_of_accepting_truncated_inventory(monkeypatch):
    api = verification.GitHub(REPO, "unused")
    paths = []
    def request(path):
        paths.append(path)
        return {"jobs": list(range(100)) if path.endswith("&page=1") else [100]}
    monkeypatch.setattr(api, "request", request)
    assert api.pages("jobs?filter=all", "jobs") == list(range(101))
    assert paths[-1] == "jobs?filter=all&per_page=100&page=2"


def test_privileged_loader_fetches_only_fixed_workflow_revision(monkeypatch, tmp_path):
    text = (ROOT / ".github/workflows/test.yml").read_text()
    scripts = re.findall(r"python3 - <<'PYTHON'\n(.*?)\n          PYTHON", text, re.S)
    assert len(scripts) == 3 and len(set(scripts)) == 1
    requests = []
    def fetch(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(b"# reviewed workflow helper\n")
    monkeypatch.setattr(verification.urllib.request, "urlopen", fetch)
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", HEAD)
    monkeypatch.setenv("GITHUB_SHA", MERGED)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GH_TOKEN", "synthetic")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    exec(compile(textwrap.dedent(scripts[0]), "workflow-loader", "exec"), {})
    assert requests == [f"https://api.github.com/repos/{REPO}/contents/tools/verification.py?ref={HEAD}"]
    assert (tmp_path / "verification.py").read_text() == "# reviewed workflow helper\n"


def test_workflow_contract_keeps_suites_manual_and_writer_isolated():
    text = (ROOT / ".github/workflows/test.yml").read_text()
    jobs = dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", text.split("jobs:\n", 1)[1], re.M | re.S))
    for name in ("announce", "complete", "initialize"):
        assert "statuses: write" in jobs[name]
        assert "actions/checkout" not in jobs[name]
        assert "ar3-full-suite-status-${{ github.repository }}-${{ github.event.pull_request.head.sha || github.sha }}" in jobs[name]
        assert "cancel-in-progress: false" in jobs[name]
    for name in ("prepare", "suite", "isolation", "policy", "record"):
        assert "github.event_name == 'workflow_dispatch'" in jobs[name]
        assert "statuses: write" not in jobs[name]
        assert "ref: ${{ github.sha }}" in jobs[name]
    assert "fail-fast: false" in jobs["suite"]
    for lane in verification.SUITES:
        assert f"lane: {lane}," in jobs["suite"]
    assert "--durations=30" in jobs["suite"]
    assert "actions/download-artifact@v4" in jobs["record"]
    assert " -x" not in jobs["suite"]
    assert "AR3_PUBLIC_DEPLOY_KEY" not in text
    assert verification.CONTEXT == "full-suite"
    assert "pii" in verification.REQUIRED
    assert "needs: [announce, prepare, policy, pii, suite, isolation, record]" in jobs["complete"]
    assert "needs: [prepare, policy, pii, suite, isolation]" in jobs["record"]
    assert "    if:" not in jobs["pii"] and "    needs:" not in jobs["pii"]
    assert "statuses: write" not in jobs["pii"]
    assert 'run: test "$HEAD_REPOSITORY" = "$GITHUB_REPOSITORY"' in jobs["pii"]
    assert jobs["pii"].index('run: test "$HEAD_REPOSITORY"') < jobs["pii"].index("actions/checkout")
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in jobs["pii"]
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in jobs["pii"]
    assert 'python3 .github/pii_check.py --range "$BASE_SHA...HEAD" || result=1' in jobs["pii"]
    assert "PII_PATTERNS: ${{ secrets.PII_PATTERNS }}" in jobs["pii"]
    assert 'python3 tools/pii-scan.py || result=1' in jobs["pii"]
    assert 'exit "$result"' in jobs["pii"]
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "pytest" not in release and "run-as.sh" not in release
    assert "tools/verification.py release" in release
    assert "types: [requested, in_progress, completed]" in release
    release_jobs = dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", release.split("jobs:\n", 1)[1], re.M | re.S))
    for name in ("verified-build", "policy", "publish"):
        assert "if: github.event_name != 'workflow_run'" in release_jobs[name]
    assert "github.event.workflow_run.event == 'workflow_dispatch'" in release_jobs["reconcile"]
    assert "actions/checkout" not in release_jobs["reconcile"]
    assert "SUBJECT_SHA: ${{ github.event.workflow_run.head_sha }}" in release_jobs["reconcile"]
    assert "ar3-full-suite-status-${{ github.repository }}-${{ github.event.workflow_run.head_sha }}" in release_jobs["reconcile"]
    for exclusion in (".github", ".github-public", "apps/r4t/experiments", "apps/a8s/experiments", "apps/a8s/ext", "org", "/CLAUDE.md", "/AGENTS.md"):
        assert f"--exclude='{exclusion}'" in release


# The token scope each API path that `verification.py release` reads needs.
SCOPES = [
    (r"commits/[^/]+/statuses", "statuses"),
    (r"commits/[^/]+/pulls", "pull-requests"),
    (r"actions/", "actions"),
    (r"(git|contents)/", "contents"),
]


def scope_of(path):
    for pattern, scope in SCOPES:
        if re.match(pattern, path):
            return scope
    raise AssertionError(f"no scope known for {path}")


def permissions(block):
    match = re.search(r"^( *)permissions:\n((?:\1  [\w-]+: \w+\n)+)", block, re.M)
    return None if match is None else dict(re.findall(r"([\w-]+): (\w+)", match[2]))


def test_release_token_grants_every_scope_the_release_check_reads():
    """The release check runs with the workflow's token. A scope it reads and
    the token lacks is a 403 that only the first real publication meets."""
    api = API()
    paths = []
    request, pages = api.request, api.pages
    api.request = lambda path, data=None: (paths.append(path), request(path, data))[1]
    api.pages = lambda path, key=None: (paths.append(path), pages(path, key))[1]
    verification.release_evidence(api, MERGED, "refs/heads/main", "push", "")
    needed = {scope_of(path) for path in paths}
    assert "statuses" in needed
    release = (ROOT / ".github/workflows/release.yml").read_text()
    top = permissions(release.split("jobs:\n", 1)[0])
    jobs = dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", release.split("jobs:\n", 1)[1], re.M | re.S))
    callers = [name for name, body in jobs.items() if "tools/verification.py release" in body]
    assert callers == ["verified-build", "publish"]
    for name in callers:
        granted = permissions(jobs[name]) or top
        missing = {scope for scope in needed if granted.get(scope) not in {"read", "write"}}
        assert not missing, f"{name} token lacks {sorted(missing)}"
