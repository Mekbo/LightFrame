# -*- coding: utf-8 -*-
"""生成「增量补丁包」：LightFrame_Patch_vX.Y.Z.zip + SHA256SUMS.txt。

用法：
  python installer/make_patch.py --version 1.0.1 \
      --notes "修复 xxx" \
      --file backend/services/app_update.py \
      --file static/index.html

产出目录：output/patch_vX.Y.Z/
  ├─ LightFrame_Patch_vX.Y.Z.zip   （ASCII 名，上传 GitHub Release）
  ├─ SHA256SUMS.txt                （同一 Release 的第二个附件）
  └─ 本版本更新说明.txt             （供人工查阅，不进 zip）

zip 内部结构（路径相对「安装目录」，与 app_updater 的白名单一致）：
  manifest.json
  app/backend/services/app_update.py
  app/static/index.html
  ...

manifest.json 含每个文件的 sha256 与体积，安装端会逐个校验；
SHA256SUMS.txt 让安装端在解压前先校验整个 zip，双重防篡改。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "output"

# 源码路径 → 补丁包内路径（相对安装目录）
# 源码在项目根，安装后位于 <安装目录>/app 下，故统一加 app/ 前缀
PREFIX = "app/"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="补丁版本号，如 1.0.1")
    ap.add_argument("--notes", default="", help="本版本更新说明（一句话）")
    ap.add_argument("--min-base", default="", help="最低基线版本，如 1.0.0")
    ap.add_argument("--file", action="append", default=[],
                    help="相对项目根的变动文件，可重复；路径需在白名单内")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不产出文件")
    a = ap.parse_args()

    version = a.version.lstrip("v")
    if not version.replace(".", "").isdigit():
        print("错误：版本号格式不合法，应形如 1.0.1")
        return 2
    if not a.file:
        print("错误：至少用 --file 指定一个变动文件")
        return 2

    files = []
    for raw in a.file:
        rel = raw.replace("\\", "/").lstrip("/")
        src = ROOT / rel
        if not src.is_file():
            print("错误：文件不存在 %s" % rel)
            return 2
        zip_rel = PREFIX + rel
        # 与 app_updater._safe_rel 的白名单保持一致
        if not (zip_rel.startswith("app/backend/") or zip_rel.startswith("app/static/")):
            print("错误：文件不在允许范围（仅 backend/ 与 static/）：%s" % rel)
            return 2
        if rel.lower().endswith((".pyc", ".pyo", ".json")):
            print("错误：不允许的文件类型：%s" % rel)
            return 2
        blob = src.read_bytes()
        files.append({
            "path": zip_rel,
            "sha256": sha256(blob),
            "size": len(blob),
        })
        print("  加入 %-52s %8d 字节" % (zip_rel, len(blob)))

    manifest = {
        "version": version,
        "min_base": a.min_base or "",
        "notes": a.notes or "",
        "files": files,
    }
    print("文件数：%d" % len(files))
    if a.dry_run:
        print("[dry-run] 检查通过，未产出文件")
        return 0

    out = OUT_ROOT / ("patch_v" + version)
    out.mkdir(parents=True, exist_ok=True)
    zip_name = "LightFrame_Patch_v%s.zip" % version
    zip_path = out / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        for item, raw in zip(files, a.file):
            zf.writestr(item["path"], (ROOT / raw.replace("\\", "/")).read_bytes())
        if a.notes:
            zf.writestr("更新说明.txt", a.notes + "\n")

    zip_hash = sha256(zip_path.read_bytes())
    (out / "SHA256SUMS.txt").write_text(
        "%s  %s\n" % (zip_hash, zip_name), encoding="utf-8")
    (out / "本版本更新说明.txt").write_text(
        "光影工坊 LightFrame v%s 增量补丁包\n\n%s\n\n"
        "安装方式：见仓库 docs/增量补丁包使用说明.md\n"
        "应用内管理员也可在「关于应用 → 系统更新」点「安装更新」自动完成。\n"
        % (version, a.notes or "（无说明）"), encoding="utf-8")

    print("\n产出目录：%s" % out)
    print("  附件 1：%s  %.1f KB" % (zip_name, zip_path.stat().st_size / 1024))
    print("  附件 2：SHA256SUMS.txt   （%s）" % zip_hash[:16])
    print("\n发版：新建 Release tag v%s，上传这两个附件（文件名保持 ASCII）。" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
