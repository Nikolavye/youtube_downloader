#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app_ultimate.py – Flask-SocketIO 后端 (终极优化版 + 下载器选择)

结合两个版本的优势：
• 线程池复用 + 高度优化的yt-dlp内置下载器
• 可选择aria2c外部加速器
• 原始格式支持 + 格式转换选择  
• 性能监控 + 智能重试策略
• 进度节流 + User-Agent 轮换
• 确保进度条正常显示
• 修复音频下载问题，仅支持mp3和wav
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
import re
import json
import tempfile

import yt_dlp
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)
from flask_socketio import SocketIO, emit, join_room

# ────────────── 目录与常量 ──────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SAFE_OUTTMPL = "%(title,id)s.%(ext)s"
ORIGINAL_OUTTMPL = "%(title,id)s[original].%(ext)s"

# ────────────── Flask & Socket.IO ──────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me"

# 添加CORS处理
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode="threading",
    logger=True,  # 启用详细日志
    engineio_logger=True  # 启用引擎日志
)

# ────────────── 线程池与下载状态缓存 ──────────────
MAX_WORKERS = int(os.getenv("DL_WORKERS", "6"))
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="DownloadWorker")
DownloadStatus: dict[str, dict] = {}

# ────────────── 性能配置 ──────────────
PERFORMANCE_CONFIG = {
    # 检测是否安装 aria2c
    "has_aria2c": shutil.which("aria2c") is not None,
    
    # 高性能yt-dlp配置
    "concurrent_fragments": 16,  # 提高到16线程并发
    "buffersize": 1024 * 1024,  # 1MB缓冲区
    "http_chunk_size": 10 * 1024 * 1024,  # 10MB块大小
    
    # 重试策略
    "retries": 20,  # 增加重试次数
    "fragment_retries": 30,
    
    # 进度推送节流
    "progress_throttle": 0.1,  # 100ms更频繁的更新
    
    # 网络优化
    "socket_timeout": 60,
    "sleep_interval_requests": 0.2,  # 减少请求间隔
    
    # aria2c 配置
    "aria2c_max_connection_per_server": 16,
    "aria2c_split": 16,
    "aria2c_min_split_size": "1M",
    
    # User-Agent 池
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
    ]
}

# ────────────── 工具函数 ──────────────
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
    """判断文件是否为原始格式下载的文件"""
    return "[original]" in filename


def get_optimal_user_agent():
    """智能选择User-Agent"""
    return random.choice(PERFORMANCE_CONFIG["user_agents"])


def check_aria2c_availability():
    """检查aria2c是否可用"""
    if not PERFORMANCE_CONFIG["has_aria2c"]:
        return False, "aria2c 未安装或不在 PATH 中"
    
    try:
        result = subprocess.run(
            ["aria2c", "--version"], 
            capture_output=True, 
            text=True, 
            timeout=5
        )
        
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            return True, f"aria2c 可用: {version_line}"
        else:
            return False, f"aria2c 执行错误: {result.stderr}"
            
    except subprocess.TimeoutExpired:
        return False, "aria2c 响应超时"
    except Exception as e:
        return False, f"测试aria2c时出错: {e}"


def parse_aria2c_progress(line: str) -> dict:
    """解析aria2c的进度输出"""
    try:
        # aria2c进度格式: [#1 SIZE:256KiB/1.5MiB(16%) CN:1 DL:256KiB ETA:5s SPD:51.2KiB/s]
        if not line.startswith('[#') or 'SIZE:' not in line:
            return None
        
        # 提取SIZE信息 - 已下载/总大小(百分比)
        size_match = re.search(r'SIZE:([^/]+)/([^(]+)\((\d+)%\)', line)
        if not size_match:
            return None
        
        downloaded_str = size_match.group(1).strip()
        total_str = size_match.group(2).strip()
        percentage = int(size_match.group(3))
        
        # 提取速度信息
        speed_match = re.search(r'SPD:([^\]]+)', line)
        speed_str = speed_match.group(1).strip() if speed_match else "0B/s"
        
        # 提取ETA信息
        eta_match = re.search(r'ETA:([^\s\]]+)', line)
        eta_str = eta_match.group(1).strip() if eta_match else "--"
        
        # 转换大小单位为字节
        def parse_size(size_str):
            if not size_str or size_str == "0B":
                return 0
            
            units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
            
            for unit, multiplier in units.items():
                if size_str.endswith(unit):
                    try:
                        return int(float(size_str[:-len(unit)]) * multiplier)
                    except:
                        return 0
            return 0
        
        downloaded_bytes = parse_size(downloaded_str)
        total_bytes = parse_size(total_str)
        
        return {
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "percentage": percentage,
            "speed_str": speed_str,
            "eta_str": eta_str if eta_str != "-" else "--"
        }
        
    except Exception as e:
        print(f"解析aria2c进度失败: {e}")
        return None


