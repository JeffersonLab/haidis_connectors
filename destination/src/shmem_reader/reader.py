"""POSIX shared memory reader."""

import sys
import time
import math
import mmap
import struct
from posix_ipc import SharedMemory, Semaphore, ExistentialError, BusyError
import numpy as np

# Upper bound on ndim accepted from a frame header (numpy's historical limit)
MAX_NDIM = 32
DOUBLE_SIZE = 8


class ShmemReader:
    """Reader for POSIX shared memory produced by a C++ ShmemWriter."""

    def __init__(self, shmem_name: str, size: int, sem_name: str, sem_ack_name: str):
        self.shmem_name = shmem_name
        self.size = size
        self.sem_name = sem_name
        self.sem_ack_name = sem_ack_name
        self.shm = None
        self.mapfile = None
        self.sem = None
        self.sem_ack = None

    def initialize(self) -> bool:
        """Initialize shared memory connection, retrying until the writer creates resources."""
        try:
            print(f"Waiting for shared memory '{self.shmem_name}' to be created by writer...")
            while True:
                try:
                    self.shm = SharedMemory(self.shmem_name)
                    break
                except ExistentialError:
                    time.sleep(0.5)

            self.mapfile = mmap.mmap(self.shm.fd, self.size)
            print(f"Shared memory opened: {self.shmem_name} ({self.size} bytes)")

            print("Waiting for semaphores...")
            while True:
                try:
                    self.sem = Semaphore(self.sem_name)
                    break
                except ExistentialError:
                    time.sleep(0.5)

            while True:
                try:
                    self.sem_ack = Semaphore(self.sem_ack_name)
                    break
                except ExistentialError:
                    time.sleep(0.5)

            print(f"Semaphores opened: {self.sem_name}, {self.sem_ack_name}")

            # Drain any stale data-ready signals left by a prior run or an
            # eager writer that posted before the reader was ready.  Each
            # drained signal must be matched with an ack so the writer can
            # proceed.
            drained = 0
            while True:
                try:
                    self.sem.acquire(timeout=0)
                    drained += 1
                    self.sem_ack.release()
                except BusyError:
                    break
            if drained > 0:
                print(
                    f"Drained {drained} stale signal(s) from {self.sem_name} (acks sent)",
                    file=sys.stderr,
                )

            return True
        except Exception as e:
            print(f"Failed to initialize: {e}", file=sys.stderr)
            return False

    def wait_for_data(self):
        """Block until the writer signals new data is ready."""
        if self.sem is not None:
            self.sem.acquire()

    def acknowledge_data(self):
        """Signal that the reader has finished consuming the data."""
        if self.sem_ack is not None:
            self.sem_ack.release()

    def receive(self) -> np.ndarray | tuple[np.ndarray, int] | None:
        """Wait for a frame, read it and acknowledge it.

        The acknowledgment is sent even when the frame is malformed or
        reading fails, so a bad frame never leaves the writer blocked.
        Returns the same values as read_data().
        """
        self.wait_for_data()
        try:
            return self.read_data()
        finally:
            self.acknowledge_data()

    def read_data(self) -> np.ndarray | tuple[np.ndarray, int] | None:
        """Read data array from shared memory.

        Returns the array directly when data_id is 0 (default/unused),
        or (array, data_id) when the writer set a non-zero data_id.
        Returns None on error or when the frame header is malformed.

        The caller must still call acknowledge_data() when this returns
        None, otherwise the writer times out on every subsequent write.
        Prefer receive(), which does that unconditionally.
        """
        if self.mapfile is None:
            print("Shared memory not initialized", file=sys.stderr)
            return None

        try:
            self.mapfile.seek(0)

            data_size = struct.unpack('Q', self.mapfile.read(8))[0]
            if data_size == 0:
                return None

            data_id = struct.unpack('H', self.mapfile.read(2))[0]
            ndim = struct.unpack('I', self.mapfile.read(4))[0]

            # Validate the header before trusting it for any further reads
            if not 1 <= ndim <= MAX_NDIM:
                print(f"Malformed frame: ndim ({ndim}) must be in 1..{MAX_NDIM}",
                      file=sys.stderr)
                return None

            if data_size % DOUBLE_SIZE != 0:
                print(f"Malformed frame: data_size ({data_size}) is not a multiple "
                      f"of {DOUBLE_SIZE}", file=sys.stderr)
                return None

            header_size = 8 + 2 + 4 + ndim * 4
            if header_size + data_size > self.size:
                print(f"Malformed frame: header ({header_size}) + data_size ({data_size}) "
                      f"exceeds shared memory size ({self.size})", file=sys.stderr)
                return None

            dims = struct.unpack(f'{ndim}I', self.mapfile.read(ndim * 4))

            num_doubles = data_size // DOUBLE_SIZE
            if math.prod(dims) != num_doubles:
                print(f"Malformed frame: product of dims {dims} != number of doubles "
                      f"({num_doubles}, data_size={data_size})", file=sys.stderr)
                return None

            data_bytes = self.mapfile.read(data_size)
            data = np.frombuffer(data_bytes, dtype=np.float64, count=num_doubles)
            array = data.reshape(dims)
            return array if data_id == 0 else (array, data_id)
        except Exception as e:
            print(f"Error reading data: {e}", file=sys.stderr)
            return None

    def cleanup(self):
        """Release all shared memory resources."""
        if self.mapfile is not None:
            self.mapfile.close()
            self.mapfile = None

        if self.shm is not None:
            self.shm.close_fd()
            self.shm = None

        if self.sem is not None:
            self.sem.close()
            self.sem = None

        if self.sem_ack is not None:
            self.sem_ack.close()
            self.sem_ack = None
