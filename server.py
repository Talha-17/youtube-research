import os
import re
import threading

from fastmcp import FastMCP
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    InvalidVideoId,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

# ---------------------------------------------------------
# MCP SERVER
# ---------------------------------------------------------

mcp = FastMCP("youtube-research")


# ---------------------------------------------------------
# YOUTUBE HELPERS
# ---------------------------------------------------------

YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?"
    r"(?:www\.)?"
    r"(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})"
)

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

_yt_client = None
_yt_client_lock = threading.Lock()


def extract_video_id(url_or_id: str) -> str:
    """Extract YouTube video ID from a URL or return the ID directly."""

    if not isinstance(url_or_id, str):
        raise ValueError("Video URL or ID must be a string.")

    url_or_id = url_or_id.strip()[:500]

    match = YOUTUBE_URL_RE.search(url_or_id)

    if match:
        return match.group(1)

    if VIDEO_ID_RE.match(url_or_id):
        return url_or_id

    raise ValueError(
        f"Could not extract video ID from: {url_or_id[:100]}"
    )


def parse_duration(iso: str) -> str:
    """Convert ISO 8601 duration to human-readable format."""

    match = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        iso or "",
    )

    if not match:
        return iso

    hours, minutes, seconds = (
        int(x) if x else 0
        for x in match.groups()
    )

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def safe_int(value, default: int = 0) -> int:
    """Safely convert a value to int."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_api_error(error: HttpError) -> str:
    """Format YouTube API error safely."""

    try:
        reason = error._get_reason()
    except Exception:
        reason = str(error)

    return f"YouTube API error {error.resp.status}: {reason}"


def get_youtube_client():
    """Create a thread-safe lazy YouTube API client."""

    global _yt_client

    if _yt_client is None:

        with _yt_client_lock:

            if _yt_client is None:

                api_key = os.environ.get("YOUTUBE_API_KEY")

                if not api_key:
                    raise RuntimeError(
                        "YOUTUBE_API_KEY environment variable is not set."
                    )

                _yt_client = build(
                    "youtube",
                    "v3",
                    developerKey=api_key,
                )

    return _yt_client


def format_video(
    snippet: dict,
    details: dict,
    stats: dict,
    video_id: str,
) -> dict:

    return {
        "id": video_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "duration": parse_duration(
            details.get("duration", "")
        ),
        "view_count": safe_int(
            stats.get("viewCount")
        ),
        "published_at": snippet.get("publishedAt", ""),
        "channel": snippet.get("channelTitle", ""),
    }


# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

@mcp.tool
def server_status() -> dict:
    """Check whether the YouTube Research MCP server is running."""

    return {
        "status": "ok",
        "server": "youtube-research",
        "youtube_api_key_configured": bool(
            os.environ.get("YOUTUBE_API_KEY")
        ),
    }


# ---------------------------------------------------------
# SEARCH
# ---------------------------------------------------------

@mcp.tool
def youtube_search(
    query: str,
    max_results: int = 10,
) -> list[dict] | str:

    """Search YouTube videos."""

    if not query:
        return "Query cannot be empty."

    if len(query) > 500:
        return "Query too long. Maximum 500 characters."

    try:

        yt = get_youtube_client()

        search_response = (
            yt.search()
            .list(
                part="snippet",
                type="video",
                q=query,
                maxResults=min(
                    max(1, max_results),
                    50,
                ),
                order="relevance",
            )
            .execute()
        )

        ids = [
            item["id"]["videoId"]
            for item in search_response.get("items", [])
            if "videoId" in item.get("id", {})
        ]

        if not ids:
            return []

        videos_response = (
            yt.videos()
            .list(
                part="snippet,contentDetails,statistics",
                id=",".join(ids),
            )
            .execute()
        )

        results = []

        for item in videos_response.get("items", []):

            results.append(
                format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )

        return results

    except HttpError as e:

        if e.resp.status == 403:
            return f"YouTube API quota/access error: {safe_api_error(e)}"

        return safe_api_error(e)

    except RuntimeError as e:

        return str(e)


# ---------------------------------------------------------
# VIDEO INFO
# ---------------------------------------------------------

@mcp.tool
def youtube_video_info(
    video_url_or_id: str,
) -> dict | str:

    """Get metadata for a YouTube video."""

    try:

        video_id = extract_video_id(video_url_or_id)

        yt = get_youtube_client()

        response = (
            yt.videos()
            .list(
                part="snippet,contentDetails,statistics",
                id=video_id,
            )
            .execute()
        )

        items = response.get("items", [])

        if not items:
            return f"Video not found: {video_id}"

        item = items[0]

        return format_video(
            item["snippet"],
            item["contentDetails"],
            item.get("statistics", {}),
            item["id"],
        )

    except ValueError as e:
        return str(e)

    except HttpError as e:
        return safe_api_error(e)

    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------
# TRANSCRIPT
# ---------------------------------------------------------

@mcp.tool
def youtube_transcript(
    video_url_or_id: str,
    lang: list[str] | None = None,
) -> str:

    """Get subtitles/transcript from a YouTube video."""

    if lang is None:
        lang = ["en", "ru"]

    try:

        video_id = extract_video_id(video_url_or_id)

    except ValueError as e:

        return str(e)

    try:

        api = YouTubeTranscriptApi()

        transcript = api.fetch(
            video_id,
            languages=lang,
        )

        header = (
            f"Video ID: {transcript.video_id}\n"
            f"Language: {transcript.language} "
            f"({transcript.language_code})\n"
            f"Auto-generated: "
            f"{'yes' if transcript.is_generated else 'no'}\n"
            f"---\n"
        )

        lines = []

        for snippet in transcript:

            total_seconds = int(snippet.start)

            minutes, seconds = divmod(
                total_seconds,
                60,
            )

            hours, minutes = divmod(
                minutes,
                60,
            )

            if hours:

                timestamp = (
                    f"{hours}:"
                    f"{minutes:02d}:"
                    f"{seconds:02d}"
                )

            else:

                timestamp = (
                    f"{minutes}:"
                    f"{seconds:02d}"
                )

            lines.append(
                f"[{timestamp}] {snippet.text}"
            )

        return header + "\n".join(lines)

    except TranscriptsDisabled:

        return (
            f"Subtitles are disabled for "
            f"this video ({video_id})."
        )

    except NoTranscriptFound:

        return (
            f"No transcript found for "
            f"languages {lang} "
            f"(video: {video_id})."
        )

    except VideoUnavailable:

        return f"Video unavailable: {video_id}"

    except InvalidVideoId:

        return f"Invalid video ID: {video_url_or_id}"

    except CouldNotRetrieveTranscript as e:

        return f"Could not retrieve transcript: {e}"

    except Exception as e:

        return f"Transcript error: {e}"


# ---------------------------------------------------------
# CHANNEL INFO
# ---------------------------------------------------------

@mcp.tool
def youtube_channel_info(
    channel_url_or_id: str,
) -> dict | str:

    """Get YouTube channel information."""

    try:

        yt = get_youtube_client()

        channel = channel_url_or_id.strip()[:500]

        if channel.startswith("@"):

            response = (
                yt.channels()
                .list(
                    part="snippet,statistics",
                    forHandle=channel,
                )
                .execute()
            )

        elif "youtube.com" in channel:

            match = re.search(
                r"youtube\.com/(?:channel/|@)([^/?&]+)",
                channel,
            )

            if not match:
                return f"Could not parse channel URL: {channel}"

            value = match.group(1)

            if value.startswith("UC"):

                response = (
                    yt.channels()
                    .list(
                        part="snippet,statistics",
                        id=value,
                    )
                    .execute()
                )

            else:

                response = (
                    yt.channels()
                    .list(
                        part="snippet,statistics",
                        forHandle=f"@{value}",
                    )
                    .execute()
                )

        elif channel.startswith("UC"):

            response = (
                yt.channels()
                .list(
                    part="snippet,statistics",
                    id=channel,
                )
                .execute()
            )

        else:

            response = (
                yt.channels()
                .list(
                    part="snippet,statistics",
                    forHandle=f"@{channel}",
                )
                .execute()
            )

        items = response.get("items", [])

        if not items:

            return f"Channel not found: {channel}"

        channel_data = items[0]

        snippet = channel_data["snippet"]
        stats = channel_data.get(
            "statistics",
            {},
        )

        return {
            "id": channel_data["id"],
            "title": snippet.get("title", ""),
            "description": snippet.get(
                "description",
                "",
            ),
            "custom_url": snippet.get(
                "customUrl",
                "",
            ),
            "subscriber_count": safe_int(
                stats.get("subscriberCount")
            ),
            "view_count": safe_int(
                stats.get("viewCount")
            ),
            "video_count": safe_int(
                stats.get("videoCount")
            ),
            "published_at": snippet.get(
                "publishedAt",
                "",
            ),
            "thumbnail": snippet.get(
                "thumbnails",
                {},
            )
            .get("high", {})
            .get("url", ""),
        }

    except HttpError as e:

        return safe_api_error(e)

    except RuntimeError as e:

        return str(e)


# ---------------------------------------------------------
# CHANNEL VIDEOS
# ---------------------------------------------------------

@mcp.tool
def youtube_channel_videos(
    channel_url_or_id: str,
    max_results: int = 20,
) -> list[dict] | str:

    """List recent videos from a YouTube channel."""

    try:

        info = youtube_channel_info(
            channel_url_or_id
        )

        if isinstance(info, str):
            return info

        yt = get_youtube_client()

        uploads_playlist = (
            "UU" + info["id"][2:]
        )

        response = (
            yt.playlistItems()
            .list(
                part="snippet",
                playlistId=uploads_playlist,
                maxResults=min(
                    max(1, max_results),
                    50,
                ),
            )
            .execute()
        )

        video_ids = [
            item["snippet"]["resourceId"]["videoId"]
            for item in response.get("items", [])
        ]

        if not video_ids:
            return []

        videos_response = (
            yt.videos()
            .list(
                part="snippet,contentDetails,statistics",
                id=",".join(video_ids),
            )
            .execute()
        )

        results = []

        for item in videos_response.get("items", []):

            results.append(
                format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )

        return results

    except HttpError as e:

        return safe_api_error(e)

    except RuntimeError as e:

        return str(e)


# ---------------------------------------------------------
# PLAYLIST
# ---------------------------------------------------------

@mcp.tool
def youtube_playlist(
    playlist_url_or_id: str,
    max_results: int = 50,
) -> list[dict] | str:

    """List videos from a YouTube playlist."""

    try:

        playlist_id = playlist_url_or_id.strip()[:500]

        match = re.search(
            r"[?&]list=([a-zA-Z0-9_-]+)",
            playlist_id,
        )

        if match:
            playlist_id = match.group(1)

        yt = get_youtube_client()

        response = (
            yt.playlistItems()
            .list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=min(
                    max(1, max_results),
                    50,
                ),
            )
            .execute()
        )

        video_ids = [
            item["snippet"]["resourceId"]["videoId"]
            for item in response.get("items", [])
        ]

        if not video_ids:
            return []

        videos_response = (
            yt.videos()
            .list(
                part="snippet,contentDetails,statistics",
                id=",".join(video_ids),
            )
            .execute()
        )

        results = []

        for item in videos_response.get("items", []):

            results.append(
                format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )

        return results

    except HttpError as e:

        return safe_api_error(e)

    except RuntimeError as e:

        return str(e)


# ---------------------------------------------------------
# COMMENTS
# ---------------------------------------------------------

@mcp.tool
def youtube_comments(
    video_url_or_id: str,
    max_results: int = 20,
) -> list[dict] | str:

    """Get top-level YouTube comments."""

    try:

        video_id = extract_video_id(
            video_url_or_id
        )

        yt = get_youtube_client()

        response = (
            yt.commentThreads()
            .list(
                part="snippet",
                videoId=video_id,
                maxResults=min(
                    max(1, max_results),
                    100,
                ),
                order="relevance",
                textFormat="plainText",
            )
            .execute()
        )

        results = []

        for item in response.get("items", []):

            comment = (
                item["snippet"]
                ["topLevelComment"]
                ["snippet"]
            )

            results.append(
                {
                    "author": comment.get(
                        "authorDisplayName",
                        "",
                    ),
                    "text": comment.get(
                        "textDisplay",
                        "",
                    ),
                    "likes": comment.get(
                        "likeCount",
                        0,
                    ),
                    "published_at": comment.get(
                        "publishedAt",
                        "",
                    ),
                    "reply_count": item[
                        "snippet"
                    ].get(
                        "totalReplyCount",
                        0,
                    ),
                }
            )

        return results

    except HttpError as e:

        if e.resp.status == 403:

            return (
                "Comments are disabled "
                "or inaccessible for this video."
            )

        return safe_api_error(e)

    except ValueError as e:

        return str(e)

    except RuntimeError as e:

        return str(e)


# ---------------------------------------------------------
# TRENDING
# ---------------------------------------------------------

@mcp.tool
def youtube_trending(
    region_code: str = "US",
    max_results: int = 10,
) -> list[dict] | str:

    """Get trending YouTube videos."""

    if (
        len(region_code) != 2
        or not region_code.isalpha()
    ):

        return (
            f"Invalid region_code: "
            f"{region_code!r}"
        )

    try:

        yt = get_youtube_client()

        response = (
            yt.videos()
            .list(
                part="snippet,contentDetails,statistics",
                chart="mostPopular",
                regionCode=region_code.upper(),
                maxResults=min(
                    max(1, max_results),
                    50,
                ),
            )
            .execute()
        )

        results = []

        for item in response.get("items", []):

            results.append(
                format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )

        return results

    except HttpError as e:

        return safe_api_error(e)

    except RuntimeError as e:

        return str(e)


# ---------------------------------------------------------
# START SERVER
# ---------------------------------------------------------

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8080",
        )
    )

    host = os.environ.get(
        "HOST",
        "0.0.0.0",
    )

    mcp.run(
        transport="sse",
        host=host,
        port=port,
    )