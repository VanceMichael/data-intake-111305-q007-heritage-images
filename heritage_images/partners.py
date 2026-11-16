"""合作方配置、公开范围与信任规则。

合作源（网关信封里的 ``source``）与 ActivityPub actor 的对应关系完全由本地
配置决定，不凭活动自报身份建立信任。本模块只做只读判定，不发起任何网络请求，
也不解析或抓取 ``@context`` 等远端 JSON-LD 文档。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PUBLIC_COLLECTION = "https://www.w3.org/ns/activitystreams#Public"
ADDRESS_FIELDS = ("to", "cc", "bto", "bcc")


def normalize_identifier(value: Any) -> str | None:
    """把 actor/对象/活动标识规范化为绝对 URL；不合格则返回 None。"""
    if not isinstance(value, str):
        return None
    identifier = value.strip()
    if not identifier:
        return None
    parsed = urlsplit(identifier)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return identifier


def registered_domain(identifier: str) -> str | None:
    """提取标识的主机域，作为对象归属的可核对口径。"""
    parsed = urlsplit(identifier)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return parsed.netloc.lower()


def _addresses(node: dict[str, Any]) -> list[str]:
    result = []
    for field in ADDRESS_FIELDS:
        value = node.get(field)
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, list):
            result.extend(item for item in value if isinstance(item, str))
    return result


def is_private(node: dict[str, Any]) -> bool:
    """盲收件字段出现即视为私有；公开集合不在公开寻址字段中也视为私有。

    完全不带寻址字段的节点不在这里判定为私有，由调用方按场景决定是否接收。
    """
    for blind_field in ("bto", "bcc"):
        value = node.get(blind_field)
        if value not in (None, [], ""):
            return True
    for field in ("to", "cc"):
        value = node.get(field)
        if value in (None, [], ""):
            continue
        addresses = [value] if isinstance(value, str) else value
        if isinstance(addresses, list) and any(
            isinstance(item, str) and item.strip() == PUBLIC_COLLECTION for item in addresses
        ):
            return False
        # 有明确收件人但没有公开集合：私有投递。
        return True
    return False


def is_public(node: dict[str, Any]) -> bool:
    """仅当公开集合出现在 to/cc（含被转发的内嵌对象）时才算公开内容。"""
    for field in ("to", "cc"):
        value = node.get(field)
        addresses = [value] if isinstance(value, str) else value
        if isinstance(addresses, list) and any(
            isinstance(item, str) and item.strip() == PUBLIC_COLLECTION for item in addresses
        ):
            return True
    return False


@dataclass(frozen=True)
class Partner:
    source: str
    name: str
    actors: frozenset[str]
    domains: frozenset[str]

    def trusts_actor(self, actor: str) -> bool:
        return actor in self.actors

    def owns_identifier(self, identifier: str) -> bool:
        domain = registered_domain(identifier)
        return domain is not None and domain in self.domains


class PartnerDirectory:
    """已配置合作方的只读名录；未配置的 source、actor、域一律不信任。"""

    def __init__(self, partners: list[Partner]):
        if not partners:
            raise ValueError("at least one partner is required")
        sources = [partner.source for partner in partners]
        if len(sources) != len(set(sources)):
            raise ValueError("duplicate partner source in configuration")
        self._partners = {partner.source: partner for partner in partners}

    @classmethod
    def load(cls, path: str | Path) -> "PartnerDirectory":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_partners = payload.get("partners")
        if not isinstance(raw_partners, list) or not raw_partners:
            raise ValueError("partner configuration must list at least one partner")
        partners = []
        for raw in raw_partners:
            if not isinstance(raw, dict):
                raise ValueError("each partner must be an object")
            source = str(raw.get("source", "")).strip()
            name = str(raw.get("name", "")).strip()
            actors = raw.get("actors")
            domains = raw.get("domains")
            if not source or not name or not isinstance(actors, list) or not isinstance(domains, list):
                raise ValueError("partner requires source, name, actors and domains")
            actor_ids, domain_ids = set(), set()
            for actor in actors:
                identifier = normalize_identifier(actor)
                if identifier is None:
                    raise ValueError("invalid actor identifier in configuration: " + repr(actor))
                actor_ids.add(identifier)
            for domain in domains:
                if not isinstance(domain, str) or not domain.strip():
                    raise ValueError("invalid partner domain: " + repr(domain))
                domain_ids.add(domain.strip().lower())
            if not actor_ids or not domain_ids:
                raise ValueError("partner requires at least one actor and one domain")
            partners.append(Partner(source, name, frozenset(actor_ids), frozenset(domain_ids)))
        return cls(partners)

    def partner_for(self, source: str) -> Partner | None:
        return self._partners.get(source)

    def trusts(self, source: str, actor: str) -> bool:
        partner = self._partners.get(source)
        return partner is not None and partner.trusts_actor(actor)

    def is_configured_domain(self, identifier: str) -> bool:
        domain = registered_domain(identifier)
        return domain is not None and any(domain in partner.domains for partner in self._partners.values())

    @property
    def sources(self) -> list[str]:
        return list(self._partners)
