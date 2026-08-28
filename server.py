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
import uvicorn

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
    url_or_id = url_or_id.strip()[:500]
    m = _YOUTUBE_URL_RE.search(url_or_id)
    if m:
        return m.group(1)
    if _VIDEO_ID_RE.match(url_or_id):
        return url_or_id
    raise ValueError(f"Could not extract video ID from: {url_or_id[:100]}")


def parse_duration(iso: str) -> str:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return iso
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_api_error(e: HttpError) -> str:
    return f"YouTube API error {e.resp.status}: {e._get_reason()}"


def get_youtube_client():
    global _yt_client
    if _yt_client is None:
        with _yt_client_lock:
            if _yt_client is None:
                api_key = os.environ.get("YOUTUBE_API_KEY")
                if not api_key:
                    raise RuntimeError("YOUTUBE_API_KEY environment variable is not set.")
                _yt_client = build("youtube", "v3", developerKey=api_key)
    return _yt_client


def _format_video(snippet: dict, details: dict, stats: dict, video_id: str) -> dict:
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
    """Search YouTube videos by query."""
    if len(query) > 500:
        return "Query too long (max 500 characters)."
    try:
        yt = get_youtube_client()
        search_resp = (
            yt.search()
            .list(part="snippet", type="video", q=query, maxResults=min(max(1, max_results), 50), order="relevance")
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
    """Get video metadata by URL or ID."""
    try:
        video_id = extract_video_id(video_url_or_id)
        yt = get_youtube_client()
        resp = yt.videos().list(part="snippet,contentDetails,statistics", id=video_id).execute()
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
    """Get subtitles/transcript for a YouTube video."""
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
            f"Auto-generated: {'yes' if transcript.is_generated else 'no'}\n---\n"
        )
        lines = []
        for snippet in transcript:
            total_sec = int(snippet.start)
            minutes, seconds = divmod(total_sec, 60)
            hours, minutes = divmod(minutes, 60)
            ts = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
            lines.append(f"[{ts}] {snippet.text}")
        return header + "\n".join(lines)
    except TranscriptsDisabled:
        return f"Subtitles are disabled for this video ({video_id})."
    except NoTranscriptFound:
        return f"No transcript found for languages {lang} (video: {video_id})."
    except VideoUnavailable:
        return f"Video unavailable: {video_id}"
    except InvalidVideoId:
        return f"Invalid video ID: {video_url_or_id}"
    except CouldNotRetrieveTranscript as e:
        return f"Could not retrieve transcript: {e}"


@mcp.tool
def youtube_channel_info(channel_url_or_id: str) -> dict | str:
    """Get channel metadata by URL, handle, or ID."""
    try:
        yt = get_youtube_client()
        channel_id = channel_url_or_id.strip()[:500]
        if channel_id.startswith("@"):
            resp = yt.channels().list(part="snippet,statistics", forHandle=channel_id).execute()
        elif channel_id.startswith("UC"):
            resp = yt.channels().list(part="snippet,statistics", id=channel_id).execute()
        else:
            resp = yt.channels().list(part="snippet,statistics", forHandle=f"@{channel_id}").execute()

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
    """List recent videos from a channel."""
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
        video_ids = [item["snippet"]["resourceId"]["videoId"] for item in playlist_resp.get("items", [])]
        if not video_ids:
            return []
        videos_resp = yt.videos().list(part="snippet,contentDetails,statistics", id=",".join(video_ids)).execute()
        return [_format_video(i["snippet"], i["contentDetails"], i.get("statistics", {}), i["id"]) for i in videos_resp.get("items", [])]
    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_playlist(playlist_url_or_id: str, max_results: int = 50) -> list[dict] | str:
    """List videos in a YouTube playlist."""
    try:
        playlist_id = playlist_url_or_id.strip()[:500]
        m = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", playlist_id)
        if m:
            playlist_id = m.group(1)
        yt = get_youtube_client()
        playlist_resp = yt.playlistItems().list(part="snippet", playlistId=playlist_id, maxResults=min(max(1, max_results), 50)).execute()
        video_ids = [item["snippet"]["resourceId"]["videoId"] for item in playlist_resp.get("items", [])]
        if not video_ids:
            return []
        videos_resp = yt.videos().list(part="snippet,contentDetails,statistics", id=",".join(video_ids)).execute()
        return [_format_video(i["snippet"], i["contentDetails"], i.get("statistics", {}), i["id"]) for i in videos_resp.get("items", [])]
    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


@mcp.tool
def youtube_comments(video_url_or_id: str, max_results: int = 20) -> list[dict] | str:
    """Get top-level comments for a YouTube video."""
    try:
        video_id = extract_video_id(video_url_or_id)
        yt = get_youtube_client()
        resp = yt.commentThreads().list(part="snippet", videoId=video_id, maxResults=min(max(1, max_results), 100), order="relevance", textFormat="plainText").execute()
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
    """Get trending/most popular videos for a region."""
    if len(region_code) != 2 or not region_code.isalpha():
        return f"Invalid region_code: {region_code!r}."
    try:
        yt = get_youtube_client()
        resp = yt.videos().list(part="snippet,contentDetails,statistics", chart="mostPopular", regionCode=region_code.upper(), maxResults=min(max(1, max_results), 50)).execute()
        return [_format_video(i["snippet"], i["contentDetails"], i.get("statistics", {}), i["id"]) for i in resp.get("items", [])]
    except HttpError as e:
        return _safe_api_error(e)
    except RuntimeError as e:
        return str(e)


# --- Standard ASGI App for Railway ---

app = FastAPI()


@app.get("/")
def health_check():
    return {"status": "ok", "service": "youtube-research-mcp"}


# Mount FastMCP SSE handler onto FastAPI
mcp.mount_sse(app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)