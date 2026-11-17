"""配信状態の保存。ローカルはJSON、Functionsはlease付きBlobを使う。"""

import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from .models import DeliveryState


def _encode(state: DeliveryState) -> bytes:
    data = {
        "month": state.month,
        "translation_attempts": state.translation_attempts,
        "delivered_urls": sorted(state.delivered_urls),
    }
    if state.line_delivered_urls is not None:
        data["line_delivered_urls"] = sorted(state.line_delivered_urls)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _decode(data: bytes) -> DeliveryState:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Invalid state file")
    month = value.get("month")
    attempts = value.get("translation_attempts")
    urls = value.get("delivered_urls")
    if (
        not isinstance(month, str)
        or (month != "" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month))
        or type(attempts) is not int
        or not 0 <= attempts <= 30
        or not isinstance(urls, list)
        or not all(isinstance(url, str) for url in urls)
    ):
        raise ValueError("Invalid state; restore a valid backup before retrying")
    line_urls = value.get("line_delivered_urls")
    if line_urls is not None and (
        not isinstance(line_urls, list) or not all(isinstance(url, str) for url in line_urls)
    ):
        raise ValueError("Invalid LINE delivery state")
    return DeliveryState(month, attempts, set(urls), None if line_urls is None else set(line_urls))


class JsonStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> DeliveryState:
        if not self._path.exists():
            return DeliveryState()
        return _decode(self._path.read_bytes())

    def save(self, state: DeliveryState) -> None:
        temporary_path = self._path.with_name(self._path.name + ".tmp")
        temporary_path.write_bytes(_encode(state))
        os.replace(temporary_path, self._path)


class AlreadyRunningError(RuntimeError):
    """別の実行が配信状態を使用中。"""


class LeaseLostError(RuntimeError):
    """排他を維持できないため、送信や状態更新を続けられない。"""


class BlobStateStore:
    _options = {"timeout": 10, "connection_timeout": 5, "read_timeout": 10, "retry_total": 0}

    def __init__(self, blob, *, renew_interval: float = 20) -> None:
        if not 0 < renew_interval < 30:
            raise ValueError("Lease renewal must be more frequent than every 30 seconds")
        self._blob = blob
        self._renew_interval = renew_interval
        self._lease = None
        self._io_lock = threading.RLock()
        self._stop = threading.Event()
        self._lost = threading.Event()

    def _renew(self) -> None:
        try:
            with self._io_lock:
                self._lease.renew(**self._options)
        except Exception:
            self._lost.set()
            raise LeaseLostError("Blob lock could not be renewed; stopping delivery") from None

    def _keep_lease(self) -> None:
        while not self._stop.wait(self._renew_interval):
            try:
                self._renew()
            except LeaseLostError:
                return

    def ensure_lock(self) -> None:
        if self._lease is None or self._lost.is_set():
            raise LeaseLostError("Blob lock is not held; stopping delivery")
        self._renew()

    @contextmanager
    def locked(self):
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceExistsError,
            ResourceModifiedError,
        )

        if self._lease is not None:
            raise AlreadyRunningError("Delivery is already running")
        # 既存の状態は上書きしません。コンテナーは配置時に用意します。
        try:
            self._blob.upload_blob(_encode(DeliveryState()), overwrite=False, **self._options)
        except ResourceExistsError:
            pass
        try:
            self._lease = self._blob.acquire_lease(lease_duration=60, **self._options)
        except (ResourceExistsError, ResourceModifiedError):
            raise AlreadyRunningError("Another delivery holds the Blob lock") from None
        except HttpResponseError as error:
            if (
                error.status_code == 409
                and getattr(error, "error_code", None) == "LeaseAlreadyPresent"
            ):
                raise AlreadyRunningError("Another delivery holds the Blob lock") from None
            raise

        self._stop.clear()
        self._lost.clear()
        # 翻訳の呼び出し間隔が61秒あるため、60秒のleaseを20秒ごとに更新します。
        worker = threading.Thread(target=self._keep_lease, daemon=True, name="blob-lease")
        worker.start()
        try:
            yield self
        finally:
            self._stop.set()
            worker.join(timeout=15)
            try:
                with self._io_lock:
                    self._lease.release(**self._options)
            except Exception:
                # プロセス停止や通信失敗でも、有限leaseは最後の更新から60秒で解放されます。
                pass
            finally:
                self._lease = None

    def load(self) -> DeliveryState:
        self.ensure_lock()
        with self._io_lock:
            return _decode(self._blob.download_blob(lease=self._lease, **self._options).readall())

    def save(self, state: DeliveryState) -> None:
        self.ensure_lock()
        with self._io_lock:
            self._blob.upload_blob(
                _encode(state), overwrite=True, lease=self._lease, **self._options
            )
