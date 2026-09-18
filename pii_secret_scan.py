#!/usr/bin/env python3
"""
Scan every git repo under an input folder with gitleaks (full history,
all branches) using gitleaks_pii.toml (default secret rules + custom PII
rules), then write one CSV of findings per repo.

Usage:
    python3 pii_secret_scan.py --input /path/to/folder --output /path/to/csv_dir
    python3 pii_secret_scan.py --input /path/to/single/repo --output /path/to/csv_dir

A repo is any directory containing a .git entry. The input folder is walked
recursively; once a repo is found, its subtree is not descended into further
(so nested/submodule .git dirs aren't double-scanned).

pii-credit-card candidates from gitleaks are validated with a Luhn checksum
here, since the regex alone (13-19 digit run) matches a lot of non-card
numbers (hashes, coordinates, IDs).
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "gitleaks_pii.toml")

SKIP_DIRNAMES = {"node_modules", "venv", ".venv", "__pycache__", ".tox", ".mypy_cache"}

CSV_FIELDS = [
    "RuleID", "Description", "Tags", "File", "StartLine", "EndLine",
    "Commit", "Author", "Email", "Date", "Match", "Secret", "Fingerprint", "Link",
]


def discover_repos(root):
    root = os.path.abspath(root)
    if os.path.exists(os.path.join(root, ".git")):
        return [root]

    repos = []
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=True):
        if os.path.exists(os.path.join(dirpath, ".git")):
            repos.append(dirpath)
            dirnames[:] = []  # don't descend into a repo we already found
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRNAMES]
    return sorted(repos)


def luhn_valid(digits):
    digits = [int(c) for c in digits]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def run_gitleaks(gitleaks_bin, repo_path, config_path):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report_path = tmp.name

    cmd = [
        gitleaks_bin, "detect",
        "--source", repo_path,
        "--config", config_path,
        "--log-opts=--all",
        "--report-format", "json",
        "--report-path", report_path,
        "--no-banner",
        "--exit-code", "0",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    findings = []
    if os.path.exists(report_path) and os.path.getsize(report_path) > 0:
        with open(report_path) as f:
            findings = json.load(f)
    os.unlink(report_path)

    if proc.returncode != 0:
        print(f"  [warn] gitleaks exited {proc.returncode} for {repo_path}: {proc.stderr.strip()[:300]}",
              file=sys.stderr)

    return findings


def filter_findings(findings):
    kept = []
    dropped_cc = 0
    for f in findings:
        if f.get("RuleID") == "pii-credit-card":
            digits = "".join(ch for ch in f.get("Secret", "") if ch.isdigit())
            if not luhn_valid(digits):
                dropped_cc += 1
                continue
        kept.append(f)
    return kept, dropped_cc


def repo_output_name(repo_path, input_root):
    rel = os.path.relpath(repo_path, input_root)
    if rel == ".":
        rel = os.path.basename(repo_path)
    return rel.replace(os.sep, "__")


def write_csv(findings, out_path):
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for finding in findings:
            row = dict(finding)
            tags = row.get("Tags")
            if isinstance(tags, list):
                row["Tags"] = ";".join(tags)
            writer.writerow(row)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="Folder to scan (a single repo, or a folder containing repos)")
    ap.add_argument("--output", required=True, help="Folder to write one CSV per repo into")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"gitleaks TOML config (default: {DEFAULT_CONFIG})")
    ap.add_argument("--gitleaks-bin", default="gitleaks", help="Path to gitleaks binary (default: 'gitleaks' on PATH)")
    args = ap.parse_args()

    input_root = os.path.abspath(args.input)
    os.makedirs(args.output, exist_ok=True)

    repos = discover_repos(input_root)
    if not repos:
        print(f"No git repos found under {input_root}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(repos)} repo(s) under {input_root}")

    summary_rows = []
    for repo in repos:
        name = repo_output_name(repo, input_root)
        print(f"Scanning {name} ...")
        raw = run_gitleaks(args.gitleaks_bin, repo, args.config)
        kept, dropped_cc = filter_findings(raw)

        out_csv = os.path.join(args.output, f"{name}.csv")
        write_csv(kept, out_csv)

        rule_counts = {}
        for f in kept:
            rule_counts[f["RuleID"]] = rule_counts.get(f["RuleID"], 0) + 1
        print(f"  {len(kept)} findings written to {out_csv}"
              f" (dropped {dropped_cc} pii-credit-card false positives)")
        for rule, count in sorted(rule_counts.items(), key=lambda x: -x[1]):
            print(f"    {count:4d}  {rule}")

        summary_rows.append({
            "repo": name, "path": repo, "total_findings": len(kept),
            "dropped_credit_card_fp": dropped_cc, **rule_counts,
        })

    summary_path = os.path.join(args.output, "_summary.csv")
    all_rule_ids = sorted({k for row in summary_rows for k in row if k not in
                            ("repo", "path", "total_findings", "dropped_credit_card_fp")})
    with open(summary_path, "w", newline="") as f:
        fieldnames = ["repo", "path", "total_findings", "dropped_credit_card_fp"] + all_rule_ids
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow(row)
    print(f"\nSummary written to {summary_path}")


if __name__ == "__main__":
    main()
