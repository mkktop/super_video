"""任务列表搜索 / 批量操作 / 输出覆盖预检 的集成测试（R2a/R1b）。"""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS


def make_clip(path: Path, w: int = 160, h: int = 90, duration: float = 2.0):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24",
         "-t", str(duration), "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p",
         str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture(scope="module")
def client():
    from sv.server.app import app

    with TestClient(app) as c:  # 触发 lifespan: init_db + runner
        yield c


@pytest.fixture(scope="module")
def tiny():
    p = TEMP_DIR / "sb_tiny.mp4"
    make_clip(p, 160, 90, 2.0)
    return p


def wait_status(client, task_id, statuses, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] in statuses:
            return t
        time.sleep(0.3)
    raise TimeoutError(f"任务 {task_id} 停留在 {t['status']}: {t.get('error')}")


def seed_task(input_path, output_path, model="realesr-animevideov3",
              status="queued", params=None):
    """直接写库造任务行（不经过 runner/worker）：搜索/批量端点只读库，
    用假行断言确定性最强。"""
    from sv.server import db

    t = db.new_task(str(input_path), str(output_path), model, params or {})
    if status != "queued":
        db.update_task(t["id"], status=status)
    return t


def test_search_filters_by_path_and_model(client):
    rows = [
        seed_task("C:/movies/alpha_fan_ep01.mkv", "C:/out/alpha_x2.mp4", status="done"),
        seed_task("C:/movies/beta_movie.mkv", "C:/out/beta_x2.mp4", status="done"),
        seed_task("C:/movies/gamma_art.mkv", "C:/out/gamma_x2.mp4", status="canceled"),
    ]
    ids = {r["id"] for r in rows}

    # 无 q：全部在列（<= 历史截断上限）
    all_ = client.get("/api/tasks").json()
    assert ids <= {t["id"] for t in all_}

    # 按输入路径子串
    hit = client.get("/api/tasks?q=alpha_fan").json()
    assert [t["id"] for t in hit] == [rows[0]["id"]]

    # 按输出路径子串（大小写不敏感：SQLite LIKE 对 ASCII 忽略大小写）
    hit = client.get("/api/tasks?q=BETA_X2").json()
    assert [t["id"] for t in hit] == [rows[1]["id"]]

    # 按模型 id
    hit = client.get("/api/tasks?q=realesr-animevideov3").json()
    assert ids <= {t["id"] for t in hit}

    # 无匹配词 → 空列表
    assert client.get("/api/tasks?q=不存在的关键词xyz").json() == []


