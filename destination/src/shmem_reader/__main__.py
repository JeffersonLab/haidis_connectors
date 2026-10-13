"""Entry point: python -m shmem_reader  or  shmem-reader (console script)."""

import os
import sys
import time
import random
import itertools
import multiprocessing

from .reader import ShmemReader


def reader_worker(reader_id: int, shmem_name: str, shmem_size: int,
                  sem_name: str, sem_ack_name: str):
    reader = ShmemReader(shmem_name, shmem_size, sem_name, sem_ack_name)
    if not reader.initialize():
        print(f"[Reader {reader_id}] Failed to initialize", file=sys.stderr)
        return
    print(f"[Reader {reader_id}] Waiting for data...")
    try:
        for iteration in itertools.count():
            result = reader.receive()
            if result is not None:
                if isinstance(result, tuple):
                    data, data_id = result
                    prefix = f"data_id={data_id} "
                else:
                    data = result
                    prefix = ""
                print(f"[Reader {reader_id}] Iteration {iteration}: "
                      f"{prefix}Read array {data.shape} Min: {data.min():.6f}, "
                      f"Max: {data.max():.6f}, Mean: {data.mean():.6f}")
            else:
                print(f"[Reader {reader_id}] Iteration {iteration}: No data")
            time.sleep(random.uniform(0, 0.1))
    except KeyboardInterrupt:
        pass
    finally:
        reader.cleanup()


def main():
    shmem_name   = os.getenv("SHMEM_NAME",    "/haidis_shmem")
    shmem_size   = int(os.getenv("SHMEM_SIZE", "10485760"))
    sem_name     = os.getenv("SEM_NAME",      "/haidis_sem")
    sem_ack_name = os.getenv("SEM_ACK_NAME",  "/haidis_sem_ack")
    num_readers  = int(os.getenv("NUM_READERS", "1"))

    print("Python Destination Container Starting...")
    print(f"  SHMEM_NAME: {shmem_name}, SHMEM_SIZE: {shmem_size}")
    print(f"  SEM_NAME: {sem_name}, SEM_ACK_NAME: {sem_ack_name}")
    print(f"  NUM_READERS: {num_readers}")

    worker_args = (shmem_name, shmem_size, sem_name, sem_ack_name)

    if num_readers == 1:
        reader_worker(0, *worker_args)
    else:
        procs = [
            multiprocessing.Process(target=reader_worker, args=(i, *worker_args))
            for i in range(num_readers)
        ]
        for p in procs:
            p.start()
        try:
            for p in procs:
                p.join()
        except KeyboardInterrupt:
            for p in procs:
                p.terminate()
            for p in procs:
                p.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
