import os
import re
import threading

from fastapi import FastAPI
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

mcp = FastMCP("youtube-research")

# --- Helpers ---

_YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})"
)
_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

_yt_client = None
_yt_client_lock = threading.Lock()


def extract_video_id(url_or_id: str) -> str:
    """Extract video ID from a YouTube URL or return as-is if already an ID."""
    url_or_id = url_or_id.strip()[:500]
    m = _YOUTUBE_URL_RE.search(url_or_id)
    if m:
        return m.group(1)
    if _VIDEO_ID_RE.match(url_or_id):
        return url_or_id
    raise ValueError(f"Could not extract video ID from: {url_or_id[:100]}")


def parse_duration(iso: str) -> str:
    """Convert ISO 8601 duration (PT1H2M3S) to a human-readable format."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return iso
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _safe_int(val, default: int = 0) -> int:
    """Safely convert a value to int, returning default on failure."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_api_error(e: HttpError) -> str:
    """Format HttpError without leaking the API key from the request URI."""
    return f"YouTube API error {e.resp.status}: {e._get_reason()}"


def get_youtube_client():
    """Thread-safe lazy singleton for the YouTube API client."""
    global _yt_client
    if _yt_client is None:
        with _yt_client_lock:
            if _yt_client is None:
                api_key = os.environ.get("YOUTUBE_API_KEY")
                if not api_key:
                    raise RuntimeError(
                        "YOUTUBE_API_KEY environment variable is not set. "
                        "Get a key from Google Cloud Console: "
                        "APIs & Services -> Credentials -> Create API Key, "
                        "then enable YouTube Data API v3."
                    )
                _yt_client = build("youtube", "v3", developerKey=api_key)
    return _yt_client


def _format_video(snippet: dict, details: dict, stats: dict, video_id: str) -> dict:
    """Build a unified video metadata dict."""
    return {
        "id": video_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "duration": parse_duration(details.get("duration", "")),
        "view_count": _safe_int(stats.get("viewCount")),
        "published_at": snippet.get("publishedAt", ""),
        "channel": snippet.get("channelTitle", ""),
    }


# --- Tools ---


@mcp.tool
def youtube_search(query: str, max_results: int = 10) -> list[dict] | str:
    """Search YouTube videos by query.

    Returns a list of videos with metadata: id, title, description,
    duration, view_count, published_at, channel.
    """
    if len(query) > 500:
        return "Query too long (max 500 characters)."
    try:
        yt = get_youtube_client()

        search_resp = (
            yt.search()
            .list(
                part="snippet",
                type="video",
                q=query,
                maxResults=min(max(1, max_results), 50),
                order="relevance",
            )
            .execute()
        )

        ids = [item["id"]["videoId"] for item in search_resp.get("items", [])]
        if not ids:
            return []

        videos_resp = (
            yt.videos()
            .list(part="snippet,contentDetails,statistics", id=",".join(ids))
            .execute()
        )

        results = []
        for item in videos_resp.get("items", []):
            results.append(
                _format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )
        return results

    except HttpError as e:
        if e.resp.status == 403:
            return f"YouTube API quota error: {_safe_api_error(e)}"
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_video_info(video_url_or_id: str) -> dict | str:
    """Get video metadata by URL or ID.

    Returns: id, title, description, duration, view_count, published_at, channel.
    Direct lookup by video ID — no search required.
    """
    try:
        video_id = extract_video_id(video_url_or_id)
        yt = get_youtube_client()

        resp = (
            yt.videos()
            .list(part="snippet,contentDetails,statistics", id=video_id)
            .execute()
        )

        items = resp.get("items", [])
        if not items:
            return f"Video not found: {video_id}"

        item = items[0]
        return _format_video(
            item["snippet"],
            item["contentDetails"],
            item.get("statistics", {}),
            item["id"],
        )

    except ValueError as e:
        return str(e)
    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_transcript(video_url_or_id: str, lang: list[str] | None = None) -> str:
    """Get subtitles/transcript for a YouTube video.

    Accepts a video URL or ID. Returns timestamped text.
    Looks for Russian subtitles first, then English by default.
    """
    if lang is None:
        lang = ["ru", "en"]
    try:
        video_id = extract_video_id(video_url_or_id)
    except ValueError as e:
        return str(e)

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=lang)

        header = (
            f"Video ID: {transcript.video_id}\n"
            f"Language: {transcript.language} ({transcript.language_code})\n"
            f"Auto-generated: {'yes' if transcript.is_generated else 'no'}\n"
            f"---\n"
        )

        lines = []
        for snippet in transcript:
            total_sec = int(snippet.start)
            minutes, seconds = divmod(total_sec, 60)
            hours, minutes = divmod(minutes, 60)
            if hours:
                ts = f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                ts = f"{minutes}:{seconds:02d}"
            lines.append(f"[{ts}] {snippet.text}")

        return header + "\n".join(lines)

    except TranscriptsDisabled:
        return f"Subtitles are disabled for this video ({video_id})."
    except NoTranscriptFound:
        return (
            f"No transcript found for languages {lang} (video: {video_id}). "
            f"Try a different language."
        )
    except VideoUnavailable:
        return f"Video unavailable: {video_id}"
    except InvalidVideoId:
        return f"Invalid video ID: {video_url_or_id}"
    except CouldNotRetrieveTranscript as e:
        return f"Could not retrieve transcript: {e}"


