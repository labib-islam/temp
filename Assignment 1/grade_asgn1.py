
#!/usr/bin/env python3
"""
Automatic marking for Assignment 1 (branch + doc/profiles.md per student), 0/1/2 scale.

Usage:
  python grade_asgn1.py --org MY_ORG --grades a1-grades.csv \
      --deadline 2026-09-19T23:59:00-02:30 --out grades.csv

```
python3 grade_asgn1.py --org COMP6905F26 --grades a1-grades.csv
```

a1-grades.csv is the Brightspace/D2L grade-export CSV for this course. Required
columns (matched by prefix, so exact MaxPoints/Weight/Category suffixes don't
matter):
  OrgDefinedId, Username, Last Name, First Name,
  GitHub ID Text Grade <Text>, Team ID Text Grade <Text>,
  Assignment 1 Points Grade <Numeric ...>, End-of-Line Indicator

Username is the student's mun login name. Only a Team ID is provided (a single
letter like "A"), so the team's repo name is derived as f"Asgn1Team{TeamID}".

Requires the GitHub CLI (`gh auth login`) with read access to every team repo.

Outputs:
  grades.csv   same columns as a1-grades.csv, with "Assignment 1 Points Grade"
               and a new "Feedback" column (inserted before End-of-Line
               Indicator) filled in
  profiles/    a copy of each student's doc/profiles.md for manual review

Mark rules (edit final_mark() to change them):
  Start at 2 (MaxPoints:2) and deduct 1 for each of these faults:
    - no branch named {munname}-asgn1 in the team's repo
    - no commit authored by the student on that branch before the deadline
    - no commit authored by the student on that branch that touched
      doc/profiles.md
  A student with at least one on-time commit of theirs touching
  doc/profiles.md on a correctly-named branch gets full marks (2).
"""
import argparse
import base64
import csv
import json
import os
import subprocess
from datetime import datetime
from urllib.parse import quote

DEFAULT_DEADLINE = "2026-09-19T23:59:00-02:30"


def check_gh_auth():
    """Verify the GitHub CLI is authenticated before we run dozens of API calls on faith."""
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


def is_mine(commit, mun, gh_user):
    a = commit["commit"]["author"]
    login = ((commit.get("author") or {}).get("login") or "").lower()
    return (
        (gh_user and login == gh_user.lower())
        or (mun.lower() in (a.get("email") or "").lower())
        or (mun.lower() in (a.get("name") or "").lower())
    )


def final_mark(branch_fault, on_time_fault, content_fault):
    faults = sum([branch_fault, on_time_fault, content_fault])
    return max(0, 2 - faults)


def grade_student(org, repo, mun, gh_user, deadline):
    branch = f"{mun}-asgn1"
    notes = []

    # 1. Branch exists with the exact expected name
    branch_info = gh_json(f"repos/{org}/{repo}/branches/{quote(branch)}")
    branch_fault = branch_info is None
    if branch_fault:
        near = [b for b in gh_lines(f"repos/{org}/{repo}/branches", ".[].name")
                if mun.lower() in b.lower()]
        msg = f"No branch named '{branch}' found (expected format {{munname}}-asgn1)."
        if near:
            msg += f" Did you mean: {', '.join(near)}?"
        notes.append(msg)

    # 2. At least one commit authored by the student on that branch, before the deadline
    on_time_fault = True
    if not branch_fault:
        commits = gh_json_array(f"repos/{org}/{repo}/commits?sha={quote(branch)}") or []
        mine = [c for c in commits if is_mine(c, mun, gh_user)]
        if not mine:
            notes.append("No commit on that branch is authored by you "
                         "(check that git is configured with your mun login name).")
        else:
            on_time = [c for c in mine
                       if parse_time(c["commit"]["committer"]["date"]) <= deadline]
            if on_time:
                on_time_fault = False
            else:
                notes.append("Your commits on that branch were all made after the deadline.")

    # 3. At least one commit authored by the student that touched doc/profiles.md
    content_fault = True
    if not branch_fault:
        profile_commits = gh_json_array(
            f"repos/{org}/{repo}/commits?sha={quote(branch)}&path=doc/profiles.md"
        ) or []
        mine_profile = [c for c in profile_commits if is_mine(c, mun, gh_user)]
        if mine_profile:
            content_fault = False
        else:
            notes.append("No commit of yours on that branch touched doc/profiles.md.")

    mark = final_mark(branch_fault, on_time_fault, content_fault)
    if mark == 2:
        notes.append("On time, correct branch, doc/profiles.md updated -- full marks.")

    # Fetch the file (if any) for manual review, regardless of pass/fail
    text = None
    if not branch_fault:
        f = gh_json(f"repos/{org}/{repo}/contents/doc/profiles.md?ref={quote(branch)}")
        if f and "content" in f:
            text = base64.b64decode(f["content"]).decode("utf-8", errors="replace")

    return mark, " | ".join(notes), text


