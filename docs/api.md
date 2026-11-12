# 工作台 API

服务仅绑定本机 `127.0.0.1`。请求和响应使用 UTF-8 JSON，修改操作使用 `Content-Type: application/json`。生成任务异步返回作业 ID，进度和错误从项目状态查询。

## 页面与查询

| 路由 | 用途 |
| --- | --- |
| `/`、`/canvas` | 导演画布 |
| `/studio` | 传统制作工作台 |
| `GET /api/state` | 工作台状态、人物、场景、剧集、作业、导出与统计 |
| `GET /api/source?episode_id=...` | 工作台分集原文 |
| `GET /api/canvas/projects` | 画布项目列表 |
| `GET /api/canvas/project?project_id=...` | 画布项目详情 |
| `GET /api/canvas/graph?project_id=...&episode_id=...` | 画布节点、连线与排版 |
| `/media/...`、`/canvas-media/...` | 当前项目范围内的素材访问 |

## 程序更新

| 接口 | 用途 |
| --- | --- |
| `GET /api/system/update` | 本机正在运行的版本、最近检查结果、更新阶段与消息 |
| `POST /api/system/update/check`，JSON `{}` | 检查固定公共仓库的 `main`；只获取代码对象，不替换运行代码 |
| `POST /api/system/update/apply`，JSON `{"target_sha":"检查返回的完整 SHA"}` | 安装本次检查的版本、重启原服务；不接受自定义仓库或命令 |

检查返回 `running_sha`、`target_sha`、`available`、`can_update`、`commits`、`message` 等字段。安装前请确认 `can_update`；检查失败的原因也由 `message` 返回。版本不匹配、有源码修改、有任务或其他服务时，安装返回 HTTP 409。安装被接受后返回 `operation` 和 `phase`，页面轮询 GET 接口，跨越重启重连；只有阶段为 `complete` 且运行版本匹配目标时才刷新页面。

接口仅在本机可用，沿用 Host/Origin 校验。更新期间禁止其他修改请求。实现见 `backend/updater.py`，页面共享组件见 `frontend/shared/`。部署及恢复说明见 [初始化与启动指南](初始化与启动指南.md#8-以后怎么更新)。

## 工作台修改接口

| POST 接口 | 用途 |
| --- | --- |
| `/api/plan`、`/api/review` | 分镜规划、连续性复核 |
| `/api/character`、`/api/look`、`/api/location` | 添加人物身份、服装或场景 |
| `/api/prompt`、`/api/shot`、`/api/shot-look` | 修改 Prompt、镜头或镜头人物形象 |
| `/api/insert-reaction` | 在保留原文顺序和对白的前提下插入反应镜头 |
| `/api/image`、`/api/video` | 提交图片或视频生成 |
| `/api/upload` | 上传参考图片 |
| `/api/select`、`/api/select-video` | 采用素材版本 |
| `/api/review-video` | 保存视频复核结果 |
| `/api/render` | 导出分集或合辑 |
| `/api/run`、`/api/pause` | 启动或暂停制作调度 |

画布接口位于 `/api/canvas/`，包括新建项目、保存、导演规划、排版、预览生成参数、生成、上传与版本采用。具体参数以 `backend/api/server.py` 的路由及 `backend/canvas/studio.py` 的方法为准。

素材历史保留，采用版本通过指针记录。修改人物、场景或镜头后，依赖它们的下游素材可能被标记过期；已锁定的导演选择和有效的历史素材受保护。程序不会通过公开静态目录提供 `.env` 或任意本机文件。
