"""只读 ActivityPub 公共资料采集的领域层。

把三类身份分开保存：

- 活动身份：``activity_events`` 逐条记录每次观察到的活动及处理结论，是只追加的事件依据；
- 对象身份：``objects`` 保存原作者发布的对象及其当前状态；
- 展示关系：``displays`` 保存某个账号经 Announce 对对象建立的展示关系，
  ``withdrawals`` 保存已确认的撤销，使晚到或重复的转发不能恢复已撤回的展示。

信任只来自本地配置的合作方（source 与 actor 的对应关系），不凭活动自报身份。
本层不发起任何网络请求：不抓取对象 URL，也不抓取远端 JSON-LD 上下文；
缺少对象详情时关系保持待补，引用 URL 不会被当成正文。
"""

import json
import sqlite3
from pathlib import Path

from . import fixture_path
from .decoding import records as decode_records

PUBLIC_AUDIENCE = "https://www.w3.org/ns/activitystreams#Public"
PUBLIC_ALIASES = frozenset({PUBLIC_AUDIENCE, "as:Public", "Public"})
CONTENT_ACTIVITIES = frozenset({"Create", "Update", "Announce"})
CONTROL_ACTIVITIES = frozenset({"Undo", "Delete"})
SUPPORTED_ACTIVITIES = CONTENT_ACTIVITIES | CONTROL_ACTIVITIES
AUDIENCE_FIELDS = ("to", "cc", "bto", "bcc", "audience")
REFERENCE_ONLY_KEYS = frozenset({"id", "type"})

# 事件结论取值：applied 已应用 / duplicate 幂等忽略 / rejected 拒绝 / deferred 待补。
DECISIONS = frozenset({"applied", "duplicate", "rejected", "deferred"})


