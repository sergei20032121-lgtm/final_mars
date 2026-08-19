"""Захват видео: fswebcam (фолбэк), ffmpeg (основной путь), менеджер камеры."""
import glob
import os
import select
import subprocess
import tempfile
import time

from mars.config import CameraConfig

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


def discover_video_devices():
    """Обнаружить доступные видео-устройства"""
    devices = []
    for device in sorted(glob.glob("/dev/video*")):
        name = "USB camera"
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            for line in result.stdout.splitlines():
                if "Card type" in line:
                    name = line.split(":", 1)[1].strip() or name
                    break
        except Exception:
            pass
        devices.append({"device": device, "name": name})
    return devices


class FsWebcamCamera:
    """Захват видео через fswebcam (деградированный фолбэк — процесс на каждый кадр)"""
    def __init__(self, config: CameraConfig):
        self.config = config
        self.tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
        self.last_frame = None

    def capture_jpeg(self) -> bytes:
        fd, path = tempfile.mkstemp(prefix="face_frame_", suffix=".jpg", dir=self.tmp_dir)
        os.close(fd)

        cmd = [
            "fswebcam", "--quiet", "--no-banner",
            "-d", self.config.device,
            "-r", f"{self.config.width}x{self.config.height}",
            "--jpeg", str(self.config.jpeg_quality),
            path,
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         check=True, timeout=3)
            with open(path, "rb") as file:
                self.last_frame = file.read()
                return self.last_frame
        except Exception:
            return self.last_frame or b''
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def close(self):
        pass

    @property
    def name(self):
        return "fswebcam"


class FfmpegCamera:
    """Захват видео через ffmpeg — персистентный процесс + потоковое чтение MJPEG"""
    def __init__(self, config: CameraConfig):
        self.config = config
        self.proc = None
        self.buffer = bytearray()
        self.mode_index = 0
        self.last_frame = None

    @property
    def name(self):
        mode = "mjpeg" if self.mode_index == 0 else "auto"
        return f"ffmpeg/{mode}"

    def _build_cmd(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2",
        ]
        if self.mode_index == 0:
            cmd += ["-input_format", "mjpeg"]

        cmd += [
            "-framerate", str(self.config.fps),
            "-video_size", f"{self.config.width}x{self.config.height}",
            "-i", self.config.device,
            "-an", "-q:v", "3",  # Выше качество для лучшей обработки
            "-f", "mjpeg", "pipe:1",
        ]
        return cmd

    def _start(self):
        self.close()
        self.buffer.clear()
        cmd = self._build_cmd()
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=1024*100
        )

    def _extract_frame(self):
        start = self.buffer.find(JPEG_START)
        if start < 0:
            if len(self.buffer) > 65536:
                self.buffer.clear()
            return None

        if start > 0:
            del self.buffer[:start]

        end = self.buffer.find(JPEG_END, 2)
        if end < 0:
            return None

        frame = bytes(self.buffer[:end + 2])
        del self.buffer[:end + 2]
        return frame

    def _read_frame_once(self, timeout=2.0):
        if self.proc is None or self.proc.poll() is not None:
            self._start()

        deadline = time.monotonic() + timeout
        fd = self.proc.stdout.fileno()

        while time.monotonic() < deadline:
            frame = self._extract_frame()
            if frame:
                self.last_frame = frame
                return frame

            if self.proc.poll() is not None:
                raise RuntimeError("ffmpeg stopped")

            wait_time = max(0.01, min(0.2, deadline - time.monotonic()))
            ready, _, _ = select.select([fd], [], [], wait_time)
            if not ready:
                continue

            chunk = os.read(fd, 16384)
            if not chunk:
                raise RuntimeError("ffmpeg returned empty data")

            self.buffer.extend(chunk)

        raise TimeoutError("ffmpeg frame timeout")

    def capture_jpeg(self) -> bytes:
        try:
            return self._read_frame_once(timeout=2.0)
        except Exception:
            if self.mode_index == 0:
                self.mode_index = 1
                self.close()
                try:
                    return self._read_frame_once(timeout=2.0)
                except Exception:
                    return self.last_frame or b''
            return self.last_frame or b''

    def close(self):
        proc = self.proc
        self.proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class CameraManager:
    """Управление камерой с кэшированием"""
    def __init__(self, config: CameraConfig):
        self.config = config
        self.camera = None
        self.backend_name = "none"
        self.last_error = ""
        self.cached_frame = None
        self.cache_time = 0
        self.frame_rate_actual = 0
        self.frame_count = 0
        self.init_camera()

    def init_camera(self):
        """Инициализировать камеру"""
        backends = []

        if self.config.backend in ["auto", "ffmpeg"]:
            backends.append(("ffmpeg", FfmpegCamera))

        if self.config.backend in ["auto", "fswebcam"]:
            backends.append(("fswebcam", FsWebcamCamera))

        for backend_name, backend_class in backends:
            try:
                camera = backend_class(self.config)
                test_frame = camera.capture_jpeg()
                if test_frame:
                    self.camera = camera
                    self.backend_name = backend_name
                    print(f"[✓] Камера ({backend_name}): {self.config.width}x{self.config.height} @ {self.config.fps}fps")
                    return
            except Exception as e:
                self.last_error = str(e)
                continue

        print(f"[✗] Камера не инициализирована: {self.last_error}")

    def get_frame(self):
        """Получить JPEG кадр (с кэшированием)"""
        if self.camera:
            try:
                frame = self.camera.capture_jpeg()
                if frame:
                    self.cached_frame = frame
                    self.frame_count += 1
                    now = time.time()
                    if self.cache_time > 0:
                        dt = now - self.cache_time
                        if dt > 0:
                            self.frame_rate_actual = 1.0 / dt
                    self.cache_time = now
                    return frame
            except Exception as e:
                self.last_error = str(e)

        return self.cached_frame or None

    def close(self):
        if self.camera:
            self.camera.close()
            self.camera = None

    def change_device(self, device):
        self.close()
        self.config.device = device
        self.init_camera()
