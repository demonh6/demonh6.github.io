#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本地 main 的改动同步到 GitHub 远程（REST Git Data API 通道）
================================================================

为什么不用 git push：
  本机网络经代理出网，git smart-HTTP 的 receive-pack 端点被间歇性拦截
  （CONNECT 502 / Empty reply），而 api.github.com 可稳定访问。
  此脚本用 Git Data API 逐文件建 blob → tree → commit → 更新 ref，
  结果与一次 `git push` 等价。

一次性初始化（只需做一次，令牌不会写进仓库）：
  1) 在 https://github.com/settings/personal-access-tokens/new 生成 fine-grained PAT
     - Repository access: 只选 demonh6/demonh6.github.io
     - Permissions: Contents = Read & Write, Workflows = Read & Write, Metadata = Read
  2) 把令牌写入 Windows 凭据管理器（加密存储，不明文落盘）：
       printf "protocol=https\\nhost=github.com\\nusername=demonh6\\npassword=<PAT>\\n\\n" \\
         | git credential approve
     并额外存一份到本脚本读取的专用目标：
       python scripts/sync_github.py --save-token <PAT>
     或直接设置环境变量 GITHUB_TOKEN=<PAT>

之后每次改动（代理内可直接调用，无需用户手动 push）：
  git add -A && git commit -m "..."
  python scripts/sync_github.py

凭据优先级：环境变量 GITHUB_TOKEN / GH_TOKEN → Windows 凭据管理器
专用目标 "WorkBuddy GitHub Sync" → git credential fill(github.com)。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER, NAME, BRANCH = "demonh6", "demonh6.github.io", "main"
API = "https://api.github.com/repos/%s/%s" % (OWNER, NAME)
CRED_TARGET = "WorkBuddy GitHub Sync"

# 这些文件由 GitHub Actions（每 10 分钟）自动生成，本地副本必然过期，
# 一旦推上去会把远程最新数据覆盖回旧版，因此同步时始终跳过。
EXCLUDE = {"data/dashboard.json", "data/research_urls.json"}

# ---------------------------------------------------------------- 凭据
class _FT(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _CRED(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", _FT), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR)]


def cred_read(target: str):
    if os.name != "nt":
        return None
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(_CRED))]
        adv.CredReadW.restype = wintypes.BOOL
        adv.CredFree.argtypes = [ctypes.c_void_p]
        p = ctypes.POINTER(_CRED)()
        if not adv.CredReadW(target, 1, 0, ctypes.byref(p)):
            return None
        n = p.contents.CredentialBlobSize
        raw = bytes(bytearray(ctypes.cast(p.contents.CredentialBlob,
                                          ctypes.POINTER(ctypes.c_byte * n)).contents))
        adv.CredFree(p)
        return raw.decode("utf-8", "ignore").strip("\x00").strip()
    except Exception:  # noqa: BLE001
        return None


