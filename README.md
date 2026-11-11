# 映序 · AI 视频创作工作台

本地运行的 AI 创作工具，提供画布、剧本导入、人物与场景管理、图片与视频生成、分镜规划、质量检查和成片导出。

**目录用途和修改入口请看 [项目目录说明.md](项目目录说明.md)。**

仓库只包含程序源码、通用规则、虚构测试样例和使用文档。真实剧本、分集文本、制作项目、素材、密钥和本地参考资料保存在本机。

## 初始化与启动

需要 Python 3.10+：

```bash
python3 scripts/setup_project.py
```

初始化会安装依赖、创建 `.env` 和空项目，不导入任何真实剧本。已有配置、项目和外盘链接会保留。macOS 可双击 `初始化项目.command`。

在本机 `.env` 中填写 `DASHSCOPE_API_KEY`，然后启动画布：

```bash
.venv/bin/python -m backend serve --canvas-only --port 8766
```

打开 `http://127.0.0.1:8766`，新建空白画布，或在页面中导入自己的本地剧本。macOS 也可双击 `启动短剧画布.command`。

传统工作台通过以下命令启动，访问 `http://127.0.0.1:8765/studio`：

```bash
.venv/bin/python -m backend serve --port 8765
```

## 开发检查

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/dev/check_repository.py
```

测试使用虚构样例、临时目录和模拟接口，不调用真实生成服务。GitHub Actions 会执行初始化、仓库检查、测试和 JavaScript 语法检查。

## 上传 GitHub

在 GitHub 创建空仓库，然后连接并推送本地 `main`：

```bash
git remote add origin https://github.com/YOUR_NAME/YOUR_REPOSITORY.git
git push -u origin main
```

将示例地址替换成自己的仓库地址。这个项目已有 README 和忽略配置，远程新仓库无需自动添加这些文件。
