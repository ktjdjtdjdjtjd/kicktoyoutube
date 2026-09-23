"""Fail closed unless the TikTok test-post environment has a required reviewer."""
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ENVIRONMENT = "tiktok-test-post"


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not token or not repository or "/" not in repository:
        print("TikTok test-post environment check is not configured", file=sys.stderr)
        return 1

    url = f"{api_url}/repos/{repository}/environments/{quote(ENVIRONMENT, safe='')}"
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zingisukan-tiktok-test-post",
    })
    try:
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        print("TikTok test-post environment could not be verified", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("TikTok test-post environment returned invalid data", file=sys.stderr)
        return 1

    rules = data.get("protection_rules") or []
    has_reviewer = any(
        rule.get("type") == "required_reviewers"
        and bool(rule.get("reviewers"))
        for rule in rules if isinstance(rule, dict)
    )
    if data.get("name") != ENVIRONMENT or not has_reviewer:
        print("Configure a required reviewer on the tiktok-test-post environment", file=sys.stderr)
        return 1

    print("TikTok test-post reviewer gate verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
