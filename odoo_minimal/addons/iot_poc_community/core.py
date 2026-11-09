"""Pure validation; this module has no Odoo dependency and never changes input."""
import hashlib
import json
import re
from datetime import datetime, timezone

MAX_PAYLOAD_BYTES = 65536
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")
UTC_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
PAYLOAD_KEYS = frozenset({
    "schema_version", "event_id", "session_id", "source_kind", "dataset_sha256",
    "dataset_filename", "machine_ref", "mapping_ref", "production_order_ref",
    "product_ref", "lot_ref", "cycle_label", "sample_count", "source_is_ok",
    "rule_version", "source_timezone", "source_start", "source_end",
    "nominal_sampling_interval_seconds", "emitted_at", "samples",
})
SAMPLE_KEYS = frozenset({
    "source_data_row_index", "source_csv_line_number", "original_dataframe_index",
    "original_id", "current_raw_adc", "original_timestamp",
})


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, minimum=0, maximum=2**63 - 1):
    return type(value) is int and minimum <= value <= maximum


def source_time(value):
    require(isinstance(value, str) and bool(SOURCE_TIME_RE.fullmatch(value)),
            "Source timestamps must preserve YYYY-MM-DD HH:MM:SS without a timezone.")
    datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return value


def event_identifier(dataset_sha256, session_id, machine_ref, cycle_label):
    raw = f"{dataset_sha256}|{session_id}|{machine_ref}|{cycle_label}"
    return "poc-v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_payload(payload):
    require(type(payload) is dict and set(payload) == PAYLOAD_KEYS, "Unexpected or missing payload v1 keys.")
    canonical = canonical_json(payload)
    require(len(canonical.encode("utf-8")) <= MAX_PAYLOAD_BYTES, "Payload exceeds 64 KiB.")
    for key, expected in {
        "schema_version": "iot-poc-event.v1", "session_id": "POC-REPLAY-001",
        "source_kind": "replay", "dataset_filename": "labeled_measurments.csv",
        "rule_version": "notebook-count-ge21.v1", "source_timezone": None,
    }.items():
        require(payload[key] == expected, f"Unsupported {key}.")
    require(isinstance(payload["dataset_sha256"], str) and bool(HASH_RE.fullmatch(payload["dataset_sha256"])),
            "dataset_sha256 must be a lowercase SHA-256 digest.")
    for key in ("machine_ref", "mapping_ref", "production_order_ref", "product_ref", "lot_ref"):
        value = payload[key]
        require(isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip()
                and "|" not in value and not any(ord(char) < 32 for char in value), f"Invalid {key}.")
    require(integer(payload["cycle_label"], 1, 2**31 - 1), "cycle_label must be a positive integer.")
    require(payload["event_id"] == event_identifier(payload["dataset_sha256"], payload["session_id"],
                                                   payload["machine_ref"], payload["cycle_label"]),
            "event_id does not match the deterministic v1 identifier.")
    require(type(payload["nominal_sampling_interval_seconds"]) in (int, float)
            and payload["nominal_sampling_interval_seconds"] == 0.5, "The declared nominal interval must be 0.5 seconds.")
    samples = payload["samples"]
    require(type(samples) is list and integer(payload["sample_count"], 1, 39)
            and payload["sample_count"] == len(samples), "sample_count must equal the 1–39 preserved samples.")
    require(type(payload["source_is_ok"]) is bool and payload["source_is_ok"] == (len(samples) >= 21),
            "source_is_ok must follow the declared notebook sample-count rule; it is not a quality verdict.")
    previous = None
    for sample in samples:
        require(type(sample) is dict and set(sample) == SAMPLE_KEYS, "Unexpected or missing sample keys.")
        for key in ("source_data_row_index", "original_dataframe_index", "current_raw_adc"):
            require(integer(sample[key]), f"{key} must be a nonnegative integer, not a boolean.")
        for key in ("source_csv_line_number", "original_id"):
            require(integer(sample[key], 1), f"{key} must be a positive integer, not a boolean.")
        require(sample["source_csv_line_number"] == sample["source_data_row_index"] + 2,
                "CSV line number must match the zero-based data row plus header and line offset.")
        if previous:
            require(sample["source_data_row_index"] == previous["source_data_row_index"] + 1,
                    "Samples must retain contiguous source data row order.")
            require(sample["original_dataframe_index"] == previous["original_dataframe_index"] + 1,
                    "This replay contract requires the original contiguous dataframe indices.")
        source_time(sample["original_timestamp"])
        previous = sample
    require(source_time(payload["source_start"]) == samples[0]["original_timestamp"]
            and source_time(payload["source_end"]) == samples[-1]["original_timestamp"],
            "Source bounds must equal the first and last preserved sample timestamps.")
    emitted = payload["emitted_at"]
    require(isinstance(emitted, str) and bool(UTC_TIME_RE.fullmatch(emitted)), "emitted_at must be UTC ISO 8601 ending in Z.")
    emitted_at = datetime.fromisoformat(emitted[:-1] + "+00:00").astimezone(timezone.utc).replace(tzinfo=None)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), emitted_at


def iso_utc(value):
    if not value:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds") + "Z"


def validate_review(outcome, evidence_kind, notes, evidence_ref, *, correction=False,
                    correction_reason=None, previous_evidence_kind=None):
    require(outcome in {"pass", "fail", "not_verifiable"}, "Choose a review outcome.")
    require(evidence_kind in {"workflow_test", "independent_inspection"}, "Choose an evidence kind.")
    for name, value, limit in (("notes", notes, 8000), ("evidence_ref", evidence_ref, 2000)):
        require(isinstance(value, str) and value.strip() and len(value) <= limit, f"A bounded, nonempty {name} is required.")
    reason = correction_reason or ""
    require(isinstance(reason, str) and len(reason) <= 4000, "Invalid correction reason.")
    require(not correction or reason.strip(), "A correction reason is required after the first review.")
    require(not (correction and previous_evidence_kind == "independent_inspection" and evidence_kind == "workflow_test"),
            "An independent inspection can only be corrected explicitly as an independent inspection.")
    independent = "unverified"
    if evidence_kind == "independent_inspection":
        independent = {"pass": "conforming", "fail": "nonconforming", "not_verifiable": "unverified"}[outcome]
    return independent