class Aria2cDownloader:
    """自定义aria2c下载器，支持实时进度监控"""
    
    def __init__(self, session_id: str, progress_callback):
        self.session_id = session_id
        self.progress_callback = progress_callback
        self.process = None
        self.cancelled = False
    
    def download(self, url: str, output_path: str, filename: str):
        """使用aria2c下载并监控进度"""
        try:
            # 构建aria2c命令
            cmd = [
                "aria2c",
                "--max-connection-per-server", str(PERFORMANCE_CONFIG["aria2c_max_connection_per_server"]),
                "--split", str(PERFORMANCE_CONFIG["aria2c_split"]),
                "--min-split-size", PERFORMANCE_CONFIG["aria2c_min_split_size"],
                "--max-tries", str(PERFORMANCE_CONFIG["retries"]),
                "--retry-wait", "1",
                "--timeout", "60",
                "--connect-timeout", "30",
                "--console-log-level", "info",  # 启用进度输出
                "--summary-interval", "1",  # 每秒更新进度
                "--download-result", "default",  # 显示下载结果
                "--user-agent", get_optimal_user_agent(),
                "--dir", output_path,
                "--out", filename,
                "--allow-overwrite", "true",
                url
            ]
            
            print(f"🚀 启动aria2c下载: {filename}")
            
            # 启动进程
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            
            # 监控进度
            last_progress_time = 0
            while True:
                if self.cancelled:
                    if self.process:
                        self.process.terminate()
                    break
                
                line = self.process.stdout.readline()
                if not line:
                    break
                
                line = line.strip()
                if line:
                    print(f"[aria2c] {line}")
                    
                    # 解析进度信息
                    progress_info = parse_aria2c_progress(line)
                    if progress_info:
                        current_time = monotonic()
                        # 节流：每100ms更新一次进度
                        if current_time - last_progress_time >= 0.1:
                            self.send_progress(progress_info)
                            last_progress_time = current_time
            
            # 等待进程完成
            return_code = self.process.wait()
            
            if return_code == 0 and not self.cancelled:
                # 下载完成
                output_file = os.path.join(output_path, filename)
                if os.path.exists(output_file):
                    self.send_completion(output_file, filename)
                    return True
                else:
                    self.send_error("下载完成但文件不存在")
                    return False
            else:
                if not self.cancelled:
                    self.send_error(f"aria2c下载失败，退出码: {return_code}")
                return False
                
        except Exception as e:
            self.send_error(f"aria2c下载异常: {e}")
            return False
    
    def send_progress(self, progress_info):
        """发送进度信息"""
        try:
            downloaded = progress_info["downloaded_bytes"]
            total = progress_info["total_bytes"]
            percentage = progress_info["percentage"]
            speed_str = progress_info["speed_str"]
            eta_str = progress_info["eta_str"]
            
            payload = {
                "status": "downloading",
                "percentage": min(99.9, percentage),
                "downloaded": format_bytes(downloaded),
                "total": format_bytes(total),
                "speed": speed_str,
                "eta": eta_str,
                "downloader": "aria2c"
            }
            
            DownloadStatus[self.session_id] = payload
            socketio.emit("download_progress", payload, room=self.session_id)
            
        except Exception as e:
            print(f"发送aria2c进度失败: {e}")
    
    def send_completion(self, filepath, filename):
        """发送完成信息"""
        try:
            ext = os.path.splitext(filename)[1]
            dtype, icon = get_file_type_info(ext)
            is_original = is_original_format_file(filename)
            format_type = "原始格式" if is_original else "转换后"
            
            payload = {
                "status": "finished",
                "percentage": 100,
                "filename": filename,
                "filepath": filepath,
                "download_type": dtype,
                "is_original": is_original,
                "message": f"{icon} {format_type}{get_type_text(dtype)}下载完成: {filename} (aria2c加速)",
                "downloader": "aria2c"
            }
            
            DownloadStatus[self.session_id] = payload
            socketio.emit("download_progress", payload, room=self.session_id)
            
        except Exception as e:
            print(f"发送aria2c完成状态失败: {e}")
    
    def send_error(self, message):
        """发送错误信息"""
        payload = {
            "status": "error",
            "message": f"aria2c错误: {message}",
            "downloader": "aria2c"
        }
        
        DownloadStatus[self.session_id] = payload
        socketio.emit("download_progress", payload, room=self.session_id)
    
    def cancel(self):
        """取消下载"""
        self.cancelled = True
        if self.process:
            try:
                self.process.terminate()
            except:
                pass


