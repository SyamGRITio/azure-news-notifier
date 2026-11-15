import json
import threading

import pytest
from azure.core.exceptions import HttpResponseError, ResourceExistsError

from notifier.models import DeliveryState
from notifier.state_store import AlreadyRunningError, BlobStateStore, LeaseLostError


class FakeLease:
    def __init__(self):
        self.released = False
        self.fail_renew = False
        self.renewed = threading.Event()

    def renew(self, **kwargs):
        self.renewed.set()
        if self.fail_renew:
            raise OSError("private service details")

    def release(self, **kwargs):
        self.released = True


class FakeBlob:
    def __init__(self):
        self.data = None
        self.lease = FakeLease()
        self.busy = False
        self.writes = []

    def upload_blob(self, data, *, overwrite=False, lease=None, **kwargs):
        if not overwrite and self.data is not None:
            raise ResourceExistsError("already exists")
        if overwrite:
            assert lease is self.lease
        self.data = data
        self.writes.append(data)

    def acquire_lease(self, lease_duration, **kwargs):
        assert lease_duration == 60
        if self.busy:
            error = HttpResponseError("lease held elsewhere")
            error.status_code = 409
            error.error_code = "LeaseAlreadyPresent"
            raise error
        return self.lease

    def download_blob(self, *, lease, **kwargs):
        assert lease is self.lease
        return self

    def readall(self):
        return self.data


def test_blob_roundtrip_preserves_budget_and_read_urls():
    blob = FakeBlob()
    store = BlobStateStore(blob)
    with store.locked():
        assert store.load() == DeliveryState()
        store.save(DeliveryState("2026-11", 3, {"https://example.com/a"}))
    assert blob.lease.released
    with store.locked():
        assert store.load() == DeliveryState("2026-11", 3, {"https://example.com/a"})


def test_busy_blob_cannot_enter_or_reset_existing_state():
    blob = FakeBlob()
    blob.data = b'{"month":"2026-11","translation_attempts":4,"delivered_urls":[]}'
    blob.busy = True
    with pytest.raises(AlreadyRunningError), BlobStateStore(blob).locked():
        pytest.fail("Concurrent run must not start")
    assert json.loads(blob.data)["translation_attempts"] == 4
    assert not blob.lease.released


def test_corrupt_blob_is_not_silently_reset_and_lock_is_released():
    blob = FakeBlob()
    blob.data = b"broken"
    with pytest.raises(ValueError), BlobStateStore(blob).locked() as store:
        store.load()
    assert blob.data == b"broken"
    assert blob.lease.released


def test_lost_lease_stops_state_writes_without_exposing_service_error():
    blob = FakeBlob()
    store = BlobStateStore(blob)
    with store.locked():
        original = blob.data
        blob.lease.fail_renew = True
        with pytest.raises(LeaseLostError) as error:
            store.save(DeliveryState("2026-11", 1))
        assert blob.data == original
        assert "private" not in str(error.value)


def test_background_renewal_keeps_long_translation_waits_locked():
    blob = FakeBlob()
    with BlobStateStore(blob, renew_interval=0.01).locked():
        assert blob.lease.renewed.wait(1)
    assert blob.lease.released


def test_blob_access_requires_the_run_lock():
    store = BlobStateStore(FakeBlob())
    with pytest.raises(LeaseLostError):
        store.load()
    with pytest.raises(LeaseLostError):
        store.save(DeliveryState())
