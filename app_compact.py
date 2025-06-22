#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask-SocketIO YouTube下载器后端
"""

import os
import shutil
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlparse
from time import monotonic
import random

import yt_dlp
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)
from flask_socketio import SocketIO, emit, join_room

# 基础配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SAFE_OUTTMPL = "%(title,id)s.%(ext)s"
ORIGINAL_OUTTMPL = "%(title,id)s[original].%(ext)s"

# Flask应用初始化
app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me"

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# 线程池配置
MAX_WORKERS = int(os.getenv("DL_WORKERS", "6"))
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="DownloadWorker")
DownloadStatus: dict[str, dict] = {}

# 性能配置
PERFORMANCE_CONFIG = {
    "has_aria2c": shutil.which("aria2c") is not None,
    "concurrent_fragments": 16,
    "buffersize": 1024 * 1024,
    "http_chunk_size": 10 * 1024 * 1024,
    "retries": 20,
    "fragment_retries": 30,
    "progress_throttle": 0.1,
    "socket_timeout": 60,
    "sleep_interval_requests": 0.2,
    "aria2c_max_connection_per_server": 16,
    "aria2c_split": 16,
    "aria2c_min_split_size": "1M",
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
    ]
}

# 工具函数
def format_bytes(num: float | int) -> str:
    if not num:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"

def get_file_type_info(ext: str):
    ext = ext.lower()
    audio_set = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm"}
    video_set = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
    if ext in audio_set:
        return "audio", "🎵"
    if ext in video_set:
        return "video", "🎥"
    return "unknown", "📄"

def get_type_text(dtype: str) -> str:
    return {"video": "视频", "audio": "音频"}.get(dtype, "媒体")

def is_original_format_file(filename: str) -> bool:
    return "[original]" in filename

def get_optimal_user_agent():
    return random.choice(PERFORMANCE_CONFIG["user_agents"])

def check_aria2c_availability():
    if not PERFORMANCE_CONFIG["has_aria2c"]:
        return False, "aria2c 未安装"
    try:
        result = subprocess.run(["aria2c", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return True, "aria2c 可用"
        else:
            return False, "aria2c 执行错误"
    except:
        return False, "aria2c 测试失败"

def check_ffmpeg_availability():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except:
        return False

# 下载进度回调
class DownloadProgress:
    def __init__(self, session_id: str, downloader_type: str = "ytdlp"):
        self.session_id = session_id
        self.downloader_type = downloader_type
        self._last_emit = 0.0
        self._throttle_interval = PERFORMANCE_CONFIG["progress_throttle"]

    def _emit(self, payload: dict):
        now = monotonic()
        if (payload.get("status") == "downloading" and 
            now - self._last_emit < self._throttle_interval):
            return
        
        self._last_emit = now
        payload["downloader"] = self.downloader_type
        DownloadStatus[self.session_id] = payload
        socketio.emit("download_progress", payload, room=self.session_id)

    def progress_hook(self, d: dict):
        try:
            status = d.get("status")
            
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0) or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                percent = min(99.9, downloaded / total * 100) if total else 0.0
                
                speed = d.get("speed", 0) or 0
                eta = d.get("eta", 0) or 0
                
                eta_str = "--"
                if eta:
                    if eta < 60:
                        eta_str = f"{int(eta)}s"
                    elif eta < 3600:
                        eta_str = f"{int(eta//60)}m{int(eta%60)}s"
                    else:
                        eta_str = f"{int(eta//3600)}h{int((eta%3600)//60)}m"
                
                payload = {
                    "status": "downloading",
                    "percentage": round(percent, 1),
                    "downloaded": format_bytes(downloaded),
                    "total": format_bytes(total),
                    "speed": f"{format_bytes(speed)}/s" if speed > 0 else "计算中...",
                    "eta": eta_str,
                    "fragments_downloaded": d.get("fragment_index", 0),
                    "total_fragments": d.get("fragment_count", 0),
                }
                self._emit(payload)

            elif status == "finished":
                fname = os.path.basename(d.get("filename", ""))
                ext = os.path.splitext(fname)[1]
                dtype, icon = get_file_type_info(ext)
                
                is_original = is_original_format_file(fname)
                format_type = "原始格式" if is_original else "转换后"
                downloader_text = "aria2c加速" if self.downloader_type == "aria2c" else "yt-dlp优化"
                
                payload = {
                    "status": "finished",
                    "percentage": 100,
                    "filename": fname,
                    "filepath": d.get("filename"),
                    "download_type": dtype,
                    "is_original": is_original,
                    "message": f"{icon} {format_type}{get_type_text(dtype)}下载完成: {fname} ({downloader_text})",
                }
                self._emit(payload)

        except Exception as e:
            self._emit({"status": "error", "message": f"进度回调异常: {e}"})

# yt-dlp配置生成
def generate_ydl_options(opts: dict, prog: DownloadProgress):
    downloader_type = opts.get("downloader", "ytdlp")
    
    base = {
        "progress_hooks": [prog.progress_hook],
        "no_warnings": False,
        "http_headers": {"User-Agent": get_optimal_user_agent()},
        "retries": PERFORMANCE_CONFIG["retries"],
        "fragment_retries": PERFORMANCE_CONFIG["fragment_retries"],
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "socket_timeout": PERFORMANCE_CONFIG["socket_timeout"],
        "buffersize": PERFORMANCE_CONFIG["buffersize"],
        "http_chunk_size": PERFORMANCE_CONFIG["http_chunk_size"],
        "sleep_interval_requests": PERFORMANCE_CONFIG["sleep_interval_requests"],
        "keep_fragments": False,
        "prefer_free_formats": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "web", "ios", "android"],
                "formats": "missing_pot",
                "player_skip": ["configs"],
                "bypass_native_jsi": True,
            }
        },
        "source_address": None,
        "force_ipv4": False,
        "sleep_interval": 2,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "age_limit": None,
        "cachedir": False,
    }

    if downloader_type == "aria2c" and PERFORMANCE_CONFIG["has_aria2c"]:
        base.update({
            "external_downloader": "aria2c",
            "external_downloader_args": {
                "aria2c": [
                    "--max-connection-per-server", str(PERFORMANCE_CONFIG["aria2c_max_connection_per_server"]),
                    "--split", str(PERFORMANCE_CONFIG["aria2c_split"]),
                    "--min-split-size", PERFORMANCE_CONFIG["aria2c_min_split_size"],
                    "--max-tries", str(PERFORMANCE_CONFIG["retries"]),
                    "--retry-wait", "1",
                    "--timeout", "60",
                    "--connect-timeout", "30",
                    "--summary-interval", "0",
                    "--console-log-level", "warn",
                    "--download-result", "hide",
                    "--user-agent", get_optimal_user_agent(),
                ]
            },
            "concurrent_fragments": 1,
        })
    else:
        base.update({
            "external_downloader": None,
            "concurrent_fragments": PERFORMANCE_CONFIG["concurrent_fragments"],
        })

    dtype = opts["type"]

    if dtype == "video":
        quality_map = {
            "best": "best/worst",
            "720p": "best[height<=720]/best/worst", 
            "480p": "best[height<=480]/best/worst",
            "360p": "best[height<=360]/best/worst",
            "worst": "worst/best",
        }
        fmt = quality_map.get(opts.get("video_quality", "720p"), "best/worst")
        video_format = opts.get("video_format", "original")
        
        if video_format == "original":
            base.update({
                "format": fmt,
                "outtmpl": os.path.join(DOWNLOAD_DIR, ORIGINAL_OUTTMPL),
            })
        else:
            postprocessors = []
            if video_format in ["mp4", "mkv", "avi"]:
                postprocessors.append({
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": video_format,
                })
            
            base.update({
                "format": fmt,
                "outtmpl": os.path.join(DOWNLOAD_DIR, SAFE_OUTTMPL),
            })
            if postprocessors:
                base["postprocessors"] = postprocessors

    elif dtype == "audio":
        audio_format = opts.get("audio_format", "mp3")
        
        if audio_format == "original":
            base.update({
                "format": "bestaudio/best",
                "outtmpl": os.path.join(DOWNLOAD_DIR, ORIGINAL_OUTTMPL),
                "writeinfojson": False,
                "writethumbnail": False,
            })
        else:
            if audio_format not in ["mp3", "wav"]:
                audio_format = "mp3"
            
            audio_quality = opts.get("audio_quality", "192")
            
            base.update({
                "format": "bestaudio[ext=m4a]/bestaudio[ext=aac]/bestaudio[ext=mp3]/bestaudio/best",
                "outtmpl": os.path.join(DOWNLOAD_DIR, SAFE_OUTTMPL),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": str(audio_quality),
                    "nopostoverwrites": False,
                }],
                "writeinfojson": False,
                "writethumbnail": False,
                "embedsubtitles": False,
            })
            
            if opts.get("embed_thumbnail") and audio_format == "mp3":
                base["postprocessors"].append({
                    "key": "EmbedThumbnail", 
                    "already_have_thumbnail": False
                })
            
            if opts.get("embed_metadata"):
                base["postprocessors"].append({
                    "key": "FFmpegMetadata",
                    "add_metadata": True,
                })

    return base

# 下载任务执行
def download_media(opts: dict, session_id: str):
    url = opts["url"]
    dtype = opts["type"]
    downloader_type = opts.get("downloader", "ytdlp")
    
    if dtype == "audio" and opts.get("audio_format", "mp3") != "original":
        if not check_ffmpeg_availability():
            progress = DownloadProgress(session_id, downloader_type)
            progress._emit({
                "status": "error", 
                "message": "FFmpeg未安装，无法进行音频转换。请安装FFmpeg或选择原始格式下载。"
            })
            return
    
    is_original = False
    if dtype == "audio":
        audio_format = opts.get("audio_format", "mp3")
        is_original = (audio_format == "original")
    elif dtype == "video":
        video_format = opts.get("video_format", "original")
        is_original = (video_format == "original")
    
    progress = DownloadProgress(session_id, downloader_type)
    ydl_opts = generate_ydl_options(opts, progress)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0)
            
            format_type = "原始格式" if is_original else "转换后"
            type_text = get_type_text(dtype)
            
            filesize = info.get("filesize") or info.get("filesize_approx")
            if not filesize and info.get("formats"):
                for fmt in info["formats"]:
                    if fmt.get("filesize"):
                        filesize = fmt["filesize"]
                        break
                    elif fmt.get("filesize_approx"):
                        filesize = fmt["filesize_approx"]
                        break
            
            downloader_name = "aria2c加速器" if downloader_type == "aria2c" else "yt-dlp优化版"
            
            progress._emit({
                "status": "starting",
                "title": title,
                "type": dtype,
                "is_original": is_original,
                "duration": duration,
                "estimated_size": filesize,
                "downloader": downloader_name,
                "message": f"🚀 使用{downloader_name} | {format_type}{type_text}: {title}",
            })

            ydl.download([url])

    except Exception as exc:
        error_msg = str(exc)
        if "ffmpeg" in error_msg.lower():
            error_msg = "FFmpeg处理失败，建议尝试原始格式下载。"
        elif "postprocessor" in error_msg.lower():
            error_msg = "后处理失败，建议尝试原始格式下载。"
        progress._emit({"status": "error", "message": f"下载失败: {error_msg}"})

# Flask路由
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    dtype = data.get("type", "video")
    session_id = data.get("session_id")
    downloader = data.get("downloader", "ytdlp")

    if not url:
        return jsonify({"error": "缺少 URL"}), 400
    if dtype not in {"video", "audio"}:
        return jsonify({"error": f"不支持的下载类型: {dtype}"}), 400
    if downloader not in {"ytdlp", "aria2c"}:
        return jsonify({"error": f"不支持的下载器类型: {downloader}"}), 400
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError
    except ValueError:
        return jsonify({"error": "无效的 URL"}), 400
    if not session_id:
        return jsonify({"error": "缺少 session_id"}), 400

    if dtype == "audio":
        audio_format = data.get("audio_format", "mp3")
        if audio_format not in ["original", "mp3", "wav"]:
            data["audio_format"] = "mp3"

    if downloader == "aria2c":
        is_available, message = check_aria2c_availability()
        if not is_available:
            data["downloader"] = "ytdlp"
            downloader = "ytdlp"

    EXECUTOR.submit(download_media, data, session_id)
    
    is_original = False
    format_info = "转换格式"
    
    if dtype == "audio":
        audio_format = data.get("audio_format", "mp3")
        is_original = (audio_format == "original")
        format_info = "原始格式" if is_original else f"{audio_format.upper()}格式"
    elif dtype == "video":
        video_format = data.get("video_format", "original")
        is_original = (video_format == "original")
        format_info = "原始格式" if is_original else f"{video_format.upper()}格式"
    
    downloader_name = "aria2c加速器" if downloader == "aria2c" else "yt-dlp优化版"
    
    return jsonify({
        "message": "任务已提交",
        "session_id": session_id,
        "type": dtype,
        "format": format_info,
        "is_original": is_original,
        "downloader": downloader_name,
        "performance_mode": f"高性能模式 ({downloader_name})",
        "supported_audio_formats": ["original", "mp3", "wav"] if dtype == "audio" else None,
    })

@app.route("/api/status/<session_id>")
def api_status(session_id: str):
    return jsonify(DownloadStatus.get(session_id, {"status": "not_found"}))

@app.route("/api/downloads")
def api_downloads():
    files = []
    for fname in os.listdir(DOWNLOAD_DIR):
        fpath = os.path.join(DOWNLOAD_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        stat = os.stat(fpath)
        ext = os.path.splitext(fname)[1]
        ftype, ficon = get_file_type_info(ext)
        
        is_original = is_original_format_file(fname)
        
        files.append({
            "name": fname,
            "size": stat.st_size,
            "size_formatted": format_bytes(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "download_url": f"/api/download_file/{fname}",
            "file_type": ftype,
            "file_icon": ficon,
            "is_original": is_original,
        })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(files)

@app.route("/api/download_file/<path:filename>")
def api_download_file(filename: str):
    fpath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在"}), 404

    resp = send_file(
        fpath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream",
        conditional=True,
    )
    qname = quote(filename)
    resp.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{qname}; filename=\"{qname}\""
    )
    return resp

@app.route("/api/test-aria2c")
def test_aria2c():
    is_available, message = check_aria2c_availability()
    
    if not PERFORMANCE_CONFIG["has_aria2c"]:
        return jsonify({"status": "not_found", "message": message})
    
    if is_available:
        return jsonify({
            "status": "available",
            "message": message,
            "aria2c_config": {
                "max_connection_per_server": PERFORMANCE_CONFIG["aria2c_max_connection_per_server"],
                "split": PERFORMANCE_CONFIG["aria2c_split"],
                "min_split_size": PERFORMANCE_CONFIG["aria2c_min_split_size"],
            }
        })
    else:
        return jsonify({"status": "error", "message": message})

@app.route("/api/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "message": "服务器运行正常",
        "timestamp": datetime.now().isoformat(),
        "server_info": {
            "host": "0.0.0.0",
            "port": 5000,
            "workers": MAX_WORKERS,
        }
    })

# Socket.IO事件
@socketio.on("connect")
def on_connect():
    emit("connected", {"status": "connected", "message": "服务器连接成功"})

@socketio.on("disconnect")
def on_disconnect():
    pass

@socketio.on("join_session")
def on_join(data: dict):
    session_id = data.get("session_id")
    if session_id:
        join_room(session_id)
        current = DownloadStatus.get(session_id)
        if current:
            emit("download_progress", current)

@socketio.on("ping")
def on_ping(data):
    emit("pong", {"message": "服务器响应正常", "timestamp": datetime.now().isoformat()})

# 程序入口
if __name__ == "__main__":
    print("🎬 Downloader server running at http://0.0.0.0:5000")
    print(f"✨ 线程池: {MAX_WORKERS} 工作线程")
    print(f"⚡ aria2c: {'可用' if PERFORMANCE_CONFIG['has_aria2c'] else '不可用'}")
    print(f"🎵 FFmpeg: {'可用' if check_ffmpeg_availability() else '不可用'}")
    
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False)