def check_ffmpeg_availability():
    """检查FFmpeg是否可用"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], 
            capture_output=True, 
            text=True, 
            timeout=5
        )
        return result.returncode == 0
    except:
        return False


# ────────────── 下载进度回调 (带节流) ──────────────
class DownloadProgress:
    def __init__(self, session_id: str, downloader_type: str = "ytdlp"):
        self.session_id = session_id
        self.downloader_type = downloader_type
        self._last_emit = 0.0
        self._throttle_interval = PERFORMANCE_CONFIG["progress_throttle"]

    def _emit(self, payload: dict):
        now = monotonic()
        # 进度推送节流：除了开始和完成状态，其他状态进行节流
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
                
                # 格式化ETA
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


# ────────────── yt-dlp 配置生成 ──────────────
def generate_ydl_options(opts: dict, prog: DownloadProgress):
    """生成yt-dlp配置，根据下载器类型选择不同的策略"""
    
    downloader_type = opts.get("downloader", "ytdlp")
    
    # 判断是否为原始格式
    dtype = opts["type"]
    is_original_format = False
    
    if dtype == "audio":
        audio_format = opts.get("audio_format", "mp3")
        is_original_format = (audio_format == "original")
    elif dtype == "video":
        video_format = opts.get("video_format", "original")
        is_original_format = (video_format == "original")
    
    # 基础配置
    base = {
        "progress_hooks": [prog.progress_hook],
        "no_warnings": False,  # 启用警告以便调试
        "http_headers": {
            "User-Agent": get_optimal_user_agent(),
        },
        
        # 网络与重试优化
        "retries": PERFORMANCE_CONFIG["retries"],
        "fragment_retries": PERFORMANCE_CONFIG["fragment_retries"],
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,  # 不忽略错误，方便调试
        "socket_timeout": PERFORMANCE_CONFIG["socket_timeout"],
        
        # I/O 优化
        "buffersize": PERFORMANCE_CONFIG["buffersize"],
        "http_chunk_size": PERFORMANCE_CONFIG["http_chunk_size"],
        "sleep_interval_requests": PERFORMANCE_CONFIG["sleep_interval_requests"],
        
        # 其他优化
        "keep_fragments": False,
        "prefer_free_formats": True,
        
        # 403错误解决方案 - 增强YouTube兼容性
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "web", "ios", "android"],  # 多客户端策略
                "formats": "missing_pot",  # 启用缺少PO Token的格式
                "player_skip": ["configs"],  # 跳过一些配置请求
                "bypass_native_jsi": True,  # 绕过原生JSI
            }
        },
        
        # 连接优化
        "source_address": None,  # 让系统自动选择
        "force_ipv4": False,
        "sleep_interval": 2,  # 增加请求间隔避免被封
        "sleep_interval_requests": 1,  # 请求间休眠
        
        # 403错误缓解措施
        "geo_bypass": True,  # 启用地理绕过
        "geo_bypass_country": "US",  # 设置绕过国家
        "age_limit": None,  # 移除年龄限制
        
        # 缓存管理
        "cachedir": False,  # 禁用缓存避免403
    }

    # 根据下载器类型配置
    if (downloader_type == "aria2c" and 
        PERFORMANCE_CONFIG["has_aria2c"] and 
        not is_original_format):
        # aria2c但需要格式转换时，回退到yt-dlp + 外部aria2c
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
                    "--summary-interval", "0",  # 禁用aria2c自己的进度输出
                    "--console-log-level", "warn",  # 减少日志输出
                    "--download-result", "hide",  # 隐藏下载结果
                    "--user-agent", get_optimal_user_agent(),
                ]
            },
            # aria2c模式下减少并发片段，让aria2c自己处理
            "concurrent_fragments": 1,
        })
    else:
        # 使用yt-dlp内置下载器（确保进度显示正常）
        base.update({
            "external_downloader": None,
            "concurrent_fragments": PERFORMANCE_CONFIG["concurrent_fragments"],
        })

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
            # 原始格式音频 - 直接下载最佳音频
            base.update({
                "format": "bestaudio/best",
                "outtmpl": os.path.join(DOWNLOAD_DIR, ORIGINAL_OUTTMPL),
                "writeinfojson": False,
                "writethumbnail": False,
            })
        else:
            # 音频转换 - 仅支持mp3和wav
            if audio_format not in ["mp3", "wav"]:
                audio_format = "mp3"  # 默认回退到mp3
            
            audio_quality = opts.get("audio_quality", "192")
            
            # 简化音频格式选择
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
            
            # 可选的额外处理
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


# ────────────── 下载任务执行 (线程池) ──────────────
def download_media(opts: dict, session_id: str):
    """在线程池中执行的下载任务"""
    url = opts["url"]
    dtype = opts["type"]
    downloader_type = opts.get("downloader", "ytdlp")
    
    # 检查FFmpeg可用性（音频转换需要）
    if dtype == "audio" and opts.get("audio_format", "mp3") != "original":
        if not check_ffmpeg_availability():
            progress = DownloadProgress(session_id, downloader_type)
            progress._emit({
                "status": "error", 
                "message": "FFmpeg未安装或不可用，无法进行音频转换。请安装FFmpeg或选择原始格式下载。"
            })
            return
    
    is_original = False
    if dtype == "audio":
        audio_format = opts.get("audio_format", "mp3")
        is_original = (audio_format == "original")
    elif dtype == "video":
        video_format = opts.get("video_format", "original")
        is_original = (video_format == "original")
    
    # 如果使用aria2c下载器且下载原始格式，使用自定义aria2c下载器
    if (downloader_type == "aria2c" and 
        PERFORMANCE_CONFIG["has_aria2c"] and 
        is_original):
        
        try:
            # 使用自定义aria2c下载器
            download_with_aria2c(opts, session_id)
        except Exception as exc:
            print(f"aria2c下载失败: {exc}")
            progress = DownloadProgress(session_id, downloader_type)
            progress._emit({"status": "error", "message": f"aria2c下载失败: {exc}"})
    else:
        # 使用传统yt-dlp下载器
        download_with_ytdlp(opts, session_id)


def download_with_aria2c(opts: dict, session_id: str):
    """使用自定义aria2c下载器（仅支持原始格式）"""
    url = opts["url"]
    dtype = opts["type"]
    
    try:
        # 首先用yt-dlp提取视频信息和下载链接
        print(f"📋 提取视频信息和下载链接: {url}")
        
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "simulate": True,  # 仅提取信息，不下载
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded", "web"],
                    "formats": "missing_pot",
                }
            },
            "http_headers": {
                "User-Agent": get_optimal_user_agent(),
            },
            "cachedir": False,
        }
        
        # 根据类型选择格式
        if dtype == "video":
            quality_map = {
                "best": "best[ext=mp4]/best/worst",
                "720p": "best[height<=720][ext=mp4]/best[height<=720]/best/worst", 
                "480p": "best[height<=480][ext=mp4]/best[height<=480]/best/worst",
                "360p": "best[height<=360][ext=mp4]/best[height<=360]/best/worst",
                "worst": "worst[ext=mp4]/worst/best",
            }
            ydl_opts["format"] = quality_map.get(opts.get("video_quality", "720p"), "best[ext=mp4]/best/worst")
        else:  # audio
            ydl_opts["format"] = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"
        
        # 提取信息
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        title = info.get("title", "Unknown")
        duration = info.get("duration", 0)
        
        # 获取实际下载URL和格式信息
        download_url = None
        filesize = 0
        selected_format = None
        
        # 优先从formats中选择合适的格式
        if info.get("formats") and isinstance(info["formats"], list):
            formats = info["formats"]
            print(f"📋 可用格式数量: {len(formats)}")
            
            # 按质量和兼容性排序选择格式
            best_formats = []
            
            for fmt in formats:
                url_field = fmt.get("url")
                if not url_field or not isinstance(url_field, str) or not url_field.startswith(("http://", "https://")):
                    continue  # 跳过无效URL
                
                if dtype == "video":
                    # 视频格式：需要有视频编码且优先选择mp4
                    if (fmt.get("vcodec") and fmt.get("vcodec") != "none" and 
                        fmt.get("acodec") and fmt.get("acodec") != "none"):
                        # 同时有视频和音频的格式
                        quality_score = fmt.get("height", 0) or 0
                        if fmt.get("ext") == "mp4":
                            quality_score += 1000  # mp4格式优先
                        best_formats.append((quality_score, fmt))
                        
                else:  # audio
                    # 音频格式：需要有音频编码
                    if fmt.get("acodec") and fmt.get("acodec") != "none":
                        quality_score = fmt.get("abr", 0) or 0
                        if fmt.get("ext") in ["webm", "m4a", "mp3"]:
                            quality_score += 100  # 常见音频格式优先
                        best_formats.append((quality_score, fmt))
            
            # 按质量分数排序，选择最佳格式
            if best_formats:
                best_formats.sort(key=lambda x: x[0], reverse=True)
                selected_format = best_formats[0][1]
                download_url = selected_format["url"]
                filesize = selected_format.get("filesize") or selected_format.get("filesize_approx") or 0
                
                print(f"✅ 选择格式: {selected_format.get('format_id', 'unknown')} "
                      f"({selected_format.get('ext', 'unknown')}, "
                      f"{selected_format.get('resolution', 'unknown')})")
        
        # 如果formats中没找到，尝试直接URL
        if not download_url and info.get("url"):
            url_field = info["url"]
            if isinstance(url_field, str) and url_field.startswith(("http://", "https://")):
                download_url = url_field
                filesize = info.get("filesize") or info.get("filesize_approx") or 0
                print(f"✅ 使用直接URL")
        
        if not download_url:
            raise Exception("无法获取有效的下载链接")
        
        # 验证URL格式
        if not isinstance(download_url, str) or not download_url.startswith(("http://", "https://")):
            raise Exception(f"获取的下载链接格式无效: {type(download_url)} = {download_url}")
        
        print(f"✅ 成功获取下载链接，文件大小: {format_bytes(filesize)}")
        print(f"🔗 下载URL: {download_url[:100]}...")  # 只显示前100个字符
        
        # 生成文件名
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
        if selected_format:
            ext = selected_format.get("ext", "mp4" if dtype == "video" else "webm")
        else:
            ext = "mp4" if dtype == "video" else "webm"
        
        filename = f"{safe_title}[original].{ext}"
        
        # 发送开始下载状态
        progress = DownloadProgress(session_id, "aria2c")
        progress._emit({
            "status": "starting",
            "title": title,
            "type": dtype,
            "is_original": True,
            "duration": duration,
            "estimated_size": filesize,
            "downloader": "aria2c加速器",
            "message": f"🚀 使用aria2c加速器 | 原始格式{get_type_text(dtype)}: {title}",
        })
        
        # 使用自定义aria2c下载器
        aria2c_downloader = Aria2cDownloader(session_id, progress._emit)
        success = aria2c_downloader.download(download_url, DOWNLOAD_DIR, filename)
        
        if success:
            print(f"✅ aria2c下载完成: {title}")
        else:
            print(f"❌ aria2c下载失败: {title}")
            
    except Exception as exc:
        print(f"❌ aria2c下载过程失败: {exc}")
        error_msg = str(exc)
        
        # 提供友好的错误信息和建议
        if "无法获取" in error_msg or "格式无效" in error_msg:
            error_msg += " 建议尝试使用yt-dlp下载器，或选择其他视频质量。"
        elif "Unrecognized URI" in error_msg:
            error_msg = "aria2c无法识别下载链接格式，建议使用yt-dlp下载器。"
        
        progress = DownloadProgress(session_id, "aria2c")
        progress._emit({
            "status": "error", 
            "message": f"aria2c下载失败: {error_msg}",
            "suggestion": "可以尝试使用yt-dlp下载器作为替代方案"
        })


def download_with_ytdlp(opts: dict, session_id: str):
    """使用传统yt-dlp下载器"""
    url = opts["url"]
    dtype = opts["type"]
    downloader_type = opts.get("downloader", "ytdlp")
    
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
            # 预先提取信息
            print(f"开始提取视频信息: {url}")
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0)
            
            format_type = "原始格式" if is_original else "转换后"
            type_text = get_type_text(dtype)
            
            # 获取文件大小信息
            filesize = info.get("filesize") or info.get("filesize_approx")
            
            # 尝试从formats中获取更准确的文件大小
            if not filesize and info.get("formats"):
                for fmt in info["formats"]:
                    if fmt.get("filesize"):
                        filesize = fmt["filesize"]
                        break
                    elif fmt.get("filesize_approx"):
                        filesize = fmt["filesize_approx"]
                        break
            
            print(f"视频信息提取完成: {title}, 时长: {duration}s, 预估大小: {format_bytes(filesize) if filesize else '未知'}")
            
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

            # 执行下载
            print(f"开始下载: {title} (使用 {downloader_name})")
            ydl.download([url])
            print(f"下载完成: {title}")

    except Exception as exc:
        print(f"下载失败: {exc}")
        error_msg = str(exc)
        if "ffmpeg" in error_msg.lower():
            error_msg = "FFmpeg处理失败，可能是编解码器问题。建议尝试原始格式下载。"
        elif "postprocessor" in error_msg.lower():
            error_msg = "后处理失败，建议尝试原始格式下载。"
        progress._emit({"status": "error", "message": f"下载失败: {error_msg}"})


# ────────────── Flask 路由 ──────────────
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

    # 参数验证
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

    # 音频格式验证 - 仅支持mp3和wav
    if dtype == "audio":
        audio_format = data.get("audio_format", "mp3")
        if audio_format not in ["original", "mp3", "wav"]:
            data["audio_format"] = "mp3"  # 回退到默认mp3

    # 检查aria2c可用性
    if downloader == "aria2c":
        is_available, message = check_aria2c_availability()
        if not is_available:
            # 自动回退到yt-dlp
            data["downloader"] = "ytdlp"
            downloader = "ytdlp"
            print(f"aria2c不可用，自动回退到yt-dlp: {message}")

    # 提交到线程池
    EXECUTOR.submit(download_media, data, session_id)
    
    # 构建响应
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
    """优化的文件下载"""
    fpath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在"}), 404

    resp = send_file(
        fpath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream",
        conditional=True,  # 支持断点续传
    )
    qname = quote(filename)
    resp.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{qname}; filename=\"{qname}\""
    )
    return resp


@app.route("/api/test-aria2c")
def test_aria2c():
    """测试aria2c是否可用"""
    is_available, message = check_aria2c_availability()
    
    if not PERFORMANCE_CONFIG["has_aria2c"]:
        return jsonify({
            "status": "not_found",
            "message": message
        })
    
    if is_available:
        return jsonify({
            "status": "available_but_not_used",
            "message": message,
            "aria2c_config": {
                "max_connection_per_server": PERFORMANCE_CONFIG["aria2c_max_connection_per_server"],
                "split": PERFORMANCE_CONFIG["aria2c_split"],
                "min_split_size": PERFORMANCE_CONFIG["aria2c_min_split_size"],
            }
        })
    else:
        return jsonify({
            "status": "error", 
            "message": message
        })


@app.route("/api/clear-cache")
def clear_cache():
    """清理yt-dlp缓存以解决403错误"""
    try:
        # 尝试清理yt-dlp缓存
        cache_dirs = [
            os.path.expanduser("~/.cache/yt-dlp"),
            os.path.expanduser("~/.cache/youtube-dl"),
            os.path.join(BASE_DIR, ".cache"),
        ]
        
        cleared_dirs = []
        for cache_dir in cache_dirs:
            if os.path.exists(cache_dir):
                try:
                    shutil.rmtree(cache_dir)
                    cleared_dirs.append(cache_dir)
                except:
                    pass
        
        return jsonify({
            "status": "success",
            "message": f"缓存清理完成，清理了 {len(cleared_dirs)} 个缓存目录",
            "cleared_dirs": cleared_dirs
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"缓存清理失败: {e}"
        })


@app.route("/api/update-ytdlp")
def update_ytdlp():
    """尝试更新yt-dlp"""
    try:
        result = subprocess.run(
            ["pip", "install", "--upgrade", "yt-dlp"], 
            capture_output=True, 
            text=True, 
            timeout=60
        )
        
        if result.returncode == 0:
            return jsonify({
                "status": "success",
                "message": "yt-dlp更新成功",
                "output": result.stdout
            })
        else:
            return jsonify({
                "status": "error",
                "message": "yt-dlp更新失败",
                "error": result.stderr
            })
    except subprocess.TimeoutExpired:
        return jsonify({
            "status": "error",
            "message": "更新超时，请手动执行: pip install --upgrade yt-dlp"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"更新失败: {e}，请手动执行: pip install --upgrade yt-dlp"
        })


@app.route("/api/debug-url", methods=["POST"])
def debug_url():
    """调试URL提取功能"""
    try:
        data = request.get_json(force=True)
        url = data.get("url", "").strip()
        dtype = data.get("type", "video")
        
        if not url:
            return jsonify({"error": "缺少URL"}), 400
        
        # 提取信息
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "simulate": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded", "web"],
                    "formats": "missing_pot",
                }
            },
            "http_headers": {
                "User-Agent": get_optimal_user_agent(),
            },
            "cachedir": False,
        }
        
        # 根据类型选择格式
        if dtype == "video":
            ydl_opts["format"] = "best[ext=mp4]/best/worst"
        else:
            ydl_opts["format"] = "bestaudio[ext=webm]/bestaudio/best"
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        
        # 分析可用格式
        formats_info = []
        if info.get("formats"):
            for fmt in info["formats"]:
                url_field = fmt.get("url")
                formats_info.append({
                    "format_id": fmt.get("format_id"),
                    "ext": fmt.get("ext"),
                    "resolution": fmt.get("resolution"),
                    "vcodec": fmt.get("vcodec"),
                    "acodec": fmt.get("acodec"),
                    "filesize": fmt.get("filesize"),
                    "has_valid_url": isinstance(url_field, str) and url_field.startswith(("http://", "https://"))
                })
        
        return jsonify({
            "title": info.get("title"),
            "duration": info.get("duration"),
            "direct_url": info.get("url"),
            "direct_url_type": type(info.get("url")).__name__,
            "formats_count": len(info.get("formats", [])),
            "formats_sample": formats_info[:5],  # 前5个格式
            "recommended_aria2c": any(f["has_valid_url"] for f in formats_info)
        })
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "recommendation": "此视频可能不适合使用aria2c下载，建议使用yt-dlp"
        }), 500


@app.route("/api/test-download")
def test_download():
    """测试下载一个简单视频来检查403问题"""
    test_url = "https://www.youtube.com/watch?v=BaW_jenozKc"  # YouTube测试视频
    
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "simulate": True,  # 仅模拟，不实际下载
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded", "web"],
                    "formats": "missing_pot",
                }
            },
            "http_headers": {
                "User-Agent": get_optimal_user_agent(),
            },
            "cachedir": False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(test_url, download=False)
            title = info.get("title", "Unknown")
            
            return jsonify({
                "status": "success",
                "message": f"测试成功，可以正常提取视频信息: {title}",
                "title": title
            })
            
    except Exception as e:
        error_msg = str(e)
        if "403" in error_msg:
            return jsonify({
                "status": "403_error",
                "message": "检测到403错误，建议：1) 更新yt-dlp 2) 清理缓存 3) 重启应用",
                "error": error_msg
            })
        else:
            return jsonify({
                "status": "error", 
                "message": f"测试失败: {error_msg}"
            })


@app.route("/api/health")
def health_check():
    """健康检查端点"""
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


@app.route("/ping")
def ping():
    """简单的ping端点"""
    return "pong"


@app.route("/api/connection-test")
def connection_test():
    """连接测试端点"""
    return jsonify({
        "server_running": True,
        "api_working": True,
        "timestamp": datetime.now().isoformat(),
        "message": "API连接正常"
    })


@app.route("/api/troubleshooting")
def troubleshooting():
    """提供403错误的故障排除指南"""
    return jsonify({
        "403_solutions": [
            {
                "step": 1,
                "title": "更新yt-dlp",
                "description": "执行: pip install --upgrade yt-dlp",
                "api": "/api/update-ytdlp"
            },
            {
                "step": 2, 
                "title": "清理缓存",
                "description": "清理yt-dlp缓存文件",
                "api": "/api/clear-cache"
            },
            {
                "step": 3,
                "title": "重启应用",
                "description": "重启下载服务器"
            },
            {
                "step": 4,
                "title": "使用web_embedded模式",
                "description": "已在代码中自动启用多客户端策略"
            },
            {
                "step": 5,
                "title": "检查网络",
                "description": "尝试更换网络或使用VPN"
            }
        ],
        "connection_test": [
            {
                "step": 1,
                "title": "测试基本连接",
                "url": "/ping"
            },
            {
                "step": 2,
                "title": "测试API连接",
                "url": "/api/health"
            },
            {
                "step": 3,
                "title": "测试Socket.IO",
                "description": "检查浏览器控制台"
            }
        ],
        "prevention_tips": [
            "定期更新yt-dlp到最新版本",
            "避免频繁请求同一视频",
            "使用不同的User-Agent",
            "清理缓存文件",
            "使用代理或VPN"
        ]
    })


@app.route("/api/test-ffmpeg")
def test_ffmpeg():
    """测试FFmpeg是否可用"""
    is_available = check_ffmpeg_availability()
    
    if is_available:
        return jsonify({
            "status": "available",
            "message": "FFmpeg 可用，支持音频转换"
        })
    else:
        return jsonify({
            "status": "not_found",
            "message": "FFmpeg 未安装或不可用，音频转换功能受限"
        })


@app.route("/api/performance-status")
def api_performance_status():
    """返回优化配置状态"""
    return jsonify({
        "performance_config": PERFORMANCE_CONFIG,
        "system_info": {
            "thread_pool_workers": MAX_WORKERS,
            "has_aria2c": PERFORMANCE_CONFIG["has_aria2c"],
            "has_ffmpeg": check_ffmpeg_availability(),
            "using_aria2c": False,  # 运行时动态选择
            "concurrent_fragments": PERFORMANCE_CONFIG["concurrent_fragments"],
            "buffersize": PERFORMANCE_CONFIG["buffersize"],
            "http_chunk_size": PERFORMANCE_CONFIG["http_chunk_size"],
            "aria2c_available": PERFORMANCE_CONFIG["has_aria2c"],
            "supported_audio_formats": ["original", "mp3", "wav"],
        },
        "optimization_features": [
            f"🧵 线程池复用 ({MAX_WORKERS}工作线程)",
            f"🚀 yt-dlp内置优化 ({PERFORMANCE_CONFIG['concurrent_fragments']}线程并发)",
            f"⚡ aria2c外部加速器 ({'可用' if PERFORMANCE_CONFIG['has_aria2c'] else '不可用'})",
            f"🎵 FFmpeg音频转换 ({'可用' if check_ffmpeg_availability() else '不可用'})",
            f"📊 进度推送节流 ({int(PERFORMANCE_CONFIG['progress_throttle']*1000)}ms)",
            f"💾 大缓冲区 ({PERFORMANCE_CONFIG['buffersize']//1024}KB)",
            f"📦 大块下载 ({PERFORMANCE_CONFIG['http_chunk_size']//1024//1024}MB)",
            "🔄 智能User-Agent轮换",
            "🎯 原始格式极速下载",
            f"📈 智能重试策略 ({PERFORMANCE_CONFIG['retries']}次)",
            "✅ 进度显示正常工作",
            "🔗 断点续传支持",
            "🔧 动态下载器选择",
            "🎵 音频格式：MP3 + WAV",
            "🛠️ 智能错误处理",
            "🛡️ 403错误防护 (多客户端策略)",
            "🧹 自动缓存管理",
            "🔄 故障自动恢复",
            "⚡ aria2c实时进度同步",
            "🎯 智能下载器选择"
        ],
        "anti_403_features": [
            "多客户端策略 (web_embedded, web, ios, android)",
            "智能User-Agent轮换",
            "自动缓存清理",
            "请求间隔优化",
            "地理绕过启用",
            "格式兼容性增强"
        ],
        "aria2c_features": [
            "原始格式：自定义aria2c下载器 + 实时进度监控",
            "转换格式：yt-dlp + aria2c外部下载器",
            "智能选择：根据下载类型自动切换模式",
            "进度同步：实时解析aria2c输出显示进度",
            "错误处理：完整的错误信息和重试机制"
        ]
    })


# ────────────── Socket.IO 事件 ──────────────
@socketio.on("connect")
def on_connect():
    print(f"✅ 客户端连接: {request.sid}")
    emit("connected", {"status": "connected", "message": "服务器连接成功"})


@socketio.on("disconnect")
def on_disconnect():
    print(f"❌ 客户端断开: {request.sid}")


@socketio.on("join_session")
def on_join(data: dict):
    session_id = data.get("session_id")
    if session_id:
        join_room(session_id)
        print(f"🔗 客户端 {request.sid} 加入会话: {session_id}")
        current = DownloadStatus.get(session_id)
        if current:
            emit("download_progress", current)


@socketio.on("ping")
def on_ping(data):
    """处理客户端ping"""
    print(f"📡 收到ping: {request.sid}")
    emit("pong", {"message": "服务器响应正常", "timestamp": datetime.now().isoformat()})


# ────────────── Entrypoint ──────────────
if __name__ == "__main__":
    print("🎬 Downloader server running at http://0.0.0.0:5000")
    print("🚀 高性能优化版本已启用（支持下载器选择 + 403错误防护 + aria2c实时进度）！")
    print()
    print("🌐 访问地址:")
    print("   • http://localhost:5000")
    print("   • http://127.0.0.1:5000") 
    print("   • http://192.168.237.2:5000")
    print()
    print("🔧 调试端点:")
    print("   • http://localhost:5000/ping - 基本连接测试")
    print("   • http://localhost:5000/api/health - 健康检查")
    print("   • http://localhost:5000/api/connection-test - 连接测试")
    print("   • http://localhost:5000/api/downloads - API测试")
    print()
    print("✨ 性能特性:")
    print(f"   • 线程池复用: {MAX_WORKERS} 工作线程")
    print(f"   • yt-dlp内置优化: {PERFORMANCE_CONFIG['concurrent_fragments']} 线程并发")
    print(f"   • aria2c外部加速: {'可用' if PERFORMANCE_CONFIG['has_aria2c'] else '不可用'}")
    print(f"   • FFmpeg音频转换: {'可用' if check_ffmpeg_availability() else '不可用'}")
    print(f"   • 大缓冲区: {PERFORMANCE_CONFIG['buffersize']//1024}KB")
    print(f"   • 大块下载: {PERFORMANCE_CONFIG['http_chunk_size']//1024//1024}MB")
    print(f"   • 进度推送节流: {int(PERFORMANCE_CONFIG['progress_throttle']*1000)}ms")
    print(f"   • 智能重试策略: {PERFORMANCE_CONFIG['retries']}次重试")
    print("   • User-Agent 轮换池")
    print("   • 原始格式极速下载")
    print("   • ✅ 进度显示正常工作")
    print("   • 🔧 动态下载器选择")
    print("   • 🎵 音频格式：MP3 + WAV")
    print("   • 🛠️ 智能错误处理")
    print()
    print("🛡️ 403错误防护特性:")
    print("   • 🔄 多客户端策略 (web_embedded/web/ios/android)")
    print("   • 🧹 自动缓存管理")
    print("   • ⏱️ 智能请求间隔")
    print("   • 🌍 地理绕过启用")
    print("   • 🔧 故障自动恢复")
    print("   • 📱 User-Agent智能轮换")
    print()
    print("⚡ aria2c增强特性:")
    print("   • 📊 原始格式：自定义下载器 + 实时进度监控")
    print("   • 🔄 转换格式：yt-dlp + aria2c外部下载器")
    print("   • 🎯 智能选择：根据下载类型自动切换模式")
    print("   • 📡 进度同步：实时解析aria2c输出显示进度")
    print("   • 🛠️ 错误处理：完整的错误信息和重试机制")
    print()
    print("🔧 故障排除API:")
    print("   • /api/test-download - 测试下载功能")
    print("   • /api/debug-url - 调试URL提取 (POST)")
    print("   • /api/clear-cache - 清理缓存")
    print("   • /api/update-ytdlp - 更新yt-dlp")
    print("   • /api/troubleshooting - 获取故障排除指南")
    print()
    
    if PERFORMANCE_CONFIG["has_aria2c"]:
        print("   ✅ aria2c加速器可用 (含实时进度监控)")
        print(f"      - 最大连接数: {PERFORMANCE_CONFIG['aria2c_max_connection_per_server']}")
        print(f"      - 分片数: {PERFORMANCE_CONFIG['aria2c_split']}")
        print(f"      - 最小分片: {PERFORMANCE_CONFIG['aria2c_min_split_size']}")
        print("      - 原始格式下载：自定义进度监控")
        print("      - 转换格式下载：yt-dlp集成模式")
    else:
        print("   ❌ aria2c未安装，仅可使用yt-dlp内置下载器")
    
    if check_ffmpeg_availability():
        print("   ✅ FFmpeg可用，支持音频转换 (MP3/WAV)")
    else:
        print("   ❌ FFmpeg未安装，仅支持原始格式音频下载")
    
    print()
    print("💡 遇到HTTP 403错误时的解决方案:")
    print("   1. 访问 /api/update-ytdlp 更新yt-dlp")
    print("   2. 访问 /api/clear-cache 清理缓存")
    print("   3. 重启应用程序")
    print("   4. 检查网络连接或尝试VPN")
    print()
    print("🔗 如果前端显示'无法连接到服务器':")
    print("   1. 确保防火墙未阻止5000端口")
    print("   2. 测试 http://localhost:5000/ping")
    print("   3. 检查浏览器控制台错误信息")
    print("   4. 确认前端连接地址正确")
    print()
    print("⚡ aria2c进度同步说明:")
    print("   • 原始格式下载：使用自定义aria2c监控，实时显示进度")
    print("   • 转换格式下载：使用yt-dlp集成aria2c，进度可能延迟")
    print("   • 建议选择原始格式以获得最佳aria2c体验")
    
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False,
    )