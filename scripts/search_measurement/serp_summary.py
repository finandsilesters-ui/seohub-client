from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from scripts.search_measurement.contracts import (
    validate_rank_observation,
    validate_serp_state,
)

KEYWORD_FACT_TYPE = "compact_keyword_serp_fact_v1"
RUN_SUMMARY_TYPE = "compact_serp_run_summary_v1"
CURRENT_SUMMARY_TYPE = "compact_serp_current_summary_v1"
DEFAULT_TOP_N = 10
DEFAULT_DOMAIN_AGGREGATE_LIMIT = 50
RANK_STATUSES = (
    "found",
    "not_found_within_depth",
    "source_empty",
    "source_pending",
    "partial",
    "source_error",
    "unusable",
)


class SerpSummaryError(ValueError):
    pass


def _normalize_domains(domains: Iterable[str] | None) -> set[str]:
    if domains is None:
        return set()
    normalized: set[str] = set()
    for value in domains:
        if not isinstance(value, str) or not value.strip():
            raise SerpSummaryError("tracked_domains must contain non-empty strings")
        domain = value.strip().lower().rstrip(".")
        if "://" in domain or "/" in domain:
            raise SerpSummaryError("tracked_domains must contain bare hostnames")
        normalized.add(domain)
    return normalized


def _url_domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _project_domains(*observations: dict[str, Any] | None) -> set[str]:
    domains: set[str] = set()
    for observation in observations:
        if not observation:
            continue
        for item in observation.get("project_results", []):
            domain = _url_domain(item["url"])
            if domain:
                domains.add(domain)
    return domains


def _validate_state_for_observation(
    observation: dict[str, Any],
    state: dict[str, Any] | None,
    *,
    label: str,
) -> dict[str, Any] | None:
    if state is None:
        return None
    state = validate_serp_state(state)
    for field in ("run_id", "keyword_id", "profile_id", "core_version"):
        if state[field] != observation[field]:
            raise SerpSummaryError(f"{label} SERP state {field} does not match rank observation")
    if observation["serp_snapshot_id"] != state["snapshot_id"]:
        raise SerpSummaryError(f"{label} SERP state snapshot_id is not linked by rank observation")
    return state


def _project_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "position": item["position"],
        "url": item["url"],
        "normalized_url": item["normalized_url"],
    }


def _top_domain_composition(state: dict[str, Any] | None, top_n: int) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "requested_top_n": top_n,
        "complete": state["capture_depth"] >= top_n,
        "results": [
            {"position": item["position"], "domain": item["domain"]}
            for item in state["organic_results"]
            if item["position"] <= top_n
        ],
    }


