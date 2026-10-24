import asyncio
import hashlib
import functools
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlparse

import httpx
import psutil
from langchain.tools import tool
from playwright.async_api import async_playwright

from .models import CommandSpec, CodingResult
from .store import uid

IGNORED = {".venv", "venv", "node_modules", ".git", "__pycache__", "dist", "build", ".pytest_cache"}
SOURCE = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".toml", ".yaml", ".yml", ".sql"}


def safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("路径超出当前项目目录")
    return candidate


def files(root: Path):
    result = []
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = sorted(d for d in folders if d not in IGNORED and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            p = Path(directory) / name
            if not p.is_symlink() and p.resolve().is_relative_to(root.resolve()):
                result.append(p.relative_to(root).as_posix())
            if len(result) >= 1500:
                return result
    return result


def fingerprint(root: Path):
    digest = hashlib.sha256()
    for name in files(root):
        path = root / name
        if path.suffix in SOURCE or path.name in {"package.json", "requirements.txt", "requirements.in"} or path.name.startswith("tsconfig"):
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def command_argv(spec: CommandSpec, root: Path):
    cwd = safe_path(root, spec.cwd)
    if not cwd.is_dir():
        raise ValueError("命令工作目录不存在")
    name = spec.program
    if name.lower() in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh", "rm", "del", "rmdir", "format", "shutdown"}:
        raise ValueError("请直接指定项目运行程序，不使用命令壳或系统管理命令")
    if name.lower() in {"npm", "npm.cmd", "npx", "npx.cmd"}:
        node = shutil.which("node")
        cli = Path(node).parent / "node_modules/npm/bin/npm-cli.js" if node else Path("")
        if not cli.is_file():
            raise ValueError("未找到 Node/npm 环境")
        args = (["exec", "--"] if name.startswith("npx") else []) + spec.args
        return [node, str(cli), *args], cwd
    if name in {"python", "python3", "py"}:
        name = sys.executable
    elif "/" in name or "\\" in name:
        name = str(safe_path(root, name))
    else:
        name = shutil.which(name) or name
    return [name, *spec.args], cwd


def child_env():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMSPEC", "PATHEXT", "LANG"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", NO_COLOR="1")
    return env


class Processes:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.services = {}

    async def kill(self, proc):
        if proc.returncode is not None:
            return
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for item in reversed(children):
                try:
                    item.kill()
                except psutil.Error:
                    pass
            parent.kill()
        except psutil.Error:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 8)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    async def command(self, spec, root, timeout=120):
        argv, cwd = command_argv(spec, root)
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=child_env(),
                  stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        chunks = []
        async def read():
            while block := await proc.stdout.read(8192):
                chunks.append(block)
                if sum(map(len, chunks)) > 2_000_000:
                    raise ValueError("命令输出超过2MB限制")
        reader = asyncio.create_task(read())
        try:
            await asyncio.wait_for(asyncio.gather(proc.wait(), reader), timeout)
        except BaseException:
            await self.kill(proc)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        return {"exit_code": proc.returncode, "output": self.settings.redact(b"".join(chunks).decode("utf-8", errors="replace"))}

    async def stop_all(self, project_id=None):
        for name, service in list(self.services.items()):
            if project_id and service["project_id"] != project_id:
                continue
            await self.kill(service["proc"])
            await asyncio.gather(service["reader"], return_exceptions=True)
            with self.store.conn:
                self.store.conn.execute("DELETE FROM services WHERE id=?", (service["id"],))
            self.services.pop(name, None)

    async def start(self, project_id, run_id, root, coding: CodingResult, emit):
        await self.stop_all()
        urls = {}
        try:
            for spec in coding.services:
                argv, cwd = command_argv(spec, root)
                with socket.socket() as sock:
                    if sock.connect_ex(("127.0.0.1", spec.port)) == 0:
                        raise RuntimeError(f"端口 {spec.port} 已被占用，请更换端口")
                proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=child_env(),
                           stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                item = {"id": uid(), "proc": proc, "logs": [], "spec": spec, "project_id": project_id}
                async def drain(entry):
                    while block := await entry["proc"].stdout.read(4096):
                        entry["logs"].append(block.decode("utf-8", errors="replace"))
                        entry["logs"] = entry["logs"][-200:]
                item["reader"] = asyncio.create_task(drain(item))
                self.services[spec.name] = item
                with self.store.conn:
                    self.store.conn.execute("INSERT INTO services VALUES(?,?,?,?,?,?)", (item["id"], project_id, run_id,
                       proc.pid, psutil.Process(proc.pid).create_time(), spec.model_dump_json()))
                url = f"http://127.0.0.1:{spec.port}"
                ready = False
                async with httpx.AsyncClient(trust_env=False) as client:
                    for _ in range(100):
                        if proc.returncode is not None:
                            break
                        try:
                            response = await client.get(url + spec.health_path, timeout=1)
                            if 200 <= response.status_code < 400:
                                ready = True
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.3)
                if not ready:
                    raise RuntimeError(self.settings.redact(f"{spec.name} 启动失败：" + "".join(item["logs"])[-12000:]))
                urls[spec.name] = url
                emit("service.ready", {"name": spec.name, "url": url})
            preview = next(urls[s.name] for s in coding.services if s.preview)
            return {"urls": urls, "preview_url": preview}
        except BaseException:
            await self.stop_all()
            raise

    def recover_orphans(self):
        for row in self.store.conn.execute("SELECT pid,started FROM services").fetchall():
            try:
                p = psutil.Process(row[0])
                if abs(p.create_time() - row[1]) < .1:
                    for child in p.children(recursive=True):
                        child.kill()
                    p.kill()
            except psutil.Error:
                pass
        with self.store.conn:
            self.store.conn.execute("DELETE FROM services")


