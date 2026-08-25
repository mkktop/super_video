"""数据目录迁移：≤v0.1.20 遗留在安装目录内的数据一次性搬到 DATA_ROOT。"""
from __future__ import annotations

from pathlib import Path

import sv.paths as paths


def _mk(p: Path, name: str) -> Path:
    d = p / name
    d.mkdir(parents=True)
    (d / "keep.txt").write_text(name, encoding="utf-8")
    return d


def test_migrate_moves_all_legacy_dirs(tmp_path, monkeypatch):
    old_root, new_root = tmp_path / "inst", tmp_path / "data"
    old_root.mkdir()
    for n in ("models_store", "trt-runtime", "data", ".tmp"):
        _mk(old_root, n)
    monkeypatch.setattr(paths, "ROOT", old_root)
    monkeypatch.setattr(paths, "DATA_ROOT", new_root)
    moved = paths.migrate_legacy_data(quiet=True)
    assert sorted(moved) == [".tmp", "data", "models_store", "trt-runtime"]
    for n in moved:
        assert (new_root / n / "keep.txt").read_text(encoding="utf-8") == n
        assert not (old_root / n).exists()


def test_migrate_keeps_existing_target(tmp_path, monkeypatch):
    """目标已有数据时不动源（不覆盖用户新数据），其余目录照搬。"""
    old_root, new_root = tmp_path / "inst", tmp_path / "data"
    old_root.mkdir()
    _mk(old_root, "models_store")
    _mk(old_root, "trt-runtime")
    new_root.mkdir()
    _mk(new_root, "models_store")  # 用户已重新下载的模型
    monkeypatch.setattr(paths, "ROOT", old_root)
    monkeypatch.setattr(paths, "DATA_ROOT", new_root)
    moved = paths.migrate_legacy_data(quiet=True)
    assert moved == ["trt-runtime"]
    assert (old_root / "models_store").exists()  # 源保留，不删
    assert (new_root / "models_store" / "keep.txt").exists()


def test_migrate_noop_when_same_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.migrate_legacy_data(quiet=True) == []


def test_migrate_missing_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path / "a")
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path / "b")
    (tmp_path / "a").mkdir()
    assert paths.migrate_legacy_data(quiet=True) == []
