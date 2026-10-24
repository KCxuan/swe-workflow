"""Run small, real-model examples through the public API. Opt-in, consumes model tokens.

The automatic confirmation here belongs only to this acceptance harness;
the workbench itself always waits for the user's decision.
"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import time
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import Settings
CASES = {
    "single": ("单页计数器", "做一个单页计数器，用原生HTML/CSS/JS和Python静态服务。页面有初始值0、加一按钮和重置按钮；点击加一后显示1，再点击重置回到0。不要额外功能。验收只需这两个交互项。"),
    "multi": ("多页面学习站", "做一个原生HTML/CSS/JS多页面学习站，包含index.html首页和about.html关于页，通过普通链接互相跳转。首页有一个输入框和问候按钮，输入小明后显示你好，小明；关于页显示学习站简介。用Python静态服务。验收两项：页面来回跳转；输入姓名后显示问候。不需要存储或额外功能。"),
    "database": ("SQLite留言板", "做一个非常小的留言板，前端原生HTML/CSS/JS，后端用Python标准库http.server和sqlite3，不安装第三方依赖。页面输入留言并提交，显示已有留言列表。POST /api/messages 接收text并返回创建的含id与text的对象；GET /api/messages 返回{messages:[{id,text}]}；GET /api/messages/{id}返回{id,text}。SQLite保存数据。验收三项：浏览器输入提交后出现新留言；POST创建后GET按id返回相同text；刷新网页后新留言仍在。请保持实现简单，不要增加删除或登录。"),
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", choices=list(CASES), default=list(CASES))
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--modify-single", action="store_true", help="Then modify the generated counter in the same directory")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Settings.load().data / "acceptance" / stamp
    folder.mkdir(parents=True)
    results = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=30, trust_env=False) as client:
        async def call(method, path, **kwargs):
            r = await client.request(method, "/api" + path, **kwargs)
            r.raise_for_status()
            return r.json()
        jobs = [(key, CASES[key][1], "new", folder / key) for key in args.cases]
        if args.modify_single:
            jobs.append(("single-modified", "保留计数器加一和重置，增加减一按钮，允许负数。实际验证加一到1、重置到0、减一到-1。", "modify", folder / "single"))
        for key, request, mode, project_dir in jobs:
            project_dir.mkdir(exist_ok=True)
            run = await call("POST", "/runs", json={"path": str(project_dir), "mode": mode, "request": request})
            started = time.monotonic()
            previous = None
            confirmed = False
            while time.monotonic() - started < args.timeout:
                run = await call("GET", f"/runs/{run['id']}")
                phase = (run["status"], run["phase"], run["state"].get("repair_count", 0))
                if phase != previous:
                    print(key, phase, flush=True)
                    previous = phase
                if run["status"] == "waiting_confirmation" and not confirmed:
                    (folder / f"{key}-plan.json").write_text(json.dumps(run["state"]["plan"], ensure_ascii=False, indent=2), encoding="utf-8")
                    if mode == "new":
                        assert not any(project_dir.iterdir()), "Code was written before confirmation"
                    await call("POST", f"/runs/{run['id']}/decision", json={"kind": "confirm"})
                    confirmed = True
                if run["status"] in {"completed", "failed", "stopped", "ended"}:
                    break
                await asyncio.sleep(2)
            else:
                await call("POST", f"/runs/{run['id']}/stop")
                run = await call("GET", f"/runs/{run['id']}")
            (folder / f"{key}-run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {"case": key, "run_id": run["id"], "path": str(project_dir),
                      "status": run["status"], "seconds": round(time.monotonic() - started),
                      "repairs": run["state"].get("repair_count"), "error": run["state"].get("error"),
                      "results": run["state"].get("review_report", {}).get("results", [])}
            results.append(result)
            (folder / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print("RESULT", json.dumps({k:v for k,v in result.items() if k!="results"}, ensure_ascii=False), "checks", [(r["criterion_id"],r["status"]) for r in result["results"]], flush=True)
    print("SAVED", folder, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
