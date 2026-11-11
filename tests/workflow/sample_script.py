"""Small invented script used only for offline parser tests."""


def script_fixture():
    return {"episodes": [
        {"id": "EP001", "source_text": "### 1-1 日 内 图书馆\n△窗边的书页被风吹动。\n小林：书已经找到了。\n小夏：那我们明天再来。\n【字幕：次日清晨】"},
        {"id": "EP002", "source_text": "### 2-1 日 外 公园\n△两人在长椅旁停下。\n小林：" + "每一页都要仔细读完，" * 8 + "\n小夏：我会记住的。\n【字幕】傍晚\n△天色渐暗。"},
    ]}
