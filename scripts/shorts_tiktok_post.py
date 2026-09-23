"""Public R2 media hosting and automatic TikTok scheduling through Buffer's GraphQL API.

Secrets are read only from the process environment. Never log the media URL.
"""
import json
import hashlib
import hmac
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BUFFER_API = "https://api.buffer.com"
R2_BUCKET = "zingisukan-tiktok-public"
R2_PREFIX = "tiktok"
POST_LIMIT_PER_MONTH = 25
TEST_POST_LIMIT_PER_MONTH = 1
MAX_TIKTOK_BYTES = 1_000_000_000
EXPECTED_TIKTOK_HANDLE = "tateyamaclips"
UUID_RE = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
VOD_PATH_RE = re.compile(r"^/zingisukan2525/videos/([A-Za-z0-9_-]{6,80})/?$")

CHANNEL_QUERY = """
query TikTokChannel($id: ChannelId!) {
  channel(input: { id: $id }) { id name externalLink service organizationId isDisconnected isQueuePaused }
}
"""

POSTS_QUERY = """
query RecentPosts($input: PostsInput!, $first: Int!, $after: String) {
  posts(input: $input, first: $first, after: $after) {
    edges { cursor node { id status createdAt dueAt assets { source } } }
    pageInfo { endCursor hasNextPage }
  }
}
"""

CREATE_POST = """
mutation CreateTikTokPost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id status dueAt assets { source } }
    }
    ... on MutationError { message }
  }
}
"""


class SafeFailure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def output(status, post_id="", error=""):
    allowed = {"scheduled", "already_posted", "monthly_cap", "needs_manual", "failed"}
    def clean(value, limit):
        return re.sub(r"[\x00\r\n]", "", str(value))[:limit]

    status = clean(status, 32)
    status = status if status in allowed else "failed"
    values = {"status": status, "post_id": clean(post_id, 120),
              "error": clean(error, 160)}
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")
    print(f"TikTok auto-post status: {status}")
    if values["post_id"]:
        print(f"Buffer post id: {values['post_id']}")
    if values["error"]:
        print(f"Reason: {values['error']}")