@mcp.tool
def youtube_channel_info(channel_url_or_id: str) -> dict | str:
    """Get channel metadata by URL, handle, or ID.

    Returns: id, title, description, subscriber_count, view_count,
    video_count, published_at, thumbnail, custom_url.
    """
    try:
        yt = get_youtube_client()
        channel_id = channel_url_or_id.strip()[:500]

        if channel_id.startswith("@"):
            resp = yt.channels().list(part="snippet,statistics", forHandle=channel_id).execute()
        elif "youtube.com" in channel_id:
            m = re.search(r"youtube\.com/(?:channel/|@)([^/?&]+)", channel_id)
            if m:
                val = m.group(1)
                if val.startswith("UC"):
                    resp = yt.channels().list(part="snippet,statistics", id=val).execute()
                else:
                    resp = yt.channels().list(part="snippet,statistics", forHandle=f"@{val}").execute()
            else:
                return f"Could not parse channel URL: {channel_id[:100]}"
        elif channel_id.startswith("UC"):
            resp = yt.channels().list(part="snippet,statistics", id=channel_id).execute()
        else:
            handle = channel_id if channel_id.startswith("@") else f"@{channel_id}"
            resp = yt.channels().list(part="snippet,statistics", forHandle=handle).execute()

        items = resp.get("items", [])
        if not items:
            return f"Channel not found: {channel_url_or_id[:100]}"

        ch = items[0]
        snippet = ch["snippet"]
        stats = ch.get("statistics", {})
        return {
            "id": ch["id"],
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "custom_url": snippet.get("customUrl", ""),
            "subscriber_count": _safe_int(stats.get("subscriberCount")),
            "view_count": _safe_int(stats.get("viewCount")),
            "video_count": _safe_int(stats.get("videoCount")),
            "published_at": snippet.get("publishedAt", ""),
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
        }

    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_channel_videos(channel_url_or_id: str, max_results: int = 20) -> list[dict] | str:
    """List recent videos from a channel.

    Accepts channel URL, @handle, or ID. Returns videos sorted by date (newest first)
    with metadata: id, title, description, duration, view_count, published_at.
    """
    try:
        info = youtube_channel_info(channel_url_or_id)
        if isinstance(info, str):
            return info

        yt = get_youtube_client()
        uploads_id = "UU" + info["id"][2:]

        playlist_resp = (
            yt.playlistItems()
            .list(part="snippet", playlistId=uploads_id, maxResults=min(max(1, max_results), 50))
            .execute()
        )

        video_ids = [
            item["snippet"]["resourceId"]["videoId"]
            for item in playlist_resp.get("items", [])
        ]
        if not video_ids:
            return []

        videos_resp = (
            yt.videos()
            .list(part="snippet,contentDetails,statistics", id=",".join(video_ids))
            .execute()
        )

        results = []
        for item in videos_resp.get("items", []):
            results.append(
                _format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )
        return results

    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_playlist(playlist_url_or_id: str, max_results: int = 50) -> list[dict] | str:
    """List videos in a YouTube playlist.

    Accepts a playlist URL or ID. Returns videos with metadata.
    """
    try:
        playlist_id = playlist_url_or_id.strip()[:500]
        m = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", playlist_id)
        if m:
            playlist_id = m.group(1)

        yt = get_youtube_client()
        playlist_resp = (
            yt.playlistItems()
            .list(part="snippet", playlistId=playlist_id, maxResults=min(max(1, max_results), 50))
            .execute()
        )

        video_ids = [
            item["snippet"]["resourceId"]["videoId"]
            for item in playlist_resp.get("items", [])
        ]
        if not video_ids:
            return []

        videos_resp = (
            yt.videos()
            .list(part="snippet,contentDetails,statistics", id=",".join(video_ids))
            .execute()
        )

        results = []
        for item in videos_resp.get("items", []):
            results.append(
                _format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )
        return results

    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_comments(video_url_or_id: str, max_results: int = 20) -> list[dict] | str:
    """Get top-level comments for a YouTube video.

    Returns comments sorted by relevance with: author, text, likes, published_at.
    """
    try:
        video_id = extract_video_id(video_url_or_id)
        yt = get_youtube_client()

        resp = (
            yt.commentThreads()
            .list(
                part="snippet",
                videoId=video_id,
                maxResults=min(max(1, max_results), 100),
                order="relevance",
                textFormat="plainText",
            )
            .execute()
        )

        results = []
        for item in resp.get("items", []):
            comment = item["snippet"]["topLevelComment"]["snippet"]
            results.append({
                "author": comment.get("authorDisplayName", ""),
                "text": comment.get("textDisplay", ""),
                "likes": comment.get("likeCount", 0),
                "published_at": comment.get("publishedAt", ""),
                "reply_count": item["snippet"].get("totalReplyCount", 0),
            })
        return results

    except HttpError as e:
        if e.resp.status == 403:
            return "Comments are disabled or inaccessible for this video."
        return _safe_api_error(e)
    except ValueError as e:
        return str(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_trending(region_code: str = "US", max_results: int = 10) -> list[dict] | str:
    """Get trending/most popular videos for a region.

    region_code: ISO 3166-1 alpha-2 country code (e.g. US, RU, GB, DE, JP).
    Returns videos with metadata sorted by popularity.
    """
    if len(region_code) != 2 or not region_code.isalpha():
        return f"Invalid region_code: {region_code!r}. Use ISO 3166-1 alpha-2 (e.g. US, RU, GB)."
    try:
        yt = get_youtube_client()

        resp = (
            yt.videos()
            .list(
                part="snippet,contentDetails,statistics",
                chart="mostPopular",
                regionCode=region_code.upper(),
                maxResults=min(max(1, max_results), 50),
            )
            .execute()
        )

        results = []
        for item in resp.get("items", []):
            results.append(
                _format_video(
                    item["snippet"],
                    item["contentDetails"],
                    item.get("statistics", {}),
                    item["id"],
                )
            )
        return results

    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="YouTube Research MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run as SSE server (for remote/web access)")
    parser.add_argument("--host", default="127.0.0.1", help="SSE server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="SSE server port (default: 8000)")
    args = parser.parse_args()

    if args.sse:
        # FastMCP's internal app can be customized or we hook a health check route if supported, 
        # but to cleanly satisfy Railway, let's attach the health check directly to FastMCP's starlette app:
        app = mcp._app if hasattr(mcp, "_app") else None
        
        @mcp.custom_route("/")
        async def health_check():
            return {"status": "healthy"}

        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio", show_banner=False)