def cred_write(target: str, value: str):
    """用 CredWriteW 写入 Windows 凭据管理器（加密，由当前用户账户保护）。"""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    blob = value.encode("utf-8")
    arr = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    cred = _CRED()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(arr, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = OWNER
    adv.CredWriteW.argtypes = [ctypes.POINTER(_CRED), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def get_token():
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k].strip()
    t = cred_read(CRED_TARGET)
    if t:
        return t
    # 不调用 git credential fill：GCM 在无缓存时会弹出登录窗口并阻塞自动化流程。
    return None


# ---------------------------------------------------------------- 工具
def git(*args):
    # core.quotepath=false：让中文路径按原样返回，避免被八进制转义后无法定位文件
    return subprocess.check_output(
        ["git", "-c", "core.quotepath=false", "-C", REPO] + list(args)
    ).decode("utf-8", "ignore").strip()


def log(msg):
    print("[sync] " + msg, flush=True)


def api(method, path, body=None, token=None, retries=3):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": "workbuddy-sync", "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            if e.code in (401, 403):
                raise RuntimeError("令牌无效或权限不足（%s）：%s" % (e.code, detail))
            last = RuntimeError("HTTP %s %s : %s" % (e.code, url.split("/")[-1], detail))
            if e.code < 500:
                raise last
        except Exception as e:  # noqa: BLE001
            last = e
        if i < retries - 1:
            time.sleep(1.5 * (i + 1))
    raise last


# ---------------------------------------------------------------- 主流程
def local_tracked():
    """本地 HEAD 跟踪的文件（= 工作区应有的文件清单）。"""
    out = git("ls-tree", "-r", "--name-only", "HEAD")
    return [p for p in out.splitlines() if p and p not in EXCLUDE]


def blob_sha(path):
    """工作区文件的 git blob sha（与远程 tree 里的 sha 同算法）。"""
    return git("hash-object", os.path.join(REPO, path.replace("/", os.sep)))


def remote_blobs(base_sha, token):
    tree = api("GET", "/git/trees/%s?recursive=1" % base_sha, token=token)
    return {e["path"]: e["sha"] for e in tree.get("tree", []) if e.get("type") == "blob"}


def diff_against_remote(base_sha, token):
    """按「文件内容」而不是提交历史求差集，避免本地 origin/main 过期导致误判。"""
    remote = remote_blobs(base_sha, token)
    tracked = local_tracked()
    changed, same, missing = [], [], []
    for p in tracked:
        full = os.path.join(REPO, p.replace("/", os.sep))
        if not os.path.exists(full):
            if p in remote:
                missing.append(p)  # 本地已删除但远程还在 → 需要删除
            continue
        try:
            sha = blob_sha(p)
        except Exception:  # noqa: BLE001
            continue
        if remote.get(p) == sha:
            same.append(p)
        else:
            changed.append((p, p not in remote))
    known = set(tracked)
    only_remote = sorted(
        p for p in remote
        if p not in known and not p.startswith(".") and p not in EXCLUDE
    )
    return changed, same, only_remote, missing


def sync(dry=False, token=None):
    tok = token or get_token()
    if not tok:
        raise RuntimeError("未找到 GitHub 令牌；请先运行 --save-token 或设置 GITHUB_TOKEN")
    user = api("GET", "https://api.github.com/user", token=tok)
    log("已认证为 %s" % user.get("login"))

    message = git("log", "-1", "--format=%B") or "chore: sync from local"
    for round_no in range(1, 4):
        ref = api("GET", "/git/ref/heads/" + BRANCH, token=tok)
        base_sha = ref["object"]["sha"]
        commit = api("GET", "/git/commits/" + base_sha, token=tok)
        base_tree = commit["tree"]["sha"]
        log("远程基线 %s（第 %d 轮）" % (base_sha[:8], round_no))

        changed, same, only_remote, missing = diff_against_remote(base_sha, tok)
        if only_remote:
            log("仅远程存在、本地没有的文件 %d 个（不自动删除）" % len(only_remote))
            for p in only_remote[:8]:
                log("  R   " + p)
        if missing:
            log("本地已删除的文件 %d 个（将同步删除）" % len(missing))
            for p in missing[:8]:
                log("  D   " + p)
        if not changed and not missing:
            log("远程内容已是最新（%d 个文件一致），无需推送" % len(same))
            return
        log("待同步 %d 个文件（%d 个已一致）" % (len(changed) + len(missing), len(same)))
        for p, is_new in changed:
            log("  %s %s" % ("A" if is_new else "M", p))
        if dry:
            return

        tree = []
        for p, _is_new in changed:
            full = os.path.join(REPO, p.replace("/", os.sep))
            with open(full, "rb") as f:
                raw = f.read()
            blob = api("POST", "/git/blobs", {
                "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"
            }, token=tok)
            tree.append({"path": p, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        for p in missing:
            tree.append({"path": p, "mode": "100644", "type": "blob", "sha": None})

        new_tree = api("POST", "/git/trees", {"base_tree": base_tree, "tree": tree}, token=tok)
        new_commit = api("POST", "/git/commits", {
            "message": message, "tree": new_tree["sha"], "parents": [base_sha]
        }, token=tok)
        try:
            api("PATCH", "/git/refs/heads/" + BRANCH, {"sha": new_commit["sha"]}, token=tok)
        except RuntimeError as e:
            if "422" in str(e) or "409" in str(e):
                log("远程在此期间有新提交，重试…")
                time.sleep(2)
                continue
            raise
        log("已推送 %s -> %s" % (base_sha[:8], new_commit["sha"][:8]))
        log("本地无需 reset：下次同步仍按文件内容比对，天然幂等。")
        return
    raise RuntimeError("重试 3 次仍未能更新远程引用")


def main():
    ap = argparse.ArgumentParser(description="把本地改动同步到 GitHub main（REST API 通道）")
    ap.add_argument("--save-token", metavar="PAT", help="把 PAT 加密写入 Windows 凭据管理器")
    ap.add_argument("--dry-run", action="store_true", help="只列出待同步文件")
    ap.add_argument("--token", help="临时使用指定令牌（不落盘）")
    args = ap.parse_args()
    if args.save_token:
        cred_write(CRED_TARGET, args.save_token.strip())
        log("令牌已加密保存到 Windows 凭据管理器：" + CRED_TARGET)
        return
    try:
        sync(dry=args.dry_run, token=args.token)
    except Exception as e:  # noqa: BLE001
        log("失败：" + str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
