
#!/usr/bin/env python3
"""
Automatic marking for Assignment 1 (branch + doc/profiles.md per student), 0/1/2 scale.

Usage:
  python grade_asgn1.py --org MY_ORG --roster roster.csv \
      --deadline 2026-09-30T23:59:00-02:30 --out grades.csv \
      [--template template_profiles.md]

```
python3 grade_asgn1.py --org COMP6905F26 --roster roster.csv \
    --deadline 2026-09-19T23:59:00-02:30 --out grades.csv
```
roster.csv columns (header required):
  repo,student,munname,github

Requires the GitHub CLI (`gh auth login`) with read access to every team repo.
Outputs:
  grades.csv   one row per student: each check (True/False), mark (0/1/2), notes
  profiles/    a copy of each student's profiles.md for manual review

Mark rules (edit final_mark() to change them):
  2 = branch named {munname}-asgn1, authored by the student, doc/profiles.md
      present with a filled-in profile, pushed on time
  1 = something was submitted but at least one of those checks failed
  0 = no correctly named branch, no doc/profiles.md, or file is the untouched template
"""
import argparse
import base64
import csv
import json
import os
import re
import subprocess
from datetime import datetime
from urllib.parse import quote

CHECKS = ["branch_name", "authorship", "file_path", "content", "on_time"]


def check_gh_auth():
    """Verify the GitHub CLI is authenticated before we run 60 API calls on faith."""
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "ERROR: `gh auth status` failed -- the GitHub CLI is not authenticated.\n"
            "Run `gh auth login` and try again.\n\n"
            f"{r.stderr or r.stdout}"
        )


def gh_json(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def gh_json_array(path):
    """Like gh_json, but for endpoints that return a JSON array and may be
    paginated. `gh api --paginate` normally concatenates pages into one
    array; fall back to stitching separate arrays together if not."""
    r = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        try:
            arrays = json.loads("[" + r.stdout.strip().replace("][", "],[") + "]")
            return [item for arr in arrays for item in arr]
        except Exception:
            return None


def gh_lines(path, jq):
    r = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", jq],
        capture_output=True, text=True,
    )
    return r.stdout.split() if r.returncode == 0 else []


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def final_mark(res):
    """Combine the pass/fail checks into 0/1/2."""
    if not res["branch_name"] or not res["file_path"]:
        return 0
    if all(res.values()):
        return 2
    return 1


def check_student(org, row, deadline, template):
    repo, mun, gh_user = row["repo"], row["munname"].strip(), row["github"].strip()
    branch = f"{mun}-asgn1"
    res = {k: False for k in CHECKS}
    notes = []

    # 1. Branch exists with the exact name
    info = gh_json(f"repos/{org}/{repo}/branches/{quote(branch)}")
    if not info:
        near = [b for b in gh_lines(f"repos/{org}/{repo}/branches", ".[].name")
                if mun.lower() in b.lower()]
        notes.append(
            f"branch '{branch}' not found"
            + (f"; similar branches: {', '.join(near)}" if near else "")
        )
        return res, notes, None
    res["branch_name"] = True

    # 2. On time (committer date is set client-side; see caveats)
    last = parse_time(info["commit"]["commit"]["committer"]["date"])
    if last <= deadline:
        res["on_time"] = True
    else:
        notes.append(f"last commit {last.isoformat()} is after deadline")

    # 3. Authorship of the commits that touched doc/profiles.md on this branch.
    #    NOTE: this used to diff the branch against the default branch, but that
    #    breaks the moment a PR gets merged (even though the brief says not to
    #    open one) -- once merged, the branch is no longer "ahead" of anything,
    #    the diff is empty, and authorship could never be verified. Walking the
    #    file's own commit history on the branch works regardless of merge state.
    commits = gh_json_array(
        f"repos/{org}/{repo}/commits?sha={quote(branch)}&path=doc/profiles.md"
    )
    if not commits:
        notes.append("no commit history found for doc/profiles.md on this branch")
    else:
        mine = 0
        for c in commits:
            a = c["commit"]["author"]
            login = ((c.get("author") or {}).get("login") or "").lower()
            if (login == gh_user.lower()
                    or mun.lower() in a["email"].lower()
                    or mun.lower() in a["name"].lower()):
                mine += 1
        if 0 < mine <= len(commits):
            res["authorship"] = True
        else:
            notes.append(
                f"only {mine}/{len(commits)} commits touching doc/profiles.md "
                "attributed to student (note: a squash-merged PR may show the "
                "merger, not the original author, as commit author -- check manually)"
            )

    # 4. doc/profiles.md on the branch
    f = gh_json(f"repos/{org}/{repo}/contents/doc/profiles.md?ref={quote(branch)}")
    if not f or "content" not in f:
        notes.append("doc/profiles.md missing on branch")
        return res, notes, None
    text = base64.b64decode(f["content"]).decode("utf-8", errors="replace")
    if template and text.strip() == template.strip():
        notes.append("doc/profiles.md is identical to the template (treated as not submitted)")
        return res, notes, text
    res["file_path"] = True

    # 5. Content sanity checks (structure only; quality is a manual/AI judgment)
    lines = text.splitlines()
    table_rows = [l for l in lines if re.match(r"^\s*\|.+\|\s*$", l)]
    has_sep = any(re.match(r"^\s*\|?\s*:?-{3,}", l) for l in lines)
    words = len(re.findall(r"\w+", text))
    problems = []
    if len(table_rows) < 2 or not has_sep:
        problems.append("no markdown table")
    if words < 40:
        problems.append(f"very short ({words} words)")
    if "<<<<<<<" in text:
        problems.append("merge conflict markers")
    if mun.lower() not in text.lower() and row["student"].split()[0].lower() not in text.lower():
        problems.append("student name/login not found in file")
    if problems:
        notes.append("content: " + "; ".join(problems))
    else:
        res["content"] = True

    # Informational: a PR was opened even though the brief said not to
    prs = gh_json(f"repos/{org}/{repo}/pulls?state=all&head={org}:{quote(branch)}")
    if prs:
        notes.append("PR opened (brief said not to) -- informational")

    return res, notes, text


