"""Read and write Feishu Base via the user's existing lark-cli connector."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from read_docx import file_hash, inside


class ConnectorError(ValueError):
    def __init__(self, message, *, subtype=None, retryable=False):
        super().__init__(message)
        self.subtype = subtype
        self.retryable = retryable


class NeedsSelection(ValueError):
    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = candidates


def repeated(flag, values):
    return [argument for value in values for argument in (flag, value)]


def identifier(value, prefix=""):
    if not isinstance(value, str) or not value.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("飞书返回的资源标识无效。")
    return value


def cli_path():
    executable = "lark-cli.exe" if os.name == "nt" else "lark-cli"
    configured = os.environ.get("JINGYAN_LARK_CLI")
    if configured:
        candidate = Path(configured).expanduser().absolute()
        if not candidate.is_file() or (os.name == "nt" and candidate.suffix.lower() != ".exe"):
            raise ConnectorError("JINGYAN_LARK_CLI 需指向当前飞书连接器的可执行文件。")
        return str(candidate)
    managed = Path.home() / ".workbuddy/binaries/node/cli-connector-packages"
    for modules in (managed / "lib/node_modules", managed / "node_modules"):
        candidate = modules / "@larksuite/cli/bin" / executable
        if candidate.is_file():
            return str(candidate)
    if found := shutil.which(executable):
        return found
    raise ConnectorError("未找到飞书连接器，请在 WorkBuddy 连接飞书后再试。")


class Connector:
    def __init__(self, directory):
        self.directory = directory
        self.executable = cli_path()

    def call(self, action, *arguments):
        environment = os.environ.copy()
        for key in ("DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY"):
            environment.pop(key, None)
        environment.update(LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1",
                           LARK_CLI_NO_PROXY_WARN="1")
        try:
            result = subprocess.run([self.executable, "base", "+" + action, *arguments,
                                     "--as", "user", "--format", "json"],
                                    cwd=self.directory, env=environment, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as error:
            raise ConnectorError(f"飞书 {action} 请求超时。", retryable=True) from error
        try:
            response = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as error:
            raise ConnectorError(f"飞书 {action} 未返回可读取的结果（退出码 {result.returncode}）。") from error
        if not isinstance(response, dict):
            raise ConnectorError(f"飞书 {action} 返回格式异常。")
        if result.returncode != 0 or response.get("ok") is not True:
            detail = response.get("error")
            detail = detail if isinstance(detail, dict) else {}
            subtype = detail.get("subtype")
            retryable = detail.get("type") in {"network", "rate_limit"} or str(detail.get("code")) == "429"
            reason = detail.get("message") or f"退出码 {result.returncode}"
            raise ConnectorError(f"飞书 {action} 失败：{str(reason)[:240]}", subtype=subtype, retryable=retryable)
        data = response.get("data")
        if not isinstance(data, dict) or response.get("identity") != "user":
            raise ConnectorError(f"飞书 {action} 的用户身份或返回格式异常。")
        return data

    def resolve(self, target, table=None):
        if "://" in target:
            url = urlsplit(target)
            if url.scheme != "https" or url.username or url.password:
                raise ValueError("请提供飞书多维表格的 HTTPS 链接。")
            result = self.call("url-resolve", "--url", target)
        else:
            try:
                result = self.call("title-resolve", "--title", target)
            except ConnectorError as error:
                raise NeedsSelection("未能按表名定位，请提供飞书多维表格链接。") from error
            if not result.get("base_token") or result.get("title") != target:
                raise NeedsSelection("无法唯一核对表名，请提供飞书多维表格链接。")
        if result.get("resource_type") != "bitable" or result.get("block_type") not in (None, "", "table"):
            raise NeedsSelection("请提供多维表格的数据表链接。")
        base = identifier(result.get("base_token"))
        table_id = result.get("table_id")
        table_name = result.get("block_name") or table_id
        if table is not None or not table_id:
            listing = self.call("table-list", "--base-token", base)
            tables = listing["tables"]
            matches = [item for item in tables if table is None or table in (item["name"], item["id"])]
            if len(matches) != 1:
                raise NeedsSelection("请选择数据表，或提供打开该数据表后的链接。", [item["name"] for item in tables])
            table_id, table_name = matches[0]["id"], matches[0]["name"]
        if result.get("record_id") and result.get("table_id") != table_id:
            raise ValueError("记录链接与指定数据表不一致。")
        same_table = result.get("table_id") == table_id
        return {"base_token": base, "table_id": identifier(table_id, "tbl"), "table_name": table_name,
                "view_id": result.get("view_id") if same_table else None, "record_id": result.get("record_id")}

    def table_call(self, action, table, *arguments):
        return self.call(action, "--base-token", table["base_token"], "--table-id", table["table_id"], *arguments)

    def fields(self, table):
        data = self.table_call("field-list", table)
        fields = data["fields"]
        if data.get("total", len(fields)) != len(fields):
            raise ValueError("飞书字段列表不完整。")
        return fields

    def records(self, table, fields, view):
        arguments = repeated("--field-id", fields)
        if table.get("record_id"):
            return [self.record(table, fields, table["record_id"])]
        if view:
            arguments += ["--view-id", identifier(view, "vew")]
        rows = []
        seen = set()
        revision = None
        while True:
            data = self.table_call("record-list", table, *arguments, "--offset", str(len(rows)), "--limit", "200")
            if rows and revision != data.get("rev"):
                raise ValueError("读取分页期间表格发生变化，请重新清点。")
            revision = data.get("rev")
            page = record_rows(data, fields)
            page_ids = {row["record_id"] for row in page}
            if seen & page_ids:
                raise ValueError("分页返回了重复的项目记录，请重新清点。")
            seen.update(page_ids)
            rows.extend(page)
            if data.get("has_more") is False:
                return rows
            if data.get("has_more") is not True or not page:
                raise ValueError("飞书未返回完整的项目分页。")

    def record(self, table, fields, record_id):
        data = self.table_call("record-get", table, "--record-id", identifier(record_id, "rec"),
                               *repeated("--field-id", fields))
        rows = record_rows(data, fields)
        if len(rows) != 1 or rows[0]["record_id"] != record_id:
            raise ValueError("飞书记录与本次项目不匹配。")
        return rows[0]

    def download(self, table, record_id, attachment, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        relative = "./" + path.relative_to(self.directory).as_posix()
        data = self.table_call("record-download-attachment", table, "--record-id", record_id,
                               "--file-token", attachment["file_token"], "--output", relative, "--overwrite")
        files = data["downloaded"]
        if len(files) != 1:
            raise ValueError("附件下载回执数量异常。")
        returned = self.directory / files[0]["saved_path"]
        saved = inside(returned, self.directory)
        if saved != path.resolve() or not saved.is_file() or saved.stat().st_size != attachment["size"]:
            raise ValueError(f"附件路径或文件大小核对失败：{attachment['name']}")
        if files[0].get("file_token", attachment["file_token"]) != attachment["file_token"]:
            raise ValueError("附件下载回执与请求标识不一致。")
        return file_hash(saved)


def record_rows(data, fields):
    columns = data["field_id_list"]
    keys, matrix = data["record_id_list"], data["data"]
    if (not all(isinstance(value, list) for value in (columns, keys, matrix))
            or set(columns) != set(fields) or len(columns) != len(fields) or len(keys) != len(matrix)
            or len(set(keys)) != len(keys) or any(len(row) != len(columns) for row in matrix)):
        raise ValueError("飞书记录或字段读取不完整，请检查访问权限。")
    return [{"record_id": identifier(key, "rec"), **dict(zip(columns, row))} for key, row in zip(keys, matrix)]


def attachments(value):
    if value is None or value == []:
        return []
    if not isinstance(value, list):
        raise ValueError("附件字段返回格式异常。")
    result = []
    for file in value:
        name, size = file["name"], file["size"]
        if not isinstance(name, str) or not name.strip() or type(size) is not int or size <= 0:
            raise ValueError("附件名称或大小缺失。")
        result.append({"file_token": identifier(file["file_token"]), "name": name, "size": size})
    if len({file["file_token"] for file in result}) != len(result):
        raise ValueError("附件标识重复。")
    return result
