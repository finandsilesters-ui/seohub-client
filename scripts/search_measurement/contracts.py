from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit

SEMANTIC_CORE_CONTRACT = "semantic_core_v1"
SEARCH_PROFILE_CONTRACT = "search_profile_v1"
SEARCH_RUN_CONTRACT = "search_run_v1"
RANK_OBSERVATION_CONTRACT = "rank_observation_v1"
SERP_STATE_CONTRACT = "serp_state_v1"
SERP_CHANGE_CONTRACT = "serp_change_v1"

RANK_STATUSES = {"found", "not_found_within_depth", "source_empty", "source_pending", "partial", "source_error", "unusable"}
CHANGE_TYPES = {
    "competitor_entered_top_n",
    "competitor_left_top_n",
    "competitor_position_changed",
    "our_position_changed",
    "our_ranking_url_changed",
    "title_changed",
    "snippet_changed",
    "serp_feature_appeared",
    "serp_feature_disappeared",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LANG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class SearchMeasurementError(ValueError):
    pass


def _fail(where: str, message: str) -> None:
    raise SearchMeasurementError(f"{where}: {message}")


def _obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    return value


def _fields(value: dict[str, Any], required: set[str], optional: set[str], where: str) -> None:
    missing = required - value.keys()
    if missing:
        _fail(where, f"missing fields: {', '.join(sorted(missing))}")
    unknown = value.keys() - required - optional
    if unknown:
        _fail(where, f"unknown fields: {', '.join(sorted(unknown))}")


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(where, "must be a non-empty string")
    return value


def _id(value: Any, where: str) -> str:
    value = _str(value, where)
    if not _ID.fullmatch(value):
        _fail(where, "contains unsupported characters")
    return value


def _token(value: Any, where: str) -> str:
    value = _str(value, where)
    if not _TOKEN.fullmatch(value):
        _fail(where, "must be a lowercase stable token")
    return value


def _time(value: Any, where: str, allow_date: bool = False) -> datetime:
    value = _str(value, where)
    if allow_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail(where, f"invalid ISO 8601 datetime: {exc}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(where, "datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _url(value: Any, where: str) -> str:
    value = _str(value, where)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        _fail(where, "must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment:
        _fail(where, "must not contain credentials or a fragment")
    return value


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def text_hash(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("text_hash expects a string")
    return hashlib.sha256(value.encode()).hexdigest()


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail(where, "must be a lowercase SHA-256 hex digest")
    return value


def validate_semantic_core(core: dict[str, Any]) -> dict[str, Any]:
    core = deepcopy(_obj(core, "semantic core"))
    _fields(
        core,
        {"contract", "project_id", "core_version", "effective_at", "keywords"},
        set(),
        "semantic core",
    )
    if core["contract"] != SEMANTIC_CORE_CONTRACT:
        _fail("semantic core.contract", f"must be {SEMANTIC_CORE_CONTRACT}")
    _id(core["project_id"], "semantic core.project_id")
    _id(core["core_version"], "semantic core.core_version")
    effective = _time(core["effective_at"], "semantic core.effective_at", True)
    if not isinstance(core["keywords"], list):
        _fail("semantic core.keywords", "must be an array")
    seen: set[str] = set()
    for i, keyword in enumerate(core["keywords"]):
        where = f"semantic core.keywords[{i}]"
        keyword = _obj(keyword, where)
        _fields(
            keyword,
            {"keyword_id", "query", "group", "status", "activated_at"},
            {"subgroup", "target_url", "deactivated_at"},
            where,
        )
        keyword_id = _id(keyword["keyword_id"], f"{where}.keyword_id")
        if keyword_id in seen:
            _fail("semantic core.keywords", f"duplicate keyword_id {keyword_id}")
        seen.add(keyword_id)
        _str(keyword["query"], f"{where}.query")
        _str(keyword["group"], f"{where}.group")
        if keyword.get("subgroup") is not None:
            _str(keyword["subgroup"], f"{where}.subgroup")
        if keyword.get("target_url") is not None:
            _url(keyword["target_url"], f"{where}.target_url")
        activated = _time(keyword["activated_at"], f"{where}.activated_at", True)
        if activated > effective:
            _fail(where, "activated_at cannot be after effective_at")
        if keyword["status"] == "active":
            if keyword.get("deactivated_at") is not None:
                _fail(where, "active keyword must not have deactivated_at")
        elif keyword["status"] == "inactive":
            if keyword.get("deactivated_at") is None:
                _fail(where, "inactive keyword requires deactivated_at")
            deactivated = _time(keyword["deactivated_at"], f"{where}.deactivated_at", True)
            if deactivated < activated or deactivated > effective:
                _fail(where, "invalid deactivated_at")
        else:
            _fail(f"{where}.status", "must be active or inactive")
    return core


def validate_semantic_core_transition(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    previous = validate_semantic_core(previous)
    current = validate_semantic_core(current)
    if previous["project_id"] != current["project_id"]:
        _fail("semantic core transition", "project_id cannot change")
    if previous["core_version"] == current["core_version"]:
        _fail("semantic core transition", "core_version must change")
    if _time(current["effective_at"], "current.effective_at", True) <= _time(
        previous["effective_at"], "previous.effective_at", True
    ):
        _fail("semantic core transition", "effective_at must increase")
    before = {x["keyword_id"]: x for x in previous["keywords"]}
    after = {x["keyword_id"]: x for x in current["keywords"]}
    removed = before.keys() - after.keys()
    if removed:
        _fail("semantic core transition", "removed keywords must be retained as inactive")
    for keyword_id, old in before.items():
        new = after[keyword_id]
        if old["query"] != new["query"]:
            _fail("semantic core transition", f"query is immutable for {keyword_id}")
        if old["activated_at"] != new["activated_at"]:
            _fail("semantic core transition", f"activated_at is immutable for {keyword_id}")
    return current


def validate_search_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile = deepcopy(_obj(profile, "search profile"))
    _fields(
        profile,
        {
            "contract", "profile_id", "search_engine", "provider", "provider_mode",
            "region", "device", "rank_depth", "detailed_serp_capture_depth",
        },
        {"language", "rank_parameters", "serp_parameters"},
        "search profile",
    )
    if profile["contract"] != SEARCH_PROFILE_CONTRACT:
        _fail("search profile.contract", f"must be {SEARCH_PROFILE_CONTRACT}")
    _id(profile["profile_id"], "search profile.profile_id")
    for field in ("search_engine", "provider", "provider_mode"):
        _token(profile[field], f"search profile.{field}")
    region = _obj(profile["region"], "search profile.region")
    _fields(region, {"identity"}, {"provider_value", "label"}, "search profile.region")
    _id(region["identity"], "search profile.region.identity")
    if region.get("label") is not None:
        _str(region["label"], "search profile.region.label")
    try:
        json.dumps(region.get("provider_value"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        _fail("search profile.region.provider_value", str(exc))
    if profile["device"] not in {"desktop", "mobile", "tablet"}:
        _fail("search profile.device", "must be desktop, mobile, or tablet")
    if profile.get("language") is not None and not _LANG.fullmatch(profile["language"]):
        _fail("search profile.language", "must be a BCP47-like language tag")
    rank_depth = profile["rank_depth"]
    if isinstance(rank_depth, bool) or not isinstance(rank_depth, int) or not 1 <= rank_depth <= 1000:
        _fail("search profile.rank_depth", "must be an integer from 1 to 1000")
    detail = profile["detailed_serp_capture_depth"]
    if detail is not None and (
        isinstance(detail, bool) or not isinstance(detail, int) or not 1 <= detail <= rank_depth
    ):
        _fail("search profile.detailed_serp_capture_depth", "must be null or 1..rank_depth")
    for field in ("rank_parameters", "serp_parameters"):
        value = profile.get(field, {})
        if not isinstance(value, dict):
            _fail(f"search profile.{field}", "must be an object")
        try:
            json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            _fail(f"search profile.{field}", str(exc))
    return profile


def rank_methodology_definition(profile: dict[str, Any]) -> dict[str, Any]:
    profile = validate_search_profile(profile)
    return {
        "search_engine": profile["search_engine"],
        "provider": profile["provider"],
        "provider_mode": profile["provider_mode"],
        "region": {
            "identity": profile["region"]["identity"],
            "provider_value": profile["region"].get("provider_value"),
        },
        "device": profile["device"],
        "language": profile.get("language"),
        "rank_depth": profile["rank_depth"],
        "rank_parameters": profile.get("rank_parameters", {}),
    }


def serp_methodology_definition(profile: dict[str, Any]) -> dict[str, Any]:
    profile = validate_search_profile(profile)
    return {
        "rank_methodology": rank_methodology_definition(profile),
        "detailed_serp_capture_depth": profile["detailed_serp_capture_depth"],
        "serp_parameters": profile.get("serp_parameters", {}),
    }


def rank_methodology_identity(profile: dict[str, Any]) -> str:
    return _hash(rank_methodology_definition(profile))


def serp_methodology_identity(profile: dict[str, Any]) -> str:
    return _hash(serp_methodology_definition(profile))


def compare_profiles(left: dict[str, Any], right: dict[str, Any], scope: str = "rank") -> dict[str, Any]:
    if scope == "rank":
        a, b = rank_methodology_definition(left), rank_methodology_definition(right)
    elif scope == "serp":
        a, b = serp_methodology_definition(left), serp_methodology_definition(right)
    else:
        _fail("profile comparison.scope", "must be rank or serp")
    different = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    return {
        "scope": scope,
        "compatible": _hash(a) == _hash(b),
        "left_identity": _hash(a),
        "right_identity": _hash(b),
        "differing_fields": different,
    }


def validate_run(run: dict[str, Any]) -> dict[str, Any]:
    run = deepcopy(_obj(run, "search run"))
    _fields(
        run,
        {
            "contract", "run_id", "profile_id", "core_version",
            "rank_methodology_identity", "serp_methodology_identity",
            "started_at", "collected_at", "status",
        },
        {"source_reference"},
        "search run",
    )
    if run["contract"] != SEARCH_RUN_CONTRACT:
        _fail("search run.contract", f"must be {SEARCH_RUN_CONTRACT}")
    for field in ("run_id", "profile_id", "core_version"):
        _id(run[field], f"search run.{field}")
    _digest(run["rank_methodology_identity"], "search run.rank_methodology_identity")
    if run["serp_methodology_identity"] is not None:
        _digest(run["serp_methodology_identity"], "search run.serp_methodology_identity")
    if _time(run["collected_at"], "search run.collected_at") < _time(
        run["started_at"], "search run.started_at"
    ):
        _fail("search run", "collected_at cannot be before started_at")
    if run["status"] not in {"complete", "partial", "source_pending", "source_error", "unusable"}:
        _fail("search run.status", "invalid status")
    if run.get("source_reference") is not None:
        _str(run["source_reference"], "search run.source_reference")
    return run


def validate_rank_observation(observation: dict[str, Any]) -> dict[str, Any]:
    observation = deepcopy(_obj(observation, "rank observation"))
    required = {
        "contract", "keyword_id", "profile_id", "core_version", "run_id", "status",
        "position", "ranking_url", "normalized_ranking_url", "project_results",
        "observed_at", "collected_at", "depth_checked", "methodology_identity",
        "serp_snapshot_id", "error",
    }
    _fields(observation, required, set(), "rank observation")
    if observation["contract"] != RANK_OBSERVATION_CONTRACT:
        _fail("rank observation.contract", f"must be {RANK_OBSERVATION_CONTRACT}")
    for field in ("keyword_id", "profile_id", "core_version", "run_id"):
        _id(observation[field], f"rank observation.{field}")
    if observation["serp_snapshot_id"] is not None:
        _id(observation["serp_snapshot_id"], "rank observation.serp_snapshot_id")
    _digest(observation["methodology_identity"], "rank observation.methodology_identity")
    if _time(observation["collected_at"], "rank observation.collected_at") < _time(
        observation["observed_at"], "rank observation.observed_at"
    ):
        _fail("rank observation", "collected_at cannot be before observed_at")
    depth = observation["depth_checked"]
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        _fail("rank observation.depth_checked", "must be a non-negative integer")
    status = observation["status"]
    if status not in RANK_STATUSES:
        _fail("rank observation.status", "invalid status")
    if not isinstance(observation["project_results"], list):
        _fail("rank observation.project_results", "must be an array")

    if status == "found":
        position = observation["position"]
        if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= depth:
            _fail("rank observation.position", "must be within depth_checked")
        _url(observation["ranking_url"], "rank observation.ranking_url")
        _url(observation["normalized_ranking_url"], "rank observation.normalized_ranking_url")
        results = observation["project_results"]
        if not results:
            _fail("rank observation.project_results", "found requires at least one project result")
        parsed = []
        for i, item in enumerate(results):
            where = f"rank observation.project_results[{i}]"
            item = _obj(item, where)
            _fields(item, {"position", "url", "normalized_url"}, set(), where)
            pos = item["position"]
            if isinstance(pos, bool) or not isinstance(pos, int) or not 1 <= pos <= depth:
                _fail(f"{where}.position", "must be within depth_checked")
            _url(item["url"], f"{where}.url")
            _url(item["normalized_url"], f"{where}.normalized_url")
            parsed.append(item)
        positions = [x["position"] for x in parsed]
        if positions != sorted(positions):
            _fail("rank observation.project_results", "positions must be sorted; equal provider rank slots are allowed")
        first = parsed[0]
        if (
            first["position"] != observation["position"]
            or first["url"] != observation["ranking_url"]
            or first["normalized_url"] != observation["normalized_ranking_url"]
        ):
            _fail("rank observation", "headline rank must equal the best project organic result")
        if observation["error"] is not None:
            _fail("rank observation.error", "found must not carry an error")
    elif status == "not_found_within_depth":
        if depth < 1:
            _fail("rank observation.depth_checked", "not_found requires positive checked depth")
        if any(observation[x] is not None for x in ("position", "ranking_url", "normalized_ranking_url")):
            _fail("rank observation", "not_found requires null position and ranking URLs")
        if observation["project_results"] or observation["error"] is not None:
            _fail("rank observation", "not_found cannot contain project results or error")
    else:
        if any(observation[x] is not None for x in ("position", "ranking_url", "normalized_ranking_url")):
            _fail("rank observation", f"{status} requires null rank fields")
        if observation["project_results"]:
            _fail("rank observation.project_results", f"must be empty for {status}")
        error = _obj(observation["error"], "rank observation.error")
        _fields(error, {"code"}, {"message"}, "rank observation.error")
        _token(error["code"], "rank observation.error.code")
        if error.get("message") is not None:
            _str(error["message"], "rank observation.error.message")
    return observation


def validate_observation_references(
    observation: dict[str, Any], run: dict[str, Any], profile: dict[str, Any]
) -> None:
    observation, run, profile = (
        validate_rank_observation(observation),
        validate_run(run),
        validate_search_profile(profile),
    )
    if observation["run_id"] != run["run_id"]:
        _fail("rank observation reference", "run_id does not match")
    if observation["profile_id"] != run["profile_id"] or run["profile_id"] != profile["profile_id"]:
        _fail("rank observation reference", "profile_id does not match")
    if observation["core_version"] != run["core_version"]:
        _fail("rank observation reference", "core_version does not match")
    expected = rank_methodology_identity(profile)
    if observation["methodology_identity"] != expected or run["rank_methodology_identity"] != expected:
        _fail("rank observation reference", "methodology does not match profile")


def serp_fingerprint(state: dict[str, Any]) -> str:
    reliable = state.get("feature_signal_reliable")
    if not isinstance(reliable, bool):
        _fail("SERP state.feature_signal_reliable", "must be boolean")
    results = state.get("organic_results")
    if not isinstance(results, list):
        _fail("SERP state.organic_results", "must be an array")
    payload = {
        "organic_results": [
            {
                "position": x.get("position"),
                "domain": x.get("domain"),
                "url": x.get("url"),
                "title_hash": x.get("title_hash"),
                "snippet_hash": x.get("snippet_hash"),
            }
            for x in results
        ],
        "features": sorted(set(state.get("features", []))) if reliable else None,
    }
    return _hash(payload)


def validate_serp_state(state: dict[str, Any]) -> dict[str, Any]:
    state = deepcopy(_obj(state, "SERP state"))
    required = {
        "contract", "snapshot_id", "run_id", "keyword_id", "profile_id", "core_version",
        "methodology_identity", "observed_at", "collected_at", "capture_depth",
        "organic_results", "features", "feature_signal_reliable", "fingerprint",
        "raw_evidence_ref",
    }
    _fields(state, required, set(), "SERP state")
    if state["contract"] != SERP_STATE_CONTRACT:
        _fail("SERP state.contract", f"must be {SERP_STATE_CONTRACT}")
    for field in ("snapshot_id", "run_id", "keyword_id", "profile_id", "core_version"):
        _id(state[field], f"SERP state.{field}")
    _digest(state["methodology_identity"], "SERP state.methodology_identity")
    if _time(state["collected_at"], "SERP state.collected_at") < _time(
        state["observed_at"], "SERP state.observed_at"
    ):
        _fail("SERP state", "collected_at cannot be before observed_at")
    depth = state["capture_depth"]
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        _fail("SERP state.capture_depth", "must be positive")
    if not isinstance(state["organic_results"], list):
        _fail("SERP state.organic_results", "must be an array")
    positions = []
    for i, item in enumerate(state["organic_results"]):
        where = f"SERP state.organic_results[{i}]"
        item = _obj(item, where)
        _fields(
            item,
            {"position", "domain", "url", "title", "snippet", "title_hash", "snippet_hash"},
            set(),
            where,
        )
        pos = item["position"]
        if isinstance(pos, bool) or not isinstance(pos, int) or not 1 <= pos <= depth:
            _fail(f"{where}.position", "must be within capture_depth")
        positions.append(pos)
        url = _url(item["url"], f"{where}.url")
        domain = _str(item["domain"], f"{where}.domain")
        if domain != domain.lower() or domain != (urlsplit(url).hostname or "").lower():
            _fail(f"{where}.domain", "must be lowercase and match URL hostname")
        for field in ("title", "snippet"):
            value, digest = item[field], item[f"{field}_hash"]
            if value is None:
                if digest is not None:
                    _fail(f"{where}.{field}_hash", "must be null with null text")
            elif not isinstance(value, str) or digest != text_hash(value):
                _fail(f"{where}.{field}_hash", f"does not match {field}")
    if positions != sorted(positions):
        _fail("SERP state.organic_results", "positions must be sorted; equal provider rank slots are allowed")
    if not isinstance(state["features"], list) or not all(isinstance(x, str) and x for x in state["features"]):
        _fail("SERP state.features", "must be an array of strings")
    if len(set(state["features"])) != len(state["features"]):
        _fail("SERP state.features", "must not contain duplicates")
    if not isinstance(state["feature_signal_reliable"], bool):
        _fail("SERP state.feature_signal_reliable", "must be boolean")
    if not state["feature_signal_reliable"] and state["features"]:
        _fail("SERP state.features", "must be empty when feature signal is unreliable")
    _digest(state["fingerprint"], "SERP state.fingerprint")
    if state["fingerprint"] != serp_fingerprint(state):
        _fail("SERP state.fingerprint", "does not match normalized SERP content")
    if state["raw_evidence_ref"] is not None:
        _str(state["raw_evidence_ref"], "SERP state.raw_evidence_ref")
    return state


def validate_serp_references(state: dict[str, Any], run: dict[str, Any], profile: dict[str, Any]) -> None:
    state, run, profile = validate_serp_state(state), validate_run(run), validate_search_profile(profile)
    if state["run_id"] != run["run_id"]:
        _fail("SERP state reference", "run_id does not match")
    if state["profile_id"] != run["profile_id"] or run["profile_id"] != profile["profile_id"]:
        _fail("SERP state reference", "profile_id does not match")
    if state["core_version"] != run["core_version"]:
        _fail("SERP state reference", "core_version does not match")
    expected = serp_methodology_identity(profile)
    if state["methodology_identity"] != expected or run["serp_methodology_identity"] != expected:
        _fail("SERP state reference", "methodology does not match profile")


def validate_serp_change(change: dict[str, Any]) -> dict[str, Any]:
    change = deepcopy(_obj(change, "SERP change"))
    required = {
        "contract", "change_id", "keyword_id", "profile_id", "from_snapshot_id",
        "to_snapshot_id", "detected_at", "change_type", "subject", "previous",
        "current", "rule",
    }
    _fields(change, required, set(), "SERP change")
    if change["contract"] != SERP_CHANGE_CONTRACT:
        _fail("SERP change.contract", f"must be {SERP_CHANGE_CONTRACT}")
    for field in ("change_id", "keyword_id", "profile_id", "from_snapshot_id", "to_snapshot_id"):
        _id(change[field], f"SERP change.{field}")
    if change["from_snapshot_id"] == change["to_snapshot_id"]:
        _fail("SERP change", "snapshot ids must differ")
    _time(change["detected_at"], "SERP change.detected_at")
    kind = change["change_type"]
    if kind not in CHANGE_TYPES:
        _fail("SERP change.change_type", "invalid change type")
    subject = _obj(change["subject"], "SERP change.subject")
    rule = _obj(change["rule"], "SERP change.rule")
    previous, current = change["previous"], change["current"]

    if kind.startswith("competitor_"):
        _fields(subject, {"type", "value", "classification"}, set(), "SERP change.subject")
        if subject["type"] != "domain" or subject["classification"] != "competitor":
            _fail("SERP change.subject", "competitor change requires competitor domain")
        top_n = rule.get("top_n")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
            _fail("SERP change.rule.top_n", "must be positive")
        if kind == "competitor_entered_top_n":
            if previous is not None or not isinstance(current, int) or not 1 <= current <= top_n:
                _fail("SERP change", "entered change must be null -> TOP-N position")
        elif kind == "competitor_left_top_n":
            if not isinstance(previous, int) or not 1 <= previous <= top_n or current is not None:
                _fail("SERP change", "left change must be TOP-N position -> null")
        else:
            threshold = rule.get("position_delta_threshold")
            if (
                isinstance(threshold, bool)
                or not isinstance(threshold, int)
                or threshold < 1
                or not isinstance(previous, int)
                or not isinstance(current, int)
                or abs(previous - current) < threshold
            ):
                _fail("SERP change", "position delta is below the declared material threshold")
    elif kind == "our_ranking_url_changed":
        if _url(previous, "SERP change.previous") == _url(current, "SERP change.current"):
            _fail("SERP change", "ranking URL did not change")
    elif kind in {"title_changed", "snippet_changed"}:
        _digest(previous, "SERP change.previous")
        _digest(current, "SERP change.current")
        if previous == current:
            _fail("SERP change", "content hash did not change")
    elif kind in {"serp_feature_appeared", "serp_feature_disappeared"}:
        if rule.get("structured_signal_reliable") is not True:
            _fail("SERP change", "feature changes require a reliable structured provider signal")
        expected = (False, True) if kind.endswith("appeared") else (True, False)
        if (previous, current) != expected:
            _fail("SERP change", "invalid feature transition")
    elif previous == current:
        _fail("SERP change", "previous and current must differ")
    return change
