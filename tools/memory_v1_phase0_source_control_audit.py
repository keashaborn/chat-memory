#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path


PRODUCTION = "catalog_exercise_holds_defaultlog_v0"
V5 = "memory_v1_extraction_v2"
PHASE0 = "memory_v1_phase0_stabilization"
FOUNDATION = "memory_v1_foundation"
RESSE = "resse_policy_v0_1"


def command(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def left_right(repo: Path, left: str, right: str) -> dict[str, int]:
    values = command(repo, "rev-list", "--left-right", "--count", f"{left}...{right}").split()
    return {"left_only": int(values[0]), "right_only": int(values[1])}


def ancestor(repo: Path, older: str, newer: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"merge-base failed: {older} {newer}")
    return result.returncode == 0


def cherry(repo: Path, upstream: str, head: str) -> tuple[dict[str, int], list[str]]:
    lines = command(repo, "cherry", upstream, head).splitlines()
    counts = Counter(line[0] for line in lines if line)
    plus = [line.split()[1] for line in lines if line.startswith("+")]
    return {"missing_patch": counts["+"], "patch_equivalent": counts["-"]}, plus


def worktrees(repo: Path) -> list[dict[str, object]]:
    blocks = command(repo, "worktree", "list", "--porcelain").split("\n\n")
    values: list[dict[str, object]] = []
    for block in blocks:
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        path = Path(fields["worktree"])
        dirty = command(path, "status", "--porcelain=v1").splitlines()
        values.append(
            {
                "path": str(path),
                "head": fields["HEAD"],
                "branch": fields.get("branch", "").removeprefix("refs/heads/") or None,
                "dirty_path_count": len(dirty),
            }
        )
    return values


def merge_simulation(repo: Path, left: str, right: str) -> dict[str, object]:
    base = command(repo, "merge-base", left, right)
    output = command(repo, "merge-tree", base, left, right)
    conflict_files: set[str] = set()
    current_file: str | None = None
    marker_starts = 0
    header_count = 0
    for line in output.splitlines():
        if line in {"added in both", "changed in both", "removed in local", "removed in remote"}:
            current_file = None
            header_count += 1
            continue
        if current_file is None and line.startswith("  our    "):
            current_file = line.split(maxsplit=3)[-1]
            continue
        if line.startswith("+<<<<<<< "):
            marker_starts += 1
            if current_file:
                conflict_files.add(current_file)
    status_counts = Counter()
    for line in command(repo, "diff", "--name-status", f"{left}..{right}").splitlines():
        if line:
            status_counts[line[0]] += 1
    return {
        "left": left,
        "right": right,
        "merge_base": base,
        "ancestry_delta": left_right(repo, left, right),
        "diff_path_count": sum(status_counts.values()),
        "diff_status_counts": dict(sorted(status_counts.items())),
        "overlap_header_count": header_count,
        "conflict_marker_start_count": marker_starts,
        "conflict_files": sorted(conflict_files),
    }


def blob(repo: Path, revision: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{revision}:{path}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/opt/chat-memory")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")

    tree_rows = worktrees(repo)
    if any(row["dirty_path_count"] for row in tree_rows):
        raise RuntimeError("all worktrees must be clean")

    remote_url = command(repo, "config", "--get", "remote.origin.url")
    remote_url = re.sub(r"(https?://)[^/@]+@", r"\1***@", remote_url)
    remote_heads = {}
    for line in command(repo, "ls-remote", "--heads", "origin", PRODUCTION, "main").splitlines():
        sha, ref = line.split()
        remote_heads[ref.removeprefix("refs/heads/")] = sha

    branch_rows = []
    branch_names = command(
        repo, "for-each-ref", "refs/heads", "--sort=refname", "--format=%(refname:short)"
    ).splitlines()
    for branch in branch_names:
        prod_patch, _ = cherry(repo, PRODUCTION, branch)
        v5_patch, _ = cherry(repo, V5, branch)
        tip = command(repo, "show", "-s", "--format=%H%x09%cs%x09%s", branch).split("\t", 2)
        branch_rows.append(
            {
                "branch": branch,
                "head": tip[0],
                "tip_date": tip[1],
                "tip_subject": tip[2],
                "against_production": left_right(repo, PRODUCTION, branch),
                "patches_against_production": prod_patch,
                "patches_against_v5": v5_patch,
                "ancestor_of_production": ancestor(repo, branch, PRODUCTION),
                "ancestor_of_v5": ancestor(repo, branch, V5),
            }
        )

    _, foundation_plus = cherry(repo, PRODUCTION, FOUNDATION)
    foundation_commits = []
    foundation_paths: set[str] = set()
    for commit in foundation_plus:
        fields = command(repo, "show", "-s", "--format=%H%x09%cs%x09%s", commit).split("\t", 2)
        foundation_commits.append({"commit": fields[0], "date": fields[1], "subject": fields[2]})
        foundation_paths.update(
            item
            for item in command(repo, "show", "--pretty=format:", "--name-only", commit).splitlines()
            if item
        )
    path_rows = []
    for path in sorted(foundation_paths):
        foundation_blob = blob(repo, FOUNDATION, path)
        v5_blob = blob(repo, V5, path)
        production_blob = blob(repo, PRODUCTION, path)
        if foundation_blob == v5_blob:
            v5_state = "identical"
        elif v5_blob is None:
            v5_state = "missing"
        else:
            v5_state = "different"
        if foundation_blob == production_blob:
            production_state = "identical"
        elif production_blob is None:
            production_state = "missing"
        else:
            production_state = "different"
        path_rows.append(
            {
                "path": path,
                "v5_state": v5_state,
                "production_state": production_state,
            }
        )

    production_head = command(repo, "rev-parse", PRODUCTION)
    origin_production = command(repo, "rev-parse", f"origin/{PRODUCTION}")
    if remote_heads.get(PRODUCTION) != origin_production:
        raise RuntimeError("local origin production ref is stale")
    if command(repo, "status", "--porcelain=v1"):
        raise RuntimeError("production worktree must be clean")

    unpushed = command(
        repo,
        "log",
        "--reverse",
        "--format=%H%x09%cs%x09%s",
        f"origin/{PRODUCTION}..{PRODUCTION}",
    ).splitlines()
    value = {
        "contract_version": "memory_v1_phase0_source_control_reconciliation_v1",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "effects": {"git_mutations": 0, "database_writes": 0, "qdrant_writes": 0, "model_calls": 0},
        "repo": str(repo),
        "origin": {"url": remote_url, "verified_heads": remote_heads},
        "production": {
            "branch": PRODUCTION,
            "head": production_head,
            "origin_head": origin_production,
            "ahead_of_origin": len(unpushed),
            "oldest_unpushed": unpushed[0] if unpushed else None,
            "newest_unpushed": unpushed[-1] if unpushed else None,
        },
        "worktrees": tree_rows,
        "branches": branch_rows,
        "key_deltas": {
            "production_to_v5": left_right(repo, PRODUCTION, V5),
            "production_to_phase0": left_right(repo, PRODUCTION, PHASE0),
            "v5_to_phase0": left_right(repo, V5, PHASE0),
            "production_patches_in_v5": cherry(repo, V5, PRODUCTION)[0],
            "v5_patches_in_production": cherry(repo, PRODUCTION, V5)[0],
        },
        "merge_simulations": [
            merge_simulation(repo, PHASE0, V5),
            merge_simulation(repo, PHASE0, FOUNDATION),
            merge_simulation(repo, V5, FOUNDATION),
        ],
        "foundation_provenance": {
            "unique_patch_count": len(foundation_plus),
            "unique_commits": foundation_commits,
            "path_count": len(path_rows),
            "path_state_counts_against_v5": dict(sorted(Counter(row["v5_state"] for row in path_rows).items())),
            "paths": path_rows,
        },
        "classification": {
            "already_integrated_by_patch": [
                "memory_v1_evidence_ingest_audit_deploy",
                "memory_v1_evidence_lifecycle_deploy",
                "memory_v1_governance_scheduler",
                "memory_v1_owner_consolidation",
                "memory_v1_selector_legacy_retirement",
            ],
            "canonical_integration_base": PHASE0,
            "v5_status": "101_non_equivalent_patches_to_integrate_after_backup",
            "foundation_status": "21_non_equivalent_patches_require_provenance_preservation_and_semantic_resolution",
            "resse_status": "one_deliberately_separate_policy_commit_hold_for_runtime_boundary_review",
        },
        "recommended_sequence": [
            "push each clean local branch to a namespaced remote backup before integration",
            "create a new integration branch and worktree from memory_v1_phase0_stabilization",
            "merge memory_v1_extraction_v2 and resolve its sole conflict with the evolved V5 clone suite, then rerun clone tests",
            "merge memory_v1_foundation only after classifying its missing provenance artifacts; retain schema/docs/manifests, archive retired experimental runtime files, and prefer reviewed V5 active runtime paths",
            "keep resse_policy_v0_1 separate until the memory/RESSE runtime boundary review",
            "stop before changing the production branch or activating retrieval",
        ],
        "status": "audit_complete_stop_before_merge_rebase_or_push",
    }
    value["audit_payload_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    sidecar = Path(f"{output}.sha256")
    sidecar.write_text(f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output}\n", encoding="utf-8")
    print("memory_v1_phase0_source_control_audit: PASS")
    print(f"report={output}")
    print(f"sha256={hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
