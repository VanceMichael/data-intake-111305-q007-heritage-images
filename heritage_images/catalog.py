"""只读 ActivityPub 公共资料的对象级采集目录。

三层身份严格分开：

* 活动身份（activity id）：Create/Update/Delete/Announce/Undo 各自一条，按 id 去重；
* 对象身份（object id）：原作者对象只有一份当前投影，转发不产生新对象；
* 展示关系（actor, object）：某个账号对对象的转发关系，Undo 只取消该关系。

所有写入都追加到 ``journal`` 事件台账，保留每个判定的依据。模块不发起任何
HTTP 请求：对象详情缺失时只登记待补占位，绝不把引用 URL 当作正文，也不抓取
远端 JSON-LD 上下文或未配置域。
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .partners import PartnerDirectory, is_private, normalize_identifier, registered_domain

# journal.decision
REJECTED = "rejected"
PENDING = "pending"
APPLIED = "applied"
DUPLICATE = "duplicate"

# 具体原因，便于馆方核对事件依据
REASON_MALFORMED = "malformed_envelope"
REASON_UNCONFIGURED_SOURCE = "unconfigured_source"
REASON_UNTRUSTED_ACTOR = "untrusted_actor"
REASON_PRIVATE = "non_public_addressing"
REASON_BAD_IDENTIFIER = "invalid_identifier"
REASON_TYPE_MISMATCH = "activity_type_unsupported"
REASON_OWNERSHIP = "actor_is_not_object_owner"
REASON_UNDO_ACTOR = "undo_actor_mismatch"
REASON_UNDO_TARGET = "undo_target_not_announce"
REASON_TOMBSTONE = "object_already_tombstoned"
REASON_UPDATE_CONFLICT = "update_conflicts_with_known_attribution"

PENDING_OBJECT = "object_details_missing"
PENDING_UNDO = "undo_waiting_for_announce"


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    source: str | None
    observation: Any
    activity_id: str | None
    activity_type: str | None
    actor: str | None
    decision: str
    reason: str
    detail: str
    recorded_at: str


def _embedded_object(activity: dict[str, Any]) -> dict[str, Any] | None:
    obj = activity.get("object")
    return obj if isinstance(obj, dict) else None


def _object_ref(activity: dict[str, Any]) -> str | None:
    obj = activity.get("object")
    if isinstance(obj, str):
        return normalize_identifier(obj)
    if isinstance(obj, dict):
        return normalize_identifier(obj.get("id"))
    return None


class Catalog:
    """对象级采集状态库；所有方法在本地 SQLite 事务内完成，无网络行为。"""

    def __init__(self, path: str | Path, partners: PartnerDirectory | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.partners = partners
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activities (
                activity_id TEXT PRIMARY KEY,
                activity_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                object_id TEXT,
                payload TEXT NOT NULL,
                source TEXT NOT NULL,
                observation TEXT,
                first_seen_seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS objects (
                object_id TEXT PRIMARY KEY,
                object_type TEXT,
                attributed_to TEXT,
                content TEXT,
                is_tombstone INTEGER NOT NULL DEFAULT 0,
                details_complete INTEGER NOT NULL DEFAULT 0,
                configured_domain INTEGER NOT NULL DEFAULT 1,
                first_seen_seq INTEGER NOT NULL,
                updated_seq INTEGER
            );
            CREATE TABLE IF NOT EXISTS shares (
                share_activity_id TEXT PRIMARY KEY,
                announcer TEXT NOT NULL,
                object_id TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                announced_seq INTEGER NOT NULL,
                withdrawn_seq INTEGER,
                undo_activity_id TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_undo (
                undo_activity_id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                target_activity_id TEXT NOT NULL,
                seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journal (
                seq INTEGER PRIMARY KEY,
                source TEXT,
                observation TEXT,
                activity_id TEXT,
                activity_type TEXT,
                actor TEXT,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            """
        )
        if partners is not None:
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)",
                    ("partner_sources", json.dumps(partners.sources)),
                )

    @classmethod
    def reopen(cls, path: str | Path) -> "Catalog":
        """重开已存在的目录做只读查看；入库仍需用带名录的构造方式。"""
        catalog = cls(path)
        row = catalog.connection.execute(
            "SELECT value FROM meta WHERE key='partner_sources'"
        ).fetchone()
        if row is None:
            catalog.close()
            raise ValueError("catalog has no partner configuration; open it with a PartnerDirectory")
        catalog._partner_sources = json.loads(row["value"])
        return catalog

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 事件台账
    # ------------------------------------------------------------------ #
    def _journal(self, connection, *, source, observation, activity_id, activity_type, actor,
                 decision, reason, detail) -> int:
        cursor = connection.execute(
            """
            INSERT INTO journal(source,observation,activity_id,activity_type,actor,decision,reason,detail,recorded_at)
            VALUES(?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (
                source,
                None if observation is None else str(observation),
                activity_id,
                activity_type,
                actor,
                decision,
                reason,
                detail,
            ),
        )
        return cursor.lastrowid

    def journal(self) -> list[JournalEntry]:
        rows = self.connection.execute("SELECT * FROM journal ORDER BY seq").fetchall()
        return [JournalEntry(**dict(row)) for row in rows]

    # ------------------------------------------------------------------ #
    # 入库入口
    # ------------------------------------------------------------------ #
    def ingest(self, envelope: dict[str, Any]) -> JournalEntry:
        """处理一条合作网关信封（含 source/observation/activity），返回台账记录。"""
        if self.partners is None:
            raise ValueError("catalog reopened read-only; construct it with a PartnerDirectory to ingest")
        source = envelope.get("source")
        observation = envelope.get("observation")
        source = source.strip() if isinstance(source, str) else source

        def reject(reason: str, detail: str, *, actor=None, activity_id=None, activity_type=None) -> JournalEntry:
            with self.connection:
                seq = self._journal(
                    self.connection, source=source, observation=observation,
                    activity_id=activity_id, activity_type=activity_type, actor=actor,
                    decision=REJECTED, reason=reason, detail=detail,
                )
            return self._entry(seq)

        if not isinstance(envelope, dict) or not isinstance(source, str) or not source:
            return reject(REASON_MALFORMED, "envelope source is required")
        partner = self.partners.partner_for(source)
        if partner is None:
            return reject(REASON_UNCONFIGURED_SOURCE, "source is not a configured partner")

        activity = envelope.get("activity")
        if not isinstance(activity, dict):
            return reject(REASON_MALFORMED, "activity payload missing or not an object")

        activity_id = normalize_identifier(activity.get("id"))
        activity_type = activity.get("type") if isinstance(activity.get("type"), str) else None
        actor = normalize_identifier(activity.get("actor"))
        if activity_id is None:
            return reject(REASON_BAD_IDENTIFIER, "activity id must be an absolute http(s) URL",
                          actor=actor, activity_type=activity_type)
        if not isinstance(activity_type, str) or not activity_type.strip():
            return reject(REASON_BAD_IDENTIFIER, "activity type is required",
                          actor=actor, activity_id=activity_id)
        if actor is None:
            return reject(REASON_BAD_IDENTIFIER, "activity actor must be an absolute http(s) URL",
                          activity_id=activity_id, activity_type=activity_type)
        if not self.partners.trusts(source, actor):
            return reject(REASON_UNTRUSTED_ACTOR,
                          "actor is not registered for this partner source",
                          actor=actor, activity_id=activity_id, activity_type=activity_type)

        # 私有收件对象（含 bto/bcc 盲收件、无公开集合的定向投递）不收。
        if is_private(activity):
            return reject(REASON_PRIVATE, "activity is not publicly addressed",
                          actor=actor, activity_id=activity_id, activity_type=activity_type)
        embedded = _embedded_object(activity)
        if embedded is not None and is_private(embedded):
            return reject(REASON_PRIVATE, "embedded object is not publicly addressed",
                          actor=actor, activity_id=activity_id, activity_type=activity_type)

        with self.connection:
            duplicate = self.connection.execute(
                "SELECT activity_id FROM activities WHERE activity_id=?", (activity_id,)
            ).fetchone()
            if duplicate is not None:
                seq = self._journal(
                    self.connection, source=source, observation=observation,
                    activity_id=activity_id, activity_type=activity_type, actor=actor,
                    decision=DUPLICATE, reason="activity_id_seen",
                    detail="repeat delivery changes no state",
                )
                return self._entry(seq)
            seq = self.connection.execute(
                """
                INSERT INTO activities(activity_id,activity_type,actor,object_id,payload,source,observation,first_seen_seq)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    activity_id,
                    activity_type,
                    actor,
                    _object_ref(activity),
                    json.dumps(activity, ensure_ascii=False, sort_keys=True),
                    source,
                    None if observation is None else str(observation),
                    0,
                ),
            ).lastrowid
            self.connection.execute("UPDATE activities SET first_seen_seq=? WHERE activity_id=?", (seq, activity_id))

            handler = {
                "Create": self._handle_create,
                "Update": self._handle_update,
                "Delete": self._handle_delete,
                "Announce": self._handle_announce,
                "Undo": self._handle_undo,
            }.get(activity_type)
            if handler is None:
                journal_seq = self._journal(
                    self.connection, source=source, observation=observation,
                    activity_id=activity_id, activity_type=activity_type, actor=actor,
                    decision=REJECTED, reason=REASON_TYPE_MISMATCH,
                    detail="only the public collection subset is interpreted",
                )
                self.connection.execute("DELETE FROM activities WHERE activity_id=?", (activity_id,))
                return self._entry(journal_seq)
            decision, reason, detail = handler(
                self.connection, seq=seq, activity=activity, activity_id=activity_id,
                activity_type=activity_type, actor=actor,
            )
            if decision == REJECTED:
                # 被拒活动不构成有效活动身份；对象占位与台账仍作为事件依据保留。
                self.connection.execute("DELETE FROM activities WHERE activity_id=?", (activity_id,))
            journal_seq = self._journal(
                self.connection, source=source, observation=observation,
                activity_id=activity_id, activity_type=activity_type, actor=actor,
                decision=decision, reason=reason, detail=detail,
            )
            return self._entry(journal_seq)

    def _entry(self, seq: int) -> JournalEntry:
        row = self.connection.execute("SELECT * FROM journal WHERE seq=?", (seq,)).fetchone()
        return JournalEntry(**dict(row))

    # ------------------------------------------------------------------ #
    # 对象投影辅助
    # ------------------------------------------------------------------ #
    def _ensure_object_stub(self, connection, object_id: str, seq: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM objects WHERE object_id=?", (object_id,)).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO objects(object_id,details_complete,configured_domain,first_seen_seq)
                VALUES(?,?,?,?)
                """,
                (
                    object_id,
                    0,
                    1 if self.partners.is_configured_domain(object_id) else 0,
                    seq,
                ),
            )
            row = connection.execute("SELECT * FROM objects WHERE object_id=?", (object_id,)).fetchone()
        return row

    def _apply_object_details(self, connection, obj: dict[str, Any], seq: int) -> tuple[str, str]:
        object_id = normalize_identifier(obj.get("id"))
        if object_id is None:
            return REJECTED, REASON_BAD_IDENTIFIER
        row = self._ensure_object_stub(connection, object_id, seq)
        tombstone = isinstance(obj.get("type"), str) and obj["type"] == "Tombstone"
        if tombstone or row["is_tombstone"]:
            return REJECTED, REASON_TOMBSTONE
        attributed = normalize_identifier(obj.get("attributedTo"))
        if attributed is not None and row["attributed_to"] is not None and attributed != row["attributed_to"]:
            return REJECTED, REASON_UPDATE_CONFLICT
        content = obj.get("content")
        connection.execute(
            """
            UPDATE objects SET object_type=COALESCE(?,object_type),
                               attributed_to=COALESCE(?,attributed_to),
                               content=?,
                               details_complete=1,
                               updated_seq=?
             WHERE object_id=?
            """,
            (
                obj.get("type") if isinstance(obj.get("type"), str) else None,
                attributed,
                None if content is None else str(content),
                seq,
                object_id,
            ),
        )
        return APPLIED, "object_details_recorded"

    # ------------------------------------------------------------------ #
    # 活动类型处理
    # ------------------------------------------------------------------ #
    def _handle_create(self, connection, *, seq, activity, activity_id, activity_type, actor):
        obj = _embedded_object(activity)
        if obj is None:
            # 只有引用 URL：登记待补占位，绝不把 URL 当正文。
            object_id = _object_ref(activity)
            if object_id is None:
                return REJECTED, REASON_BAD_IDENTIFIER, "create carries no resolvable object id"
            self._ensure_object_stub(connection, object_id, seq)
            return PENDING, PENDING_OBJECT, "awaiting embedded object details for " + object_id
        object_id = normalize_identifier(obj.get("id"))
        if object_id is None:
            return REJECTED, REASON_BAD_IDENTIFIER, "embedded object id must be an absolute URL"
        if isinstance(obj.get("type"), str) and obj["type"] == "Tombstone":
            return REJECTED, REASON_TOMBSTONE, "create cannot introduce a tombstone"
        row = self._ensure_object_stub(connection, object_id, seq)
        if row["is_tombstone"]:
            # 晚到的旧 Create 不能恢复已确认撤回（删除）的对象。
            return REJECTED, REASON_TOMBSTONE, "late create cannot resurrect a deleted object"
        attributed = normalize_identifier(obj.get("attributedTo"))
        if attributed is not None and attributed != actor:
            return REJECTED, REASON_OWNERSHIP, "embedded object is attributed to another actor"
        decision, reason = self._apply_object_details(connection, obj, seq)
        if decision == REJECTED:
            return decision, reason, "embedded object details rejected"
        if attributed is None:
            connection.execute(
                "UPDATE objects SET attributed_to=? WHERE object_id=? AND attributed_to IS NULL",
                (actor, object_id),
            )
        return APPLIED, reason, "object created: " + object_id

    def _handle_update(self, connection, *, seq, activity, activity_id, activity_type, actor):
        object_id = _object_ref(activity)
        if object_id is None:
            return REJECTED, REASON_BAD_IDENTIFIER, "update carries no object id"
        obj = _embedded_object(activity)
        if obj is None:
            # 原帖 Update 与转发引用可能乱序：详情缺失时挂起待补。
            self._ensure_object_stub(connection, object_id, seq)
            return PENDING, PENDING_OBJECT, "update without details waits for object " + object_id
        row = self._ensure_object_stub(connection, object_id, seq)
        if row["is_tombstone"]:
            return REJECTED, REASON_TOMBSTONE, "update cannot resurrect a deleted object"
        attributed = normalize_identifier(obj.get("attributedTo"))
        known_owner = row["attributed_to"]
        if known_owner is not None and actor != known_owner:
            return REJECTED, REASON_OWNERSHIP, "update actor does not own the object"
        if attributed is not None and attributed != actor:
            return REJECTED, REASON_OWNERSHIP, "updated object is attributed to another actor"
        decision, reason = self._apply_object_details(connection, obj, seq)
        if decision == REJECTED:
            return decision, reason, "embedded update rejected"
        connection.execute(
            "UPDATE objects SET attributed_to=COALESCE(attributed_to,?) WHERE object_id=?",
            (actor, object_id),
        )
        return APPLIED, reason, "object updated: " + object_id

    def _handle_delete(self, connection, *, seq, activity, activity_id, activity_type, actor):
        object_id = _object_ref(activity)
        if object_id is None:
            return REJECTED, REASON_BAD_IDENTIFIER, "delete carries no object id"
        row = self._ensure_object_stub(connection, object_id, seq)
        # 可信来源核对：actor 必须是该 source 的注册 actor（入口已保证）。
        # 对象归属核对：只接受对象属主或对象所在合作域自身发起的删除。
        owner = row["attributed_to"]
        if owner is not None and owner != actor:
            return REJECTED, REASON_OWNERSHIP, (
                "delete from a non-owner account is ignored; original content retained"
            )
        if owner is None and not self._actor_shares_object_domain(actor, object_id):
            return REJECTED, REASON_OWNERSHIP, (
                "delete cannot be verified against object ownership; original content retained"
            )
        embedded = _embedded_object(activity)
        if embedded is not None and not isinstance(embedded.get("type"), str):
            return REJECTED, REASON_BAD_IDENTIFIER, "embedded delete object lacks a type"
        connection.execute(
            """
            UPDATE objects SET is_tombstone=1,
                               content=NULL,
                               object_type=COALESCE(?,object_type),
                               updated_seq=?
             WHERE object_id=?
            """,
            ("Tombstone" if embedded is not None else None, seq, object_id),
        )
        # 对象删除后，所有账号对它的展示关系一并停展，但记录保留为事件依据；
        # 这些关系不是被 Undo 撤销的（清空 undo 标记），列入“删除隐藏”而非“撤展转发”。
        connection.execute(
            "UPDATE shares SET active=0, withdrawn_seq=?, undo_activity_id=NULL WHERE object_id=? AND active=1",
            (seq, object_id),
        )
        return APPLIED, "object_tombstoned_by_owner", "object deleted by verified owner: " + object_id

    def _actor_shares_object_domain(self, actor: str, object_id: str) -> bool:
        actor_domain = registered_domain(actor)
        object_domain = registered_domain(object_id)
        return actor_domain is not None and actor_domain == object_domain

    def _handle_announce(self, connection, *, seq, activity, activity_id, activity_type, actor):
        object_id = _object_ref(activity)
        if object_id is None:
            return REJECTED, REASON_BAD_IDENTIFIER, "announce carries no object reference"
        self._ensure_object_stub(connection, object_id, seq)
        existing = connection.execute(
            "SELECT * FROM shares WHERE share_activity_id=?", (activity_id,)
        ).fetchone()
        if existing is not None:
            # 活动 id 已在入口去重；理论上不可达，稳妥保留。
            return DUPLICATE, "announce_seen", "announce activity already recorded"
        row = connection.execute("SELECT * FROM objects WHERE object_id=?", (object_id,)).fetchone()
        hidden_by_tombstone = bool(row["is_tombstone"])
        connection.execute(
            """
            INSERT INTO shares(share_activity_id,announcer,object_id,active,announced_seq,withdrawn_seq)
            VALUES(?,?,?,?,?,?)
            """,
            (activity_id, actor, object_id, 0 if hidden_by_tombstone else 1, seq, seq if hidden_by_tombstone else None),
        )
        # Undo 先于 Announce 到达时曾挂起；转发关系一建立即兑现那次撤回。
        waiting = connection.execute(
            "SELECT * FROM pending_undo WHERE target_activity_id=? AND actor=?",
            (activity_id, actor),
        ).fetchall()
        if waiting:
            connection.execute(
                "UPDATE shares SET active=0, withdrawn_seq=?, undo_activity_id=? WHERE share_activity_id=?",
                (seq, waiting[0]["undo_activity_id"], activity_id),
            )
            connection.execute(
                "DELETE FROM pending_undo WHERE target_activity_id=? AND actor=?",
                (activity_id, actor),
            )
        row = connection.execute("SELECT * FROM objects WHERE object_id=?", (object_id,)).fetchone()
        if waiting:
            return APPLIED, "late_announce_withdrawn", (
                "announce arrived after its undo; share created already withdrawn for " + object_id
            )
        if hidden_by_tombstone:
            return APPLIED, "share_hidden_object_deleted", (
                "announce recorded but object is already a tombstone: " + object_id
            )
        if not row["details_complete"]:
            return PENDING, PENDING_OBJECT, (
                "share recorded; awaiting object details for " + object_id
            )
        return APPLIED, "share_recorded", actor + " announces " + object_id

    def _handle_undo(self, connection, *, seq, activity, activity_id, activity_type, actor):
        target_id = _object_ref(activity)
        if target_id is None:
            return REJECTED, REASON_BAD_IDENTIFIER, "undo carries no target activity id"
        target = connection.execute(
            "SELECT * FROM activities WHERE activity_id=?", (target_id,)
        ).fetchone()
        if target is None:
            # 目标活动尚未到达：挂起，待 Announce 晚到时补处理，不做任何撤展。
            connection.execute(
                "INSERT OR IGNORE INTO pending_undo(undo_activity_id,actor,target_activity_id,seq) VALUES(?,?,?,?)",
                (activity_id, actor, target_id, seq),
            )
            return PENDING, PENDING_UNDO, "undo waits for target activity " + target_id
        if target["activity_type"] != "Announce":
            # Undo 只取消转发；撤销其它活动不在只读公共采集范围内。
            return REJECTED, REASON_UNDO_TARGET, "undo target is not an Announce"
        if target["actor"] != actor:
            return REJECTED, REASON_UNDO_ACTOR, "only the announcer may undo their announce"
        share = connection.execute(
            "SELECT * FROM shares WHERE share_activity_id=?", (target_id,)
        ).fetchone()
        if share is None:
            connection.execute(
                "INSERT OR IGNORE INTO pending_undo(undo_activity_id,actor,target_activity_id,seq) VALUES(?,?,?,?)",
                (activity_id, actor, target_id, seq),
            )
            return PENDING, PENDING_UNDO, "undo waits for share projection " + target_id
        if not share["active"]:
            return DUPLICATE, "share_already_withdrawn", "undo confirms an already withdrawn share"
        connection.execute(
            "UPDATE shares SET active=0, withdrawn_seq=?, undo_activity_id=? WHERE share_activity_id=?",
            (seq, activity_id, target_id),
        )
        # 只取消这一次转发：原作者对象保持原样。
        return APPLIED, "share_withdrawn", "announce " + target_id + " withdrawn; original object untouched"

    # ------------------------------------------------------------------ #
    # 馆方当前视图
    # ------------------------------------------------------------------ #
    def objects(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM objects WHERE configured_domain=1 ORDER BY first_seen_seq, object_id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_tombstone"] = bool(row["is_tombstone"])
            item["details_complete"] = bool(row["details_complete"])
            item["configured_domain"] = bool(row["configured_domain"])
            result.append(item)
        return result

    def shares(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM shares"
        if not include_inactive:
            query += " WHERE active=1"
        rows = self.connection.execute(query + " ORDER BY announced_seq, share_activity_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["active"] = bool(row["active"])
            result.append(item)
        return result

    def display_view(self) -> dict[str, Any]:
        """按馆方口径汇总当前可展示对象与转发关系，并附事件依据。"""
        visible_objects = [
            item for item in self.objects()
            if item["details_complete"] and not item["is_tombstone"]
        ]
        active_shares = []
        for share in self.shares(include_inactive=False):
            obj = self.connection.execute(
                "SELECT details_complete,is_tombstone,configured_domain FROM objects WHERE object_id=?",
                (share["object_id"],),
            ).fetchone()
            if obj is None or not obj["configured_domain"]:
                continue
            if not obj["details_complete"] or obj["is_tombstone"]:
                continue
            active_shares.append(share)
        decisions = self.connection.execute(
            "SELECT decision, count(*) AS n FROM journal GROUP BY decision ORDER BY decision"
        ).fetchall()
        return {
            "visible_objects": visible_objects,
            "active_shares": active_shares,
            "withdrawn_shares": [
                dict(row, active=bool(row["active"]))
                for row in self.connection.execute(
                    "SELECT * FROM shares WHERE active=0 AND undo_activity_id IS NOT NULL ORDER BY withdrawn_seq"
                ).fetchall()
            ],
            "tombstone_hidden_share_ids": [
                row["share_activity_id"]
                for row in self.connection.execute(
                    "SELECT share_activity_id FROM shares WHERE active=0 AND undo_activity_id IS NULL ORDER BY announced_seq"
                ).fetchall()
            ],
            "pending_undo": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM pending_undo ORDER BY seq"
                ).fetchall()
            ],
            "pending_object_ids": [
                row["object_id"] for row in self.connection.execute(
                    "SELECT object_id FROM objects WHERE details_complete=0 AND configured_domain=1 ORDER BY first_seen_seq"
                ).fetchall()
            ],
            "tombstoned_object_ids": [
                row["object_id"] for row in self.connection.execute(
                    "SELECT object_id FROM objects WHERE is_tombstone=1 ORDER BY updated_seq"
                ).fetchall()
            ],
            "journal_counts": {row["decision"]: row["n"] for row in decisions},
            "journal_size": self.connection.execute("SELECT count(*) FROM journal").fetchone()[0],
        }
