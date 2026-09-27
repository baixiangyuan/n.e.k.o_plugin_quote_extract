# 语录抽取 (quote_extract)

一个 **N.E.K.O.** 插件：导入**你自己的**语录库，随机抽取，也可以让 AI 伙伴在对话中主动引用。

> **本插件不内置任何语录。** 所有内容都来自你自己配置的外部来源：
> **Cloudflare R2 / Cloudflare D1 / WebDAV / 任意 HTTP 直链 / 本地文件**。

## 功能

- 🎲 **随机抽取**：支持标签筛选；默认开启「近期抽过的不重复」
- 📥 **六种导入源**：
  - Cloudflare R2：公开直链（推荐）/ Cloudflare API / S3 兼容签名三种方式任选
  - Cloudflare D1：直接执行 SQL 查询
  - WebDAV：坚果云、Alist、Nextcloud 等网盘上的语录文件
  - HTTP 直链：GitHub Raw、任意对象存储公开链接
  - 本地文件
- 🔁 **定时自动同步**（可选），导入按内容去重、字段增量更新
- ✍️ **手动添加 / 删除**、📊 **统计**
- 🤖 **LLM 工具 `draw_quote`**：AI 伙伴在对话中想引用语录时自动调用（对应表情包插件「AI 智能发送」的思路）

零第三方运行时依赖（纯 Python 标准库），无需 vendor/。

## 安装

- **插件市场**：审核通过后在 N.E.K.O 插件市场搜索「语录抽取」。
- **开发模式**：N.E.K.O 插件管理器 → 「开发模式」→ 「加载未打包插件」→ 选择本目录（含 `plugin.toml` 的目录）。

## 快速开始（三步）

1. **配置导入源**：打开插件的运行配置（用户数据目录 `config/plugin.toml`），设置 `source` 与对应凭据；
2. 在插件详情「入口点」中触发 **导入语录**；
3. 触发 **抽语录**。完成。

### 来源配置示例

R2 公开直链（最简单，在 R2 控制台开启 r2.dev 公开访问后）：

```toml
[import]
source = "r2"
r2_public_url = "https://pub-xxxxxxxx.r2.dev/quotes.json"
```

Cloudflare D1（Token 权限：Account / D1: Edit）：

```toml
[import]
source = "d1"
d1_account_id = "你的账户 ID"
d1_database_id = "数据库 ID"
d1_api_token = "你的 API Token"
d1_sql = "SELECT text, author, source, tag FROM quotes"
```

WebDAV（坚果云请使用「应用密码」）：

```toml
[import]
source = "webdav"
webdav_url = "https://dav.jianguoyun.com/dav/"
webdav_path = "/quotes/quotes.json"
webdav_username = "你的账号"
webdav_password = "应用密码"
```

全部字段说明见 [`config.example.toml`](config.example.toml)。

## 语录文件格式

**JSON**（UTF-8）——以下形式都支持：

```json
["语录一", "语录二"]
[{"text": "语录", "author": "作者", "source": "出处", "tag": "励志"}]
{"quotes": [ ... ]}
```

**纯文本** ——一行一条；空行分段；`语录 —— 作者` 自动识别作者；`#` 开头的行是注释：

```text
# 我的语录库
路虽远行则将至，事虽难做则必成 —— 荀子

愿你出走半生，归来仍是少年。
佚名
```

## 入口一览

| ID | 名称 | 说明 |
| --- | --- | --- |
| `draw` | 抽语录 | 随机一条，可选 `tag` 筛选 |
| `import_quotes` | 导入语录 | 从配置来源拉取并合并；可临时传 `source` 覆盖 |
| `add_quote` | 添加语录 | 手动添加（text / author / source / tag） |
| `remove_quote` | 删除语录 | 按 `quote_id` 或完整原文删除 |
| `stats` | 语录统计 | 总数、标签分布、上次同步时间 |

另有 LLM 工具 `draw_quote`（对话期由 AI 调用）与定时任务 `auto_sync`（配置 `auto_sync = true` 后每小时按 `sync_interval_minutes` 同步）。

## 隐私说明

本插件**不读取**宿主的会话总线（`conversations` / `frames`）；配置中的凭据仅用于访问你自己指定的来源，语录数据只保存在本机插件数据目录。

## 开发与测试

```bash
# 单元测试（独立运行，无需 N.E.K.O 宿主）
uv run --with pytest python -m pytest tests -q   # 44 个单元测试

# 代码检查（与 CI / Market 审核一致的 ruff 规则；注意 --isolated 模式下
# 需在插件目录之外运行，例如在其父目录对插件目录执行）
uvx ruff==0.12.4 check --ignore-noqa --isolated --target-version py311 \
  --line-length 120 --select E4,E7,E9,F,I --exclude vendor <插件目录>

# 官方校验 + 打包（在 N.E.K.O 源码根目录）
uv run neko-plugin check quote_extract
uv run neko-plugin check -r quote_extract   # 发布级：含测试与 .neko-plugin 构建
```

## 发布到 N.E.K.O 插件市场

1. 在 GitHub 创建**公开空仓库**，名称必须是 `n.e.k.o_plugin_quote_extract`（命名规则 `n.e.k.o_plugin_<插件ID>`，不要让 GitHub 自动初始化 README/License）；
2. 推送本仓库并确认 Actions 的 **Verify N.E.K.O Plugin** 通过：

   ```bash
   git remote add origin https://github.com/<你的用户名>/n.e.k.o_plugin_quote_extract.git
   git push -u origin main
   ```

3. 把本目录放入 [N.E.K.O 源码](https://github.com/Project-N-E-K-O/N.E.K.O) 的 `plugin/plugins/quote_extract`，在 N.E.K.O 根目录执行：

   ```bash
   uv sync
   uv run neko-plugin check quote_extract
   uv run neko-plugin setup-repo quote_extract --upgrade-github-actions  # 写入官方标准 CI（发布要求标准 release.yml）
   ```

   提交并推送 workflow 变更；
4. 打开 [市场投稿页](https://market.project-neko.cn/#/upload) 登录 → 填写仓库地址 → 「读取仓库信息」→ 选分区和 1–5 个标签 → 「提交审核申请」；
5. 审核通过后，在 N.E.K.O 根目录执行 `uv run neko-plugin publish quote_extract`：它会创建 `v0.1.0` 标签、生成 GitHub Release（`.neko-plugin` 安装包 + 校验报告 + 发布证据）并通知市场上架 stable；
6. 后续版本：修改 `plugin.toml` 的 `version` → `check` → commit/push → `publish`（版本号不可复用）。

## License

MIT