REQUIRED_COLUMN_PREFIXES = {
    "orgid": "OrgDefinedId",
    "username": "Username",
    "lastname": "Last Name",
    "firstname": "First Name",
    "github": "GitHub ID",
    "teamid": "Team ID",
    "grade": "Assignment 1 Points Grade",
    "eol": "End-of-Line Indicator",
}


def resolve_columns(fieldnames):
    cols = {}
    for key, prefix in REQUIRED_COLUMN_PREFIXES.items():
        match = next((c for c in fieldnames if c.startswith(prefix)), None)
        if not match:
            raise SystemExit(
                f"ERROR: could not find a column starting with '{prefix}' in a1-grades.csv.\n"
                f"Columns found: {list(fieldnames)}"
            )
        cols[key] = match
    return cols


def open_grades(path):
    """Excel's 'CSV' export (as opposed to 'CSV UTF-8') often saves as
    Windows-1252, which breaks on curly quotes/apostrophes in names when read
    as UTF-8. Try UTF-8 first (the correct case), fall back to cp1252."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as fh:
                rows = list(csv.DictReader(fh))
            if enc != "utf-8-sig":
                print(f"NOTE: {path} was not valid UTF-8; read it as {enc} instead. "
                      f"Consider re-saving it as 'CSV UTF-8' to avoid this.")
            cols = resolve_columns(rows[0].keys() if rows else [])
            return rows, cols
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"ERROR: could not read {path} as UTF-8 or cp1252. Check its encoding.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--grades", default="a1-grades.csv")
    ap.add_argument("--deadline", default=DEFAULT_DEADLINE, help="ISO 8601 with UTC offset")
    ap.add_argument("--out", default="grades.csv")
    args = ap.parse_args()

    check_gh_auth()

    deadline = parse_time(args.deadline)
    rows, cols = open_grades(args.grades)
    os.makedirs("profiles", exist_ok=True)

    out_fields = [
        cols["orgid"], cols["username"], cols["lastname"], cols["firstname"],
        cols["github"], cols["teamid"], cols["grade"], "Feedback", cols["eol"],
    ]

    with open(args.out, "w", newline="") as out:
        w = csv.DictWriter(out, fieldnames=out_fields)
        w.writeheader()
        for row in rows:
            mun = row[cols["username"]].strip()
            gh_user = row[cols["github"]].strip()
            team_id = row[cols["teamid"]].strip()
            display = f"{row[cols['firstname']].strip()} {row[cols['lastname']].strip()}"

            if not mun or not gh_user or not team_id:
                mark, notes, text = 0, (
                    "No GitHub ID and/or Team ID on file (quiz not completed) "
                    "-- unable to locate a repository to grade."
                ), None
                repo = None
            else:
                repo = f"Asgn1Team{team_id}"
                mark, notes, text = grade_student(args.org, repo, mun, gh_user, deadline)

            row_out = dict(row)
            row_out[cols["grade"]] = mark
            row_out["Feedback"] = notes
            row_out[cols["eol"]] = "#"
            w.writerow({k: row_out.get(k, "") for k in out_fields})

            if text:
                with open(f"profiles/{repo}__{mun}.md", "w") as p:
                    p.write(text)

            print(f"{(repo or '-'):<16} {mun or display:<16} mark={mark}  {notes}")


if __name__ == "__main__":
    main()
