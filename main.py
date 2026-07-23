from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import cv2
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RECORDINGS_DIR = BASE_DIR / "recordings"

CAMERA_DEVICE = "/dev/video0"

CAMERA_WIDTH = 1920
CAMERA_HEIGHT = 1080
CAMERA_FPS = 30

PREVIEW_FPS = 10
PREVIEW_JPEG_QUALITY = 75
TIMELAPSE_JPEG_QUALITY = 92
OUTPUT_VIDEO_FPS = 30

AUTH_USERNAME = os.environ.get("TIMELAPSE_AUTH_USERNAME", "admin")
AUTH_PASSWORD_HASH = os.environ.get("TIMELAPSE_AUTH_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("TIMELAPSE_SESSION_SECRET", "")
SESSION_COOKIE = "timelapse_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
PASSWORD_HASHER = PasswordHasher()

LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5


class TimelapseStartRequest(BaseModel):
    name: str = Field(
        default="3D Print",
        min_length=1,
        max_length=80,
    )

    interval_seconds: float = Field(
        default=10,
        ge=1,
        le=3600,
    )

    duration_hours: float | None = Field(
        default=8,
        gt=0,
        le=168,
    )

    delete_frames_after_encoding: bool = True


class TimelapseEncodeRequest(BaseModel):
    session_folder: str = Field(
        min_length=1,
        max_length=150,
    )

    output_fps: int = Field(
        default=30,
        ge=1,
        le=60,
    )

    delete_frames_after_encoding: bool = False


class CameraPowerRequest(BaseModel):
    enabled: bool


class CameraManager:
    def __init__(self) -> None:
        self.capture: cv2.VideoCapture | None = None
        self.latest_frame = None
        self.latest_frame_time = 0.0

        self.frame_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.control_lock = threading.Lock()
        self.enabled = True

        self.last_error: str | None = None

    def start(self) -> None:
        with self.control_lock:
            self._start()

    def _start(self) -> None:
        if self.capture is not None and self.capture.isOpened():
            self.enabled = True
            return

        self.enabled = True
        self.capture = cv2.VideoCapture(
            CAMERA_DEVICE,
            cv2.CAP_V4L2,
        )

        self.capture.set(
            cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*"MJPG"),
        )
        self.capture.set(
            cv2.CAP_PROP_FRAME_WIDTH,
            CAMERA_WIDTH,
        )
        self.capture.set(
            cv2.CAP_PROP_FRAME_HEIGHT,
            CAMERA_HEIGHT,
        )
        self.capture.set(
            cv2.CAP_PROP_FPS,
            CAMERA_FPS,
        )
        self.capture.set(
            cv2.CAP_PROP_BUFFERSIZE,
            1,
        )

        if not self.capture.isOpened():
            self.capture.release()
            self.capture = None
            self.last_error = f"Could not open camera at {CAMERA_DEVICE}"
            raise RuntimeError(
                self.last_error
            )

        actual_width = int(
            self.capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        )
        actual_height = int(
            self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        )
        actual_fps = self.capture.get(cv2.CAP_PROP_FPS)

        print(
            "Camera opened:",
            f"{actual_width}x{actual_height}",
            f"at approximately {actual_fps:.1f} FPS",
        )

        self.stop_event.clear()

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name="camera-reader",
            daemon=True,
        )
        self.reader_thread.start()

    def _reader_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.capture is None:
                break

            success, frame = self.capture.read()

            if not success:
                self.last_error = "Failed to read camera frame"
                time.sleep(0.1)
                continue

            with self.frame_lock:
                self.latest_frame = frame
                self.latest_frame_time = time.time()

            self.last_error = None

    def get_frame_copy(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None

            return self.latest_frame.copy()

    def is_available(self) -> bool:
        if self.capture is None:
            return False

        return (
            self.capture.isOpened()
            and self.latest_frame is not None
        )

    def stop(self) -> None:
        with self.control_lock:
            self._stop()

    def _stop(self) -> None:
        self.enabled = False
        self.stop_event.set()

        if self.reader_thread is not None:
            self.reader_thread.join(timeout=3)

        if self.capture is not None:
            self.capture.release()

        with self.frame_lock:
            self.latest_frame = None
            self.latest_frame_time = 0.0

        self.capture = None
        self.reader_thread = None
        self.last_error = None


class TimelapseManager:
    def __init__(self, camera: CameraManager) -> None:
        self.camera = camera

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None

        self.state = "idle"
        self.session_name: str | None = None
        self.session_directory: Path | None = None
        self.frames_directory: Path | None = None
        self.output_file: Path | None = None

        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.interval_seconds = 10.0
        self.duration_seconds: float | None = None
        self.delete_frames_after_encoding = True

        self.frame_count = 0
        self.last_frame_at: float | None = None
        self.error_message: str | None = None

    @staticmethod
    def sanitize_name(name: str) -> str:
        allowed = []

        for character in name.strip():
            if character.isalnum():
                allowed.append(character)
            elif character in {" ", "-", "_"}:
                allowed.append("_")

        result = "".join(allowed).strip("_")

        while "__" in result:
            result = result.replace("__", "_")

        return result[:50] or "timelapse"

    def start(
        self,
        name: str,
        interval_seconds: float,
        duration_hours: float | None,
        delete_frames_after_encoding: bool,
    ) -> dict[str, Any]:
        with self.lock:
            if self.state in {
                "recording",
                "stopping",
                "encoding",
            }:
                raise RuntimeError(
                    "A timelapse is already running"
                )

            if not self.camera.is_available():
                raise RuntimeError(
                    "The camera is not currently available"
                )

            timestamp = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )
            safe_name = self.sanitize_name(name)

            session_directory = (
                RECORDINGS_DIR
                / f"{timestamp}_{safe_name}"
            )
            frames_directory = (
                session_directory / "frames"
            )

            frames_directory.mkdir(
                parents=True,
                exist_ok=False,
            )

            output_file = (
                session_directory
                / f"{safe_name}_{timestamp}.mp4"
            )

            self.state = "recording"
            self.session_name = name
            self.session_directory = session_directory
            self.frames_directory = frames_directory
            self.output_file = output_file

            self.started_at = time.time()
            self.finished_at = None
            self.interval_seconds = interval_seconds
            self.duration_seconds = (
                duration_hours * 3600
                if duration_hours is not None
                else None
            )
            self.delete_frames_after_encoding = (
                delete_frames_after_encoding
            )

            self.frame_count = 0
            self.last_frame_at = None
            self.error_message = None

            self.stop_event.clear()

            self.worker_thread = threading.Thread(
                target=self._capture_loop,
                name="timelapse-capture",
                daemon=True,
            )
            self.worker_thread.start()

        return self.get_status()

    def request_stop(self) -> dict[str, Any]:
        with self.lock:
            if self.state == "idle":
                raise RuntimeError(
                    "No timelapse is currently running"
                )

            if self.state in {"complete", "error"}:
                return self.get_status()

            if self.state == "recording":
                self.state = "stopping"
                self.stop_event.set()

        return self.get_status()

    def _capture_loop(self) -> None:
        next_capture_time = time.monotonic()

        try:
            while not self.stop_event.is_set():
                if (
                    self.started_at is not None
                    and self.duration_seconds is not None
                ):
                    elapsed = time.time() - self.started_at

                    if elapsed >= self.duration_seconds:
                        break

                now = time.monotonic()

                if now < next_capture_time:
                    self.stop_event.wait(
                        min(
                            next_capture_time - now,
                            0.5,
                        )
                    )
                    continue

                frame = self.camera.get_frame_copy()

                if frame is None:
                    time.sleep(0.25)
                    continue

                next_frame_number = self.frame_count + 1

                if self.frames_directory is None:
                    raise RuntimeError(
                        "Frames directory is unavailable"
                    )

                frame_path = (
                    self.frames_directory
                    / f"frame_{next_frame_number:06d}.jpg"
                )

                success = cv2.imwrite(
                    str(frame_path),
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        TIMELAPSE_JPEG_QUALITY,
                    ],
                )

                if not success:
                    raise RuntimeError(
                        f"Could not save {frame_path.name}"
                    )

                with self.lock:
                    self.frame_count = next_frame_number
                    self.last_frame_at = time.time()

                next_capture_time += self.interval_seconds

                if (
                    next_capture_time
                    < time.monotonic() - self.interval_seconds
                ):
                    next_capture_time = (
                        time.monotonic()
                        + self.interval_seconds
                    )

            with self.lock:
                self.state = "encoding"

            self._encode_video()

            with self.lock:
                self.state = "complete"
                self.finished_at = time.time()

        except Exception as exc:
            print(f"Timelapse error: {exc}")

            with self.lock:
                self.state = "error"
                self.error_message = str(exc)
                self.finished_at = time.time()

    def _encode_video(self) -> None:
        if self.frame_count < 1:
            raise RuntimeError("No frames were captured")

        if self.frames_directory is None:
            raise RuntimeError("Frames directory is unavailable")

        if self.output_file is None:
            raise RuntimeError("Output filename is unavailable")

        temporary_output = self.output_file.with_name(
            f"{self.output_file.stem}.encoding.mp4"
        )

        encode_frames_to_video(
            frames_directory=self.frames_directory,
            output_file=self.output_file,
            temporary_output=temporary_output,
            output_fps=OUTPUT_VIDEO_FPS,
        )

        if self.delete_frames_after_encoding:
            shutil.rmtree(
                self.frames_directory,
                ignore_errors=False,
            )

    def get_status(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()

            elapsed_seconds = (
                now - self.started_at
                if self.started_at is not None
                else 0
            )

            if (
                self.finished_at is not None
                and self.started_at is not None
            ):
                elapsed_seconds = (
                    self.finished_at - self.started_at
                )

            remaining_seconds = None

            if (
                self.duration_seconds is not None
                and self.started_at is not None
                and self.state
                in {"recording", "stopping"}
            ):
                remaining_seconds = max(
                    0,
                    self.duration_seconds
                    - (now - self.started_at),
                )

            output_size_bytes = 0

            if (
                self.output_file is not None
                and self.output_file.exists()
            ):
                output_size_bytes = (
                    self.output_file.stat().st_size
                )

            session_size_bytes = 0

            if (
                self.session_directory is not None
                and self.session_directory.exists()
            ):
                session_size_bytes = directory_size(
                    self.session_directory
                )

            return {
                "state": self.state,
                "session_name": self.session_name,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "interval_seconds": self.interval_seconds,
                "duration_seconds": self.duration_seconds,
                "elapsed_seconds": elapsed_seconds,
                "remaining_seconds": remaining_seconds,
                "frame_count": self.frame_count,
                "last_frame_at": self.last_frame_at,
                "session_size_bytes": session_size_bytes,
                "output_size_bytes": output_size_bytes,
                "output_filename": (
                    self.output_file.name
                    if self.output_file is not None
                    and self.output_file.exists()
                    else None
                ),
                "error": self.error_message,
            }


def directory_size(directory: Path) -> int:
    total = 0

    try:
        for path in directory.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        pass

    return total


def get_storage_status() -> dict[str, int]:
    usage = shutil.disk_usage(BASE_DIR)

    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def validate_session_folder(folder_name: str) -> Path:
    if Path(folder_name).name != folder_name:
        raise ValueError("Invalid session folder")

    session_directory = (
        RECORDINGS_DIR / folder_name
    ).resolve()

    recordings_root = RECORDINGS_DIR.resolve()

    if recordings_root not in session_directory.parents:
        raise ValueError("Invalid session folder")

    if not session_directory.is_dir():
        raise ValueError("Session folder does not exist")

    return session_directory


def validate_video_file(video_path: Path) -> dict[str, Any]:
    probe_command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,nb_frames",
        "-show_entries",
        "format=duration,size,format_name",
        "-of",
        "json",
        str(video_path),
    ]

    probe_result = subprocess.run(
        probe_command,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if probe_result.returncode != 0:
        raise RuntimeError(
            probe_result.stderr.strip()
            or "The encoded MP4 failed validation"
        )

    try:
        probe_data = json.loads(probe_result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FFprobe returned invalid validation data"
        ) from exc

    streams = probe_data.get("streams", [])
    format_data = probe_data.get("format", {})

    if not streams:
        raise RuntimeError(
            "The encoded file contains no video stream"
        )

    video_stream = streams[0]

    if video_stream.get("codec_name") != "h264":
        raise RuntimeError(
            "The encoded file does not contain H.264 video"
        )

    try:
        duration = float(format_data.get("duration", 0))
        file_size = int(format_data.get("size", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "FFprobe returned invalid video metadata"
        ) from exc

    if duration <= 0:
        raise RuntimeError(
            "The encoded video has an invalid duration"
        )

    if file_size <= 0:
        raise RuntimeError(
            "The encoded video has an invalid file size"
        )

    return probe_data


def encode_frames_to_video(
    frames_directory: Path,
    output_file: Path,
    temporary_output: Path,
    output_fps: int,
) -> dict[str, Any]:
    frame_files = sorted(
        frames_directory.glob("frame_*.jpg")
    )

    if not frame_files:
        raise RuntimeError("No timelapse frames were found")

    temporary_output.unlink(missing_ok=True)

    encode_command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(output_fps),
        "-start_number",
        "1",
        "-i",
        str(frames_directory / "frame_%06d.jpg"),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(temporary_output),
    ]

    encode_result = subprocess.run(
        encode_command,
        capture_output=True,
        text=True,
        timeout=60 * 60,
    )

    if encode_result.returncode != 0:
        temporary_output.unlink(missing_ok=True)

        raise RuntimeError(
            encode_result.stderr.strip()
            or "FFmpeg failed to create the video"
        )

    if not temporary_output.exists():
        raise RuntimeError(
            "FFmpeg finished without creating a video"
        )

    if temporary_output.stat().st_size <= 0:
        temporary_output.unlink(missing_ok=True)

        raise RuntimeError(
            "FFmpeg created an empty video"
        )

    try:
        probe_data = validate_video_file(temporary_output)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise

    output_file.unlink(missing_ok=True)
    temporary_output.replace(output_file)

    return {
        "frame_count": len(frame_files),
        "probe": probe_data,
    }


def list_recordings() -> list[dict[str, Any]]:
    recordings: list[dict[str, Any]] = []

    for video_path in RECORDINGS_DIR.rglob("*.mp4"):
        if video_path.name.endswith(".encoding.mp4"):
            continue

        try:
            stat = video_path.stat()
            validate_video_file(video_path)
        except (OSError, RuntimeError):
            continue

        recordings.append(
            {
                "filename": video_path.name,
                "size_bytes": stat.st_size,
                "created_at": stat.st_mtime,
                "download_url": (
                    f"/api/recordings/"
                    f"{video_path.name}/download"
                ),
            }
        )

    recordings.sort(
        key=lambda item: item["created_at"],
        reverse=True,
    )

    return recordings


def clear_recordings() -> dict[str, int]:
    files_deleted = 0
    bytes_deleted = 0

    if not RECORDINGS_DIR.exists():
        return {"files_deleted": 0, "bytes_deleted": 0}

    for path in RECORDINGS_DIR.rglob("*"):
        if not path.is_file():
            continue

        try:
            bytes_deleted += path.stat().st_size
            path.unlink()
            files_deleted += 1
        except FileNotFoundError:
            continue

    directories = (
        path for path in RECORDINGS_DIR.rglob("*")
        if path.is_dir()
    )
    for directory in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            continue

    return {
        "files_deleted": files_deleted,
        "bytes_deleted": bytes_deleted,
    }


def find_recording(filename: str) -> Path | None:
    safe_filename = Path(filename).name

    if safe_filename != filename:
        return None

    if not safe_filename.lower().endswith(".mp4"):
        return None

    matches = list(
        RECORDINGS_DIR.rglob(safe_filename)
    )

    if len(matches) != 1:
        return None

    resolved_recordings = RECORDINGS_DIR.resolve()
    resolved_match = matches[0].resolve()

    if resolved_recordings not in resolved_match.parents:
        return None

    try:
        validate_video_file(resolved_match)
    except RuntimeError:
        return None

    return resolved_match


def encode_token_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_token_part(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_session_token() -> tuple[str, str]:
    csrf_token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "username": AUTH_USERNAME,
            "expires_at": int(time.time()) + SESSION_SECONDS,
            "csrf": csrf_token,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded_payload = encode_token_part(payload)
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{encode_token_part(signature)}", csrf_token


def read_session_token(token: str | None) -> dict[str, Any] | None:
    if not token or not SESSION_SECRET:
        return None

    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        expected_signature = hmac.new(
            SESSION_SECRET.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied_signature = decode_token_part(encoded_signature)

        if not hmac.compare_digest(expected_signature, supplied_signature):
            return None

        payload = json.loads(decode_token_part(encoded_payload))
        if payload.get("username") != AUTH_USERNAME:
            return None
        if int(payload.get("expires_at", 0)) <= time.time():
            return None
        if not isinstance(payload.get("csrf"), str):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def client_address(request: Request) -> str:
    cloudflare_address = request.headers.get("cf-connecting-ip")
    if cloudflare_address:
        return cloudflare_address
    return request.client.host if request.client else "unknown"


def login_is_rate_limited(address: str) -> bool:
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    with LOGIN_ATTEMPTS_LOCK:
        attempts = [
            attempt for attempt in LOGIN_ATTEMPTS.get(address, [])
            if attempt >= cutoff
        ]
        LOGIN_ATTEMPTS[address] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def record_failed_login(address: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.setdefault(address, []).append(time.time())


def clear_failed_logins(address: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(address, None)


camera_manager = CameraManager()
timelapse_manager = TimelapseManager(camera_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not AUTH_PASSWORD_HASH or not SESSION_SECRET:
        raise RuntimeError(
            "TIMELAPSE_AUTH_PASSWORD_HASH and TIMELAPSE_SESSION_SECRET "
            "must be configured"
        )

    RECORDINGS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        camera_manager.start()
    except RuntimeError as exc:
        print(f"Camera unavailable at startup: {exc}")

    yield

    try:
        status = timelapse_manager.get_status()

        if status["state"] in {
            "recording",
            "stopping",
        }:
            timelapse_manager.request_stop()

            if timelapse_manager.worker_thread is not None:
                timelapse_manager.worker_thread.join(
                    timeout=60,
                )
    finally:
        camera_manager.stop()


app = FastAPI(
    title="3D Printer Timelapse",
    lifespan=lifespan,
)


class AuthenticationMiddleware:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path
        public_path = (
            path in {"/login", "/health"}
            or path.startswith("/static/")
        )

        if public_path:
            await self.application(scope, receive, send)
            return

        session = read_session_token(request.cookies.get(SESSION_COOKIE))
        if session is None:
            if path.startswith("/api/"):
                response = JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                )
            else:
                response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied_csrf = request.headers.get("x-csrf-token")
            if not hmac.compare_digest(
                supplied_csrf or "",
                session["csrf"],
            ):
                response = JSONResponse(
                    {"detail": "Invalid security token"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

        scope["auth_session"] = session
        await self.application(scope, receive, send)


app.add_middleware(AuthenticationMiddleware)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request):
    if read_session_token(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
async def login(request: Request):
    address = client_address(request)
    if login_is_rate_limited(address):
        return RedirectResponse("/login?error=locked", status_code=303)

    body = (await request.body()).decode("utf-8", errors="replace")
    fields = parse_qs(body, keep_blank_values=True)
    username = fields.get("username", [""])[0]
    password = fields.get("password", [""])[0]

    password_matches = False
    try:
        password_matches = PASSWORD_HASHER.verify(
            AUTH_PASSWORD_HASH,
            password,
        )
    except (VerifyMismatchError, InvalidHashError):
        pass

    if username != AUTH_USERNAME or not password_matches:
        record_failed_login(address)
        return RedirectResponse("/login?error=invalid", status_code=303)

    clear_failed_logins(address)
    session_token, _ = create_session_token()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/auth/session")
def auth_session(request: Request):
    session = request.scope["auth_session"]
    return {
        "username": session["username"],
        "csrf_token": session["csrf"],
    }


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/")
def home():
    return FileResponse(
        STATIC_DIR / "index.html"
    )


def generate_preview():
    delay = 1 / PREVIEW_FPS

    while True:
        if not camera_manager.enabled:
            break

        frame = camera_manager.get_frame_copy()

        if frame is None:
            time.sleep(0.1)
            continue

        success, encoded = cv2.imencode(
            ".jpg",
            frame,
            [
                cv2.IMWRITE_JPEG_QUALITY,
                PREVIEW_JPEG_QUALITY,
            ],
        )

        if not success:
            time.sleep(delay)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-cache\r\n\r\n"
            + encoded.tobytes()
            + b"\r\n"
        )

        time.sleep(delay)


@app.get("/api/stream")
def preview_stream():
    return StreamingResponse(
        generate_preview(),
        media_type=(
            "multipart/x-mixed-replace; "
            "boundary=frame"
        ),
        headers={
            "Cache-Control": (
                "no-store, no-cache, "
                "must-revalidate"
            ),
            "Pragma": "no-cache",
        },
    )


@app.get("/api/status")
def application_status():
    return {
        "camera": {
            "connected": camera_manager.is_available(),
            "enabled": camera_manager.enabled,
            "device": CAMERA_DEVICE,
            "resolution": (
                f"{CAMERA_WIDTH}x{CAMERA_HEIGHT}"
            ),
            "requested_fps": CAMERA_FPS,
            "last_error": camera_manager.last_error,
        },
        "timelapse": timelapse_manager.get_status(),
        "storage": get_storage_status(),
    }


@app.post("/api/timelapse/start")
def start_timelapse(request: TimelapseStartRequest):
    try:
        return timelapse_manager.start(
            name=request.name,
            interval_seconds=request.interval_seconds,
            duration_hours=request.duration_hours,
            delete_frames_after_encoding=(
                request.delete_frames_after_encoding
            ),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc


@app.post("/api/timelapse/stop")
def stop_timelapse():
    try:
        return timelapse_manager.request_stop()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc


@app.post("/api/timelapse/encode-existing")
def encode_existing_timelapse(
    request: TimelapseEncodeRequest,
):
    try:
        session_directory = validate_session_folder(
            request.session_folder
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    frames_directory = session_directory / "frames"

    if not frames_directory.is_dir():
        raise HTTPException(
            status_code=404,
            detail="No frames directory was found",
        )

    output_file = (
        session_directory / "recovered_timelapse.mp4"
    )
    temporary_output = (
        session_directory
        / "recovered_timelapse.encoding.mp4"
    )

    try:
        result = encode_frames_to_video(
            frames_directory=frames_directory,
            output_file=output_file,
            temporary_output=temporary_output,
            output_fps=request.output_fps,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc

    if request.delete_frames_after_encoding:
        shutil.rmtree(
            frames_directory,
            ignore_errors=False,
        )

    return {
        "success": True,
        "frame_count": result["frame_count"],
        "filename": output_file.name,
        "size_bytes": output_file.stat().st_size,
        "download_url": (
            f"/api/recordings/"
            f"{output_file.name}/download"
        ),
    }


@app.get("/api/recordings")
def recordings():
    return {
        "recordings": list_recordings(),
    }


@app.post("/api/camera/power")
def set_camera_power(request: CameraPowerRequest):
    with timelapse_manager.lock:
        if timelapse_manager.state in {"recording", "stopping", "encoding"}:
            raise HTTPException(
                status_code=409,
                detail="Camera power cannot change during an active timelapse",
            )

        if request.enabled:
            try:
                camera_manager.start()
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=str(exc),
                ) from exc
        else:
            camera_manager.stop()

    return {
        "enabled": camera_manager.enabled,
        "connected": camera_manager.is_available(),
        "last_error": camera_manager.last_error,
    }


@app.delete("/api/recordings")
def delete_recordings():
    with timelapse_manager.lock:
        if timelapse_manager.state in {
            "recording",
            "stopping",
            "encoding",
        }:
            raise HTTPException(
                status_code=409,
                detail="Stop the active timelapse before clearing files",
            )

        return {
            "success": True,
            **clear_recordings(),
        }


@app.get(
    "/api/recordings/{filename}/download",
    response_class=FileResponse,
)
def download_recording(filename: str):
    video_path = find_recording(filename)

    if video_path is None:
        raise HTTPException(
            status_code=404,
            detail="Recording not found or invalid",
        )

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=video_path.name,
    )
