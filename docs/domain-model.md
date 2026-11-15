# 领域模型：只读 ActivityPub 公共资料采集

本层在原始归档之上解释合作馆的公开活动，只读、不发送任何联邦消息，也不实现社交功能。三类身份分开保存：

| 身份 | 表 | 说明 |
| --- | --- | --- |
| 活动身份 | `activity_events` | 每次观察到的活动及处理结论，只追加，是全部状态的事件依据 |
| 对象身份 | `objects` | 原作者发布的对象及当前状态（`current` / `deleted`） |
| 展示关系 | `displays` / `withdrawals` | 某账号经 Announce 对对象建立的展示关系，以及已确认的撤销 |

## 信任与公开范围

- 信任只来自本地配置（`fixtures/partners.json` 或 `--partners` 指定）：信封 `source` 必须是已配置合作方，活动 `actor` 必须等于该合作方配置的 actor。网关转来他方账号的活动会因 `actor_mismatch` 被拒绝。
- 只采明确投递到 `https://www.w3.org/ns/activitystreams#Public` 的 Create / Update / Announce；只有私有收件对象的活动记为 `non_public`，不采集。Undo / Delete 是控制活动，不新增内容，按归属规则处理。
- 本层不发起任何网络请求：不抓取对象 URL，不抓取远端 JSON-LD `@context`。缺少对象详情时关系保持待补（`pending`），引用 URL 不会被当成正文。

## 各类活动的处理

- **Create**：对象必须内嵌且归属（`attributedTo`）就是 actor 本人，否则拒绝。对象已存在时幂等忽略。
- **Update**：携带完整对象时按归属核对后更新；对象尚不存在但归属可核对时可先建立（乱序到达）。只有引用没有详情时记为 `deferred`，等待后续完整活动补全。
- **Announce**：建立账号 → 对象的展示关系。对象未知时关系先登记，视图显示 `pending`，详情到达后自动可展示。Announce 内嵌的对象详情不被采纳（非原作者所发，不足为凭）。
- **Undo(Announce)**：只取消那一次转发，原对象不受影响。只能撤回自己的转发（`not_announcer` 拒绝）。Undo 先于 Announce 到达时先登记撤销，晚到的转发关系登记即为 `withdrawn`。
- **Delete**：必须同时满足来源可信与对象归属——对象已知且 `attributedTo` 就是 actor 本人才生效。其他账号转来的删除声明（`not_object_owner`）、无法核对归属的删除（`object_unknown`）都只留证据，不抹除原文。被拒绝的删除不留墓碑：同一活动再次到达时若归属已可核对，仍会生效。

## 最终性规则

- 已确认撤回的展示不恢复：重复活动按活动身份幂等忽略；晚到的旧引用（撤销之后才到达的 Announce）登记即为已撤回。
- 已确认删除的对象保持 `deleted`：晚到的旧 Create / Update 记为 `object_deleted` 拒绝。原作者删除之后到达的新引用关系可以登记，但视图标记为不可展示。
- 源站短时失联（非 200 响应）只在 `ingest_log` 记录，不改变任何领域状态；解码失败同样只记录并继续前进。

## 资料员视图

`DomainStore.view()` 按馆方（配置的合作方）列出：

- `objects`：该馆账号拥有的对象，`displayable` 表示当前可展示；
- `forwards`：该馆账号建立的转发关系，`object_status` 为 `current` / `pending` / `deleted`，只有关系 `active` 且对象 `current` 才 `displayable`；
- 每项的 `evidence` 是 `activity_events` 的序号，可经 `DomainStore.events()` 查到原始活动、来源信封与处理结论。

领域状态由事件重放得出，与原始归档分离演进；归档库仍是只追加的事实来源。
