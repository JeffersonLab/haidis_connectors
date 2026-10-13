"""Unit tests for ShmemReader frame validation and acknowledgment.

The reader's mapping is replaced with an anonymous mmap, so no POSIX shared
memory or writer process is needed.
"""

import mmap
import struct

import numpy as np
import pytest

from shmem_reader import ShmemReader

SIZE = 4096


class StubSemaphore:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    def acquire(self, timeout=None):
        self.acquired += 1

    def release(self):
        self.released += 1


def make_reader(data_size, data_id, ndim, dims, payload=b""):
    """Build a reader whose mapping holds a frame with the given raw header."""
    reader = ShmemReader("/unused", SIZE, "/unused_sem", "/unused_sem_ack")
    reader.mapfile = mmap.mmap(-1, SIZE)
    frame = struct.pack("=QHI", data_size, data_id, ndim)
    frame += struct.pack(f"={len(dims)}I", *dims) + payload
    reader.mapfile.write(frame[:SIZE])
    reader.sem = StubSemaphore()
    reader.sem_ack = StubSemaphore()
    return reader


def valid_frame(data_id=0):
    array = np.arange(6, dtype=np.float64).reshape(2, 3)
    return array, make_reader(array.nbytes, data_id, 2, (2, 3), array.tobytes())


def test_valid_frame_roundtrip():
    array, reader = valid_frame()
    result = reader.read_data()
    assert isinstance(result, np.ndarray)
    assert result.shape == (2, 3)
    np.testing.assert_array_equal(result, array)


def test_nonzero_data_id_returns_tuple():
    array, reader = valid_frame(data_id=42)
    result, data_id = reader.read_data()
    assert data_id == 42
    np.testing.assert_array_equal(result, array)


@pytest.mark.parametrize(
    "data_size, ndim, dims, expected",
    [
        (47, 2, (2, 3), "not a multiple"),            # trailing partial double
        (48, 0, (), "ndim"),                          # no dimensions
        (48, 0xFFFFFFFF, (2, 3), "ndim"),             # garbage ndim
        (SIZE * 8, 2, (SIZE // 3, 3), "exceeds"),     # payload beyond the mapping
        (40, 2, (2, 3), "product of dims"),           # truncated payload, stale dims
        (48, 2, (3, 3), "product of dims"),           # dims disagree with data_size
    ],
)
def test_malformed_frame_rejected(capsys, data_size, ndim, dims, expected):
    reader = make_reader(data_size, 0, ndim, dims, b"\0" * 48)
    assert reader.read_data() is None
    err = capsys.readouterr().err
    assert "Malformed frame" in err
    assert expected in err


def test_empty_frame_returns_none():
    reader = make_reader(0, 0, 1, (0,))
    assert reader.read_data() is None


def test_receive_acks_valid_frame():
    array, reader = valid_frame()
    np.testing.assert_array_equal(reader.receive(), array)
    assert reader.sem.acquired == 1
    assert reader.sem_ack.released == 1


def test_receive_acks_malformed_frame():
    reader = make_reader(40, 0, 2, (2, 3), b"\0" * 48)
    assert reader.receive() is None
    assert reader.sem_ack.released == 1


def test_receive_acks_when_read_raises(monkeypatch):
    _, reader = valid_frame()

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(reader, "read_data", boom)
    with pytest.raises(RuntimeError):
        reader.receive()
    assert reader.sem_ack.released == 1
