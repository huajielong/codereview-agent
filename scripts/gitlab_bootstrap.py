"""Bootstrap the private GitLab project, push main, and configure CI variables.

Never prints secret values. The PAT is read from ``GITLAB_TOKEN``; the script
only reports key names and whether they are masked.

Examples::

    GITLAB_TOKEN=glpat-... python -I -X utf8 scripts/gitlab_bootstrap.py \
      --project huajielong/codereview-agent --push \
      --create-deploy-token

    GITLAB_TOKEN=glpat-... python -I -X utf8 scripts/gitlab_bootstrap.py \
      --project huajielong/codereview-agent \
      --target-project huajielong/demo \
      --from-env-file /tmp/codereview-ci.env \
      --create-deploy-token --set-review-token

The outward-facing actions (project creation, push, variables, deploy token)
are intentional; pass ``--yes`` to skip the confirmation prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

SECRET_HINTS = ("TOKEN", "KEY", "SECRET")


def _client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{base_url.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": token, "User-Agent": "codereview-agent/0.1"},
        timeout=30.0,
        # Manual/no redirects: PRIVATE-TOKEN must never be replayed to another origin.
        follow_redirects=False,
    )


def _check(response: httpx.Response, action: str) -> dict:
    if 300 <= response.status_code < 400:
        raise SystemExit(
            f"{action} failed: unexpected redirect HTTP {response.status_code}"
        )
    if response.status_code >= 400:
        try:
            message = response.json().get("message")
        except ValueError:
            message = response.text[:300]
        raise SystemExit(f"{action} failed: HTTP {response.status_code}: {message}")
    try:
        return response.json()
    except ValueError as exc:
        raise SystemExit(f"{action} returned non-JSON") from exc


def get_user(client: httpx.Client) -> dict:
    return _check(client.get("/user"), "GET /user")


def resolve_namespace(client: httpx.Client, user: dict, namespace: str) -> int:
    if namespace == user.get("username"):
        return int(user["id"])
    payload = _check(
        client.get("/namespaces", params={"search": namespace, "per_page": 100}),
        "GET /namespaces",
    )
    for item in payload if isinstance(payload, list) else []:
        if str(item.get("full_path", "")).lower() == namespace.lower():
            return int(item["id"])
    raise SystemExit(f"namespace not found: {namespace}")


def ensure_project(
    client: httpx.Client, project_path: str, visibility: str = "private"
) -> dict:
    encoded = quote(project_path, safe="")
    response = client.get(f"/projects/{encoded}")
    if response.status_code == 200:
        return response.json()
    if response.status_code != 404:
        _check(response, "GET project")

    user = get_user(client)
    namespace, _, name = project_path.rpartition("/")
    if not namespace:
        raise SystemExit("project must be namespace/name")
    body = {
        "name": name,
        "path": name,
        "visibility": visibility,
        "initialize_with_readme": False,
    }
    # GitLab.com currently rejects namespace_id for a personal namespace with
    # "undefined method 'organization_id' for nil"; omitting it lets the API
    # default to the authenticated user's namespace.
    if namespace.lower() != str(user.get("username", "")).lower():
        body["namespace_id"] = resolve_namespace(client, user, namespace)
    payload = _check(client.post("/projects", json=body), "POST /projects")
    actual = str(payload.get("path_with_namespace") or "")
    if actual.lower() != project_path.lower():
        raise SystemExit(
            f"project was created at {actual!r}, expected {project_path!r}"
        )
    print(f"created project {project_path} (visibility={visibility})")
    return payload


def set_variable(
    client: httpx.Client,
    project_id: int,
    key: str,
    value: str,
    *,
    masked: bool | None = None,
    protected: bool = False,
) -> None:
    if masked is None:
        masked = any(hint in key.upper() for hint in SECRET_HINTS)
    body = {
        "key": key,
        "value": value,
        "masked": masked,
        "protected": protected,
        "environment_scope": "*",
    }
    response = client.post(f"/projects/{project_id}/variables", json=body)
    if response.status_code == 400 and "already been taken" in response.text:
        response = client.put(f"/projects/{project_id}/variables/{key}", json=body)
    if response.status_code >= 400 and masked:
        # Some values cannot be masked (newlines / unsupported characters).
        body["masked"] = False
        response = client.put(f"/projects/{project_id}/variables/{key}", json=body)
        if response.status_code == 404:
            response = client.post(f"/projects/{project_id}/variables", json=body)
    _check(response, f"set variable {key}")
    print(f"variable {key}: masked={body['masked']} protected={protected}")


def create_deploy_token(
    client: httpx.Client, project_id: int, name: str = "codereview-agent-clone"
) -> dict | None:
    existing = _check(
        client.get(f"/projects/{project_id}/deploy_tokens"), "GET deploy tokens"
    )
    for token in existing if isinstance(existing, list) else []:
        if token.get("name") == name:
            print(
                f"deploy token {name} already exists; its value cannot be read again"
            )
            return None
    payload = _check(
        client.post(
            f"/projects/{project_id}/deploy_tokens",
            json={"name": name, "scopes": ["read_repository"]},
        ),
        "POST deploy token",
    )
    print(f"created deploy token {name} (value not printed)")
    return payload


def _run_git(repo_dir: Path, args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def push_main(repo_dir: Path, remote_url: str, token: str, branch: str = "main") -> None:
    env = os.environ.copy()
    env["GITLAB_TOKEN"] = token
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Git credential helper instead of an askpass file: the token stays in the
    # environment, not in argv or a temporary file. Git for Windows runs "!"
    # helpers with its bundled sh, so printf is available.
    helper = (
        '!f() { test "$1" = get && '
        'printf "username=oauth2\\npassword=%s\\n" "$GITLAB_TOKEN"; }; f'
    )

    remotes = _run_git(repo_dir, ["remote"], env)
    if "gitlab" in remotes.stdout.split():
        _run_git(repo_dir, ["remote", "set-url", "gitlab", remote_url], env)
    else:
        _run_git(repo_dir, ["remote", "add", "gitlab", remote_url], env)

    result = _run_git(
        repo_dir,
        ["-c", f"credential.helper={helper}", "push", "-u", "gitlab", branch],
        env,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").replace(token, "***")
        raise SystemExit(f"git push failed: {output[-2000:]}")
    print(f"pushed {branch} to {remote_url}")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the private GitLab project")
    parser.add_argument("--project", default="huajielong/codereview-agent")
    parser.add_argument("--base-url", default=os.environ.get("GITLAB_BASE_URL", "https://gitlab.com"))
    parser.add_argument("--visibility", choices=["private", "internal", "public"], default="private")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--push", action="store_true", help="push the branch to a gitlab remote")
    parser.add_argument(
        "--set-review-token",
        action="store_true",
        help="set CODEREVIEW_GITLAB_TOKEN on target projects",
    )
    parser.add_argument(
        "--review-token",
        help="project access token to write as CODEREVIEW_GITLAB_TOKEN "
        "(required with --set-review-token unless --allow-pat-as-review-token)",
    )
    parser.add_argument(
        "--review-token-file",
        help="file containing the review token (avoids exposing it in argv)",
    )
    parser.add_argument(
        "--allow-pat-as-review-token",
        action="store_true",
        help="explicitly allow the bootstrap PAT to be written as CODEREVIEW_GITLAB_TOKEN",
    )
    parser.add_argument(
        "--protected-variables",
        action="store_true",
        help="create CI variables as protected (unavailable to MR pipelines on "
        "unprotected branches; use only for protected-branch-only setups)",
    )
    parser.add_argument(
        "--target-project",
        action="append",
        default=[],
        help="project that receives CI variables (repeatable); defaults to the agent project",
    )
    parser.add_argument("--from-env-file", help="KEY=VALUE file of extra CI variables")
    parser.add_argument(
        "--create-deploy-token",
        action="store_true",
        help="create a read_repository deploy token for cloning the agent repo",
    )
    parser.add_argument(
        "--deploy-token-name",
        default="codereview-agent-clone",
        help="name for the read_repository deploy token",
    )
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args(argv)

    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not token:
        print("GITLAB_TOKEN is required (api + write_repository scope)", file=sys.stderr)
        return 2

    if args.review_token and args.review_token_file:
        print("use only one of --review-token / --review-token-file", file=sys.stderr)
        return 2
    if (args.review_token or args.review_token_file) and not args.set_review_token:
        print("--review-token requires --set-review-token", file=sys.stderr)
        return 2

    review_token = ""
    if args.review_token:
        review_token = args.review_token.strip()
    elif args.review_token_file:
        try:
            review_token = Path(args.review_token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"cannot read --review-token-file: {exc}", file=sys.stderr)
            return 2
    if args.set_review_token:
        if not review_token:
            if not args.allow_pat_as_review_token:
                print(
                    "--set-review-token requires --review-token or "
                    "--review-token-file; use a project access token. "
                    "Pass --allow-pat-as-review-token only if you accept "
                    "writing the bootstrap PAT into target CI variables.",
                    file=sys.stderr,
                )
                return 2
            review_token = token

    base_url = args.base_url.rstrip("/")
    remote_url = f"{base_url}/{args.project}.git"
    targets = list(args.target_project)

    print(f"GitLab: {base_url}")
    print(f"project: {args.project}")
    print(f"targets: {', '.join(targets) if targets else '(none)'}")
    print("secrets: not printed")
    if not args.yes:
        answer = input("Proceed with project creation/push/variables? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    client = _client(base_url, token)
    try:
        # ensure_project only calls /user when the project GET is 404 (i.e. it
        # must resolve a namespace for creation). Fine-grained PATs may not
        # grant User: Read, so avoid an unconditional /user probe.
        project = ensure_project(client, args.project, args.visibility)

        if args.push:
            push_main(Path(args.repo_dir).resolve(), remote_url, token, args.branch)
            try:
                _check(
                    client.put(
                        f"/projects/{project['id']}",
                        json={"default_branch": args.default_branch},
                    ),
                    "set default branch",
                )
            except SystemExit as exc:
                print(f"warning: {exc}")

        extra: dict[str, str] = {}
        if args.from_env_file:
            extra.update(_read_env_file(Path(args.from_env_file)))
        if args.set_review_token:
            extra["CODEREVIEW_GITLAB_TOKEN"] = review_token

        deploy = None
        if args.create_deploy_token:
            deploy = create_deploy_token(
                client, project["id"], args.deploy_token_name
            )
            if deploy:
                extra.setdefault("CODEREVIEW_AGENT_CLONE_USER", deploy.get("username", "oauth2"))
                extra.setdefault("CODEREVIEW_AGENT_CLONE_TOKEN", deploy.get("token", ""))

        if targets:
            for target in targets:
                target_project = ensure_project(client, target, args.visibility)
                for key, value in extra.items():
                    if value:
                        set_variable(
                            client,
                            target_project["id"],
                            key,
                            value,
                            protected=args.protected_variables,
                        )
                print(f"configured variables for {target}")
        elif extra:
            print("no --target-project given; skipped CI variable setup")

        if deploy and not targets:
            secret_dir = Path.home() / ".config" / "codereview-agent"
            secret_dir.mkdir(parents=True, exist_ok=True)
            secret_file = secret_dir / "deploy-token.json"
            secret_file.write_text(
                json.dumps(
                    {
                        "username": deploy.get("username"),
                        "token": deploy.get("token"),
                        "project": args.project,
                    }
                ),
                encoding="utf-8",
            )
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass
            print(f"deploy token written to {secret_file} (content not printed)")
    finally:
        client.close()

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
