# YouTube Research MCP Server

An MCP server for YouTube integration with Claude Code — search videos, get metadata, fetch transcripts, explore channels, and more.

## Tools

| Tool | Description | API Quota |
|---|---|---|
| `youtube_search(query, max_results)` | Search videos by query | 100 + 1 |
| `youtube_video_info(video_url_or_id)` | Get video metadata by URL or ID | 1 |
| `youtube_transcript(video_url_or_id, lang)` | Fetch video subtitles with timestamps | 0 (no API) |
| `youtube_channel_info(channel_url_or_id)` | Get channel stats (subscribers, views, etc.) | 1 |
| `youtube_channel_videos(channel_url_or_id, max_results)` | List recent videos from a channel | 2 |
| `youtube_playlist(playlist_url_or_id, max_results)` | List videos in a playlist | 2 |
| `youtube_comments(video_url_or_id, max_results)` | Get top comments for a video | 1 |
| `youtube_trending(region_code, max_results)` | Get trending videos by country | 1 |

> YouTube Data API v3 free quota: 10,000 units/day. `youtube_search` is the most expensive at ~101 units per call.

## Setup

### 1. Get a YouTube API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or select an existing one)
3. Navigate to **APIs & Services → Library**
4. Find and enable **YouTube Data API v3**
5. Go to **APIs & Services → Credentials**
6. Click **Create Credentials → API Key**
7. Copy the key

### 2. Install Dependencies

```bash
cd /path/to/youtube_research
uv sync
```

### 3. Configure Claude Code

Copy the example config and add your API key:

```bash
cp .mcp.json.example ~/.claude/.mcp.json
```

Edit `~/.claude/.mcp.json` — replace `/path/to/youtube_research` and `YOUR_API_KEY_HERE` with actual values.

> **Important:** `.mcp.json` contains your API key — do not commit it to git.

Alternatively, place `.mcp.json` in the project root — the server will only be available when working from that directory.

### 4. Restart

Restart Claude Code — all 8 tools will become available.

## Remote Access (SSE)

To use with Claude.ai web chat or other remote MCP clients, run the server in SSE mode on a VPS:

```bash
YOUTUBE_API_KEY=your_key uv run server.py --sse --host 0.0.0.0 --port 8000
```

Options:
- `--sse` — enable SSE transport (default is stdio)
- `--host` — bind address (default: `127.0.0.1` — use `0.0.0.0` to expose externally)
- `--port` — port number (default: `8000`)

The server will be available at `http://your-vps:8000/sse`.

> **Security:** The SSE endpoint has no built-in authentication. For public deployment, use a reverse proxy (nginx) with token-based auth in front of it.

## Usage Examples

In Claude Code:

- "Find videos about Python asyncio" → `youtube_search`
- "Show info for https://youtu.be/dQw4w9WgXcQ" → `youtube_video_info`
- "Get subtitles for this video" → `youtube_transcript`
- "How many subscribers does @lexfridman have?" → `youtube_channel_info`
- "Show latest videos from @ThePrimeagen" → `youtube_channel_videos`
- "List videos in this playlist: https://youtube.com/playlist?list=..." → `youtube_playlist`
- "What are people saying about this video?" → `youtube_comments`
- "What's trending in Japan?" → `youtube_trending`

## License

[MIT](LICENSE)
