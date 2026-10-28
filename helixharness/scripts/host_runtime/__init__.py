"""HelixHarness 宿主无关的发现与能力契约。

Registry 不感知任何具体宿主 ID。增加一个符合版本化契约的
``hosts/<host-id>/host.json`` 清单即可安装宿主。
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import eval_json


HOST_API_VERSION = "1.1"
SUPPORTED_HOST_API_VERSIONS = frozenset({HOST_API_VERSION})
HOST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CAPABILITY_LEVELS = frozenset({"native", "emulated", "unsupported"})
SUPPORT_LEVELS = frozenset({"stable", "preview"})
ADAPTER_REFERENCE_KINDS = frozenset({"python", "resource"})
PYTHON_NAME_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
MANIFEST_FIELDS = frozenset(
    {"host_id", "host_api_version", "aliases", "capabilities", "adapters", "support"}
)
SUPPORT_FIELDS = frozenset({"level", "display_name", "evidence", "limitations"})
REQUIRED_RESOURCE_ADAPTERS = frozenset(
    {
        "distribution",
        "lifecycle",
        "project_config",
        "runtime",
        "subagent_protocol",
        "telemetry_protocol",
        "ut",
        "workflow_transition",
    }
)
REQUIRED_RESOURCE_ADAPTERS = REQUIRED_RESOURCE_ADAPTERS | frozenset(
    {"command_projection", "installer", "project_hooks"}
)
CAPABILITY_ADAPTER_REQUIREMENTS = {
    "plugin_update": {"update": "python", "version": "python"},
    "subagents": {"subagent_protocol": "resource"},
    "telemetry": {"telemetry": "python", "version": "python"},
    "unit_test_workflow": {"ut": "resource"},
    "workflow_transition": {"workflow_transition": "resource"},
}
CAPABILITY_ADAPTER_REQUIREMENTS = {
    **CAPABILITY_ADAPTER_REQUIREMENTS,
    "command_surface": {"command_projection": "resource"},
    "plugin_install": {"installer": "resource"},
    "project_hooks": {"project_hooks": "resource"},
}


class HostContractError(ValueError):
    """宿主包违反公共 Host API 契约时抛出。"""


@dataclass(frozen=True)
class AdapterReference:
    """宿主清单中一个已经校验并确定类型的适配器引用。"""

    kind: str
    target: str


def _adapter_reference(value: Any, label: str) -> AdapterReference:
    raw = str(value or "").strip()
    if not raw or len(raw) > 256 or ":" not in raw:
        raise HostContractError(f"适配器引用无效: {label}")
    kind, target = raw.split(":", 1)
    if kind not in ADAPTER_REFERENCE_KINDS:
        raise HostContractError(f"适配器引用协议不支持: {label}: {kind}")
    if not target:
        raise HostContractError(f"适配器引用无效: {label}")
    if kind == "python":
        parts = target.split(":", 1)
        if len(parts) != 2 or not all(PYTHON_NAME_PATTERN.fullmatch(part) for part in parts):
            raise HostContractError(f"Python 适配器引用无效: {label}")
    return AdapterReference(kind=kind, target=target)


def _host_id(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not HOST_ID_PATTERN.fullmatch(normalized):
        raise HostContractError(f"{label} 无效: {normalized or '<empty>'}")
    return normalized


@dataclass(frozen=True)
class HostManifest:
    """一个已经校验且不可变的已安装 Host Kit 描述。"""

    host_id: str
    host_api_version: str
    aliases: tuple[str, ...]
    capabilities: dict[str, str]
    adapters: dict[str, str]
    support_level: str
    display_name: str
    evidence: str | None
    limitations: tuple[str, ...]
    root: Path

    def capability(self, name: str) -> str:
        """返回能力等级，不虚构宿主专属回退行为。"""

        return self.capabilities.get(name, "unsupported")

    def adapter_reference(self, name: str) -> AdapterReference | None:
        raw = self.adapters.get(name)
        return _adapter_reference(raw, f"{self.host_id}.{name}") if raw else None

    def require_adapter_reference(self, name: str) -> AdapterReference:
        reference = self.adapter_reference(name)
        if reference is None:
            raise HostContractError(f"宿主适配器未声明: {self.host_id}.{name}")
        return reference

    def load_python_adapter(self, name: str) -> Any:
        """不通过中心宿主分支，直接加载清单声明的适配器对象。"""

        reference = self.require_adapter_reference(name)
        if reference.kind != "python":
            raise HostContractError(
                f"宿主适配器不是 Python 引用: {self.host_id}.{name}"
            )
        module_name, attribute_path = reference.target.split(":", 1)
        try:
            value: Any = importlib.import_module(module_name)
            for attribute in attribute_path.split("."):
                value = getattr(value, attribute)
        except (ImportError, AttributeError) as exc:
            raise HostContractError(
                f"无法加载 Python 适配器: {self.host_id}.{name}"
            ) from exc
        return value

    def resolve_resource_adapter(self, name: str) -> Path:
        """解析插件相对资源，同时阻止路径穿越。"""

        reference = self.require_adapter_reference(name)
        if reference.kind != "resource":
            raise HostContractError(
                f"宿主适配器不是资源引用: {self.host_id}.{name}"
            )
        relative = Path(reference.target)
        if relative.is_absolute():
            raise HostContractError(
                f"适配器资源越过插件根目录: {self.host_id}.{name}"
            )
        plugin_root = self.root.parent.parent.resolve()
        resolved = (plugin_root / relative).resolve()
        try:
            resolved.relative_to(plugin_root)
        except ValueError as exc:
            raise HostContractError(
                f"适配器资源越过插件根目录: {self.host_id}.{name}"
            ) from exc
        if not resolved.is_file():
            raise HostContractError(
                f"适配器资源不存在: {self.host_id}.{name}"
            )
        return resolved


def _load_manifest(path: Path) -> HostManifest:
    try:
        payload = eval_json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HostContractError(f"无法读取宿主清单: {path}") from exc
    if not isinstance(payload, dict):
        raise HostContractError(f"宿主清单必须是对象: {path}")
    host_id = _host_id(payload.get("host_id"), "宿主 ID")
    if host_id != path.parent.name:
        raise HostContractError(
            f"宿主 ID 必须与目录名一致: {host_id} != {path.parent.name}"
        )

    api_version = str(payload.get("host_api_version") or "").strip()
    if api_version not in SUPPORTED_HOST_API_VERSIONS:
        raise HostContractError(
            f"宿主 {host_id} 的 Host API 版本不受支持: {api_version or '<empty>'}"
        )
    unknown_fields = sorted(set(payload) - MANIFEST_FIELDS)
    if unknown_fields:
        raise HostContractError(
            f"宿主清单包含未知字段: {path}: {', '.join(unknown_fields)}"
        )

    raw_aliases = payload.get("aliases")
    if not isinstance(raw_aliases, list):
        raise HostContractError(f"宿主别名必须是数组: {host_id}")
    aliases: list[str] = []
    for value in raw_aliases:
        alias = _host_id(value, "宿主别名")
        if alias == host_id or alias in aliases:
            raise HostContractError(f"宿主 {host_id} 的别名重复: {alias}")
        aliases.append(alias)

    raw_capabilities = payload.get("capabilities")
    if not isinstance(raw_capabilities, dict):
        raise HostContractError(f"宿主能力必须是对象: {host_id}")
    capabilities: dict[str, str] = {}
    for raw_name, raw_level in raw_capabilities.items():
        name = _host_id(raw_name, "能力名称")
        level = str(raw_level or "").strip().lower()
        if level not in CAPABILITY_LEVELS:
            raise HostContractError(
                f"{host_id}.{name} 的能力等级无效: {level or '<empty>'}"
            )
        capabilities[name] = level

    raw_adapters = payload.get("adapters")
    if not isinstance(raw_adapters, dict):
        raise HostContractError(f"宿主适配器必须是对象: {host_id}")
    adapters: dict[str, str] = {}
    for raw_name, raw_reference in raw_adapters.items():
        name = _host_id(raw_name, "适配器名称")
        reference = _adapter_reference(raw_reference, f"{host_id}.{name}")
        adapters[name] = f"{reference.kind}:{reference.target}"

    raw_support = payload.get("support")
    if raw_support is None:
        support_level = "preview"
        display_name = host_id
        evidence = None
        limitations: tuple[str, ...] = ()
    else:
        if not isinstance(raw_support, dict):
            raise HostContractError(f"宿主支持声明必须是对象: {host_id}")
        unknown_support_fields = sorted(set(raw_support) - SUPPORT_FIELDS)
        if unknown_support_fields:
            raise HostContractError(
                f"宿主支持声明包含未知字段: {host_id}: "
                f"{', '.join(unknown_support_fields)}"
            )
        missing_support_fields = sorted(SUPPORT_FIELDS - set(raw_support))
        if missing_support_fields:
            raise HostContractError(
                f"宿主支持声明缺少字段: {host_id}: "
                f"{', '.join(missing_support_fields)}"
            )
        support_level = str(raw_support.get("level") or "").strip().lower()
        if support_level not in SUPPORT_LEVELS:
            raise HostContractError(
                f"宿主 {host_id} 的支持等级无效: {support_level or '<empty>'}"
            )
        display_name = str(raw_support.get("display_name") or "").strip()
        if not display_name or len(display_name) > 80:
            raise HostContractError(f"宿主 {host_id} 的展示名称无效")
        raw_evidence = raw_support.get("evidence")
        evidence = str(raw_evidence).strip() if raw_evidence is not None else None
        if evidence == "" or (evidence is not None and len(evidence) > 256):
            raise HostContractError(f"宿主 {host_id} 的 E2E 证据引用无效")
        if evidence is not None:
            evidence_path = Path(evidence)
            expected_prefix = ("evidence", "host-e2e", host_id)
            if (
                evidence_path.is_absolute()
                or "\\" in evidence
                or any(part in {"", ".", ".."} for part in evidence_path.parts)
                or evidence_path.suffix != ".json"
                or evidence_path.parts[:3] != expected_prefix
            ):
                raise HostContractError(f"宿主 {host_id} 的 E2E 证据引用无效")
        raw_limitations = raw_support.get("limitations", [])
        if not isinstance(raw_limitations, list):
            raise HostContractError(f"宿主 {host_id} 的限制声明必须是数组")
        normalized_limitations: list[str] = []
        for item in raw_limitations:
            limitation = str(item or "").strip()
            if not limitation or len(limitation) > 300:
                raise HostContractError(f"宿主 {host_id} 的限制声明无效")
            normalized_limitations.append(limitation)
        limitations = tuple(normalized_limitations)

    required_resources = REQUIRED_RESOURCE_ADAPTERS
    missing_resources = sorted(required_resources - set(adapters))
    if missing_resources:
        raise HostContractError(
            f"宿主 {host_id} 缺少必需资源适配器: {', '.join(missing_resources)}"
        )
    for name in sorted(required_resources):
        if _adapter_reference(adapters[name], f"{host_id}.{name}").kind != "resource":
            raise HostContractError(
                f"宿主 {host_id} 的必需资源适配器类型无效: {name}"
            )

    capability_adapter_requirements = CAPABILITY_ADAPTER_REQUIREMENTS
    for capability, required_adapters in capability_adapter_requirements.items():
        if capabilities.get(capability, "unsupported") == "unsupported":
            continue
        missing = sorted(set(required_adapters) - set(adapters))
        if missing:
            raise HostContractError(
                f"宿主 {host_id} 的 {capability} 能力缺少适配器: {', '.join(missing)}"
            )
        invalid_kinds = [
            f"{name}（期望 {expected_kind}，实际 "
            f"{_adapter_reference(adapters[name], f'{host_id}.{name}').kind}）"
            for name, expected_kind in required_adapters.items()
            if _adapter_reference(adapters[name], f"{host_id}.{name}").kind
            != expected_kind
        ]
        if invalid_kinds:
            raise HostContractError(
                f"宿主 {host_id} 的 {capability} 能力适配器类型无效: "
                f"{', '.join(invalid_kinds)}"
            )

    manifest = HostManifest(
        host_id=host_id,
        host_api_version=api_version,
        aliases=tuple(aliases),
        capabilities=capabilities,
        adapters=adapters,
        support_level=support_level,
        display_name=display_name,
        evidence=evidence,
        limitations=limitations,
        root=path.parent.resolve(),
    )
    for name in sorted(adapters):
        reference = manifest.require_adapter_reference(name)
        if reference.kind == "resource":
            manifest.resolve_resource_adapter(name)
    from .command_projection import load_command_projection
    from .project_hooks import load_project_hooks

    load_command_projection(manifest)
    load_project_hooks(manifest)
    return manifest


class HostRegistry:
    """清单驱动、显式识别且关闭失败的宿主 Registry。"""

    def __init__(self, manifests: tuple[HostManifest, ...]) -> None:
        self._hosts: dict[str, HostManifest] = {}
        self._aliases: dict[str, str] = {}
        for manifest in manifests:
            if manifest.host_id in self._hosts or manifest.host_id in self._aliases:
                raise HostContractError(f"宿主 ID 重复: {manifest.host_id}")
            self._hosts[manifest.host_id] = manifest
            for alias in manifest.aliases:
                if alias in self._hosts or alias in self._aliases:
                    raise HostContractError(f"宿主别名重复: {alias}")
                self._aliases[alias] = manifest.host_id

    @classmethod
    def discover(cls, hosts_root: Path) -> "HostRegistry":
        root = hosts_root.expanduser().resolve()
        if not root.is_dir():
            raise HostContractError(f"宿主根路径不是目录: {root}")
        manifests = tuple(
            _load_manifest(path)
            for path in sorted(root.glob("*/host.json"), key=lambda item: item.parent.name)
        )
        return cls(manifests)

    def resolve_id(self, value: Any) -> str | None:
        try:
            candidate = _host_id(value, "宿主 ID")
        except HostContractError:
            return None
        if candidate in self._hosts:
            return candidate
        return self._aliases.get(candidate)

    def get(self, value: Any) -> HostManifest | None:
        host_id = self.resolve_id(value)
        return self._hosts.get(host_id) if host_id else None

    def require(self, value: Any) -> HostManifest:
        manifest = self.get(value)
        if manifest is None:
            raise HostContractError(f"宿主未注册: {value}")
        return manifest

    def supported_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._hosts))

    def manifests(self) -> tuple[HostManifest, ...]:
        return tuple(self._hosts[host_id] for host_id in sorted(self._hosts))

    def detect(self, environment: Mapping[str, str]) -> str | None:
        """选择置信度最高的清单 detector；识别冲突时关闭失败。"""

        matches: list[tuple[int, str]] = []
        for manifest in self.manifests():
            if manifest.adapter_reference("detector") is None:
                continue
            detector = manifest.load_python_adapter("detector")
            if not callable(detector):
                raise HostContractError(
                    f"宿主 detector 不可调用: {manifest.host_id}"
                )
            try:
                score = detector(environment)
            except Exception as exc:
                raise HostContractError(
                    f"宿主 detector 执行失败: {manifest.host_id}"
                ) from exc
            if score is None:
                continue
            if type(score) is not int or not 0 <= score <= 100:
                raise HostContractError(
                    f"宿主 detector 分数无效: {manifest.host_id}"
                )
            matches.append((score, manifest.host_id))
        if not matches:
            return None
        matches.sort(reverse=True)
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            raise HostContractError(
                f"宿主识别冲突: {matches[0][1]}, {matches[1][1]}"
            )
        return matches[0][1]


__all__ = [
    "ADAPTER_REFERENCE_KINDS",
    "CAPABILITY_LEVELS",
    "CAPABILITY_ADAPTER_REQUIREMENTS",
    "HOST_API_VERSION",
    "REQUIRED_RESOURCE_ADAPTERS",
    "SUPPORTED_HOST_API_VERSIONS",
    "SUPPORT_LEVELS",
    "AdapterReference",
    "HostContractError",
    "HostManifest",
    "HostRegistry",
]
