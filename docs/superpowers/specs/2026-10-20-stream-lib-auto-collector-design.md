# stream-lib-cat-auto.json 自动搜集定时任务 — 设计文档

- 日期：2026-10-20
- 状态：已确认（用户批准）

## 背景与目标

STREAM-LIB-REPO 仓库目前手工维护 `stream-lib-cat.json`（8 条 TVBox 影视线路）。目标：新增一个每日自动运行的定时任务，从 GitHub 自动发现并验证 TVBox JSON 配置（类似 `https://gh-proxy.com/https://raw.githubusercontent.com/xmbjm/svip/refs/heads/main/svip.json`），生成 `stream-lib-cat-auto.json`。

- 手工文件 `stream-lib-cat.json` 保持不动
- 自动文件只包含脚本搜集并通过验证的线路，不合并手工 8 条
- 运行环境：GitHub Actions（云端定时，自动提交回仓库）

## 架构

新增 3 个文件：

| 文件 | 职责 |
|---|---|
| `scripts/collect_stream_lib.py` | 搜集 + 多因素验证 + 生成 JSON。纯 Python 标准库（urllib/json/re/concurrent），无第三方依赖 |
| `scripts/seeds.json` | 种子来源配置：聚合仓库/页面的 raw URL 列表，每条可带固定名称，可随时增删 |
| `.github/workflows/collect-stream-lib.yml` | 每日 UTC 02:17 定时 + `workflow_dispatch` 手动触发；运行脚本，有变化则 commit & push（`permissions: contents: write`，内置 `GITHUB_TOKEN`） |

## 发现渠道

1. **代码搜索（主力）**：GitHub Code Search API（`/search/code`，`GITHUB_TOKEN` 认证），多组 ASCII 关键词查询：
   - `"spider" "sites" extension:json`
   - `"lives" "sites" extension:json`
   - `"spider" "lives" extension:json`
   - `"parses" "sites" extension:json`
   - `tvbox extension:json`
2. **仓库搜索（辅助，支持中文）**：`/search/repositories`，关键词如 `tvbox`、`影视 接口`、`影视配置`；对结果仓库取根目录 `.json` 文件作为候选
3. **种子提取**：抓取 `seeds.json` 中每个种子内容（README/JSON），正则提取候选配置 URL

注：Code Search 仅索引默认分支、约 384KB 以下的文件；超大配置由仓库搜索的根目录探测兜底。

## 多因素匹配与打分

**硬性门槛**（必须全部满足）：

- URL 可访问（GET，10s 超时，8MB 响应上限）
- 内容为 TVBox 配置（实现修正：真实世界的配置常是「宽松 JSON」——字符串内含未转义控制字符、尾部杂讯等，连示例 svip.json 都过不了严格解析。解析降级链：严格 JSON → `strict=False` 宽松 JSON → 文本级 `"marker":` 键扫描）
- 含 ≥2 个内容标记：`sites`、`spider`、`lives`、`parses`、`wallpaper`

**加分排序**（降序）：

| 因子 | 分值 |
|---|---|
| 内容标记数量 | +（0–5） |
| 仓库名/描述含 tvbox、影视、ott、box、配置 | +1 |
| star 数活跃度 | +round(log10(stars+1) × 2)，约 0–8 |
| 仓库超过 1 年未更新 | −3 |

排序后取 Top 50。

## 去重与输出

- 归一化 URL 去重：统一剥离已有 `gh-proxy` 类前缀、小写 host、去除尾部斜杠
- GitHub raw 链接发布时统一加 `https://gh-proxy.com/` 前缀（方便国内 TVBox 设备直连）；验证时直连原始地址
- 命名：种子条目用固定名称；搜索发现的用「仓库名 + 线路」，同仓库多个文件时附加文件名后缀
- 输出 `stream-lib-cat-auto.json`，格式与手工文件一致：`{"urls": [{"url", "name"}]}`，`ensure_ascii=False`，2 空格缩进

## 错误处理

- **防清空**：验证通过数为 0 时保留现有文件不覆盖，日志 warning，退出码 0
- 单个候选失败（超时/非 JSON/缺标记）只跳过，不影响整体
- API 限流：搜索调用之间 sleep，失败重试 1 次；Actions 内用内置 `GITHUB_TOKEN`；本地运行可设 `GH_TOKEN`/`GITHUB_TOKEN`（无 token 时 code search 跳过，repo search 匿名）
- 无变化时不产生空提交

## 测试计划

1. 本机运行脚本，检查 `stream-lib-cat-auto.json` 产出格式与内容合理性
2. 推送后 `workflow_dispatch` 手动触发一次，确认云端运行成功、文件正常更新
3. 确认手工文件 `stream-lib-cat.json` 未被改动

## 非目标

- 不抓取/解析配置内的视频源内容
- 不合并手工列表、不修改 `stream-lib-cat.json`
- 不做多 CDN 前缀配置（固定 `gh-proxy.com` 一个前缀）
