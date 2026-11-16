# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `source`、`observation`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

`fixtures/federation_cases.json` 逐条保存下列交错情况的合成信封与可观察口径，
由 `tests/test_federation.py` 按用例独立核对；对象级解释规则见
`docs/federation-collection.md`。

- `undo_announce`：取消转发且原作者内容仍存在；只撤展示关系，不删原对象。
- `untrusted_delete`：另一账号转来删除声明；核对来源与对象归属，不直接抹除。
- `reference_before_object`：引用先于对象详情；待补关系不冒充完整正文。
- `out_of_order_update`：Update 与引用乱序，非属主改稿被拒。
- `verified_delete_and_tombstone`：属主删除后晚到旧稿、旧转发均不能复活展示。
- `private_addressing_rejected`：定向与盲收件不采集。
- `unconfigured_source_and_actor`：未配置合作方、冒名 actor 不建立信任。
- `remote_context_and_foreign_domain`：远端上下文不抓取，未配置域对象不展示。
- `duplicates_do_not_revive`：重复活动与晚到旧引用不能恢复已确认撤回。
- `delete_with_unknown_ownership_retained`：归属未知时删除声明不足为凭。
- `undo_before_announce_and_cross_actor_undo`：Undo 早到挂起、跨账号撤销被拒。

## 领域接入契约

activity 内层采用 W3C ActivityPub/ActivityStreams 的公开活动子集，外层 source、observation 是合作网关信封。合作源与actor的对应关系由本地 `fixtures/partners.json` 确定，不凭活动自报身份建立信任。只读采集，不发送任何活动，不执行远端 `@context` 或抓取未配置域；对象详情缺失时只登记待补占位，绝不把引用 URL 当作正文。