class ToolContext:
    def __init__(self, root, run_id, settings, processes, emit):
        self.root, self.run_id = root.resolve(), run_id
        self.settings, self.processes, self.emit = settings, processes, emit
        self.artifacts = settings.data / "runs" / run_id
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.evidence = {}
        self.review_hash = ""
        self.urls = {}
        self.checks = []
        self.pw = self.browser = self.page = None
        self.console_errors = []
        self.page_errors = []

    def record(self, kind, details, *, passed=True, assertion=False):
        data = {"id": uid(), "kind": kind, "passed": passed, "assertion": assertion,
                "source_hash": self.review_hash, "details": details}
        data = json.loads(self.settings.redact(json.dumps(data, ensure_ascii=False)))
        self.evidence[data["id"]] = data
        with (self.artifacts / "evidence.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.emit("tool.result", {"tool": kind, "evidence_id": data["id"], "passed": passed,
                                  "detail": json.dumps(details, ensure_ascii=False)[:700]})
        return data

    def check_url(self, url):
        p = urlparse(url)
        if p.scheme != "http" or p.hostname != "127.0.0.1" or p.username or p.password:
            raise ValueError("只能访问本项目登记的本地服务")
        if f"http://127.0.0.1:{p.port}" not in self.urls.values():
            raise ValueError("URL 不属于本次应用服务")

    async def get_page(self):
        if self.page is None:
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=True)
            context = await self.browser.new_context()
            self.page = await context.new_page()
            self.page.set_default_timeout(10000)
            self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
            self.page.on("console", lambda message: self.console_errors.append(message.text) if message.type == "error" else None)
        return self.page

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        self.pw = self.browser = self.page = None

    def tools(self, role):
        ctx = self

        @tool
        async def list_files() -> list[str]:
            """列出当前项目文件，自动排除依赖与构建目录。"""
            return files(ctx.root)

        @tool
        async def read_file(path: str) -> dict:
            """读取项目内文本文件，路径相对于项目根目录。"""
            p = safe_path(ctx.root, path)
            if p.stat().st_size > 500000:
                raise ValueError("文件超过500KB读取限制")
            text = p.read_text(encoding="utf-8")
            e = ctx.record("code", {"path": path, "content": text[:18000]})
            return e

        @tool
        async def write_file(path: str, content: str) -> dict:
            """创建或覆盖项目文本文件；所有路径必须在当前项目内。"""
            p = safe_path(ctx.root, path)
            if len(content.encode()) > 1_000_000:
                raise ValueError("单文件超过1MB")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            ctx.emit("file.written", {"path": path, "bytes": len(content.encode())})
            return {"written": path}

        @tool
        async def run_command(program: str, arguments: list[str], cwd: str = ".") -> dict:
            """执行短期项目命令，程序和参数分开传入；不要在此启动长期服务。"""
            result = await ctx.processes.command(CommandSpec(program=program, args=arguments, cwd=cwd), ctx.root)
            return ctx.record("command", {"program": program, "args": arguments, **result}, passed=result["exit_code"] == 0)

        @tool
        async def available_port() -> int:
            """分配一个当前可用的本地端口，供启动方案使用。"""
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        @tool
        async def run_check(check_id: str) -> dict:
            """运行编码阶段登记的检查任务，返回实际退出码和输出证据。"""
            spec = next((c for c in ctx.checks if c.id == check_id), None)
            if spec is None:
                raise ValueError("检查任务未登记")
            result = await ctx.processes.command(spec, ctx.root)
            return ctx.record("test", {"check_id": check_id, **result}, passed=result["exit_code"] == 0, assertion=True)

        @tool
        async def http_check(url: str, method: str = "GET", body: dict | None = None,
                             expected_status: int = 200, expected_json: dict | None = None) -> dict:
            """请求登记的本地接口，核对状态码及可选 JSON 字段子集，保留真实响应。"""
            ctx.check_url(url)
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.request(method, url, json=body, timeout=15)
            try:
                value = response.json()
            except ValueError:
                value = response.text[:8000]
            passed = response.status_code == expected_status
            if expected_json is not None:
                passed = passed and isinstance(value, dict) and all(value.get(k) == v for k, v in expected_json.items())
            return ctx.record("api", {"method": method.upper(), "url": url, "request": body, "status": response.status_code,
                  "response": value, "expected_json": expected_json}, passed=passed, assertion=True)

        @tool
        async def browser_open(url: str) -> dict:
            """实际打开本项目页面，返回正文和可交互元素。"""
            ctx.check_url(url)
            page = await ctx.get_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            return await browser_snapshot.ainvoke({})

        @tool
        async def browser_snapshot() -> dict:
            """查看当前页面正文与按钮、输入框等元素，操作前先查看。"""
            page = await ctx.get_page()
            return {"url": page.url, "text": (await page.locator("body").inner_text())[:14000],
                    "elements": await page.locator("input,button,a,select,textarea").evaluate_all(
                        "els=>els.slice(0,80).map(e=>({tag:e.tagName,id:e.id,text:e.innerText,name:e.name,placeholder:e.placeholder,type:e.type}))")}

        @tool
        async def browser_diagnostics() -> dict:
            """检查本轮浏览器实际采集的JS异常和console.error；空列表才通过，不以源码阅读替代。"""
            return ctx.record("browser", {"console_errors": ctx.console_errors, "page_errors": ctx.page_errors},
                              passed=not ctx.console_errors and not ctx.page_errors, assertion=True)

        @tool
        async def browser_action(action: str, selector: str = "", value: str = "") -> dict:
            """实际操作页面。action 可为 click、fill、select、press、reload；selector 使用 CSS 或 text=。"""
            page = await ctx.get_page()
            if action == "reload":
                await page.reload(wait_until="networkidle")
            else:
                ctx.check_url(page.url)
                loc = page.locator(selector)
                if action == "click":
                    await loc.click()
                elif action == "fill":
                    await loc.fill(value)
                elif action == "select":
                    await loc.select_option(value)
                elif action == "press":
                    await loc.press(value)
                else:
                    raise ValueError("不支持的页面操作")
            ctx.record("browser_action", {"action": action, "selector": selector, "value": value})
            return await browser_snapshot.ainvoke({})

        @tool
        async def browser_assert(selector: str, mode: str, expected: str, after_reload: bool = False) -> dict:
            """核对真实页面；mode为 text包含、value等于、count等于、visible。after_reload用于验证刷新后数据保留。"""
            page = await ctx.get_page()
            ctx.check_url(page.url)
            if after_reload:
                await page.reload(wait_until="networkidle")
            loc = page.locator(selector)
            if mode == "text":
                observed = await loc.inner_text()
                passed = expected in observed
            elif mode == "value":
                observed = await loc.input_value()
                passed = observed == expected
            elif mode == "count":
                observed = str(await loc.count())
                passed = observed == expected
            elif mode == "visible":
                observed = str(await loc.is_visible()).lower()
                passed = observed == expected.lower()
            else:
                raise ValueError("不支持的断言类型")
            screenshot = uid() + ".png"
            await page.screenshot(path=str(ctx.artifacts / screenshot), full_page=True)
            return ctx.record("data" if after_reload else "browser", {"selector": selector, "mode": mode,
                    "expected": expected, "observed": str(observed)[:5000], "screenshot": screenshot}, passed=passed, assertion=True)

        if role == "planner":
            selected = [list_files, read_file]
        elif role == "coder":
            selected = [list_files, read_file, write_file, run_command, available_port]
        else:
            selected = [list_files, read_file, run_check, http_check, browser_open, browser_snapshot, browser_diagnostics, browser_action, browser_assert]
        for selected_tool in selected:
            original = selected_tool.coroutine
            def wrap(fn, name):
                @functools.wraps(fn)
                async def guarded(*args, **kwargs):
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:
                        return ctx.record("tool_error", {"tool": name, "error": ctx.settings.redact(str(exc))}, passed=False)
                return guarded
            selected_tool.coroutine = wrap(original, selected_tool.name)
        return selected
