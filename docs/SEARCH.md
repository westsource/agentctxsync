# 全局搜索方案(决策记录)

> 状态:已实现 · 决策日期:2026-08-27 · 版本:v2026.08.27.4+

## 1. 需求

Web UI 提供跨工作空间的全局搜索:一次查询同时命中**会话标题/ID** 与 **消息内容**,结果可定位跳转到对应会话。

## 2. 现状与差距

| 已有 | 缺失 |
|---|---|
| 会话级搜索:`s.title ILIKE OR s.id ILIKE`(workspace.py `q` 参数,all-sessions 页) | 消息内容全文搜索 |
| 单工作空间会话列表筛选(agent/profile/时间/分页) | 跨工作空间统一搜索入口(顶部全局可见) |
| — | 结果定位跳转(命中消息 → 会话) |

## 3. 范围与权限(租户隔离,决策 2026-08-27)

- 搜索范围 = **当前用户拥有的全部工作空间**(`sessions JOIN workspaces ON w.user_id = 当前用户`)
- **admin 无特权**:与 README 承诺一致("admins never read anyone's sessions");会话内容相关查询一律按 `user_id` 过滤
- 例外(保留现状):dashboard 设备列表为同步基础设施元数据(device_id/同步时间/计数),admin 全域可见,不含会话内容
- 排除已隐藏/归档会话与隐藏消息(`COALESCE(hidden,0)=0 AND COALESCE(archived,0)=0`)
- **排除 tool 消息**(内容含二进制/API 噪音),只搜 user/assistant/system

## 4. 技术选型

**PG `pg_trgm` GIN 索引 + `ILIKE`**(已实测定 236 服务器扩展可用,zhparser/pg_jieba 不可用)

- 中文 `ILIKE '%词%'` 天然支持,不依赖分词器;trigram 索引把全表扫描变为索引扫描
- 数据规模(2026-08):~5 万消息 / 115MB,两个 GIN 索引约 +30-50MB,查询毫秒级
- 无新组件、无部署负担(DDL 幂等,`init_db` 执行)

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
    ON messages USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sessions_title_trgm
    ON sessions  USING gin (title  gin_trgm_ops);
```

### 备选(数据量增长后)

- >50 万消息:自建 PG 镜像加 `zhparser`(tsvector + GIN 中文分词)
- 复杂相关性/高亮:Meilisearch(单二进制,自带中文分词)

## 5. 实现

### 数据层
- `db.py init_db` 追加上述 DDL(幂等)

### 后端(`server/search.py`,新模块)
- 路由:`GET /web/search?q=&page=`(页面)
- 两条查询并行:
  - **会话命中**:`sessions JOIN workspaces WHERE w.user_id=… AND (title ILIKE %q% OR id ILIKE %q%) AND 非隐藏/归档`,ORDER BY 最后消息时间倒序
  - **消息命中**:`messages JOIN sessions JOIN workspaces WHERE w.user_id=… AND content ILIKE %q% AND role <> 'tool' AND 非隐藏`,ORDER BY timestamp 倒序
- 每类 20 条/页;`q` 转义 `%`/`_`/`\`
- 权限:一律 `w.user_id = 当前用户`(admin 同,无特权分支)
- `main.py` 注册 `search` 模块

### 前端
- `base.html` 侧边栏顶部新增搜索框(GET → `/web/search`),全局可见
- `templates/search.html`:会话命中(复用 all-sessions 卡片样式)+ 消息命中(会话标题、角色标签、内容摘要、时间),点击跳 `/web/workspace/<ws>/session/<id>`;关键词 `<mark>` 高亮(JS)
- `translations.py`:zh/en 键(`nav_search`、`search_placeholder`、`search_sessions`、`search_messages`、`search_empty` 等)

## 6. 验收

1. 普通用户:搜到自己 workspace 的会话/消息;**搜不到他人 workspace 内容**
2. admin:同样只搜自己 workspace(无全库特权)
3. 转义:`%`/`_` 作为字面量;空 q 返回空结果页
4. tool 消息内容不命中
5. 分页正常,点击跳转正确
6. 部署 236 后 DDL 幂等,服务重启正常

## 7. 边界与后续

- 只读查询,不影响同步/写入路径
- 消息定位(**已实现**):消息命中链接携带 `?focus=<mid>`,会话页消息容器带 `data-mid`,加载后滚动居中 + 高亮;目标消息跨分页时服务端自动跳至所在页
- 数据量大后按第 4 节备选升级
