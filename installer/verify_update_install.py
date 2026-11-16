# -*- coding: utf-8 -*-
"""增量补丁包更新引擎隔离测试：不碰真实配置 / 安装目录 / 服务进程。"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # installer/ -> 项目根
sys.path.insert(0, str(ROOT))

import backend.services.app_updater as U
import backend.services.app_update as A

TMP = Path(tempfile.mkdtemp(prefix="lf_upd_test_"))
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name, extra)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_zip(files: dict, version="1.0.1", sums=True):
    """files: {relpath: bytes}，生成 (zip_bytes, name, sums_text)。"""
    buf = io.BytesIO()
    manifest_files = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, blob in files.items():
            zf.writestr(rel, blob)
            manifest_files.append({"path": rel, "sha256": sha(blob), "size": len(blob)})
        zf.writestr("manifest.json", json.dumps({
            "version": version, "min_base": "1.0.0", "notes": "test",
            "files": manifest_files,
        }, ensure_ascii=False))
    data = buf.getvalue()
    name = "LightFrame_Patch_v%s.zip" % version
    return data, name, ("%s  %s\n" % (sha(data), name)) if sums else ""


try:
    print("\n[1] 路径白名单与越界防护")
    ok_paths = ["app/backend/services/app_update.py", "app/static/index.html",
                "app/backend/main.py"]
    bad_paths = ["../evil.py", "app/../evil.py", "/etc/passwd", "C:/Windows/x.py",
                 "app/config/config.json", "app/space/u/file.png", "runtime/python.exe",
                 "光影工坊.exe", "app/backend/x.pyc", "app/backend/data.json",
                 "app/output/x.txt", "app/data/x.db"]
    for p in ok_paths:
        try:
            U._safe_rel(p)
            check("允许 " + p, True)
        except U.PatchError as e:
            check("允许 " + p, False, str(e))
    for p in bad_paths:
        try:
            U._safe_rel(p)
            check("拒绝 " + p, False, "未拒绝")
        except U.PatchError:
            check("拒绝 " + p, True)

    print("\n[2] SHA256SUMS 解析")
    data, name, sums = make_zip({"app/backend/services/t.py": b"hello"})
    check("解析正常", U._parse_sums(sums, name) == sha(data))
    try:
        U._parse_sums("deadbeef  other.zip\n", name)
        check("缺同名行应报错", False)
    except U.PatchError:
        check("缺同名行应报错", True)

    print("\n[3] 补丁完整链路（下载/校验/解压）")
    fake_root = TMP / "install"
    (fake_root / "app" / "backend" / "services").mkdir(parents=True)
    (fake_root / "app" / "static").mkdir(parents=True)
    old_install = U.INSTALL_ROOT
    U.INSTALL_ROOT = fake_root

    # 保存并替换真实状态路径，避免污染 config/
    old_state, old_backup = U.STATE_PATH, U.BACKUP_ROOT
    U.STATE_PATH = TMP / "state.json"
    U.BACKUP_ROOT = TMP / "backup"

    remote = {
        "has_update": True, "latest_version": "1.0.1",
        "patch_asset": {"name": name, "url": "https://example.invalid/p.zip"},
        "checksum_asset": {"url": "https://example.invalid/SUMS"},
    }
    orig_download, orig_status = U._download, A.get_status
    U._download = lambda url, timeout=60.0: (data if url.endswith("p.zip")
                                             else sums.encode("utf-8"))
    A.get_status = lambda force=False: dict(remote)
    try:
        nm, mf, files = U._fetch_patch()
        check("补丁包识别", nm == name)
        check("文件清单", len(files) == 1 and files[0][0] == "app/backend/services/t.py")
        check("状态进入 verifying", U._load_state()["phase"] == "verifying")
    finally:
        U._download, A.get_status = orig_download, orig_status

    print("\n[4] 整包 SHA256 不匹配应拒绝")
    U._download = lambda url, timeout=60.0: (data if url.endswith("p.zip")
                                             else ("0" * 64 + "  " + name + "\n").encode("utf-8"))
    A.get_status = lambda force=False: dict(remote)
    try:
        try:
            U._fetch_patch()
            check("SHA 不匹配被拒绝", False, "未拒绝")
        except U.PatchError as e:
            check("SHA 不匹配被拒绝", "校验失败" in str(e))
    finally:
        U._download, A.get_status = orig_download, orig_status

    print("\n[5] 文件 SHA256 不匹配应拒绝")
    tampered = dict(files)
    U._download = lambda url, timeout=60.0: (data if url.endswith("p.zip")
                                             else sums.encode("utf-8"))
    A.get_status = lambda force=False: dict(remote)
    # 直接改内部 manifest：用另一个 zip，manifest 声明错误 hash
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("app/backend/services/t.py", b"hello")
        zf.writestr("manifest.json", json.dumps({
            "version": "1.0.1",
            "files": [{"path": "app/backend/services/t.py", "sha256": "0" * 64}],
        }))
    bad_zip = buf.getvalue()
    U._download = lambda url, timeout=60.0: (
        bad_zip if url.endswith("p.zip")
        else ("%s  %s\n" % (sha(bad_zip), name)).encode("utf-8"))
    try:
        try:
            U._fetch_patch()
            check("文件 SHA 不匹配被拒绝", False, "未拒绝")
        except U.PatchError as e:
            check("文件 SHA 不匹配被拒绝", "校验失败" in str(e))
    finally:
        U._download, A.get_status = orig_download, orig_status

    print("\n[6] 覆盖 + 备份 + 回滚")
    target = fake_root / "app" / "backend" / "services" / "t.py"
    target.write_bytes(b"OLD_CONTENT")
    newfile = fake_root / "app" / "static" / "brand_new.html"
    payload = [("app/backend/services/t.py", b"NEW_CONTENT"),
               ("app/static/brand_new.html", b"<html>new</html>")]
    U._save_state({"target_version": "1.0.1"})
    bk = U._apply_files(payload)
    check("覆盖已有文件", target.read_bytes() == b"NEW_CONTENT")
    check("新增文件", newfile.read_bytes() == b"<html>new</html>")
    check("备份清单写入", (Path(bk["backup_dir"]) / "backup_manifest.json").exists())
    U._restore(bk["items"])
    check("回滚已有文件", target.read_bytes() == b"OLD_CONTENT")
    check("回滚删除新增文件", not newfile.exists())

    print("\n[7] 忙闲判定与重启时段")
    U.set_idle_probe(lambda: {"active": 0, "waiting": 0})
    check("全空=空闲", U.is_idle())
    U.set_idle_probe(lambda: {"active": 1, "waiting": 0})
    check("有执行中=忙", not U.is_idle())
    U.set_idle_probe(lambda: {"active": 0, "waiting": 3})
    check("有排队=忙", not U.is_idle())
    U.set_idle_probe(lambda: {"active": 0, "waiting": 0})
    check("any 时段允许", U._in_window())

    print("\n[8] 模式读写与状态接口")
    old_get_mode, old_save = U.get_mode, U.save_config
    store = {"update_install_mode": "manual", "update_auto_window": "any"}
    U.save_config = lambda uid="", patch=None: store.update(patch or {})
    def fake_get_mode():
        m, w = store["update_install_mode"], store["update_auto_window"]
        return {"mode": m, "mode_label": U.MODES[m],
                "window": w, "window_label": U.WINDOWS[w]}
    U.get_mode = fake_get_mode
    try:
        r = U.set_mode(mode="auto", window="night")
        check("设置 auto/night", r["mode"] == "auto" and r["window"] == "night")
        try:
            U.set_mode(mode="bogus")
            check("非法模式拒绝", False)
        except ValueError:
            check("非法模式拒绝", True)
        st = U.get_status()
        check("状态含忙闲字段", all(k in st for k in
              ("busy", "active", "waiting", "can_install", "hint", "installed_layout")))
        check("状态含模式", st["mode"] == "auto")
        check("night 时段字段存在", "in_window" in st)
    finally:
        U.get_mode, U.save_config = old_get_mode, old_save

    print("\n[9] 手动重启在忙时拒绝")
    U.set_idle_probe(lambda: {"active": 2, "waiting": 1})
    U._save_state({"pending_restart": True, "phase": "pending_restart"})
    r = U.request_restart()
    check("忙时拒绝且给出友好提示",
          (not r["ok"]) and "有任务在执行" in (r.get("message") or ""))
    U.set_idle_probe(lambda: {"active": 0, "waiting": 0})
    r = U.request_restart()
    check("空闲且非安装形态时给出明确提示",
          (not r["ok"]) and "源码运行形态" in (r.get("message") or ""))

    print("\n[9b] 重启助手：僵尸进程存活判断 + 完整流程")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rh_test", str(ROOT / "backend" / "services" / "restart_helper.py"))
    rh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rh)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    check("已退出进程判定为不存活", not rh.pid_alive(dead.pid),
          "OpenProcess 僵尸句柄坑回归")
    check("当前进程判定为存活", rh.pid_alive(__import__("os").getpid()))
    marker = TMP / "restart.json"
    marker.write_text(json.dumps({"target_version": "9.9.9", "pid": dead.pid}),
                      encoding="utf-8")
    rr = subprocess.run(
        [sys.executable, "backend/services/restart_helper.py",
         "--pid", str(dead.pid), "--exe", sys.executable,
         "--app", str(TMP), "--marker", str(marker)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=30)
    md = json.loads(marker.read_text(encoding="utf-8"))
    check("助手退出码 0", rr.returncode == 0, (rr.stderr or "")[:200])
    check("助手写入完成标记", md.get("ok") is True and bool(md.get("restarted_at")))

    print("\n[10] 补丁包制作脚本产出校验")
    py = sys.executable
    proc = subprocess.run(
        [py, "installer/make_patch.py", "--version", "9.9.9", "--notes", "selftest",
         "--file", "backend/services/app_update.py"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    check("脚本执行成功", proc.returncode == 0, (proc.stdout + proc.stderr)[-300:])
    zp = ROOT / "output" / "patch_v9.9.9" / "LightFrame_Patch_v9.9.9.zip"
    check("zip 生成", zp.exists())
    if zp.exists():
        with zipfile.ZipFile(zp) as zf:
            names = zf.namelist()
            check("含 manifest.json", "manifest.json" in names)
            check("路径带 app/ 前缀", "app/backend/services/app_update.py" in names)
            mf = json.loads(zf.read("manifest.json"))
            check("manifest 版本", mf["version"] == "9.9.9")
            check("manifest 带 sha256", len(mf["files"][0]["sha256"]) == 64)
        sums_p = ROOT / "output" / "patch_v9.9.9" / "SHA256SUMS.txt"
        check("SUMS 生成", sums_p.exists())
        if sums_p.exists():
            check("SUMS 值正确", sums_p.read_text(encoding="utf-8").startswith(sha(zp.read_bytes())))
        shutil.rmtree(ROOT / "output" / "patch_v9.9.9", ignore_errors=True)

    # 还原所有替换
    U.INSTALL_ROOT = old_install
    U.STATE_PATH, U.BACKUP_ROOT = old_state, old_backup
    print("\n================ 测试汇总 ================")
    print("PASS = %d   FAIL = %d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
finally:
    shutil.rmtree(TMP, ignore_errors=True)