def test_batch_cancel_delete_resume(client, tiny):
    a = seed_task(tiny, str(TEMP_DIR / "sb_bat_a.mp4"))          # queued
    b = seed_task(tiny, str(TEMP_DIR / "sb_bat_b.mp4"))          # queued
    f = seed_task(tiny, str(TEMP_DIR / "sb_bat_f.mp4"), status="failed")

    # 批量取消：queued/running 均可取消（runner 若抢先开跑同样被取消）
    r = client.post("/api/tasks/batch", json={"action": "cancel", "ids": [a["id"], b["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["done"]) == {a["id"], b["id"]}
    for tid in (a["id"], b["id"]):
        assert client.get(f"/api/tasks/{tid}").json()["status"] == "canceled"

    # 批量续跑失败任务（输入真实存在、无进度 → 从头重跑），再取消清理
    r = client.post("/api/tasks/batch", json={"action": "resume", "ids": [f["id"]]})
    assert r.status_code == 200, r.text
    assert r.json()["done"] == [f["id"]]
    assert client.get(f"/api/tasks/{f['id']}").json()["status"] == "queued"
    client.post("/api/tasks/batch", json={"action": "cancel", "ids": [f["id"]]})

    # 批量删除：三个已落终态 + 一个不存在的 id（部分成功语义）
    r = client.post("/api/tasks/batch", json={
        "action": "delete", "ids": [a["id"], b["id"], f["id"], "no_such_task"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["done"]) == {a["id"], b["id"], f["id"]}
    assert "no_such_task" in body["failed"]
    for tid in (a["id"], b["id"], f["id"]):
        assert client.get(f"/api/tasks/{tid}").status_code == 404

    # 非法 action
    assert client.post("/api/tasks/batch",
                       json={"action": "purge", "ids": [a["id"]]}).status_code == 400


def test_output_conflict_existing_file_requires_overwrite(client, tiny):
    p = TEMP_DIR / "sb_exist.mp4"
    p.write_bytes(b"fake existing output")

    r = client.post("/api/tasks", json={
        "input": str(tiny), "output": str(p),
        "model_id": "realesr-animevideov3",
        "params": {"scale": 2, "target_scale": 2}})
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]

    # overwrite=true → 放行（会真跑一个 2s 任务覆盖假文件）
    r = client.post("/api/tasks", json={
        "input": str(tiny), "output": str(p), "overwrite": True,
        "model_id": "realesr-animevideov3",
        "params": {"scale": 2, "target_scale": 2}})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    wait_status(client, tid, ("done", "failed"))
    client.delete(f"/api/tasks/{tid}")


def test_conflict_with_active_task_and_auto_name_avoidance(client, tiny):
    # 把全局输出目录指到独立目录：否则自动命名落在源同目录时 plain 恒等于源文件
    # 本身（永远退后缀），测不到「活动任务占用」这一分支
    orig = client.get("/api/settings").json().get("output_dir", "")
    out_dir = TEMP_DIR / "sb_out"
    shutil.rmtree(out_dir, ignore_errors=True)  # 上次运行的产物残留会让首建变 409
    r = client.put("/api/settings", json={"output_dir": str(out_dir)})
    assert r.status_code == 200, r.text
    try:
        # A：显式输出占住自动命名会选的 plain 名字（out/sb_tiny.mp4）
        plain = out_dir / "sb_tiny.mp4"
        r = client.post("/api/tasks", json={
            "input": str(tiny), "output": str(plain),
            "model_id": "realesr-animevideov3",
            "params": {"scale": 2, "target_scale": 2}})
        assert r.status_code == 201, r.text
        a_id = r.json()["id"]

        # B：另一任务显式指向同一路径 → 409（活动任务占用；A 若已完成则文件已存在，同样 409）
        r = client.post("/api/tasks", json={
            "input": str(tiny), "output": str(plain),
            "model_id": "realesr-animevideov3",
            "params": {"scale": 2, "target_scale": 2}})
        assert r.status_code == 409, r.text

        # C：带 overwrite 重交 → 放行
        r = client.post("/api/tasks", json={
            "input": str(tiny), "output": str(plain), "overwrite": True,
            "model_id": "realesr-animevideov3",
            "params": {"scale": 2, "target_scale": 2}})
        assert r.status_code == 201, r.text
        c_id = r.json()["id"]

        # D：同输入不指定输出 → 自动命名避让活动任务占用的 plain，退 _2x 后缀
        r = client.post("/api/tasks", json={
            "input": str(tiny), "model_id": "realesr-animevideov3",
            "params": {"scale": 2, "target_scale": 2}})
        assert r.status_code == 201, r.text
        d_id = r.json()["id"]
        d_out = client.get(f"/api/tasks/{d_id}").json()["output_path"]
        assert Path(d_out).name == "sb_tiny_2x.mp4"

        for tid in (a_id, c_id, d_id):
            wait_status(client, tid, ("done", "failed"))
            client.delete(f"/api/tasks/{tid}")
    finally:
        client.put("/api/settings", json={"output_dir": orig})


def test_trim_output_conflict(client, tiny):
    p = TEMP_DIR / "sb_trim_exist.mp4"
    p.write_bytes(b"fake existing output")
    r = client.post("/api/trim", json={
        "input": str(tiny), "start_s": 0, "end_s": 0.3, "mode": "exact", "output": str(p)})
    assert r.status_code == 409

    r = client.post("/api/trim", json={
        "input": str(tiny), "start_s": 0, "end_s": 0.3, "mode": "exact",
        "output": str(p), "overwrite": True})
    assert r.status_code == 201, r.text


def test_input_exists_flags(client, tiny):
    """全页对比入口的置灰依据：视频任务看 input_path，图片批量看清单全部健在。"""
    alive = TEMP_DIR / "sb_input_alive.mp4"
    shutil.copyfile(tiny, alive)
    gone = TEMP_DIR / "sb_input_gone.mp4"
    gone.unlink(missing_ok=True)

    t_video = seed_task(alive, "C:/out/v_x2.mp4", status="done")
    t_lost = seed_task(gone, "C:/out/g_x2.mp4", status="done")
    t_img_ok = seed_task(
        "C:/out/i_ok.png", "C:/out/i_ok_out.png", status="done",
        params={"kind": "image", "images": [{"in": str(alive), "out": "a.png"}]})
    t_img_part = seed_task(
        "C:/out/i_part.png", "C:/out/i_part_out.png", status="done",
        params={"kind": "image",
                "images": [{"in": str(alive), "out": "a.png"},
                           {"in": str(gone), "out": "b.png"}]})
    try:
        rows = {t["id"]: t for t in client.get("/api/tasks").json()}
        assert rows[t_video["id"]]["input_exists"] is True
        assert rows[t_lost["id"]]["input_exists"] is False
        assert rows[t_img_ok["id"]]["input_exists"] is True
        assert rows[t_img_part["id"]]["input_exists"] is False

        # 单任务详情同语义，且源删除后即时反映
        assert client.get(f"/api/tasks/{t_video['id']}").json()["input_exists"] is True
        os.remove(alive)
        assert client.get(f"/api/tasks/{t_video['id']}").json()["input_exists"] is False
        assert client.get(f"/api/tasks/{t_img_ok['id']}").json()["input_exists"] is False
    finally:
        for t in (t_video, t_lost, t_img_ok, t_img_part):
            client.delete(f"/api/tasks/{t['id']}")


@pytest.fixture()
def iso_settings(tmp_path, monkeypatch):
    """设置文件隔离：删源开关的读写落在临时文件，不碰真实 settings.json。"""
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    return sv_settings


def test_delete_source_after_done(client, tiny, iso_settings):
    """超分完成后删除源文件：默认关=保留；开启=成功任务删源、输出健在。"""
    sfx = uuid.uuid4().hex[:6]  # 输出/源均唯一：任务记录删除不清用户输出文件，重跑不撞 409
    keep_src = TEMP_DIR / f"sb_keep_src_{sfx}.mp4"
    keep_out = TEMP_DIR / f"sb_keep_out_{sfx}.mp4"
    del_src = TEMP_DIR / f"sb_del_src_{sfx}.mp4"
    del_out = TEMP_DIR / f"sb_del_out_{sfx}.mp4"
    keep_id = del_id = None
    try:
        # 默认关：源保留
        shutil.copyfile(tiny, keep_src)
        r = client.post("/api/tasks", json={
            "input": str(keep_src), "output": str(keep_out),
            "model_id": "realesr-animevideov3", "params": {"scale": 2, "target_scale": 2}})
        assert r.status_code == 201, r.text
        keep_id = r.json()["id"]
        wait_status(client, keep_id, ("done", "failed"))
        assert keep_src.exists(), "默认关：源文件必须保留"

        # 开启：成功任务删源；详情端点即时反映 input_exists=false（对比入口置灰依据）
        r = client.put("/api/settings", json={"delete_source_after_done": True})
        assert r.status_code == 200, r.text
        shutil.copyfile(tiny, del_src)
        r = client.post("/api/tasks", json={
            "input": str(del_src), "output": str(del_out),
            "model_id": "realesr-animevideov3", "params": {"scale": 2, "target_scale": 2}})
        assert r.status_code == 201, r.text
        del_id = r.json()["id"]
        done = wait_status(client, del_id, ("done", "failed"))
        assert done["status"] == "done", done.get("error")
        assert not del_src.exists(), "开启后成功任务应删除源文件"
        assert Path(done["output_path"]).exists(), "输出文件必须健在"
        assert client.get(f"/api/tasks/{del_id}").json()["input_exists"] is False
    finally:
        for p in (keep_src, keep_out, del_src, del_out):
            p.unlink(missing_ok=True)
        for tid in (keep_id, del_id):
            if tid:
                client.delete(f"/api/tasks/{tid}")


def test_delete_source_image_batch_and_guard(tmp_path, monkeypatch, iso_settings):
    """图片批量按清单逐张删；源=输出同路径护栏不误删；非 bool 值校验拒绝。"""
    import sv.server.runner as runner_mod

    src1 = tmp_path / "a.png"
    src2 = tmp_path / "b.png"
    out1 = tmp_path / "a_2x.png"
    out2 = tmp_path / "b_2x.png"
    for p in (src1, src2, out1, out2):
        p.write_bytes(b"x")
    task = {"id": "t_img", "input_path": str(src1), "output_path": str(out1),
            "params": {"kind": "image",
                       "images": [{"in": str(src1), "out": str(out1)},
                                  {"in": str(src2), "out": str(out2)}]}}

    runner_mod._delete_source_if_enabled(task)
    assert src1.exists() and src2.exists(), "开关默认关：不动源文件"

    with pytest.raises(ValueError):
        iso_settings.save({"delete_source_after_done": "yes"})
    assert iso_settings.save({"delete_source_after_done": True})["delete_source_after_done"] is True

    runner_mod._delete_source_if_enabled(task)
    assert not src1.exists() and not src2.exists(), "开启后按清单逐张删源"
    assert out1.exists() and out2.exists(), "输出产物必须健在"

    # 源=输出同路径（创建期命名纪律本会避开，这里钉死护栏不误删输出）
    same = tmp_path / "same.mp4"
    same.write_bytes(b"x")
    runner_mod._delete_source_if_enabled(
        {"id": "t_same", "input_path": str(same), "output_path": str(same), "params": {}})
    assert same.exists(), "同路径护栏：不删"
