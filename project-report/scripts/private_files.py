"""Restrict per-user model credentials on macOS, Linux and Windows."""

import os
from pathlib import Path
import re
import subprocess


def protect_private_file(path):
    path = Path(path)
    if os.name != "nt":
        path.chmod(0o600)
        return
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True)
    sid = re.search(rb"S-1-\d+(?:-\d+)+", identity.stdout)
    if not sid:
        raise ValueError("无法识别当前 Windows 用户，密钥文件权限设置未完成。")
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                             "*" + sid[0].decode("ascii") + ":F", "*S-1-5-18:F"], capture_output=True)
    if result.returncode:
        raise ValueError("个人密钥文件权限设置失败，请检查当前用户权限。")
