"""Keyboard-controlled audio recorder for the shop processing pipeline.

The recorder owns only audio capture and keyboard state. Once a recording is
complete, it sends a dictionary to the callback supplied by the caller, such
as ``main.handle_recording``.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


RecordingCallback = Callable[[dict], object]
ShutdownCallback = Callable[[], object]


class KeyboardRecorder:
    """Start/stop recordings with a configurable keyboard key.

    The first press while recording starts a short double-press window. If no
    second press arrives, the recording is saved with ``process_now=False``.
    A second press inside that window saves it with ``process_now=True``.
    """

    def __init__(
        self,
        on_recording_saved: RecordingCallback,
        on_shutdown: Optional[ShutdownCallback] = None,
    ):
        if not callable(on_recording_saved):
            raise TypeError("on_recording_saved must be callable")

        from config import (
            AUDIO_BLOCK_SIZE,
            AUDIO_CHANNELS,
            AUDIO_DTYPE,
            AUDIO_FILE_EXTENSION,
            AUDIO_FILE_SUBTYPE,
            AUDIO_SAMPLE_RATE,
            BUTTON_DEBOUNCE_SECONDS,
            DOUBLE_PRESS_WINDOW_SECONDS,
            RECORDINGS_DIR,
            RECORD_TOGGLE_KEY,
            SHUTDOWN_KEY,
        )

        self.on_recording_saved = on_recording_saved
        self.on_shutdown = on_shutdown
        self.recordings_dir = Path(RECORDINGS_DIR)
        self.sample_rate = AUDIO_SAMPLE_RATE
        self.channels = AUDIO_CHANNELS
        self.dtype = AUDIO_DTYPE
        self.file_extension = AUDIO_FILE_EXTENSION.lstrip(".")
        self.file_subtype = AUDIO_FILE_SUBTYPE
        self.block_size = AUDIO_BLOCK_SIZE
        self.record_toggle_key = RECORD_TOGGLE_KEY
        self.shutdown_key = SHUTDOWN_KEY
        self.double_press_window = DOUBLE_PRESS_WINDOW_SECONDS
        self.button_debounce = BUTTON_DEBOUNCE_SECONDS

        if self.record_toggle_key == self.shutdown_key:
            raise ValueError("RECORD_TOGGLE_KEY and SHUTDOWN_KEY must be different")

        self._lock = threading.RLock()
        self._keyboard = None
        self._listener = None
        self._stream = None
        self._frames = []
        self._recording_started_at: Optional[datetime] = None
        self._stop_timer: Optional[threading.Timer] = None
        self._last_button_press = 0.0
        self._recording_active = False
        self._shutting_down = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording_active

    def start(self) -> None:
        """Start listening for the configured keyboard controls."""
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise RuntimeError(
                "Keyboard recording requires pynput. Install pynput first."
            ) from exc

        with self._lock:
            if self._listener is not None:
                return

            self._keyboard = keyboard
            self._shutting_down = False
            self._listener = keyboard.Listener(on_press=self._on_key_press)
            self._listener.start()

        print(
            f"Recorder ready: {self.record_toggle_key} toggles recording; "
            f"{self.shutdown_key} shuts down the recorder."
        )

    def wait(self) -> None:
        """Block until the keyboard listener stops."""
        with self._lock:
            listener = self._listener
        if listener is not None:
            listener.join()

    def shutdown(self) -> None:
        """Stop recording/listening, preserving an active recording."""
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            listener = self._listener

        if self.is_recording:
            self._finish_recording(process_now=False)

        if listener is not None:
            listener.stop()

        with self._lock:
            self._listener = None

        print("Recorder stopped.")

        if self.on_shutdown is not None:
            try:
                self.on_shutdown()
            except Exception as exc:
                print(f"[Recorder Shutdown Error] {exc}")

    def _on_key_press(self, key):
        try:
            if self._matches_key(key, self.shutdown_key):
                self.shutdown()
                return False

            if self._matches_key(key, self.record_toggle_key):
                now = time.monotonic()
                with self._lock:
                    if now - self._last_button_press < self.button_debounce:
                        return
                    self._last_button_press = now

                self._handle_record_button()
        except Exception as exc:
            print(f"[Recorder Error] {exc}")

    def _matches_key(self, key, configured_key: str) -> bool:
        if self._keyboard is None:
            return False

        configured_key = configured_key.casefold().strip()
        named_key = getattr(self._keyboard.Key, configured_key, None)
        if named_key is not None:
            return key == named_key

        if len(configured_key) == 1:
            return key == self._keyboard.KeyCode.from_char(configured_key)

        return False

    def _handle_record_button(self) -> None:
        with self._lock:
            if not self._recording_active:
                self._start_recording()
                return

            if self._stop_timer is None:
                timer = threading.Timer(
                    self.double_press_window,
                    self._single_press_stop,
                )
                timer.daemon = True
                self._stop_timer = timer
                timer.start()
                return

            self._stop_timer.cancel()
            self._stop_timer = None

        self._finish_recording(process_now=True)

    def _single_press_stop(self) -> None:
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer = None
        self._finish_recording(process_now=False)

    def _start_recording(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "Audio recording requires sounddevice. Install sounddevice first."
            ) from exc

        self._frames = []
        self._recording_started_at = datetime.now().astimezone()
        self._recording_active = True

        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=self.channels,
                dtype=self.dtype,
                callback=self._audio_callback,
            )
            stream.start()
            self._stream = stream
        except Exception:
            self._recording_active = False
            self._recording_started_at = None
            self._frames = []
            raise

        print("[Recorder] recording started")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"[Recorder Audio] {status}")

        with self._lock:
            if self._recording_active:
                self._frames.append(indata.copy())

    def _finish_recording(self, process_now: bool) -> None:
        with self._lock:
            if not self._recording_active:
                return

            self._recording_active = False
            stream = self._stream
            self._stream = None
            frames = self._frames
            self._frames = []
            started_at = self._recording_started_at
            self._recording_started_at = None

        if stream is not None:
            stream.stop()
            stream.close()

        if not frames or started_at is None:
            print("[Recorder] no audio captured; recording discarded")
            return

        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            print(
                "[Recorder Error] Saving audio requires numpy and soundfile. "
                "Install them before recording."
            )
            raise RuntimeError("Audio file dependencies are missing") from exc

        audio = np.concatenate(frames, axis=0)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            started_at.strftime("%Y%m%d_%H%M%S_%f")
            + f"_{uuid.uuid4().hex[:8]}.{self.file_extension}"
        )
        audio_path = self.recordings_dir / filename
        sf.write(
            str(audio_path),
            audio,
            self.sample_rate,
            subtype=self.file_subtype,
        )

        recording = {
            "file_path": str(audio_path),
            "recorded_at": started_at,
            "duration_seconds": float(len(audio) / self.sample_rate),
            "process_now": process_now,
        }

        print(
            f"[Recorder] saved {audio_path.name}; "
            f"process_now={process_now}"
        )
        self.on_recording_saved(recording)


def start_keyboard_recorder(
    on_recording_saved: RecordingCallback,
    on_shutdown: Optional[ShutdownCallback] = None,
) -> KeyboardRecorder:
    """Create and start a keyboard recorder, returning its controller."""
    recorder = KeyboardRecorder(on_recording_saved, on_shutdown=on_shutdown)
    recorder.start()
    return recorder