def _rank_comparison(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    if previous is None:
        return {
            "comparison": "no_baseline",
            "movement": None,
            "previous_position": None,
            "position_delta": None,
            "ranking_url_switched": None,
        }

    if current["methodology_identity"] != previous["methodology_identity"]:
        return {
            "comparison": "methodology_mismatch",
            "movement": None,
            "previous_position": None,
            "position_delta": None,
            "ranking_url_switched": None,
        }

    current_status = current["status"]
    previous_status = previous["status"]
    rankable = {"found", "not_found_within_depth"}
    if current_status not in rankable or previous_status not in rankable:
        return {
            "comparison": "measurement_status_not_comparable",
            "movement": None,
            "previous_position": previous["position"] if previous_status == "found" else None,
            "position_delta": None,
            "ranking_url_switched": None,
        }

    if previous_status == "found" and current_status == "found":
        previous_position = previous["position"]
        current_position = current["position"]
        if current_position < previous_position:
            movement = "moved_up"
        elif current_position > previous_position:
            movement = "moved_down"
        else:
            movement = "stable"
        return {
            "comparison": "comparable",
            "movement": movement,
            "previous_position": previous_position,
            "position_delta": current_position - previous_position,
            "ranking_url_switched": (
                current["normalized_ranking_url"] != previous["normalized_ranking_url"]
            ),
        }

    if previous_status == "not_found_within_depth" and current_status == "found":
        return {
            "comparison": "comparable",
            "movement": "entered",
            "previous_position": None,
            "position_delta": None,
            "ranking_url_switched": None,
        }

    if previous_status == "found" and current_status == "not_found_within_depth":
        return {
            "comparison": "comparable",
            "movement": "left",
            "previous_position": previous["position"],
            "position_delta": None,
            "ranking_url_switched": None,
        }

    return {
        "comparison": "comparable",
        "movement": "not_ranked_both",
        "previous_position": None,
        "position_delta": None,
        "ranking_url_switched": None,
    }


def _tracked_signature(
    state: dict[str, Any],
    tracked_domains: set[str],
) -> list[tuple[Any, ...]]:
    return [
        (
            item["position"],
            item["domain"],
            item["url"],
            item["title_hash"],
            item["snippet_hash"],
        )
        for item in state["organic_results"]
        if item["domain"] in tracked_domains
    ]


def _tracked_content_changes(
    previous: dict[str, Any],
    current: dict[str, Any],
    tracked_domains: set[str],
) -> list[dict[str, Any]]:
    previous_by_url: dict[str, dict[str, Any]] = {}
    current_by_url: dict[str, dict[str, Any]] = {}
    for item in previous["organic_results"]:
        if item["domain"] in tracked_domains and item["url"] not in previous_by_url:
            previous_by_url[item["url"]] = item
    for item in current["organic_results"]:
        if item["domain"] in tracked_domains and item["url"] not in current_by_url:
            current_by_url[item["url"]] = item

    evidence: list[dict[str, Any]] = []
    for url in sorted(previous_by_url.keys() & current_by_url.keys()):
        before = previous_by_url[url]
        after = current_by_url[url]
        title_changed = before["title_hash"] != after["title_hash"]
        snippet_changed = before["snippet_hash"] != after["snippet_hash"]
        if not title_changed and not snippet_changed:
            continue
        evidence.append(
            {
                "domain": after["domain"],
                "url": url,
                "title": (
                    {"previous_hash": before["title_hash"], "current_hash": after["title_hash"]}
                    if title_changed
                    else None
                ),
                "snippet": (
                    {
                        "previous_hash": before["snippet_hash"],
                        "current_hash": after["snippet_hash"],
                    }
                    if snippet_changed
                    else None
                ),
            }
        )
    return evidence


def _serp_comparison(
    current: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    *,
    top_n: int,
    tracked_domains: set[str],
    rank_comparison: dict[str, Any],
) -> dict[str, Any]:
    if current is None:
        return {
            "comparison": "current_serp_unavailable",
            "previous_fingerprint": previous["fingerprint"] if previous else None,
            "fingerprint_changed": None,
            "top_n_domain_composition_changed": None,
            "tracked_content_changes": [],
            "materially_changed": None,
        }
    if previous is None:
        return {
            "comparison": "no_baseline",
            "previous_fingerprint": None,
            "fingerprint_changed": None,
            "top_n_domain_composition_changed": None,
            "tracked_content_changes": [],
            "materially_changed": None,
        }
    if current["methodology_identity"] != previous["methodology_identity"]:
        return {
            "comparison": "methodology_mismatch",
            "previous_fingerprint": previous["fingerprint"],
            "fingerprint_changed": None,
            "top_n_domain_composition_changed": None,
            "tracked_content_changes": [],
            "materially_changed": None,
        }

    fingerprint_changed = current["fingerprint"] != previous["fingerprint"]
    top_n_complete = current["capture_depth"] >= top_n and previous["capture_depth"] >= top_n
    top_n_changed = None
    if top_n_complete:
        top_n_changed = (
            _top_domain_composition(current, top_n)["results"]
            != _top_domain_composition(previous, top_n)["results"]
        )

    content_changes = _tracked_content_changes(previous, current, tracked_domains)
    tracked_signature_changed = (
        _tracked_signature(previous, tracked_domains)
        != _tracked_signature(current, tracked_domains)
    )
    rank_material = (
        rank_comparison["comparison"] == "comparable"
        and (
            rank_comparison["movement"]
            in {"moved_up", "moved_down", "entered", "left"}
            or rank_comparison["ranking_url_switched"] is True
        )
    )
    materially_changed = bool(
        top_n_changed is True
        or tracked_signature_changed
        or rank_material
    )

    return {
        "comparison": "comparable",
        "previous_fingerprint": previous["fingerprint"],
        "fingerprint_changed": fingerprint_changed,
        "top_n_domain_composition_changed": top_n_changed,
        "tracked_content_changes": content_changes,
        "materially_changed": materially_changed,
    }


def build_keyword_serp_fact(
    observation: dict[str, Any],
    serp_state: dict[str, Any] | None = None,
    *,
    previous_observation: dict[str, Any] | None = None,
    previous_serp_state: dict[str, Any] | None = None,
    top_n: int = DEFAULT_TOP_N,
    tracked_domains: Iterable[str] | None = None,
) -> dict[str, Any]:
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise SerpSummaryError("top_n must be a positive integer")

    current = validate_rank_observation(observation)
    current_state = _validate_state_for_observation(current, serp_state, label="current")

    previous = None
    previous_state = None
    if previous_observation is not None:
        previous = validate_rank_observation(previous_observation)
        if (
            previous["keyword_id"] != current["keyword_id"]
            or previous["profile_id"] != current["profile_id"]
        ):
            raise SerpSummaryError("previous observation must match keyword_id and profile_id")
        previous_state = _validate_state_for_observation(
            previous, previous_serp_state, label="previous"
        )
    elif previous_serp_state is not None:
        raise SerpSummaryError("previous_serp_state requires previous_observation")

    monitored_domains = _normalize_domains(tracked_domains)
    monitored_domains.update(_project_domains(current, previous))
    rank_change = _rank_comparison(current, previous)
    serp_change = _serp_comparison(
        current_state,
        previous_state,
        top_n=top_n,
        tracked_domains=monitored_domains,
        rank_comparison=rank_change,
    )

    project_result = None
    additional_project_results: list[dict[str, Any]] = []
    if current["status"] == "found":
        project_result = _project_result(current["project_results"][0])
        additional_project_results = [
            _project_result(item) for item in current["project_results"][1:]
        ]

    return {
        "type": KEYWORD_FACT_TYPE,
        "keyword_id": current["keyword_id"],
        "profile_id": current["profile_id"],
        "run_id": current["run_id"],
        "observed_at": current["observed_at"],
        "methodology": {
            "rank_identity": current["methodology_identity"],
            "serp_identity": current_state["methodology_identity"] if current_state else None,
        },
        "measurement": {
            "status": current["status"],
            "depth_checked": current["depth_checked"],
            "error_code": current["error"]["code"] if current["error"] else None,
        },
        "project_result": project_result,
        "additional_project_results": additional_project_results,
        "serp": {
            "snapshot_id": current_state["snapshot_id"] if current_state else None,
            "capture_depth": current_state["capture_depth"] if current_state else None,
            "fingerprint": current_state["fingerprint"] if current_state else None,
            "top_domain_composition": _top_domain_composition(current_state, top_n),
        },
        "change": {
            "baseline_observed_at": previous["observed_at"] if previous else None,
            "rank": rank_change,
            "serp": serp_change,
        },
    }


def _index_by_keyword(
    observations: Iterable[dict[str, Any]] | None,
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in observations or []:
        observation = validate_rank_observation(raw)
        keyword_id = observation["keyword_id"]
        if keyword_id in indexed:
            raise SerpSummaryError(f"{label} contains duplicate keyword_id {keyword_id}")
        indexed[keyword_id] = observation
    return indexed


def _index_states(
    states: Iterable[dict[str, Any]] | None,
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in states or []:
        state = validate_serp_state(raw)
        keyword_id = state["keyword_id"]
        if keyword_id in indexed:
            raise SerpSummaryError(f"{label} contains duplicate keyword_id {keyword_id}")
        indexed[keyword_id] = state
    return indexed


def _single(values: set[str], label: str) -> str:
    if len(values) != 1:
        raise SerpSummaryError(f"current run must have one {label}")
    return next(iter(values))


def _share(count: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(count / denominator, 6)


def _domain_presence(
    states: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SerpSummaryError("domain_aggregate_limit must be a positive integer")

    eligible_3 = [state for state in states.values() if state["capture_depth"] >= 3]
    eligible_10 = [state for state in states.values() if state["capture_depth"] >= 10]
    top3: Counter[str] = Counter()
    top10: Counter[str] = Counter()

    for state in eligible_3:
        top3.update({item["domain"] for item in state["organic_results"] if item["position"] <= 3})
    for state in eligible_10:
        top10.update({item["domain"] for item in state["organic_results"] if item["position"] <= 10})

    domains = sorted(set(top3) | set(top10))
    items = [
        {
            "domain": domain,
            "top_3_count": top3[domain],
            "top_3_share": _share(top3[domain], len(eligible_3)),
            "top_10_count": top10[domain],
            "top_10_share": _share(top10[domain], len(eligible_10)),
        }
        for domain in domains
    ]
    items.sort(
        key=lambda item: (
            -item["top_10_count"],
            -item["top_3_count"],
            item["domain"],
        )
    )
    returned = items[:limit]
    return {
        "top_3_eligible_keywords": len(eligible_3),
        "top_10_eligible_keywords": len(eligible_10),
        "domains_observed": len(items),
        "domains_returned": len(returned),
        "truncated": len(returned) < len(items),
        "items": returned,
    }


def build_profile_run_summary(
    observations: Iterable[dict[str, Any]],
    serp_states: Iterable[dict[str, Any]] | None = None,
    *,
    previous_observations: Iterable[dict[str, Any]] | None = None,
    previous_serp_states: Iterable[dict[str, Any]] | None = None,
    top_n: int = DEFAULT_TOP_N,
    tracked_domains: Iterable[str] | None = None,
    domain_aggregate_limit: int = DEFAULT_DOMAIN_AGGREGATE_LIMIT,
) -> dict[str, Any]:
    current = _index_by_keyword(observations, label="observations")
    if not current:
        raise SerpSummaryError("observations must not be empty")
    current_states = _index_states(serp_states, label="serp_states")
    previous = _index_by_keyword(previous_observations, label="previous_observations")
    previous_states = _index_states(previous_serp_states, label="previous_serp_states")

    current_keywords = set(current)
    extra_states = set(current_states) - current_keywords
    if extra_states:
        raise SerpSummaryError(
            f"SERP states have no matching current observation: {', '.join(sorted(extra_states))}"
        )

    profile_id = _single({item["profile_id"] for item in current.values()}, "profile_id")
    run_id = _single({item["run_id"] for item in current.values()}, "run_id")
    rank_identity = _single(
        {item["methodology_identity"] for item in current.values()},
        "rank methodology identity",
    )
    serp_identities = {item["methodology_identity"] for item in current_states.values()}
    if len(serp_identities) > 1:
        raise SerpSummaryError("current run must have one SERP methodology identity")
    serp_identity = next(iter(serp_identities)) if serp_identities else None

    for keyword_id, state in current_states.items():
        _validate_state_for_observation(current[keyword_id], state, label="current")

    status_counts = Counter(item["status"] for item in current.values())
    movement_counts = Counter()
    ranking_url_switches = 0
    materially_changed_serps = 0
    comparable_serps = 0
    fingerprint_changed = 0
    fingerprint_unchanged = 0
    methodology_mismatches = 0

    for keyword_id in sorted(current):
        fact = build_keyword_serp_fact(
            current[keyword_id],
            current_states.get(keyword_id),
            previous_observation=previous.get(keyword_id),
            previous_serp_state=previous_states.get(keyword_id),
            top_n=top_n,
            tracked_domains=tracked_domains,
        )
        rank_change = fact["change"]["rank"]
        movement = rank_change["movement"]
        keyword_methodology_mismatch = False
        if rank_change["comparison"] == "no_baseline":
            movement_counts["no_baseline"] += 1
        elif rank_change["comparison"] == "methodology_mismatch":
            movement_counts["methodology_mismatch"] += 1
            keyword_methodology_mismatch = True
        elif rank_change["comparison"] != "comparable":
            movement_counts["incomparable"] += 1
        elif movement is not None:
            movement_counts[movement] += 1

        if rank_change["ranking_url_switched"] is True:
            ranking_url_switches += 1

        serp_change = fact["change"]["serp"]
        if serp_change["comparison"] == "methodology_mismatch":
            keyword_methodology_mismatch = True
        if keyword_methodology_mismatch:
            methodology_mismatches += 1
        if serp_change["comparison"] == "comparable":
            comparable_serps += 1
            if serp_change["fingerprint_changed"]:
                fingerprint_changed += 1
            else:
                fingerprint_unchanged += 1
            if serp_change["materially_changed"]:
                materially_changed_serps += 1

    observed_values = sorted(item["observed_at"] for item in current.values())
    return {
        "type": RUN_SUMMARY_TYPE,
        "run_id": run_id,
        "profile_id": profile_id,
        "rank_methodology_identity": rank_identity,
        "serp_methodology_identity": serp_identity,
        "observed_at": {
            "from": observed_values[0],
            "to": observed_values[-1],
        },
        "keyword_counts": {
            "total": len(current),
            **{status: status_counts[status] for status in RANK_STATUSES},
        },
        "movement_counts": {
            key: movement_counts[key]
            for key in (
                "moved_up",
                "moved_down",
                "stable",
                "entered",
                "left",
                "not_ranked_both",
                "incomparable",
                "no_baseline",
                "methodology_mismatch",
            )
        },
        "ranking_url_switches": ranking_url_switches,
        "serp_change_counts": {
            "comparable": comparable_serps,
            "fingerprint_changed": fingerprint_changed,
            "fingerprint_unchanged": fingerprint_unchanged,
            "materially_changed": materially_changed_serps,
            "methodology_mismatches": methodology_mismatches,
        },
        "domain_presence": _domain_presence(
            current_states,
            limit=domain_aggregate_limit,
        ),
    }



def _utc_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SerpSummaryError(f"{label} must be a non-empty ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SerpSummaryError(f"{label} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SerpSummaryError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _keyword_universe(keyword_ids: Iterable[str]) -> set[str]:
    universe: set[str] = set()
    for keyword_id in keyword_ids:
        if not isinstance(keyword_id, str) or not keyword_id.strip():
            raise SerpSummaryError("keyword_ids must contain non-empty strings")
        if keyword_id in universe:
            raise SerpSummaryError(f"keyword_ids contains duplicate keyword_id {keyword_id}")
        universe.add(keyword_id)
    if not universe:
        raise SerpSummaryError("keyword_ids must not be empty")
    return universe


def _latest_observations(
    observations: Iterable[dict[str, Any]],
    *,
    keyword_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str, str]:
    validated = [validate_rank_observation(raw) for raw in observations]
    if not validated:
        raise SerpSummaryError("observations must not be empty")

    profiles = {item["profile_id"] for item in validated}
    if len(profiles) != 1:
        raise SerpSummaryError("current state must have one profile_id")
    profile_id = next(iter(profiles))

    rank_identities = {item["methodology_identity"] for item in validated}
    if len(rank_identities) != 1:
        raise SerpSummaryError("current state must have one rank methodology identity")
    rank_identity = next(iter(rank_identities))

    extra = {item["keyword_id"] for item in validated} - keyword_ids
    if extra:
        raise SerpSummaryError(
            "observations contain keyword IDs outside keyword universe: "
            + ", ".join(sorted(extra))
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in validated:
        grouped.setdefault(item["keyword_id"], []).append(item)

    missing = keyword_ids - grouped.keys()
    if missing:
        raise SerpSummaryError(
            "missing latest observations for keyword universe: "
            + ", ".join(sorted(missing))
        )

    latest: dict[str, dict[str, Any]] = {}
    for keyword_id in sorted(keyword_ids):
        candidates = grouped[keyword_id]
        newest_time = max(_utc_timestamp(item["collected_at"], "collected_at") for item in candidates)
        newest = [
            item
            for item in candidates
            if _utc_timestamp(item["collected_at"], "collected_at") == newest_time
        ]
        if len(newest) != 1:
            raise SerpSummaryError(
                f"duplicate latest observation for keyword_id {keyword_id}"
            )
        latest[keyword_id] = newest[0]
    return latest, validated, profile_id, rank_identity


def _states_by_snapshot(
    states: Iterable[dict[str, Any]] | None,
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in states or []:
        state = validate_serp_state(raw)
        snapshot_id = state["snapshot_id"]
        previous = indexed.get(snapshot_id)
        if previous is not None and previous != state:
            raise SerpSummaryError(f"{label} contains conflicting snapshot_id {snapshot_id}")
        indexed[snapshot_id] = state
    return indexed


def _linked_states(
    observations: dict[str, dict[str, Any]],
    states: dict[str, dict[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    linked: dict[str, dict[str, Any]] = {}
    for keyword_id, observation in observations.items():
        snapshot_id = observation["serp_snapshot_id"]
        if snapshot_id is None:
            continue
        state = states.get(snapshot_id)
        if state is None:
            continue
        linked[keyword_id] = _validate_state_for_observation(
            observation, state, label=label
        )
    return linked


def _previous_observations(
    pools: Iterable[dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in pools:
        item = validate_rank_observation(raw)
        current_item = current.get(item["keyword_id"])
        if current_item is None:
            continue
        if (
            item["profile_id"] != current_item["profile_id"]
            or item["methodology_identity"] != current_item["methodology_identity"]
        ):
            continue
        if _utc_timestamp(item["collected_at"], "collected_at") >= _utc_timestamp(
            current_item["collected_at"], "collected_at"
        ):
            continue
        grouped.setdefault(item["keyword_id"], []).append(item)

    selected: dict[str, dict[str, Any]] = {}
    for keyword_id, candidates in grouped.items():
        newest_time = max(_utc_timestamp(item["collected_at"], "collected_at") for item in candidates)
        newest = [
            item
            for item in candidates
            if _utc_timestamp(item["collected_at"], "collected_at") == newest_time
        ]
        if len(newest) != 1:
            raise SerpSummaryError(
                f"duplicate previous observation for keyword_id {keyword_id}"
            )
        selected[keyword_id] = newest[0]
    return selected


def _project_domain_presence(
    observations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    eligible = sum(
        item["status"] in {"found", "not_found_within_depth"}
        for item in observations.values()
    )
    top_3 = sum(
        item["status"] == "found" and item["position"] <= 3
        for item in observations.values()
    )
    top_10 = sum(
        item["status"] == "found" and item["position"] <= 10
        for item in observations.values()
    )
    return {
        "eligible_keywords": eligible,
        "top_3_count": top_3,
        "top_3_share": _share(top_3, eligible),
        "top_10_count": top_10,
        "top_10_share": _share(top_10, eligible),
    }


def build_profile_current_summary(
    observations: Iterable[dict[str, Any]],
    serp_states: Iterable[dict[str, Any]] | None = None,
    *,
    keyword_ids: Iterable[str],
    generated_at: str,
    previous_observations: Iterable[dict[str, Any]] | None = None,
    previous_serp_states: Iterable[dict[str, Any]] | None = None,
    top_n: int = DEFAULT_TOP_N,
    tracked_domains: Iterable[str] | None = None,
    domain_aggregate_limit: int = DEFAULT_DOMAIN_AGGREGATE_LIMIT,
) -> dict[str, Any]:
    """Build the latest compatible project/profile state across one or more runs."""

    _utc_timestamp(generated_at, "generated_at")
    universe = _keyword_universe(keyword_ids)
    current, validated_pool, profile_id, rank_identity = _latest_observations(
        observations,
        keyword_ids=universe,
    )

    current_state_pool = _states_by_snapshot(serp_states, label="serp_states")
    current_states = _linked_states(current, current_state_pool, label="current")
    serp_identities = {state["methodology_identity"] for state in current_states.values()}
    if len(serp_identities) > 1:
        raise SerpSummaryError("current state must have one SERP methodology identity")
    serp_identity = next(iter(serp_identities)) if serp_identities else None

    previous_pool = list(validated_pool)
    previous_pool.extend(
        validate_rank_observation(raw) for raw in (previous_observations or [])
    )
    previous = _previous_observations(previous_pool, current)

    previous_state_pool = dict(current_state_pool)
    for snapshot_id, state in _states_by_snapshot(
        previous_serp_states,
        label="previous_serp_states",
    ).items():
        prior = previous_state_pool.get(snapshot_id)
        if prior is not None and prior != state:
            raise SerpSummaryError(
                f"SERP state pools contain conflicting snapshot_id {snapshot_id}"
            )
        previous_state_pool[snapshot_id] = state
    previous_states = _linked_states(previous, previous_state_pool, label="previous")

    status_counts = Counter(item["status"] for item in current.values())
    rank_buckets = {
        f"top_{threshold}": sum(
            item["status"] == "found" and item["position"] <= threshold
            for item in current.values()
        )
        for threshold in (3, 10, 20, 50, 100)
    }

    movement_counts = Counter()
    ranking_url_switches = 0
    comparable_serps = 0
    fingerprint_changed = 0
    fingerprint_unchanged = 0
    materially_changed_serps = 0
    methodology_mismatches = 0

    for keyword_id in sorted(current):
        fact = build_keyword_serp_fact(
            current[keyword_id],
            current_states.get(keyword_id),
            previous_observation=previous.get(keyword_id),
            previous_serp_state=previous_states.get(keyword_id),
            top_n=top_n,
            tracked_domains=tracked_domains,
        )
        rank_change = fact["change"]["rank"]
        movement = rank_change["movement"]
        keyword_methodology_mismatch = False
        if rank_change["comparison"] == "no_baseline":
            movement_counts["no_baseline"] += 1
        elif rank_change["comparison"] == "methodology_mismatch":
            movement_counts["methodology_mismatch"] += 1
            keyword_methodology_mismatch = True
        elif rank_change["comparison"] != "comparable":
            movement_counts["incomparable"] += 1
        elif movement is not None:
            movement_counts[movement] += 1

        if rank_change["ranking_url_switched"] is True:
            ranking_url_switches += 1

        serp_change = fact["change"]["serp"]
        if serp_change["comparison"] == "methodology_mismatch":
            keyword_methodology_mismatch = True
        if keyword_methodology_mismatch:
            methodology_mismatches += 1
        if serp_change["comparison"] == "comparable":
            comparable_serps += 1
            if serp_change["fingerprint_changed"]:
                fingerprint_changed += 1
            else:
                fingerprint_unchanged += 1
            if serp_change["materially_changed"]:
                materially_changed_serps += 1

    run_counts = Counter(item["run_id"] for item in current.values())
    observed = sorted(
        current.values(),
        key=lambda item: (_utc_timestamp(item["observed_at"], "observed_at"), item["keyword_id"]),
    )
    collected = sorted(
        current.values(),
        key=lambda item: (_utc_timestamp(item["collected_at"], "collected_at"), item["keyword_id"]),
    )

    return {
        "type": CURRENT_SUMMARY_TYPE,
        "profile_id": profile_id,
        "rank_methodology_identity": rank_identity,
        "serp_methodology_identity": serp_identity,
        "generated_at": generated_at,
        "observed_at": {
            "from": observed[0]["observed_at"],
            "to": observed[-1]["observed_at"],
        },
        "collected_at": {
            "from": collected[0]["collected_at"],
            "to": collected[-1]["collected_at"],
        },
        "keyword_universe_count": len(universe),
        "latest_keyword_count": len(current),
        "mixed_run": len(run_counts) > 1,
        "source_runs": [
            {"run_id": run_id, "keyword_count": run_counts[run_id]}
            for run_id in sorted(run_counts)
        ],
        "keyword_counts": {
            "total": len(current),
            **{status: status_counts[status] for status in RANK_STATUSES},
        },
        "rank_buckets": rank_buckets,
        "serp_state_counts": {
            "available": len(current_states),
            "missing": len(current) - len(current_states),
        },
        "movement_counts": {
            key: movement_counts[key]
            for key in (
                "moved_up",
                "moved_down",
                "stable",
                "entered",
                "left",
                "not_ranked_both",
                "incomparable",
                "no_baseline",
                "methodology_mismatch",
            )
        },
        "ranking_url_switches": ranking_url_switches,
        "serp_change_counts": {
            "comparable": comparable_serps,
            "fingerprint_changed": fingerprint_changed,
            "fingerprint_unchanged": fingerprint_unchanged,
            "materially_changed": materially_changed_serps,
            "methodology_mismatches": methodology_mismatches,
        },
        "domain_presence": _domain_presence(
            current_states,
            limit=domain_aggregate_limit,
        ),
        "project_domain_presence": _project_domain_presence(current),
    }