class _Reject(Exception):
    """活动被拒绝，reason 会作为事件依据保留。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_partners(path=None):
    """读取本地合作方配置：source 与 actor 的对应关系是唯一的信任来源。"""
    config_path = Path(path) if path is not None else fixture_path("partners.json")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    entries = payload.get("partners") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("partners config must be a list")
    partners = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("partner entries must be objects")
        source = str(entry.get("source", "")).strip()
        actor = str(entry.get("actor", "")).strip()
        if not source or not actor:
            raise ValueError("partner entries require source and actor")
        if source in partners:
            raise ValueError("duplicate partner source: " + source)
        name = str(entry.get("name", "")).strip() or source
        partners[source] = {"actor": actor, "name": name}
    if not partners:
        raise ValueError("at least one partner is required")
    return partners


def _identity(value):
    """字符串或 {"id": ...} 形式的标识；其他形式返回 None。"""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, dict):
        return _identity(value.get("id"))
    return None


def _is_public(activity):
    """活动明确投递到 Public 集合才视为公开；没有公开收件人的一律不采。"""
    iris = set()
    for field in AUDIENCE_FIELDS:
        value = activity.get(field)
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            iri = _identity(item)
            if iri:
                iris.add(iri)
    return bool(iris & PUBLIC_ALIASES)


def _reference(value):
    """从 object 字段取出 (标识, 是否仅为引用)。引用只有 id/type，没有正文字段。"""
    if isinstance(value, str):
        return _identity(value), True
    if isinstance(value, dict):
        return _identity(value.get("id")), not (set(value) - REFERENCE_ONLY_KEYS)
    return None, True


class DomainStore:
    """领域状态库：与原始归档分离演进，只追加事件依据，状态可重放。"""

    def __init__(self, path, partners=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS partners (
                source TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingest_log (
                capture_id INTEGER PRIMARY KEY,
                outcome TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_events (
                seq INTEGER PRIMARY KEY,
                capture_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                observation TEXT NOT NULL,
                activity_id TEXT,
                type TEXT,
                actor TEXT,
                object_id TEXT,
                decision TEXT NOT NULL,
                detail TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS objects (
                object_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('current','deleted'))
            );
            CREATE TABLE IF NOT EXISTS displays (
                announce_id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                source TEXT NOT NULL,
                object_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active','withdrawn'))
            );
            CREATE TABLE IF NOT EXISTS withdrawals (
                announce_id TEXT NOT NULL,
                account TEXT NOT NULL,
                PRIMARY KEY (announce_id, account)
            );
        """)
        if partners is None:
            partners = {
                row["source"]: {"actor": row["actor"], "name": row["name"]}
                for row in self.connection.execute("SELECT source, actor, name FROM partners")
            }
            if not partners:
                raise ValueError("domain store has no partner config; run ingest first")
        self.partners = {source: dict(info) for source, info in partners.items()}
        with self.connection:
            for source, info in self.partners.items():
                self.connection.execute(
                    "INSERT INTO partners VALUES (?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET actor=excluded.actor, name=excluded.name",
                    (source, info["actor"], info["name"]),
                )

    def close(self):
        self.connection.close()

    # ---------- 采集 ----------

    def watermark(self):
        row = self.connection.execute(
            "SELECT COALESCE(MAX(capture_id), 0) AS mark FROM ingest_log"
        ).fetchone()
        return row["mark"]

    def ingest(self, archive):
        """按归档顺序应用新捕获；每个捕获要么整体应用，要么整体记录为失败。"""
        mark = self.watermark()
        pending = [capture for capture in archive.captures() if capture["id"] > mark]
        summary = {"applied_captures": 0, "records": 0, "decisions": {}}
        for capture in pending:
            events = self._apply_capture(archive, capture)
            summary["applied_captures"] += 1
            summary["records"] += len(events)
            for event in events:
                decision = event["decision"]
                summary["decisions"][decision] = summary["decisions"].get(decision, 0) + 1
        return summary

    def _apply_capture(self, archive, capture):
        capture_id = capture["id"]
        if capture["status"] != 200:
            # 源站短时失联等失败观察只记录，不改变任何领域状态。
            self._log_ingest(capture_id, "skipped_status", "http " + str(capture["status"]))
            return []
        body = archive.body(capture_id)
        content_type = capture["headers"].get("content-type", "")
        try:
            records = decode_records(body, content_type)
        except ValueError as error:
            self._log_ingest(capture_id, "decode_error", str(error))
            return []
        events = []
        with self.connection:
            for record in records:
                events.append(self._apply_record(self.connection, capture_id, record))
            self.connection.execute(
                "INSERT INTO ingest_log VALUES (?,?,?)",
                (capture_id, "applied", str(len(records)) + " records"),
            )
        return events

    def _log_ingest(self, capture_id, outcome, detail):
        with self.connection:
            self.connection.execute(
                "INSERT INTO ingest_log VALUES (?,?,?)", (capture_id, outcome, detail)
            )

    def _apply_record(self, connection, capture_id, record):
        source = str(record.get("source", "")).strip() if isinstance(record, dict) else ""
        observation = record.get("observation") if isinstance(record, dict) else None
        activity = record.get("activity") if isinstance(record, dict) else None
        event = {
            "capture_id": capture_id,
            "source": source,
            "observation": "" if observation is None else str(observation),
            "activity_id": None,
            "type": None,
            "actor": None,
            "object_id": None,
            "decision": "rejected",
            "detail": "malformed_record",
            "payload": json.dumps(activity, ensure_ascii=False, sort_keys=True),
        }
        try:
            if not isinstance(activity, dict):
                raise _Reject("malformed_activity")
            activity_id = _identity(activity.get("id"))
            actor = _identity(activity.get("actor"))
            activity_type = activity.get("type")
            object_id, _ = _reference(activity.get("object"))
            event["activity_id"] = activity_id
            event["actor"] = actor
            event["type"] = activity_type if isinstance(activity_type, str) else None
            event["object_id"] = object_id
            if activity_id is None or actor is None or not isinstance(activity_type, str):
                raise _Reject("malformed_activity")
            partner = self.partners.get(source)
            if partner is None:
                raise _Reject("untrusted_source")
            if actor != partner["actor"]:
                # 网关转来他方活动：信封来源与 actor 配置不符，不凭自报身份建立信任。
                raise _Reject("actor_mismatch")
            if activity_type not in SUPPORTED_ACTIVITIES:
                raise _Reject("unsupported_type")
            if self._seen(connection, activity_id):
                event["decision"] = "duplicate"
                event["detail"] = "activity_already_recorded"
            else:
                if activity_type in CONTENT_ACTIVITIES and not _is_public(activity):
                    raise _Reject("non_public")
                decision, detail = self._apply_activity(
                    connection, source, activity_id, actor, activity_type, activity
                )
                event["decision"] = decision
                event["detail"] = detail
        except _Reject as rejection:
            event["decision"] = "rejected"
            event["detail"] = rejection.reason
        cursor = connection.execute(
            "INSERT INTO activity_events"
            " (capture_id, source, observation, activity_id, type, actor, object_id, decision, detail, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                event["capture_id"], event["source"], event["observation"],
                event["activity_id"], event["type"], event["actor"], event["object_id"],
                event["decision"], event["detail"], event["payload"],
            ),
        )
        event["seq"] = cursor.lastrowid
        return event

    @staticmethod
    def _seen(connection, activity_id):
        """已应用或已登记待补的同一活动再次到达时幂等忽略；曾被拒绝的允许重新评估。"""
        row = connection.execute(
            "SELECT 1 FROM activity_events WHERE activity_id=? AND decision IN ('applied','deferred')",
            (activity_id,),
        ).fetchone()
        return row is not None

    def _apply_activity(self, connection, source, activity_id, actor, activity_type, activity):
        handler = {
            "Create": self._apply_create,
            "Update": self._apply_update,
            "Announce": self._apply_announce,
            "Undo": self._apply_undo,
            "Delete": self._apply_delete,
        }[activity_type]
        return handler(connection, source, activity_id, actor, activity)

    @staticmethod
    def _apply_create(connection, source, activity_id, actor, activity):
        obj = activity.get("object")
        object_id, reference_only = _reference(obj)
        if not isinstance(obj, dict) or object_id is None:
            raise _Reject("object_not_embedded")
        if reference_only:
            raise _Reject("object_details_missing")
        owner = _identity(obj.get("attributedTo"))
        if owner != actor:
            raise _Reject("ownership_mismatch")
        row = connection.execute(
            "SELECT status FROM objects WHERE object_id=?", (object_id,)
        ).fetchone()
        if row is not None:
            if row["status"] == "deleted":
                # 已确认删除的对象不因晚到的旧活动恢复。
                raise _Reject("object_deleted")
            return "duplicate", "object_already_known"
        content = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO objects VALUES (?,?,?,?,'current')",
            (object_id, owner, source, content),
        )
        return "applied", "created"

    @staticmethod
    def _apply_update(connection, source, activity_id, actor, activity):
        object_id, reference_only = _reference(activity.get("object"))
        if object_id is None:
            raise _Reject("malformed_object")
        if reference_only:
            # 只有引用没有详情：登记待补，引用 URL 不冒充正文。
            return "deferred", "reference_only_object"
        obj = activity["object"]
        owner = _identity(obj.get("attributedTo"))
        row = connection.execute(
            "SELECT owner, status FROM objects WHERE object_id=?", (object_id,)
        ).fetchone()
        if row is None:
            if owner is None:
                return "deferred", "ownership_unverifiable"
            if owner != actor:
                raise _Reject("ownership_mismatch")
            # Update 先于 Create 到达：详情齐全且归属可核对，可先建立对象。
            content = json.dumps(obj, ensure_ascii=False, sort_keys=True)
            connection.execute(
                "INSERT INTO objects VALUES (?,?,?,?,'current')",
                (object_id, owner, source, content),
            )
            return "applied", "established_by_update"
        if row["owner"] != actor:
            raise _Reject("not_object_owner")
        if owner is not None and owner != actor:
            raise _Reject("ownership_mismatch")
        if row["status"] == "deleted":
            raise _Reject("object_deleted")
        content = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "UPDATE objects SET content=?, source=? WHERE object_id=?",
            (content, source, object_id),
        )
        return "applied", "updated"

    @staticmethod
    def _apply_announce(connection, source, activity_id, actor, activity):
        object_id, _ = _reference(activity.get("object"))
        if object_id is None:
            raise _Reject("malformed_object")
        row = connection.execute(
            "SELECT account, status FROM displays WHERE announce_id=?", (activity_id,)
        ).fetchone()
        if row is not None:
            if row["account"] != actor:
                raise _Reject("identity_conflict")
            return "duplicate", "announce_known"
        withdrawn = connection.execute(
            "SELECT 1 FROM withdrawals WHERE announce_id=? AND account=?",
            (activity_id, actor),
        ).fetchone()
        if withdrawn is not None:
            # 撤销先于转发到达（或旧引用晚到）：关系登记即为已撤回，不恢复展示。
            connection.execute(
                "INSERT INTO displays VALUES (?,?,?,?,'withdrawn')",
                (activity_id, actor, source, object_id),
            )
            return "applied", "withdrawn_before_arrival"
        connection.execute(
            "INSERT INTO displays VALUES (?,?,?,?,'active')",
            (activity_id, actor, source, object_id),
        )
        return "applied", "announced"

    @staticmethod
    def _apply_undo(connection, source, activity_id, actor, activity):
        target = activity.get("object")
        target_id, _ = _reference(target)
        if target_id is None:
            raise _Reject("malformed_object")
        if isinstance(target, dict):
            embedded_actor = _identity(target.get("actor"))
            if embedded_actor is not None and embedded_actor != actor:
                raise _Reject("not_announcer")
        row = connection.execute(
            "SELECT account, status FROM displays WHERE announce_id=?", (target_id,)
        ).fetchone()
        if row is not None:
            if row["account"] != actor:
                # 只能撤回自己的转发；Undo 只取消那次转发，不触及原对象。
                raise _Reject("not_announcer")
            if row["status"] == "withdrawn":
                return "duplicate", "already_withdrawn"
            connection.execute(
                "UPDATE displays SET status='withdrawn' WHERE announce_id=?", (target_id,)
            )
            connection.execute(
                "INSERT OR IGNORE INTO withdrawals VALUES (?,?)", (target_id, actor)
            )
            return "applied", "withdrawn"
        known = connection.execute(
            "SELECT 1 FROM withdrawals WHERE announce_id=? AND account=?",
            (target_id, actor),
        ).fetchone()
        if known is not None:
            return "duplicate", "already_withdrawn"
        # Undo 先于 Announce 到达：先登记撤销，晚到的转发不能恢复展示。
        connection.execute("INSERT INTO withdrawals VALUES (?,?)", (target_id, actor))
        return "applied", "withdrawal_registered"

    @staticmethod
    def _apply_delete(connection, source, activity_id, actor, activity):
        object_id, _ = _reference(activity.get("object"))
        if object_id is None:
            raise _Reject("malformed_object")
        row = connection.execute(
            "SELECT owner, status FROM objects WHERE object_id=?", (object_id,)
        ).fetchone()
        if row is None:
            # 对象未知则归属无法核对，删除声明不足为凭。
            raise _Reject("object_unknown")
        if row["owner"] != actor:
            # 其他账号转来的删除声明不能抹掉原文。
            raise _Reject("not_object_owner")
        if row["status"] == "deleted":
            return "duplicate", "already_deleted"
        connection.execute(
            "UPDATE objects SET status='deleted' WHERE object_id=?", (object_id,)
        )
        return "applied", "deleted"

    # ---------- 资料员视图与事件依据 ----------

    def view(self, source=None):
        """按馆方列出当前可展示的对象与转发关系；每项都带事件依据序号。"""
        sources = [source] if source is not None else list(self.partners)
        unknown = [name for name in sources if name not in self.partners]
        if unknown:
            raise ValueError("unknown partner source: " + ", ".join(sorted(unknown)))
        objects = {
            row["object_id"]: row
            for row in self.connection.execute("SELECT * FROM objects ORDER BY rowid")
        }
        displays = self.connection.execute("SELECT * FROM displays ORDER BY rowid").fetchall()
        result = {
            "applied_captures": self.watermark(),
            "activity_events": self.connection.execute(
                "SELECT COUNT(*) AS n FROM activity_events"
            ).fetchone()["n"],
            "partners": [],
        }
        for name in sources:
            info = self.partners[name]
            actor = info["actor"]
            object_entries = []
            for row in objects.values():
                if row["owner"] != actor:
                    continue
                object_entries.append({
                    "object_id": row["object_id"],
                    "status": row["status"],
                    "displayable": row["status"] == "current",
                    "content": json.loads(row["content"]),
                    "evidence": self._object_evidence(row["object_id"]),
                })
            forward_entries = []
            for row in displays:
                if row["account"] != actor:
                    continue
                target = objects.get(row["object_id"])
                object_status = "pending" if target is None else target["status"]
                forward_entries.append({
                    "announce_id": row["announce_id"],
                    "object_id": row["object_id"],
                    "status": row["status"],
                    "object_status": object_status,
                    "displayable": row["status"] == "active" and object_status == "current",
                    "evidence": self._display_evidence(row["announce_id"]),
                })
            result["partners"].append({
                "source": name,
                "name": info["name"],
                "actor": actor,
                "objects": object_entries,
                "forwards": forward_entries,
            })
        return result

    def _object_evidence(self, object_id):
        rows = self.connection.execute(
            "SELECT seq FROM activity_events WHERE object_id=? AND type IN ('Create','Update','Delete')"
            " ORDER BY seq",
            (object_id,),
        ).fetchall()
        return [row["seq"] for row in rows]

    def _display_evidence(self, announce_id):
        rows = self.connection.execute(
            "SELECT seq FROM activity_events WHERE activity_id=? OR (object_id=? AND type='Undo')"
            " ORDER BY seq",
            (announce_id, announce_id),
        ).fetchall()
        return [row["seq"] for row in rows]

    def events(self, source=None):
        query = "SELECT * FROM activity_events"
        params = ()
        if source is not None:
            query += " WHERE source=?"
            params = (source,)
        rows = self.connection.execute(query + " ORDER BY seq", params).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def ingest_log(self):
        rows = self.connection.execute("SELECT * FROM ingest_log ORDER BY capture_id").fetchall()
        return [dict(row) for row in rows]
