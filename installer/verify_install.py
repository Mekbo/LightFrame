# -*- coding: utf-8 -*-
"""阶段 6 · 安装包实装验证。

流程：静默安装 → 结构检查 → 改端口 2027 启动 → HTTP 200 → 状态/自启/快捷方式
      → 停止 → 卸载 → 零残留检查。
所有副作用（注册表自启项、桌面/开始菜单快捷方式）在结束时全部清理。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INS = Path(__file__).resolve().parent
sys.path.insert(0, str(INS / "src"))
import common as C  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
SETUP = ROOT / "output" / "光影工坊_一键安装包_v1.0.exe"
TEST_DIR = Path(tempfile.gettempdir()) / ("GY_QA_%d" % int(time.time()))
PORT = 2027
URL = "http://127.0.0.1:%d" % PORT
RESULT = []


def check(name, ok, detail=""):
    RESULT.append({"name": name, "ok": bool(ok), "detail": detail})
    print("%s %-38s %s" % ("[PASS]" if ok else "[FAIL]", name, detail), flush=True)
    return bool(ok)


def wait_http(url, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "/", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def http_down():
    try:
        urllib.request.urlopen(URL + "/", timeout=0.6)
        return False
    except Exception:
        return True


def run(exe, *args, timeout=120):
    # 用 DEVNULL 而不是 capture_output：GUI 子进程若派生孙进程继承管道，
    # communicate() 可能永久等不到 EOF，导致验证脚本假死。
    try:
        return subprocess.run([str(exe), *args], timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        print("  run error: %s" % e, flush=True)
        return None


def reg_value():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, C.REG_RUN_KEY)
        try:
            v, _ = winreg.QueryValueEx(k, C.REG_RUN_VALUE)
            return v
        finally:
            k.Close()
    except FileNotFoundError:
        return None
    except Exception:
        return None


def clear_reg():
    try:
        import winreg
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, C.REG_RUN_KEY)
        try:
            winreg.DeleteValue(k, C.REG_RUN_VALUE)
        except FileNotFoundError:
            pass
        finally:
            k.Close()
    except Exception:
        pass


BASELINE = {}


def snapshot():
    """记录验证前的注册表值与快捷方式，结束后按原样恢复，保证零污染。"""
    BASELINE["reg"] = reg_value()
    BASELINE["links"] = {}
    for p in C.all_shortcuts():
        try:
            BASELINE["links"][str(p)] = p.read_bytes() if p.exists() else None
        except Exception:
            BASELINE["links"][str(p)] = None


def restore_side_effects():
    old = BASELINE.get("reg")
    try:
        import winreg
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, C.REG_RUN_KEY)
        try:
            if old is None:
                try:
                    winreg.DeleteValue(k, C.REG_RUN_VALUE)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(k, C.REG_RUN_VALUE, 0, winreg.REG_SZ, old)
        finally:
            k.Close()
    except Exception:
        pass
    for s, data in BASELINE.get("links", {}).items():
        p = Path(s)
        try:
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        except Exception:
            pass
    try:
        sm = C.startmenu_dir()
        if sm.exists() and not any(sm.iterdir()):
            sm.rmdir()
    except Exception:
        pass
    backup = TEST_DIR.parent / "光影工坊_数据备份"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)


def main():
    if not SETUP.exists():
        print("安装包不存在：%s" % SETUP)
        return 2
    snapshot()
    baseline_reg = BASELINE["reg"]
    print("安装包：%s (%.1f MB)" % (SETUP.name, SETUP.stat().st_size / 1024 / 1024), flush=True)
    print("测试目录：%s" % TEST_DIR, flush=True)
    TEST_DIR.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 静默安装 ----------
    res = TEST_DIR.parent / ("gy_qa_result_%d.json" % int(time.time()))
    r = run(SETUP, "--silent", "--dir=" + str(TEST_DIR), "--autostart",
            "--shortcut", "--result=" + str(res), timeout=240)
    check("静默安装进程正常退出", r is not None and r.returncode == 0,
          "code=%s" % (r.returncode if r else "n/a"))
    ok = False
    for _ in range(60):
        if res.exists():
            try:
                ok = bool(json.loads(res.read_text(encoding="utf-8")).get("ok"))
            except Exception:
                ok = False
            break
        time.sleep(1)
    check("安装结果报告 ok=true", ok, str(res))

    exe = TEST_DIR / "光影工坊.exe"
    # ---------- 2. 结构 ----------
    need = [
        TEST_DIR / "runtime" / "python.exe",
        TEST_DIR / "app" / "backend" / "run.py",
        TEST_DIR / "app" / "backend" / "main.py",
        TEST_DIR / "app" / "static" / "index.html",
        TEST_DIR / "app" / "tools" / "ffmpeg" / "ffmpeg.exe",
        TEST_DIR / "app" / "backend" / "fonts",
        exe, TEST_DIR / "app.ico", TEST_DIR / "使用说明.txt",
    ]
    check("安装目录结构完整", all(p.exists() for p in need),
          "缺失：%s" % [p.name for p in need if not p.exists()])
    cfg_files = list((TEST_DIR / "app" / "config").glob("*.json"))
    check("全新安装零用户数据（未预置账号/配置）", len(cfg_files) == 0,
          "config json = %d" % len(cfg_files))
    fonts = list((TEST_DIR / "app" / "backend" / "fonts").iterdir())
    check("内置字体已随包", len(fonts) >= 10, "%d 个字体" % len(fonts))

    # ---------- 3. 启动 ----------
    (TEST_DIR / "app" / "config").mkdir(parents=True, exist_ok=True)
    (TEST_DIR / "app" / "config" / "config.json").write_text(
        json.dumps({"port": PORT}), encoding="utf-8")
    run(exe, "--autostart", timeout=30)
    check("后台服务启动并响应 HTTP 200", wait_http(URL),
          URL)
    check("服务日志已生成", (TEST_DIR / "logs" / "server.log").exists())

    run(exe, "--status", timeout=20)
    st = TEST_DIR / "logs" / "status.txt"
    check("状态探测可用", st.exists() and "running=True" in (st.read_text(encoding="utf-8") if st.exists() else ""),
          (st.read_text(encoding="utf-8").strip() if st.exists() else "无 status.txt"))

    # ---------- 4. 自启与快捷方式 ----------
    v = reg_value()
    check("开机自启已写入 HKCU Run", bool(v) and str(TEST_DIR).lower() in str(v).lower(),
          str(v))
    d = C.desktop_dir()
    sm = C.startmenu_dir()
    check("桌面快捷方式（主程序 + 启动设置）",
          (d / C.LINK_NAME).exists() and (d / C.LINK_MANAGER_NAME).exists())
    check("开始菜单快捷方式（含卸载）",
          (sm / C.LINK_NAME).exists() and (sm / C.LINK_MANAGER_NAME).exists()
          and (sm / "卸载光影工坊.lnk").exists())

    # ---------- 5a. 安装向导页面（本地 Web 界面） ----------
    wz_proc = None
    try:
        wpf = Path(tempfile.gettempdir()) / "gy_setup_wizard.port"
        if wpf.exists():
            wpf.unlink()
        wz_proc = subprocess.Popen(
            [str(SETUP), "--wizard-headless"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        wport = None
        for _ in range(60):
            if wpf.exists():
                try:
                    wport = int(wpf.read_text(encoding="utf-8").strip())
                    break
                except Exception:
                    pass
            time.sleep(0.3)
        check("安装向导页面服务已就绪", wport is not None, "port=%s" % wport)
        if wport:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/" % wport,
                                           timeout=3) as rp:
                    page = rp.read().decode("utf-8", "ignore")
                check("安装向导为中文可视化界面",
                      "一键安装" in page and "开机自动启动" in page)
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/defaults" % wport, timeout=3) as rp:
                    d = json.loads(rp.read().decode("utf-8"))
                check("向导默认参数接口正常", bool(d.get("dir")), str(d.get("dir")))
                # 内置目录选择器 API：盘符 + 目录列表 + 新建文件夹
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/listdir" % wport,
                    data=json.dumps({"path": str(Path.home())}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as rp:
                    ld = json.loads(rp.read().decode("utf-8"))
                check("目录选择器能列出盘符与主目录",
                      ld.get("ok") is True and len(ld.get("drives") or []) >= 1
                      and isinstance(ld.get("dirs"), list),
                      "drives=%s dirs=%d" % (ld.get("drives"), len(ld.get("dirs") or [])))
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/listdir" % wport,
                    data=json.dumps({"path": "Z:\\不存在的目录"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as rp:
                    ld2 = json.loads(rp.read().decode("utf-8"))
                check("目录选择器对无效路径返回错误", ld2.get("ok") is False,
                      str(ld2.get("error", ""))[:60])
                mk_name = "gy_qa_dir_%d" % int(time.time())
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/mkdir" % wport,
                    data=json.dumps({"parent": str(Path.home()), "name": mk_name}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as rp:
                    md = json.loads(rp.read().decode("utf-8"))
                mk_dir = Path(md.get("dir") or "")
                check("目录选择器支持新建文件夹",
                      md.get("ok") is True and mk_dir.exists() and mk_dir.name == mk_name,
                      str(md.get("dir") or md.get("error")))
                if mk_dir.exists():
                    shutil.rmtree(mk_dir, ignore_errors=True)
                # 旧「系统窗口」接口：probe 模式不弹窗，验证环境可用性与返回结构
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/browse" % wport,
                    data=json.dumps({"probe": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=8) as rp:
                    bd = json.loads(rp.read().decode("utf-8"))
                check("系统窗口接口（probe）可用且结构明确",
                      isinstance(bd, dict) and bd.get("probe") is True
                      and bd.get("powershell") is True,
                      "powershell=%s" % bd.get("powershell"))
                # 页面已内置目录选择器弹层
                check("向导页面含内置目录选择器",
                      "pkMask" in page and "listdir" in page and "新建文件夹" in page)
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/quit" % wport, timeout=3).read()
            except Exception as e:
                check("安装向导接口调用", False, str(e))
    finally:
        if wz_proc:
            try:
                wz_proc.wait(timeout=15)
                wizard_gone = True
            except Exception:
                wz_proc.terminate()
                wizard_gone = False
            check("点「完成」后安装器进程自动退出", wizard_gone,
                  "退出耗时<15s，无需手动结束" if wizard_gone else "进程未退出，被强制终止")

    # ---------- 5b. 启动设置控制台（本地 Web API） ----------
    mgr_proc = None
    try:
        mgr_proc = subprocess.Popen(
            [str(exe), "--manager-headless"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        pf = TEST_DIR / "manager.port"
        port = None
        for _ in range(40):
            if pf.exists():
                try:
                    port = int(pf.read_text(encoding="utf-8").strip())
                    break
                except Exception:
                    pass
            time.sleep(0.3)
        check("启动设置控制台已就绪", port is not None,
              "port=%s" % port)
        if port:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/status" % port, timeout=3) as rp:
                    st = json.loads(rp.read().decode("utf-8"))
                check("控制台状态接口正确",
                      st.get("running") is True and st.get("autostart") is True
                      and str(PORT) in str(st.get("url")),
                      "running=%s url=%s auto=%s" %
                      (st.get("running"), st.get("url"), st.get("autostart")))
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/" % port, timeout=3) as rp:
                    page = rp.read().decode("utf-8", "ignore")
                check("控制台页面可访问且为中文界面",
                      "启动设置" in page and "开机自动启动" in page)
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/quit" % port, timeout=3).read()
            except Exception as e:
                check("控制台接口调用", False, str(e))
    finally:
        if mgr_proc:
            try:
                mgr_proc.wait(timeout=15)
                mgr_gone = True
            except Exception:
                mgr_proc.terminate()
                mgr_gone = False
            check("控制台关闭后主程序进程自动退出", mgr_gone,
                  "退出耗时<15s" if mgr_gone else "进程未退出，被强制终止")

    # ---------- 5c. 「立即启动」回归：安装器不得因常驻子进程挂住 ----------
    res2 = TEST_DIR.parent / ("gy_qa_result2_%d.json" % int(time.time()))
    t0 = time.time()
    # 升级式重装（数据目录保留），并勾选「安装完成后立即启动」：
    # 修复前 onefile 引导进程会等常驻的应用进程退出 → 安装器永远不退出（需手动结束）。
    r2 = run(SETUP, "--silent", "--dir=" + str(TEST_DIR), "--launch",
             "--result=" + str(res2), timeout=180)
    check("带「立即启动」安装后安装器自行退出",
          r2 is not None and r2.returncode == 0,
          "%.1fs code=%s（超时即挂住）" % (time.time() - t0, r2.returncode if r2 else "TIMEOUT"))
    check("「立即启动」后服务恢复在线", wait_http(URL, 90), URL)

    # ---------- 5d. 向导安装完成后不点「完成」：安装器必须自行退出 ----------
    # 复现用户场景：安装完成 / 关闭标签页 / 走开 → 旧版向导要挂满 15 分钟空闲超时，
    # 表现为「安装器没有正常退出，需要手动结束」。
    wz2 = None
    try:
        wpf = Path(tempfile.gettempdir()) / "gy_setup_wizard.port"
        if wpf.exists():
            wpf.unlink()
        wz2 = subprocess.Popen(
            [str(SETUP), "--wizard-headless"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        w2port = None
        for _ in range(60):
            if wpf.exists():
                try:
                    w2port = int(wpf.read_text(encoding="utf-8").strip())
                    break
                except Exception:
                    pass
            time.sleep(0.3)
        done_ok = False
        t_install = time.time()
        if w2port:
            # 通过向导 API 真实安装（不勾任何选项、不启动应用）
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/install" % w2port,
                data=json.dumps({"dir": str(TEST_DIR), "autostart": False,
                                 "shortcut": False, "lan": False,
                                 "launch": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=8).read()
            deadline = time.time() + 240
            while time.time() < deadline:
                try:
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/api/progress" % w2port,
                        data=b"{}", headers={"Content-Type": "application/json"},
                        method="POST")
                    with urllib.request.urlopen(req, timeout=5) as rp:
                        pg = json.loads(rp.read().decode("utf-8"))
                    if pg.get("done"):
                        done_ok = bool(pg.get("ok"))
                        break
                except Exception:
                    pass  # 服务已自行退出也视为完成
                time.sleep(0.7)
        check("向导 API 真实安装完成", done_ok,
              "%.1fs" % (time.time() - t_install))
        # 不调用 /api/quit：等安装器靠「完成后缩短的空闲回收」自行退出
        t_wait = time.time()
        auto_gone = False
        try:
            wz2.wait(timeout=100)
            auto_gone = True
        except Exception:
            wz2.terminate()
        check("不点「完成」时安装器也自动退出（≤90s）", auto_gone,
              "%.1fs 后退出，无需手动结束" % (time.time() - t_wait))
    finally:
        if wz2:
            try:
                wz2.wait(timeout=5)
            except Exception:
                wz2.terminate()

    # ---------- 6. 停止 ----------
    run(exe, "--stop", timeout=30)
    check("停止服务后端口不可达", http_down())

    # ---------- 6. 卸载 ----------
    run(exe, "--uninstall-silent", timeout=30)
    gone = False
    for _ in range(30):
        if not TEST_DIR.exists():
            gone = True
            break
        time.sleep(0.5)
    check("卸载后安装目录自动清除", gone, str(TEST_DIR))

    # ---------- 7. 零残留 ----------
    restore_side_effects()
    check("注册表自启项已还原到验证前", reg_value() == baseline_reg)
    restored = all(
        (Path(s).exists() if data is not None else not Path(s).exists())
        for s, data in BASELINE["links"].items()
    )
    check("快捷方式已还原到验证前", restored)
    check("测试端口已释放", http_down())
    check("测试安装目录已清除", not TEST_DIR.exists())

    passed = sum(1 for x in RESULT if x["ok"])
    print("\n=== 验证结果：%d/%d PASS ===" % (passed, len(RESULT)), flush=True)
    report = INS / "dist" / "verify_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        "total": len(RESULT), "passed": passed,
        "failed": len(RESULT) - passed, "cases": RESULT,
        "setup_size_mb": round(SETUP.stat().st_size / 1024 / 1024, 1),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(RESULT) else 1


if __name__ == "__main__":
    sys.exit(main())
