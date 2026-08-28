from utils.ingestion_capacity import load_ingestion_capacity_config
from utils.logging_config import get_logger

logger = get_logger(__name__)


def detect_gpu_devices():
    """Detect if GPU devices are actually available"""
    try:
        import torch

        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            return True, torch.cuda.device_count()
    except ImportError:
        pass

    try:
        import subprocess

        result = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
        if result.returncode == 0:
            return True, "detected"
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return False, 0


def get_worker_count():
    """Return initial ingestion capacity for legacy callers.

    Dynamic updates are owned by ``TaskService``. In ``auto`` mode this value
    is the deployment fallback used before the first successful metrics read.
    """
    has_gpu_devices, gpu_count = detect_gpu_devices()
    capacity = load_ingestion_capacity_config()
    mode = "GPU" if has_gpu_devices else "CPU-only"

    logger.info(
        f"{mode} mode enabled",
        gpu_count=gpu_count,
        worker_count=capacity.initial_capacity,
        ingestion_capacity_mode=capacity.mode,
    )
    return capacity.initial_capacity
