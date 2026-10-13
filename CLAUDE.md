# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a demonstration project showing uni-directional, high-performance data transfer between two Docker containers using POSIX shared memory (shmem) with semaphore-based synchronization.

**Architecture:**
- **Source container (C++)**: Writes large arrays to shared memory and signals availability via semaphore
- **Destination container (Python)**: Blocks on semaphore, reads data when signaled
- **IPC mechanism**: Both containers share `ipc: host` namespace for access to shared memory and semaphores

## Key Commands

### Running the System
```bash
# Build and start both containers
docker compose up --build

# Start without rebuilding
docker compose up

# Stop all containers
docker compose down

# View logs from specific container
docker logs haidis-source
docker logs haidis-destination
```

### Development - C++ Source Container

```bash
# Build manually (inside container or with Docker)
cd source
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build

# Run standalone
./build/shmem_source
```

### Development - Python Destination Container

```bash
# Install dependencies with uv
cd destination
uv pip install -e .

# Run standalone
python src/main.py
```

## Architecture Details

### Synchronization Mechanism

The system uses **POSIX named semaphores** for producer-consumer synchronization:

1. **C++ writer** (source/src/main.cpp):
   - Creates shared memory segment and semaphore (initial value 0)
   - Writes data to shmem
   - Calls `sem_post()` to signal data ready

2. **Python reader** (destination/src/main.py):
   - Opens existing shared memory and semaphore
   - Calls `sem.acquire()` which **blocks** until C++ signals
   - Reads data immediately when woken up

This eliminates polling and ensures the reader only processes new data.

### Shared Memory Layout

Memory structure in the shared memory segment:
```
[0-7 bytes]: size_t data_size (number of bytes in array)
[8-N bytes]: double array data (data_size bytes)
```

Both C++ and Python must agree on this layout.

### IPC Namespace Sharing

Critical configuration in docker-compose.yml:
```yaml
services:
  source:
    ipc: host  # Makes IPC objects accessible
  destination:
    ipc: host  # Shares same namespace
```

Both containers access the same kernel IPC resources:
- **Shared memory**: `/haidis_shmem`
- **Semaphore**: `/haidis_sem`

### Configuration

All IPC parameters are centralized in `shared/config.env`:
- `SHMEM_NAME`: Name of shared memory object
- `SHMEM_SIZE`: Size in bytes (must accommodate data + size header)
- `ARRAY_SIZE`: Number of doubles to transfer
- `SEM_NAME`: Name of semaphore for synchronization

## Testing

### Unit tests — C++ ShmemWriter (`source/src/test_shmem_writer.cpp`)

Validates the six behaviors fixed/added in commit 6250271 (deadlock prevention, resource cleanup, input validation). Built alongside the main binary and run inside a Docker container so POSIX shmem is available:

```bash
DOCKER_API_VERSION=1.43 docker run --rm --ipc=host \
  -v "$(pwd)/source":/app -w /app ubuntu:22.04 \
  bash -c "cmake -B /tmp/build -DCMAKE_BUILD_TYPE=Release && \
           cmake --build /tmp/build --target test_shmem_writer && \
           /tmp/build/test_shmem_writer"
```

Expected output (all pass):
```
=== ShmemWriter unit tests ===
[PASS] init: basic initialize() succeeds
[PASS] init: succeeds with stale semaphores (sem_unlink at start)
[PASS] init: sem_ack value reset to 1 (not stale 0)
[PASS] write: fails when dims.size() < ndim
[PASS] write: fails when product(dims) != data.size()
[PASS] write: returns false for oversized data
[PASS] write: sem_ack_ restored – second call returns without deadlock
[PASS] write: write_data() succeeds for valid data
[PASS] write: consumer received the data-ready signal
=== Results: 9 passed, 0 failed ===
```

### Unit tests — Python ShmemReader (`destination/tests/test_reader.py`)

Covers frame-header validation in `read_data()` (bad `ndim`, `data_size` not a multiple of 8, payload beyond the mapping, `product(dims)` mismatch — all dropped with a `Malformed frame:` message) and that `receive()` always acknowledges the writer. Uses an anonymous mmap and stub semaphores, but `posix-ipc` is Linux-only, so run in a container from the repo root:

```bash
DOCKER_API_VERSION=1.43 docker run --rm -v "$(pwd)":/repo:ro python:3.11 \
  bash -c "cp -r /repo /work && cd /work/destination && \
           pip install -q . pytest && pytest -q tests"
```

