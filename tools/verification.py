#!/usr/bin/env python3
"""Bind manual full-suite evidence to one immutable, reviewed repository revision.

The workflow and this helper are reviewed together. This verifies GitHub's run
and job records, not the honesty of a maliciously edited workflow or test suite.
Privileged callers load only this file at GITHUB_WORKFLOW_SHA; no candidate
checkout, artifact, or test code runs in the status writer.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTEXT = "full-suite"
WORKFLOW = ".github/workflows/test.yml"
SUITES = ("repo", "a8s-1", "a8s-2", "r4t-1", "r4t-2", "k7e", "ar3", "tools")
REQUIRED = {"prepare", "policy", "pii", "isolation", "record"} | {f"suite ({name})" for name in SUITES}
SHA = re.compile(r"[0-9a-f]{40}\Z")


class Refused(RuntimeError):
    """The available evidence does not authorize this action."""


class GitHub:
    def __init__(self, repo, token):
        self.repo = repo
        self.token = token

    def request(self, path, data=None):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{path}",
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def pages(self, path, key=None):
        separator = "&" if "?" in path else "?"
        page = 1
        result = []
        while True:
            response = self.request(f"{path}{separator}per_page=100&page={page}")
            rows = response[key] if key else response
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1


def require(condition, message):
    if not condition:
        raise Refused(message)


def run_url(api, run):
    return f"https://github.com/{api.repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"


def validate_run(api, run, sha, workflow_id):
    require(SHA.fullmatch(sha), "invalid subject SHA")
    require(run.get("head_sha") == sha, "verification belongs to a different SHA")
    require(run.get("repository", {}).get("full_name") == api.repo
            and run.get("head_repository", {}).get("full_name") == api.repo,
            "verification belongs to a different repository")
    require(run.get("event") == "workflow_dispatch", "verification is not an explicit dispatch")
    require(run.get("workflow_id") == workflow_id and run.get("path") == WORKFLOW,
            "unrecognized verification workflow")


def latest_run(api, sha, observed_id=None):
    workflow_id = api.request("actions/workflows/test.yml")["id"]
    runs = api.pages(f"actions/workflows/{workflow_id}/runs?event=workflow_dispatch&head_sha={sha}", "workflow_runs")
    runs = [run for run in runs if run.get("head_sha") == sha]
    # List endpoints may lag behind a directly addressable new run. A writer
    # knows its own run; a later initializer also sees its authenticated status.
    status = latest_status(api, sha)
    linked = re.fullmatch(re.escape(f"https://github.com/{api.repo}/actions/runs/")
                          + r"(\d+)/attempts/\d+", (status or {}).get("target_url") or "")
    ids = [run["id"] for run in runs]
    if observed_id is not None:
        ids.append(observed_id)
    if linked and status.get("creator", {}).get("login") == "github-actions[bot]":
        ids.append(int(linked[1]))
    if not ids:
        return None
    run = api.request(f"actions/runs/{max(ids)}")
    validate_run(api, run, sha, workflow_id)
    return run


def jobs_for(api, run):
    rows = api.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
    jobs = {}
    for job in rows:
        require(job["name"] not in jobs, "duplicate verification job name")
        jobs[job["name"]] = job
    return jobs


def jobs_pass(jobs, required=REQUIRED):
    return all(name in jobs and jobs[name].get("status") == "completed"
               and jobs[name].get("conclusion") == "success" for name in required)


def latest_status(api, sha):
    return next((status for status in api.pages(f"commits/{sha}/statuses")
                 if status.get("context") == CONTEXT), None)


def evidence(api, sha, *, finishing=False, run=None):
    run = run or latest_run(api, sha)
    require(run is not None, "no manual full-suite run for this SHA")
    jobs = jobs_for(api, run)
    require(jobs_pass(jobs), "full verification has missing or unsuccessful jobs")
    if run.get("status") == "completed":
        require(run.get("conclusion") == "success" and jobs_pass(jobs, {"complete"}),
                "verification run did not finish successfully")
    elif finishing:
        require(run.get("status") == "in_progress", "verification is not finishing")
        writer = jobs.get("complete", {})
        require(writer.get("status") == "in_progress"
                or (writer.get("status") == "completed" and writer.get("conclusion") == "success"),
                "terminal status writer has not run")
        status = latest_status(api, sha)
        require(status and status.get("state") == "success"
                and status.get("creator", {}).get("login") == "github-actions[bot]"
                and status.get("target_url") == run_url(api, run),
                "no authenticated terminal status while run is finishing")
    else:
        raise Refused("verification run has not completed")
    return run


def validate_pr(api, number, sha, branch=None):
    pr = api.request(f"pulls/{int(number)}")
    require(pr.get("state") == "open" and not pr.get("merged"), "verification requires an open PR")
    require(pr.get("head", {}).get("repo", {}).get("full_name") == api.repo
            and pr.get("base", {}).get("repo", {}).get("full_name") == api.repo,
            "fork verification is not supported")
    require(pr["base"]["ref"] == "main", "verification requires a PR targeting main")
    require(pr["head"]["sha"] == sha, "PR tip moved; dispatch its current SHA again")
    if branch is not None:
        require(branch == f"refs/heads/{pr['head']['ref']}", "dispatch must select the PR head branch")
    return pr


def version_at(api, sha):
    item = api.request(f"contents/VERSION?ref={sha}")
    return base64.b64decode(item["content"]).decode().strip()


def tree_at(api, sha):
    return api.request(f"git/commits/{sha}")["tree"]["sha"]


def post_status(api, sha, state, description, run=None):
    payload = {"context": CONTEXT, "state": state, "description": description[:140]}
    if run:
        payload["target_url"] = run_url(api, run)
    api.request(f"statuses/{sha}", payload)


def post_current(api, sha, state, description, run):
    # The list and job reads above can span a new dispatch or rerun. Resolve
    # authority again immediately before the serialized mutation.
    current = latest_run(api, sha, run["id"] if run else None)
    if ((current is None) != (run is None)
            or (run and (current["id"], current["run_attempt"]) != (run["id"], run["run_attempt"]))):
        return "authority changed; no status written"
    post_status(api, sha, state, description, run)
    return description


def update_status(api, mode, sha, number, run_id, attempt, ref):
    # All callers share the workflow's per-SHA writer concurrency group. Read
    # authority inside that group: GitHub concurrency does not promise FIFO.
    if mode == "initialize":
        pr = api.request(f"pulls/{int(number)}")
        if pr.get("head", {}).get("sha") != sha:
            return "stale PR event; no status written"
        validate_pr(api, number, sha)
        run = None
        try:
            run = latest_run(api, sha)
            run = evidence(api, sha, finishing=True, run=run)
        except Refused as error:
            if run is None:
                post_status(api, sha, "failure", str(error))
                return str(error)
            return post_current(api, sha, "failure", str(error), run)
        return post_current(api, sha, "success", "Full verification passed for this exact commit", run)

    own = api.request(f"actions/runs/{run_id}")
    validate_run(api, own, sha, api.request("actions/workflows/test.yml")["id"])
    run = latest_run(api, sha, run_id)
    require(run is not None, "dispatch run is absent")
    if mode == "reconcile":
        try:
            evidence(api, sha, finishing=True, run=run)
            state, description = "success", "Full verification passed for this exact commit"
        except Refused as error:
            state = "failure" if run.get("status") == "completed" else "pending"
            description = str(error)
        return post_current(api, sha, state, description, run)
    if run["id"] != run_id or run["run_attempt"] != attempt:
        return "superseded run attempt; no status written"
    if mode == "announce":
        validate_pr(api, number, sha, ref)
        return post_current(api, sha, "pending", "Explicit full verification is running", run)
    jobs = jobs_for(api, run)
    success = jobs_pass(jobs)
    return post_current(api, sha, "success" if success else "failure",
                "Full verification passed for this exact commit" if success
                else "Full verification failed; inspect every mandatory job", run)


def release_evidence(api, sha, ref, event, requested_version):
    require(event in {"push", "workflow_dispatch"} and ref == "refs/heads/main",
            "publication requires a main push or main dispatch")
    require(api.request("git/ref/heads/main")["object"]["sha"] == sha,
            "publication SHA is no longer the main tip")
    candidates = [pr for pr in api.pages(f"commits/{sha}/pulls")
                  if pr.get("merged_at") and pr.get("merge_commit_sha") == sha
                  and pr.get("base", {}).get("ref") == "main"
                  and pr.get("head", {}).get("repo", {}).get("full_name") == api.repo
                  and pr.get("base", {}).get("repo", {}).get("full_name") == api.repo]
    require(len(candidates) == 1, "release must identify exactly one merged same-repository PR")
    head = candidates[0]["head"]["sha"]
    run = evidence(api, head)
    tree = tree_at(api, sha)
    require(tree_at(api, head) == tree, "released tree differs from the verified PR head")
    version = version_at(api, sha)
    if event == "workflow_dispatch":
        require(requested_version == version, "requested version differs from main VERSION")
    return {"sha": sha, "verified_sha": head, "tree": tree, "version": version,
            "run_id": run["id"], "run_attempt": run["run_attempt"], "run_url": run_url(api, run)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "initialize", "announce", "complete", "reconcile", "record", "release"))
    args = parser.parse_args(argv)
    env = os.environ
    api = GitHub(env["GITHUB_REPOSITORY"], env["GH_TOKEN"])
    sha = env.get("SUBJECT_SHA", env["GITHUB_SHA"])
    require(SHA.fullmatch(sha), "invalid subject SHA")
    number = env.get("PR_NUMBER", "0")
    run_id = int(env.get("CERTIFICATION_RUN_ID") or env["GITHUB_RUN_ID"])
    attempt = int(env.get("CERTIFICATION_RUN_ATTEMPT") or env["GITHUB_RUN_ATTEMPT"])
    if args.mode in {"initialize", "announce", "complete", "reconcile"}:
        print(update_status(api, args.mode, sha, number, run_id, attempt, env["GITHUB_REF"]))
        return
    if args.mode == "prepare":
        require(env["GITHUB_EVENT_NAME"] == "workflow_dispatch", "certification is manual only")
        require(sha == env["GITHUB_WORKFLOW_SHA"], "dispatch and workflow revisions differ")
        validate_pr(api, number, sha, env["GITHUB_REF"])
        run = api.request(f"actions/runs/{run_id}")
        validate_run(api, run, sha, api.request("actions/workflows/test.yml")["id"])
        result = {"sha": sha, "tree": tree_at(api, sha), "version": version_at(api, sha)}
    elif args.mode == "record":
        # Checkout-only path; privileged status writers remain a standalone file.
        from test_shards import load_receipts, validate_artifacts
        validate_artifacts(api.pages(f"actions/runs/{run_id}/artifacts", "artifacts"), run_id, attempt)
        coverage = load_receipts("shard-receipts", sha, run_id, attempt)
        run = api.request(f"actions/runs/{run_id}")
        result = {"sha": sha, "tree": tree_at(api, sha), "version": version_at(api, sha),
                  "run_id": run_id, "run_attempt": attempt, "workflow": WORKFLOW,
                  "workflow_sha": env["GITHUB_WORKFLOW_SHA"], "shard_coverage": coverage,
                  "jobs": {name: job.get("conclusion") for name, job in jobs_for(api, run).items()
                           if name in REQUIRED}}
        Path("verification.json").write_text(json.dumps(result, indent=2) + "\n")
    else:
        result = release_evidence(api, sha, env["GITHUB_REF"], env["GITHUB_EVENT_NAME"],
                                  env.get("REQUESTED_VERSION", ""))
    print(json.dumps(result, indent=2))
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key, value in result.items():
                if isinstance(value, (str, int)):
                    output.write(f"{key}={value}\n")


if __name__ == "__main__":
    try:
        main()
    except (Refused, urllib.error.URLError, KeyError, ValueError) as error:
        print(f"verification: {error}", file=sys.stderr)
        sys.exit(1)
