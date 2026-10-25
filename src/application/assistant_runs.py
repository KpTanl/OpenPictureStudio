from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from threading import Event

from .assistant_models import AssistantConnection, AssistantContext, AssistantModel, LOOP_MAX_ROUNDS
from .assistant_tools import current_values, grouped


RUN_MAX_SECONDS = 600
AGENT_MAX_CALLS = 32


def _state(photo, adjustments=None):
    if photo is None:
        return "{}"
    changes = grouped(adjustments or {})
    values = {tool: current_values(photo, tool, changes.get(tool)) for tool in ("color", "beauty")}
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


@dataclass
class AssistantRun:
    id: str
    conversation_id: str
    message_id: str
    goal: str
    start: AssistantContext | None
    preferences: dict
    language: str
    model: AssistantModel
    connection: AssistantConnection
    photo: AssistantContext | None = field(init=False)
    cancelled: Event = field(default_factory=Event)
    phase: str = "preparing"
    reason: str = ""
    round: int = 1
    applied_rounds: int = 0
    tokens: int = 0
    estimated_usage: bool = False
    started: float = field(default_factory=time.monotonic)
    elapsed: int = 0
    changes: dict = field(default_factory=dict)
    states: list = field(default_factory=list)
    baseline: list = field(default_factory=list)
    baseline_analysis: dict | None = None
    reference: dict | None = None
    regions: tuple = ()
    faces: bool | None = None
    latest_message: str = ""
    last_response: str = ""
    receipt: dict = field(default_factory=dict)
    runtime: str = "legacy"
    calls: list = field(default_factory=list)
    evaluation_requests: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    goal_version: int = 0
    updates: list = field(default_factory=list)
    runtime_state: dict = field(default_factory=dict, repr=False)
    observation_frames: dict = field(default_factory=dict, repr=False)
    image_memory: object = field(default=None, repr=False)
    request_task: object = field(default=None, repr=False)
    paused: float = 0.0
    batch_context: dict = field(default_factory=dict)
    batch_mode: bool = False
    batch_receipts: list = field(default_factory=list)
    export_receipts: list = field(default_factory=list)

    def __post_init__(self):
        self.photo = self.start
        self.preferences = copy.deepcopy(self.preferences)
        self.states.append(_state(self.start))

    @property
    def looping(self):
        return self.runtime == "agent" or bool(self.preferences.get("loop"))

    @property
    def active(self):
        return not self.reason

    @property
    def sends_image(self):
        return bool(self.preferences["context"]["image"] and self.model.image_input)

    def material_request(self):
        return {"image": self.sends_image,
                "analysis": self.preferences["context"].get("analysis", True), "regions": self.regions,
                "run_id": self.id, "goal_version": self.goal_version}

    def limit_reason(self):
        if (self.paused or time.monotonic()) - self.started >= RUN_MAX_SECONDS:
            return "time_limit"
        return ""

    def repeated(self, adjustments):
        return _state(self.photo, adjustments) in self.states[1:]

    def add_usage(self, usage, estimated_input, estimated_output):
        incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
        outgoing = usage.get("output_tokens", usage.get("completion_tokens"))
        known_input = type(incoming) is int and incoming >= 0
        known_output = type(outgoing) is int and outgoing >= 0
        self.estimated_usage |= not known_input or not known_output
        self.tokens += incoming if known_input else estimated_input
        self.tokens += outgoing if known_output else estimated_output

    def written(self, message_id, result):
        self.photo = result["context"]
        self.latest_message = message_id
        self.applied_rounds += 1
        self.states.append(_state(self.photo))
        for change in result["changes"]:
            key = (change["tool"], change["key"])
            previous = self.changes.get(key, change)
            self.changes[key] = {**change, "before": previous["before"]}
        self.receipt = {"photo": self.start.identity(), "before_revision": self.start.revision_id,
                        "before_workflow_hash": self.start.workflow.workflow_hash(),
                        "after_revision": result["revision_id"],
                        "after_workflow_hash": result["after_workflow_hash"]}

    def finish(self, reason):
        if self.active:
            self.reason = reason
            self.elapsed = int((self.paused or time.monotonic()) - self.started)
            self.cancelled.set()

    def pause(self):
        self.paused = time.monotonic()
        self.phase = "waiting_user"

    def resume(self):
        if self.paused:
            self.started += time.monotonic() - self.paused
            self.paused = 0.0
        self.phase = "preparing"

    def snapshot(self):
        return {"id": self.id, "runtime": self.runtime, "loop": self.looping, "phase": self.phase, "reason": self.reason,
                "round": self.round, "max_rounds": LOOP_MAX_ROUNDS, "applied_rounds": self.applied_rounds,
                "tokens": self.tokens, "estimated_usage": self.estimated_usage,
                "elapsed": self.elapsed if self.reason else int((self.paused or time.monotonic()) - self.started),
                "max_seconds": RUN_MAX_SECONDS, "tool_calls": copy.deepcopy(self.calls), "plan": copy.deepcopy(self.plan),
                "evaluation_requests": copy.deepcopy(self.evaluation_requests),
                "max_calls": AGENT_MAX_CALLS, "goal_version": self.goal_version,
                "reference": self.reference["attachment"] if self.reference else None,
                "changes": [dict(row) for row in self.changes.values() if row["before"] != row["after"]],
                "latest_message": self.latest_message, "last_response": self.last_response,
                "receipt": copy.deepcopy(self.receipt), "batch_mode": self.batch_mode,
                "batch_context": copy.deepcopy(self.batch_context), "batch_receipts": copy.deepcopy(self.batch_receipts),
                "export_receipts": copy.deepcopy(self.export_receipts)}