Expected: `12 passed`.

`ShmemReader.receive()` does wait → read → ack with the ack in a `finally`. Callers using the three methods separately must ack even when `read_data()` returns `None`, or the writer times out on every subsequent write.

### Integration test — C++ source ↔ Python destination (`scripts/integration_test.sh`)

Builds both containers, runs them for a configurable window, then validates successful handoffs end-to-end. Must be run from the repo root:

```bash
DOCKER_API_VERSION=1.43 ./scripts/integration_test.sh [RUN_SECONDS]
# default: 20 seconds
```

The script checks:
1. Both containers initialized shmem and semaphores
2. Each side completed ≥ 5 iterations
3. Destination received the correct array shape (e.g. `(1000000, 3)`)
4. All reported Min/Max values fall within `[-1, 1]`
5. Neither container logged any errors

**Expected output (no errors):**
```
[PASS] source:    shmem + semaphores initialized
[PASS] destination: shmem + semaphores opened
[PASS] source:      >= 5 iterations written (NNNN)
[PASS] destination: >= 5 iterations read (NNNN)
[PASS] destination: array shape is (1000000, 3)
[PASS] destination: all Min/Max values within [-1, 1]
[PASS] source:      no error messages in log
[PASS] destination: no error messages in log
── Results: 8 passed, 0 failed ──
```

**If errors occur**, the failing check prints `[FAIL]` with details, e.g.:
- `[FAIL] destination: >= 5 iterations read (0)` — Python side never received data; likely a deadlock from stale IPC objects. Run the cleanup command below, then retry.
- `[FAIL] destination: array shape is (1000000, 3)` — shape mismatch between `ARRAY_SIZE` in `config.env` and what Python reports; check both sides agree on the layout.
- `[FAIL] source: no error messages in log` — followed by the error text printed inline.

The script always tears down containers via `trap EXIT`, so failed runs leave no orphaned containers.

## Cleaning Up Stale IPC Objects

When containers are killed or crash, POSIX shared memory and semaphores persist in the Docker VM's `/dev/shm` because of `ipc: host`. Stale semaphores with incorrect values cause deadlocks on subsequent runs (both containers initialize but no data flows).

**Cleanup command** (must run from inside a container since macOS has no `/dev/shm`):
```bash
docker run --rm --ipc=host ubuntu:22.04 \
  rm -f /dev/shm/sem.haidis_sem /dev/shm/sem.haidis_sem_ack /dev/shm/haidis_shmem
```

**Root cause:** `sem_open()` with `O_CREAT` reuses existing semaphore files without resetting their values. If the previous run left a semaphore in a non-initial state (e.g., `sem_ack` at 0 instead of 1), the writer blocks on `sem_wait(sem_ack_)` and the reader blocks on `sem.acquire()`, producing a deadlock.

**Symptoms:** Both containers log successful initialization ("Shared memory initialized", "Semaphores opened") but no iteration messages appear. Verify by checking timestamps in `/dev/shm/` — stale semaphore files will have old timestamps.

## Code Organization

### C++ Source Container (source/)
- **include/shmem_writer.hpp**: Class interface for shared memory operations
- **src/main.cpp**: Implementation of ShmemWriter + main loop that generates sine wave data
- **CMakeLists.txt**: Links against `rt` (POSIX realtime) and `pthread` for semaphore support

### Python Destination Container (destination/)
- **src/main.py**: ShmemReader class using `posix_ipc` and `numpy`
- **pyproject.toml**: Dependencies managed by `uv` package manager

## Modifying Data Transfer

To change what data is transferred:

1. **Update ARRAY_SIZE** in `shared/config.env`
2. **Ensure SHMEM_SIZE** is large enough: `SHMEM_SIZE >= (ARRAY_SIZE * 8) + 8`
3. **Modify data generation** in C++ main loop (source/src/main.cpp:~110)
4. **Modify data processing** in Python main loop (destination/src/main.py:~118)

## Important Implementation Notes

- **Semaphore semantics**: Initial value is 0 (no data ready). Each `sem_post()` increments, each `acquire()` decrements and blocks if zero.
- **No mutex needed**: Single producer, single consumer with one-way data flow
- **Data format**: Currently doubles (8 bytes), but any binary format works if both sides agree
- **Cleanup**: C++ source owns semaphore/shmem creation and unlinking on shutdown
- **Python unbuffering**: `PYTHONUNBUFFERED=1` set in Dockerfile for immediate log output
