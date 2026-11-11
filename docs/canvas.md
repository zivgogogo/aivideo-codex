# 导演画布

启动画布后打开 `http://127.0.0.1:8766`。

1. 在项目列表新建空白画布，或导入自己的本地剧本。
2. 空白画布可以直接添加图片卡、视频卡。导入剧本后可进行人物提取和导演规划。
3. 点击卡片展开 Prompt、引用、模型、比例、数量、时长等设置。
4. 将图片或视频上传为本地素材，或连接到其他卡片作为生成参考。
5. 生成前查看请求方案；点击生成时才提交对应 AI 任务。生成需要配置有效密钥。
6. 项目自动保存到本地，素材版本和选用记录保留。

## 模块对应

- 页面、样式和交互：`frontend/canvas/`。
- 项目与素材：`backend/canvas/studio.py`、`workspace.py`。
- 人物提取与导演规划：`backend/canvas/director.py`。
- 自动排版：`backend/canvas/layout.py`。
- 保存冲突处理：`backend/canvas/sync.py`、`frontend/canvas/canvas-sync.js`。
- HTTP 接口：`backend/api/server.py`。

所有导入内容、实际项目、生成素材和运行日志保留在本机，不属于源码仓库。
