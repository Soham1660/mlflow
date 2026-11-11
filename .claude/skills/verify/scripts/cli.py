import argparse
import ast
import html
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILLS_DIR = SKILL_DIR.parent
SCHEMA = SKILL_DIR / "payload.schema.yml"
TRUSTED_BOTS = {"copilot-swe-agent", "mlflow-app"}
HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
Side = Literal["LEFT", "RIGHT"]

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      title body state headRefOid
      author { login __typename }
      reviewThreads(last: 50) {
        nodes {
          path line originalLine diffSide isResolved isOutdated
          comments(last: 25) {
            nodes { author { login } body createdAt url }
          }
        }
      }
    }
  }
}
"""


def command(*args: str | Path, input: str | None = None) -> str:
    return subprocess.check_output(args, input=input, text=True, stderr=subprocess.PIPE)


def api(*args: str, data: dict[str, Any] | None = None) -> Any:
    if data is not None:
        args += ("--method", "POST", "--input", "-")
    result = command("gh", "api", *args, input=json.dumps(data) if data is not None else None)
    return json.loads(result) if result.strip() else None


def write_step_summary(message: str) -> None:
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a") as stream:
            stream.write(message + "\n")
    else:
        sys.stderr.write(message + "\n")


def write_step_outputs(**values: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a") as stream:
            stream.writelines(f"{k}={v}\n" for k, v in values.items())


def collect(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"/verify\s*", args.comment, re.ASCII):
        sys.stdout.write("Not a /verify command.\n")
        return 0
    owner, name = args.repo.split("/")
    response = api(
        "graphql",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"number={args.pr_number}",
        "-f",
        f"query={PR_QUERY}",
    )
    pr = response["data"]["repository"]["pullRequest"]
    if pr["state"] != "OPEN":
        raise ValueError("PR is not open")
    author = pr["author"] or {}
    match author.get("__typename"):
        case "User":
            permission = api(f"repos/{args.repo}/collaborators/{author['login']}/permission")
            if permission["permission"] not in {"admin", "write"}:
                write_step_summary("The PR author does not have write access.")
                return 0
        case "Bot" if author["login"] in TRUSTED_BOTS:
            pass
        case _:
            write_step_summary("The PR author is not trusted.")
            return 0
    head = pr["headRefOid"]
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("Unexpected PR head SHA")
    context = {
        "title": pr["title"],
        "body": pr["body"],
        "review_threads": [
            thread | {"comments": thread["comments"]["nodes"]}
            for thread in pr["reviewThreads"]["nodes"]
        ],
    }
    args.out_path.write_text(json.dumps(context, indent=2) + "\n")
    write_step_outputs(head=head, allowed="true")
    return 0


def diff_path(header: str) -> str:
    path = header[4:].split("\t", 1)[0]
    if path.startswith('"'):
        path = ast.literal_eval(path)
    return path[2:]


def diff_hunks(diff: str) -> dict[str, list[dict[tuple[Side, int], int]]]:
    files: dict[str, list[dict[tuple[Side, int], int]]] = {}
    path = ""
    hunk: dict[tuple[Side, int], int] | None = None
    old = new = 0
    for position, line in enumerate(diff.splitlines()):
        if line.startswith("diff --git "):
            path = ""
            hunk = None
        elif hunk is None and line.startswith(("--- ", "+++ ")):
            if line[4:].split("\t", 1)[0] != "/dev/null":
                path = diff_path(line)
        elif match := HUNK_HEADER.match(line):
            old, new = map(int, match.groups())
            hunk = {}
            files.setdefault(path, []).append(hunk)
        elif hunk is not None:
            if line.startswith((" ", "-")):
                hunk["LEFT", old] = position
                old += 1
            if line.startswith((" ", "+")):
                hunk["RIGHT", new] = position
                new += 1
    return files


def validate_comments(payload: dict[str, Any], diff: str) -> list[str]:
    files = diff_hunks(diff)
    errors = []
    for index, comment in enumerate(payload["comments"], start=1):
        positions = [(comment["side"], comment["line"])]
        if "start_line" in comment:
            positions.insert(0, (comment["start_side"], comment["start_line"]))
        if not any(
            all(position in hunk for position in positions)
            and (len(positions) == 1 or hunk[positions[0]] < hunk[positions[-1]])
            for hunk in files.get(comment["path"], [])
        ):
            errors.append(f"Comment {index}: position or ordered range is not within one diff hunk")
    return errors


def media_command(media_dir: Path) -> tuple[str | Path, ...]:
    return (
        "uv",
        "run",
        "--quiet",
        "--directory",
        SKILLS_DIR,
        "--package",
        "skills",
        "skills",
        "embed-media",
        "--dir",
        media_dir,
    )


def validate(args: argparse.Namespace) -> int:
    command("uvx", "check-jsonschema@0.37.4", "--schemafile", SCHEMA, args.payload_path)
    payload = json.loads(args.payload_path.read_text())
    # The diff stays on the verification runner, so the publisher skips the position check.
    if args.diff_path is not None:
        if errors := validate_comments(payload, args.diff_path.read_text()):
            raise ValueError("\n".join(errors))
    command(
        *media_command(args.media_dir),
        "--check",
        input="\n\n".join(comment["body"] for comment in payload["comments"]),
    )
    sys.stdout.write("Review payload is valid.\n")
    return 0


def embed(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload_path.read_text())
    for comment in payload["comments"]:
        comment["body"] = command(
            *media_command(args.media_dir),
            "--repository-id",
            os.environ["REPOSITORY_ID"],
            input=comment["body"],
        )
    args.payload_path.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


def publish(args: argparse.Namespace) -> int:
    endpoint = f"repos/{args.repo}/pulls/{args.pr_number}"
    if api(endpoint)["state"] != "open":
        write_step_summary("PR closed; no review will be posted.")
        return 0
    notice = f"> [Verification run]({args.run_url})."
    # Without a payload, verification did not produce a publishable review.
    if args.payload_path is not None:
        payload = json.loads(args.payload_path.read_text())
        review = {
            "commit_id": args.head_sha,
            "event": payload["event"],
            "body": f"{notice}\n\n{payload['body']}",
            "comments": payload["comments"],
        }
    else:
        review = {
            "commit_id": args.head_sha,
            "event": "COMMENT",
            "body": f"{notice}\n\nVerification did not complete. See the workflow run for details.",
        }
    if args.dry_run:
        preview = html.escape(json.dumps(review, indent=2, ensure_ascii=False))
        write_step_summary(
            "### Verification preview\n"
            "No review or media was published. Local media paths are not GitHub links.\n"
            f"<pre>{preview}</pre>"
        )
    else:
        sys.stdout.write(api(f"{endpoint}/reviews", data=review)["html_url"] + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify workflow commands")
    commands = parser.add_subparsers(dest="command", required=True)
    collection = commands.add_parser("collect", help="Authorize the request and snapshot the PR")
    collection.add_argument("--repo", required=True)
    collection.add_argument("--pr-number", required=True)
    collection.add_argument("--comment", required=True)
    collection.add_argument("--out-path", type=Path, required=True)
    collection.set_defaults(func=collect)
    validation = commands.add_parser("validate", help="Validate a review payload")
    validation.add_argument("--payload", dest="payload_path", type=Path, required=True)
    validation.add_argument("--media-dir", type=Path, required=True)
    validation.add_argument("--diff", dest="diff_path", type=Path)
    validation.set_defaults(func=validate)
    embedding = commands.add_parser("embed", help="Upload cited media and rewrite its links")
    embedding.add_argument("--payload", dest="payload_path", type=Path, required=True)
    embedding.add_argument("--media-dir", type=Path, required=True)
    embedding.set_defaults(func=embed)
    publication = commands.add_parser("publish", help="Post or preview the review")
    publication.add_argument("--repo", required=True)
    publication.add_argument("--pr-number", required=True)
    publication.add_argument("--head-sha", required=True)
    publication.add_argument("--run-url", required=True)
    publication.add_argument("--payload", dest="payload_path", type=Path)
    publication.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    publication.set_defaults(func=publish)
    args = parser.parse_args()
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except subprocess.CalledProcessError as error:
        sys.stderr.write((error.stdout or "") + (error.stderr or str(error)) + "\n")
        return 1
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        sys.stderr.write(f"Verify failed: {error}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