def api_call(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(BUFFER_API, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "zingisukan-tiktok-automation",
    })
    try:
        with urlopen(req, timeout=40) as response:
            data = json.loads(response.read())
    except HTTPError as exc:
        raise SafeFailure("needs_manual", f"Buffer API HTTP {exc.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise SafeFailure("failed", "Buffer API connection or response error") from None
    if data.get("errors"):
        raise SafeFailure("needs_manual", "Buffer API rejected the request")
    if not isinstance(data.get("data"), dict):
        raise SafeFailure("failed", "Buffer API returned an invalid response")
    return data["data"]


def month_bounds(now):
    jst = ZoneInfo("Asia/Tokyo")
    local = now.astimezone(jst)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def recent_posts(token, organization_id, channel_id, start, end):
    input_data = {
        "organizationId": organization_id,
        "filter": {
            "channelIds": [channel_id],
            "startDate": start.isoformat().replace("+00:00", "Z"),
            "endDate": end.isoformat().replace("+00:00", "Z"),
        },
    }
    after = None
    posts = []
    for _ in range(30):
        data = api_call(token, POSTS_QUERY,
                        {"input": input_data, "first": 100, "after": after})
        result = data.get("posts") or {}
        posts.extend(edge.get("node") or {} for edge in result.get("edges", []))
        page = result.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return posts
        after = page.get("endCursor")
        if not after:
            break
    raise SafeFailure("needs_manual", "Buffer post history exceeded the safe scan limit")


def asset_paths(post):
    for asset in post.get("assets") or []:
        source = str((asset or {}).get("source") or "")
        path = unquote(urlsplit(source).path)
        if path:
            yield path


def object_key(vod_uuid, key_secret):
    digest = hmac.new(key_secret.encode("utf-8"), vod_uuid.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"{R2_PREFIX}/{digest}.mp4"


def has_vod_asset(post, vod_uuid, key_secret):
    expected = f"/{object_key(vod_uuid, key_secret)}"
    legacy = f"/{R2_PREFIX}/{vod_uuid}.mp4"
    return any(path.endswith(expected) or path.endswith(legacy)
               for path in asset_paths(post))


def validate_public_base(value):
    """Require a stable HTTPS origin for the dedicated public R2 bucket."""
    try:
        parts = urlsplit(value.strip())
        valid = (
            parts.scheme.lower() == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise SafeFailure("needs_manual", "R2_PUBLIC_BASE_URL must be an HTTPS bucket domain")
    return f"https://{parts.netloc}"


def verify_public_media_url(url, expected_size):
    """Check the public custom domain before asking Buffer to schedule the post."""
    request = Request(url, method="HEAD", headers={
        "User-Agent": "zingisukan-tiktok-automation",
        "Cache-Control": "no-cache",
    })
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            content_length = response.headers.get("Content-Length", "")
            if (response.status != 200 or content_type.lower() != "video/mp4"
                    or content_length != str(expected_size)):
                raise SafeFailure("needs_manual", "Public R2 media URL failed the access check")
    except SafeFailure:
        raise
    except Exception:
        raise SafeFailure("needs_manual", "Public R2 media URL failed the access check") from None


def current_month_vod_posts(posts, month_start, month_end):
    matching = []
    for post in posts:
        created = post.get("createdAt") or ""
        due = post.get("dueAt") or ""
        stamp = due or created
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if month_start <= dt < month_end and any(
                f"/{R2_PREFIX}/" in path for path in asset_paths(post)):
            matching.append(post)
    return matching


def validate_vod_video(video, vod_uuid):
    """Require a canonical Kick VOD URL whose path UUID matches the queue item."""
    if not video:
        raise SafeFailure("needs_manual", "Kick VOD URL is required")
    video = video.strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in video):
        raise SafeFailure("needs_manual", "Kick VOD URL is invalid")
    try:
        parts = urlsplit(video)
    except ValueError:
        raise SafeFailure("needs_manual", "Kick VOD URL is invalid") from None
    if parts.scheme.lower() != "https" or parts.netloc.casefold() != "kick.com":
        raise SafeFailure("needs_manual", "Kick VOD URL must use https://kick.com")
    match = VOD_PATH_RE.fullmatch(parts.path)
    if not match:
        raise SafeFailure("needs_manual", "Kick VOD URL does not contain a supported video id")
    url_uuid = match.group(1)
    if vod_uuid and url_uuid != vod_uuid:
        raise SafeFailure("needs_manual", "Kick VOD URL does not match the VOD id")
    return url_uuid


def run():
    test_post = os.environ.get("TIKTOK_TEST_POST", "").strip().casefold() == "true"
    autopublish_enabled = os.environ.get("TIKTOK_AUTOPUBLISH_ENABLED", "").strip().casefold() == "true"
    if test_post:
        test_enabled = os.environ.get("TIKTOK_TEST_POST_ENABLED", "").strip().casefold() == "true"
        if not test_enabled or autopublish_enabled:
            raise SafeFailure("needs_manual", "Enable only the one-time TikTok test-post switch")
    elif not autopublish_enabled:
        raise SafeFailure("needs_manual", "Automatic TikTok scheduling is disabled")

    token = os.environ.get("BUFFER_API_KEY", "").strip()
    channel_id = os.environ.get("BUFFER_TIKTOK_CHANNEL_ID", "").strip()
    vod_uuid = os.environ.get("VOD_UUID", "").strip()
    vod_uuid = validate_vod_video(os.environ.get("VOD_VIDEO", ""), vod_uuid)
    required = {
        "R2_ACCOUNT_ID": os.environ.get("R2_ACCOUNT_ID", "").strip(),
        "R2_ACCESS_KEY_ID": os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
        "R2_SECRET_ACCESS_KEY": os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
        "TIKTOK_OBJECT_KEY_SECRET": os.environ.get("TIKTOK_OBJECT_KEY_SECRET", "").strip(),
    }
    public_base = os.environ.get("R2_PUBLIC_BASE_URL", "").strip()
    if not token or not channel_id or not UUID_RE.fullmatch(vod_uuid) or not all(required.values()) or not public_base:
        raise SafeFailure("needs_manual", "GitHub secrets or VOD id are not configured")
    if len(required["TIKTOK_OBJECT_KEY_SECRET"]) < 32:
        raise SafeFailure("needs_manual", "TikTok object-key secret must be at least 32 characters")
    public_base = validate_public_base(public_base)

    channel_data = api_call(token, CHANNEL_QUERY, {"id": channel_id})
    channel = channel_data.get("channel") or {}
    if channel.get("service") != "tiktok":
        raise SafeFailure("needs_manual", "Configured Buffer channel is not TikTok")
    handle = str(channel.get("name") or "").strip().lstrip("@").casefold()
    linked_handle = ""
    external_link = str(channel.get("externalLink") or "").strip()
    try:
        link = urlsplit(external_link)
        if link.hostname and link.hostname.casefold() in {"tiktok.com", "www.tiktok.com"}:
            linked_handle = link.path.rstrip("/").split("/")[-1].lstrip("@").casefold()
    except ValueError:
        pass
    if EXPECTED_TIKTOK_HANDLE not in {handle, linked_handle}:
        raise SafeFailure("needs_manual", "Configured Buffer channel does not match @tateyamaclips")
    if channel.get("isDisconnected") or channel.get("isQueuePaused"):
        raise SafeFailure("needs_manual", "Buffer TikTok channel is disconnected or paused")
    organization_id = channel.get("organizationId")
    if not organization_id:
        raise SafeFailure("needs_manual", "Buffer organization is unavailable")

    now = datetime.now(timezone.utc)
    month_start, month_end = month_bounds(now)
    history_start = min(month_start, now - timedelta(days=90))
    posts = recent_posts(token, organization_id, channel_id, history_start, month_end)
    same_vod = next((post for post in posts if has_vod_asset(
        post, vod_uuid, required["TIKTOK_OBJECT_KEY_SECRET"])), None)
    if same_vod:
        if same_vod.get("status") in {"scheduled", "sending", "sent"}:
            return "already_posted", same_vod.get("id", ""), ""
        raise SafeFailure("needs_manual", "A Buffer post for this VOD already exists")

    monthly_limit = TEST_POST_LIMIT_PER_MONTH if test_post else POST_LIMIT_PER_MONTH
    monthly_posts = len(current_month_vod_posts(posts, month_start, month_end))
    if monthly_posts >= monthly_limit:
        return "monthly_cap", "", f"{monthly_limit} TikTok post(s) already scheduled or posted this month"

    media_path = Path(os.environ.get("POST_FILE", "out_shorts/tiktok_final.mp4"))
    if not media_path.is_file() or media_path.stat().st_size < 100_000:
        raise SafeFailure("failed", "Final mosaiced video is missing or too small")
    if media_path.stat().st_size > MAX_TIKTOK_BYTES:
        raise SafeFailure("needs_manual", "Final video exceeds TikTok's 1 GB file size limit")

    import boto3

    key = object_key(vod_uuid, required["TIKTOK_OBJECT_KEY_SECRET"])
    endpoint = f"https://{required['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    s3 = boto3.client(
        "s3", endpoint_url=endpoint, region_name="auto",
        aws_access_key_id=required["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=required["R2_SECRET_ACCESS_KEY"],
    )
    try:
        with media_path.open("rb") as source:
            s3.upload_fileobj(source, R2_BUCKET, key,
                              ExtraArgs={
                                  "ContentType": "video/mp4",
                                  "CacheControl": "no-store, max-age=0",
                              })
        media_url = f"{public_base}/{key}"
    except Exception:
        raise SafeFailure("failed", "Public R2 upload failed") from None
    try:
        verify_public_media_url(media_url, media_path.stat().st_size)
    except SafeFailure:
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=key)
        except Exception:
            pass
        raise

    title = "ジンギスカン配信"
    manifest = Path("out_shorts/manifest.json")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        clips = data.get("clips") or []
        title = ((clips[0].get("titles") or [title])[0] or title).strip()
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    caption = f"{title}\n#ジンギスカン #切り抜き"
    due_at = (now + timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    post_input = {
        "channelId": channel_id,
        "text": caption,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due_at,
        "assets": [{"video": {"url": media_url,
                              "metadata": {"thumbnailOffset": 2000}}}],
    }
    created = api_call(token, CREATE_POST, {"input": post_input})
    result = created.get("createPost") or {}
    post = result.get("post") or {}
    if not post.get("id"):
        raise SafeFailure("needs_manual", "Buffer did not return a scheduled post")
    if post.get("status") != "scheduled":
        raise SafeFailure("needs_manual", "Buffer saved the post without scheduling it")
    return "scheduled", post["id"], ""


def main():
    try:
        status, post_id, message = run()
        output(status, post_id, message)
    except SafeFailure as exc:
        output(exc.status, error=str(exc))
    except Exception:
        output("failed", error="Unexpected TikTok publishing error")
        sys.exit(1)


if __name__ == "__main__":
    main()
