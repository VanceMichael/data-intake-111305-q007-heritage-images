# 只读 ActivityPub 公共资料采集

本层在原始接收（`archive.py`、`client.py`、`decoding.py`）之上做业务解释，
不替代原始归档：每条 HTTP 响应仍按摘要去重、追加保存；对象级目录只消费已收下的
来源记录，自身不发起任何网络请求。全部演练使用 `*.invalid` 合成账号，不发送联邦
消息，也不实现社交平台的互动能力。

## 三层身份

| 身份 | 存储位置 | 说明 |
| --- | --- | --- |
| 活动身份 | `activities.activity_id` | Create/Update/Delete/Announce/Undo 各自身份，按 id 去重 |
| 对象身份 | `objects.object_id` | 原作者对象只有一份当前投影，转发不产生新对象 |
| 展示关系 | `shares`（announcer, object） | 某账号对某对象的一次转发，可被该账号的 Undo 单独撤回 |

`journal` 是只追加的事件台账：接收、挂起、拒绝、去重都落一条记录并附原因，
被拒绝的活动不写入 `activities`，但事件依据仍在台账中可查。

## 合作方与信任（`partners.py`）

* 合作源（信封 `source`）与 actor、域的对应关系只来自本地配置 `partners.json`，
  不凭活动自报的 actor 建立信任；未配置 source 或 actor 一律拒收。
* 标识必须是绝对 `http(s)` URL；`bto`/`bcc` 盲收件、或定向到非公开集合的
  to/cc 视为私有内容，不采集。
* 不解析、不抓取 `@context` 中的远端 JSON-LD 文档；上下文 URL 只随原始载荷留存。
* 未配置合作域的对象只保留为隐藏占位（`configured_domain=0`），不进入馆方展示视图，
  也不会触发任何外联请求。

## 活动语义（`catalog.py`）

* **Create**：内嵌对象且 `attributedTo` 与 actor 一致才落详情；只有引用 URL 时登记
  待补占位（`details_complete=0`），绝不把 URL 当正文。已删（墓碑）对象不会被晚到的
  旧 Create 复活。
* **Update**：只有对象属主可改稿，归属冲突直接拒绝；缺内嵌详情时挂起待补，允许
  Update 与 Create/Announce 乱序到达。
* **Delete**：双重核对——actor 必须是该 source 的注册 actor，且是对象已知属主；
  归属未知时要求 actor 与对象同域，否则保留原文并拒绝。其他合作方转来的删除声明
  不足以抹掉原作者对象。核对通过后对象转为墓碑、清空正文、连带停展全部转发关系，
  这些停展记录归入“删除隐藏”，不等同于转发者主动 Undo。
* **Announce**：只建立（announcer, object）展示关系，不复制对象。对象详情缺失则
  关系挂起待补；对象已是墓碑时转发直接隐藏；未配置域对象的转发不进展示视图。
* **Undo**：只取消转发者本人的那一次 Announce，原作者对象保持原样。目标活动尚未
  到达时进入 `pending_undo`，晚到的 Announce 建立关系时立即兑现撤回；跨 actor 的
  Undo 与非 Announce 目标均拒绝。

## 乱序、失联与重复

* 活动按 `activity_id` 去重，重复投递（含源站恢复后的重发、晚到旧引用）记为
  `duplicate`，不改变任何状态，因此不能恢复已确认的撤展或墓碑。
* 源站短时失联属于采集层问题：失败响应不产生信封，目录状态保持；恢复后同一份采集
  仍按活动 id 去重处理。
* 所有挂起项（待补对象、待兑现 Undo）都可在展示视图中显式看到，不会静默冒充完整数据。

## 命令

```sh
# 合成账号的纯本地演练（无网络、无联邦消息）
python3 -m heritage_images federation-demo

# 把一份已归档的来源 JSON 读入对象级目录
python3 -m heritage_images catalog --partners fixtures/partners.json \
    --catalog .runtime/catalog.sqlite3 --feed feed.json

# 脱离名录重开目录，查看馆方当前可展示口径
python3 -m heritage_images review --catalog .runtime/catalog.sqlite3
```

`display_view()` 返回当前可展示对象、有效/撤回/删除隐藏的转发、待补对象、墓碑对象
与台账计数；原始活动载荷在 `activities.payload`，原始 HTTP 响应在 `Archive` 中，
两层证据相互独立、均可追溯。
