import argparse
import json
import os
from core.config import load_env
from core.store import Store, DEFAULT_PROJECT


def main():
    load_env()
    parser = argparse.ArgumentParser(description="按剧集管理的AI短剧制作工作流")
    parser.add_argument("command", choices=["init", "serve", "run", "status", "plan", "render"])
    parser.add_argument("--project", default=os.environ.get("AIVIDEO_PROJECT_DIR", str(DEFAULT_PROJECT)))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--episode")
    parser.add_argument("--seed", help="显式导入本地项目 JSON，仅用于 init")
    parser.add_argument("--canvas-only", action="store_true", help="仅启动画布，不接管传统工作台任务")
    parser.add_argument("--canvas-root", help="画布项目保存目录")
    args = parser.parse_args()
    if args.command == "serve":
        from backend.api.server import serve
        serve(args.project, args.port, canvas_only=args.canvas_only, canvas_root=args.canvas_root)
        return
    store = Store(args.project)
    if args.seed and args.command != "init":
        parser.error("--seed 仅用于 init")
    store.initialize(args.seed)
    if args.command == "init":
        print(store.root)
    elif args.command == "status":
        state = store.snapshot()
        print(json.dumps({"project": state["project"], "stats": state["stats"], "jobs": state["jobs"][-5:]}, ensure_ascii=False, indent=2))
    else:
        from backend.workflow.pipeline import Pipeline
        pipeline = Pipeline(store)
        if args.command == "plan":
            pipeline.plan(args.episode)
        elif args.command == "render":
            pipeline.render(args.episode)
        else:
            pipeline.run()


if __name__ == "__main__":
    main()