REQUIRED_ROSTER_COLUMNS = ["repo", "student", "munname", "github"]


def open_roster(path):
    """Excel's 'CSV' export (as opposed to 'CSV UTF-8') often saves as
    Windows-1252, which breaks on curly quotes/apostrophes in names when read
    as UTF-8. Try UTF-8 first (the correct case), fall back to cp1252.
    Also sniffs the delimiter, since some regional Excel settings default to
    semicolons instead of commas -- with the wrong delimiter, DictReader reads
    the whole header line as one field and every row["repo"] lookup KeyErrors."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                except csv.Error:
                    dialect = csv.excel  # comma-delimited default
                rows = list(csv.DictReader(fh, dialect=dialect))
            if enc != "utf-8-sig":
                print(f"NOTE: {path} was not valid UTF-8; read it as {enc} instead. "
                      f"Consider re-saving it as 'CSV UTF-8' to avoid this.")
            fieldnames = rows[0].keys() if rows else []
            missing = [c for c in REQUIRED_ROSTER_COLUMNS if c not in fieldnames]
            if missing:
                raise SystemExit(
                    f"ERROR: {path} is missing required column(s): {', '.join(missing)}.\n"
                    f"Columns found: {list(fieldnames)}\n"
                    f"Expected header: {','.join(REQUIRED_ROSTER_COLUMNS)}\n"
                    "Check the delimiter (comma vs semicolon) and header spelling."
                )
            good, incomplete = [], []
            for row in rows:
                if not row["student"].strip() or not row["munname"].strip():
                    incomplete.append(row)
                else:
                    good.append(row)
            if incomplete:
                print(f"WARNING: {len(incomplete)} roster row(s) have a blank 'student' "
                      f"and/or 'munname' and will be SKIPPED (an empty munname is a "
                      f"substring of everything, which risks false-positive authorship "
                      f"matches -- better to skip than silently mis-grade):")
                for row in incomplete:
                    print(f"  repo={row['repo']!r} github={row['github']!r} "
                          f"student={row['student']!r} munname={row['munname']!r}")
                print()
            return good
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"ERROR: could not read {path} as UTF-8 or cp1252. Check its encoding.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--roster", required=True)
    ap.add_argument("--deadline", required=True, help="ISO 8601 with UTC offset")
    ap.add_argument("--out", default="grades.csv")
    ap.add_argument("--template")
    args = ap.parse_args()

    check_gh_auth()

    deadline = parse_time(args.deadline)
    if args.template:
        template = open(args.template).read()
    else:
        template = None
        print(
            "WARNING: no --template given. An untouched template file on a "
            "student's branch will NOT be auto-detected as 'not submitted' -- "
            "it will only be caught (maybe) by the word-count/table heuristics. "
            "Review the 'content' notes and the profiles/ folder carefully.\n"
        )
    os.makedirs("profiles", exist_ok=True)

    with open(args.out, "w", newline="") as out:
        w = csv.writer(out)
        w.writerow(["repo", "student", "munname", *CHECKS, "mark_0_1_2", "notes"])
        for row in open_roster(args.roster):
            res, notes, text = check_student(args.org, row, deadline, template)
            mark = final_mark(res)
            w.writerow([row["repo"], row["student"], row["munname"],
                        *[res[k] for k in CHECKS], mark, " | ".join(notes)])
            if text:
                with open(f"profiles/{row['repo']}__{row['munname']}.md", "w") as p:
                    p.write(text)
            print(f"{row['repo']:<20} {row['munname']:<12} mark={mark}  {' | '.join(notes)}")


if __name__ == "__main__":
    main()