from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest

from app.managed_framework import vlmevalkit
from app.runner.daemon import IsolatedRunnerManager


_PUBLIC_TSV = Path("/home/share-data/zhanghq17/multi_dataset/MMMU_Pro_10c/MMMU_Pro_10c.tsv")


def test_public_image_cache_requirements(tmp_path, monkeypatch):
    root = tmp_path / "shared" / "images"
    root.mkdir(parents=True)
    mount_info = tmp_path / "mountinfo"
    mount_info.write_text(
        f"20 1 0:20 / {root.parent} rw - autofs systemd-1 rw\n"
        f"21 20 0:21 / {root.parent} rw - cifs //test/share rw\n"
    )
    monkeypatch.setenv(vlmevalkit.PUBLIC_IMAGE_CACHE_ROOT, str(root))
    monkeypatch.setenv(vlmevalkit.PUBLIC_IMAGE_CACHE_SOURCE, "//test/share")
    assert vlmevalkit.public_image_cache_root(mount_info) == root
    assert vlmevalkit.is_public_image_source(_PUBLIC_TSV)
    assert not vlmevalkit.is_public_image_source(root / _PUBLIC_TSV.name)

    mount_info.write_text(mount_info.read_text() + f"22 21 0:22 / {root} rw - ext4 /dev/local rw\n")
    with pytest.raises(ValueError, match="预期 CIFS 卷未挂载"):
        vlmevalkit.public_image_cache_root(mount_info)


def test_runner_rejects_public_image_job_if_cache_is_unconfigured(isolated_settings, monkeypatch):
    monkeypatch.delenv(vlmevalkit.PUBLIC_IMAGE_CACHE_ROOT, raising=False)
    monkeypatch.delenv(vlmevalkit.PUBLIC_IMAGE_CACHE_SOURCE, raising=False)
    isolated_settings.production_mode = True
    manager = IsolatedRunnerManager(isolated_settings, None)
    monkeypatch.setattr(manager, "health", lambda: {"ok": True, "vlmevalkit_ready": True})
    result, body = manager.submit_job({
        "job_id": "missing-vlm-cache",
        "runner_instance_id": manager.instance_id,
        "channel_secret": "test-channel-secret",
        "spec": {"kind": "vlmevalkit", "dataset_path": str(_PUBLIC_TSV)},
    })
    assert (result, body["code"]) == (503, "VLMEVALKIT_IMAGE_CACHE_UNAVAILABLE")
    assert not (isolated_settings.runner_work_dir / "missing-vlm-cache").exists()


def test_interrupted_worker_removes_its_public_image_directory(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    root.mkdir()
    dataset = tmp_path / "public.tsv"
    dataset.touch()
    checkout = tmp_path / "checkout" / "vlmeval"
    checkout.mkdir(parents=True)
    (checkout / "__init__.py").touch()
    monkeypatch.setenv(vlmevalkit.PUBLIC_IMAGE_CACHE_ROOT, str(root))
    monkeypatch.setenv("VLMEVALKIT_ROOT", str(checkout.parent))
    monkeypatch.setenv("VLMEVALKIT_PYTHON", sys.executable)
    monkeypatch.setattr(vlmevalkit, "is_public_image_source", lambda path: path == dataset)
    monkeypatch.setattr(vlmevalkit, "public_image_cache_root", lambda: root)

    def interrupt(*args, **kwargs):
        shared_job, = root.iterdir()
        (shared_job / "image.png").touch()
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(vlmevalkit.subprocess, "run", interrupt)
    previous = signal.getsignal(signal.SIGTERM)
    args = SimpleNamespace(model="test", base_url="http://127.0.0.1:1", task=[], concurrent=1)
    suite = {"name": "public", "vlmevalkit": {"dataset": "public", "dataset_class": "ImageMCQDataset"}}
    with pytest.raises(KeyboardInterrupt):
        vlmevalkit.launch(args, suite, dataset, tmp_path / "private-job", "infer")
    assert list(root.iterdir()) == []
    assert signal.getsignal(signal.SIGTERM) == previous
