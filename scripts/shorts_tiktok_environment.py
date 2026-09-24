"""Fail closed unless the selected TikTok posting environment has a required reviewer."""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_ENVIRONMENT = "tiktok-test-post"


def has_required_reviewer(data, environment):
    if not isinstance(data, dict) or data.get("name") != environment:
        return False
    rules = data.get("protection_rules") or []
    return any(
        rule.get("type") == "required_reviewers"
        and bool(rule.get("reviewers"))
        for rule in rules if isinstance(rule, dict)
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT,
                        help="GitHub environment to verify (default: %(default)s)")
    args = parser.parse_args(argv)
    environment = args.environment.strip()
    if not environment or any(ord(char) < 0x20 or ord(char) == 0x7F
                               for char in environment):
        print("TikTok posting environment name is invalid", file=sys.stderr)
        return 1

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not token or not repository or "/" not in repository:
        print("TikTok posting environment check is not configured", file=sys.stderr)
        return 1

    url = f"{api_url}/repos/{repository}/environments/{quote(environment, safe='')}"
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zingisukan-tiktok-reviewer-check",
    })
    try:
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        print("TikTok posting environment could not be verified", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("TikTok posting environment returned invalid data", file=sys.stderr)
        return 1

    if not has_required_reviewer(data, environment):
        print(f"Configure a required reviewer on the {environment} environment", file=sys.stderr)
        return 1

    print(f"TikTok {environment} reviewer gate verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
