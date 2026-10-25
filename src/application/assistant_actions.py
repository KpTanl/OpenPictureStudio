from __future__ import annotations

from dataclasses import replace
import copy
import logging
from concurrent.futures import CancelledError

from .assistant_tools import TOOLS, current_values, grouped
from .edit_changes import parameter_changes
from .photo_analysis import ANALYSIS_EDGE, ANALYSIS_WORKING_BYTES, analyzed_photo, encode_evidence, encode_jpeg
from .assistant_context import safe_parameters
from .assistant_models import context_matches
from .assistant_observation import frame_descriptor, geometry_identity


# 历史里 AI 写入的步骤：单次调整，或循环调整的第 N 轮（后接轮次）。标签随项目保存，界面据此写说明。
AI_LABEL = "ai.adjust"
AI_ROUND_LABEL = "ai.round."
AI_TOOL_LABEL = "ai.tool."
logger = logging.getLogger(__name__)


class AssistantActionService:
    def __init__(self, core, agent):
        self.core, self.agent = core, agent

    def apply(self, context, workflow, adjustments, *, label, conversation_id, message_id, request_id, model,
              is_cancelled, review=None, purpose=None):
        """把 AI 的一次调整写成历史里的一步，与手动编辑落在同一组工具参数上：建候选、渲染校验后提交。

        context 是 AI 据以给出调整的编辑状态，提交时核对它仍是当前状态。review 不为 None 时（循环调整还要自查），
        顺带给出这一步结果的画面、局部图与照片分析；review 指定 image、analysis 与归一化 regions。
        提交之前取消或失败时什么都不写；提交成功但审计记录失败时结果照常返回，error 写明原因。"""
        if is_cancelled():
            raise ValueError("AI_CANCELLED")
        proposed = self.agent.propose(context.session_id, workflow, model_version=model,
                                      expected_state=context.state_version,
                                      audit_context={"conversation_id": conversation_id, "message_id": message_id,
                                                     "request_id": request_id, "source_hash": context.source_hash,
                                                     "purpose": purpose})
        if not proposed.success:
            raise ValueError(proposed.error.code)
        candidate = proposed.data["candidate_id"]
        rendered = self.agent.preview(candidate, is_cancelled=is_cancelled)
        if not rendered.success or is_cancelled():
            self.agent.discard(candidate)
            if rendered.success:
                rendered.data.close()
            raise ValueError("AI_CANCELLED" if is_cancelled() or not rendered.success and rendered.error.code == "CANCELLED"
                             else rendered.error.code)
        committed = self.agent.commit(candidate, confirmed=True, is_cancelled=is_cancelled, label=label)
        if not committed.success and not committed.error.details.get("operation_applied"):
            self.agent.discard(candidate)
            rendered.data.close()
            raise ValueError("AI_CANCELLED" if committed.error.code == "CANCELLED" else committed.error.code)
        changes = []
        for tool, values in grouped(adjustments).items():
            before = current_values(context, tool)
            changes += [{"tool": TOOLS[tool].tool_id, "key": change.key, "before": change.before, "after": change.after}
                        for change in parameter_changes(before, {**before, **values}, {})]
        result = {"revision_id": committed.data.revision.revision_id if committed.success
                  else committed.error.details.get("revision_id", ""),
                  "error": None if committed.success else committed.error.code,
                  "image": rendered.data.image, "changes": changes,
                  "before_workflow_hash": context.workflow.workflow_hash(), "after_workflow_hash": workflow.workflow_hash()}
        state = self.core.get_session(context.session_id)
        current = (state.success and state.data.current_revision.revision_id == result["revision_id"]
                   and state.data.draft_workflow.workflow_hash() == workflow.workflow_hash())
        if not current:
            result["error"] = result["error"] or "AI_PHOTO_CHANGED"
            rendered.data.close()
            result["image"] = None
        result["context"] = replace(context, workflow=workflow, revision_id=result["revision_id"],
            state_version=state.data.state_version if current else context.state_version + 1,
            color={**context.color, **grouped(adjustments).get("color", {})},
            beauty={**context.beauty, **grouped(adjustments).get("beauty", {})})
        if review is not None:
            result["review"] = {"images": [], "analysis": None}
            if current and not is_cancelled() and (review["image"] or review["analysis"]):
                try:
                    measured = self.core.preview(context.session_id, workflow, update_draft=False,
                                                 max_dimension=ANALYSIS_EDGE, is_cancelled=is_cancelled)
                    if not measured.success:
                        raise ValueError(measured.error.code)
                    regions = review.get("regions", ())
                    with measured.data.image as image:
                        sources = self.core.image_sources
                        with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, is_cancelled), \
                                sources.compute.borrow(is_cancelled, threads=1):
                            result["review"] = {
                                "images": encode_evidence(image, regions) if review["image"] else [],
                                "analysis": analyzed_photo(image, regions,
                                    identity=safe_parameters(result["context"].identity())) if review["analysis"] else None}
                            result["review"]["frames"] = [frame_descriptor(result["context"], workflow, "current",
                                measured.data.full_size or image.size, geometry_identity(workflow, self.core.tools.descriptors()),
                                run_id=review.get("run_id", ""), goal_version=review.get("goal_version", 0), regions=regions)]
                except (ValueError, OSError, MemoryError, CancelledError):
                    if not is_cancelled():
                        result["error"] = result["error"] or "AI_IMAGE_UNAVAILABLE"
        return result

    def variants(self, source, base, adjustments, workflows, *, model, is_cancelled, regions=(), detector=None):
        from .assistant_variants import changes_between

        state = self.core.get_session(source.session_id)
        if not state.success or not context_matches(source, state.data):
            raise ValueError("AI_VARIANT_STALE")
        if any(workflow.workflow_hash() == base.workflow.workflow_hash() for workflow in workflows):
            raise ValueError("AI_VARIANT_EMPTY")
        if any(workflow.workflow_hash() == source.workflow.workflow_hash() for workflow in workflows):
            raise ValueError("AI_VARIANT_EMPTY")
        if workflows[0].workflow_hash() == workflows[1].workflow_hash():
            raise ValueError("AI_VARIANTS_IDENTICAL")
        result = {"variants": {}}
        before = self.core.preview(source.session_id, source.workflow, update_draft=False,
                                   max_dimension=ANALYSIS_EDGE, is_cancelled=is_cancelled)
        if not before.success:
            raise ValueError(before.error.code)
        with before.data.image as image:
            result["before"] = encode_jpeg(image, 1536, 90)
        for label, values, workflow in zip(("A", "B"), adjustments, workflows):
            if is_cancelled():
                raise ValueError("AI_CANCELLED")
            proposed = self.agent.propose(source.session_id, workflow, model_version=model,
                                          expected_state=source.state_version)
            if not proposed.success:
                raise ValueError(proposed.error.code)
            candidate = proposed.data["candidate_id"]
            try:
                preview = self.agent.preview(candidate, max_dimension=ANALYSIS_EDGE, is_cancelled=is_cancelled)
                if not preview.success:
                    raise ValueError(preview.error.code)
                target = replace(base, workflow=workflow,
                                 color={**base.color, **values.get("color", {})},
                                 beauty={**base.beauty, **values.get("beauty", {})})
                if not changes_between(source, target):
                    preview.data.close()
                    raise ValueError("AI_VARIANT_EMPTY")
                with preview.data.image as image:
                    face_regions = regions
                    if detector is not None:
                        try:
                            from .beauty_analysis import query_face_regions
                            detected = query_face_regions(detector, image, dict(target.beauty), is_cancelled,
                                                          photo=target, workflow=workflow)
                        except Exception:
                            logger.exception("双候选人脸检测失败，保留全图测量")
                            detected = None
                        face_regions = detected if isinstance(detected, (tuple, list)) else ()
                    sources = self.core.image_sources
                    with sources.resources.working_memory(ANALYSIS_WORKING_BYTES, is_cancelled), \
                            sources.compute.borrow(is_cancelled, threads=1):
                        result["variants"][label] = {
                            "workflow": workflow.to_dict(), "color": copy.deepcopy(dict(target.color)),
                            "beauty": copy.deepcopy(dict(target.beauty)),
                            "changes": changes_between(base, target), "editor_changes": changes_between(source, target),
                            "analysis": analyzed_photo(image, face_regions,
                                identity={"workflow_hash": workflow.workflow_hash(), "source": safe_parameters(source.identity())}),
                            "preview": encode_jpeg(image, 1536, 90)}
            finally:
                self.agent.discard(candidate)
        state = self.core.get_session(source.session_id)
        if is_cancelled() or not state.success or not context_matches(source, state.data):
            raise ValueError("AI_VARIANT_STALE")
        if result["variants"]["A"]["preview"] == result["variants"]["B"]["preview"]:
            raise ValueError("AI_VARIANTS_IDENTICAL")
        return result

    def before_image(self, context, receipt, *, is_cancelled):
        if receipt.get("photo", {}).get("object_key") != context.object_key:
            raise ValueError("AI_PHOTO_CHANGED")
        workflow = self.core.revision_workflow(context.session_id, receipt["before_revision"])
        if not workflow.success or workflow.data.workflow_hash() != receipt["before_workflow_hash"]:
            raise ValueError("AI_HISTORY_CHANGED")
        result = self.core.preview(context.session_id, workflow.data, update_draft=False,
                                   max_dimension=1600, is_cancelled=is_cancelled)
        if not result.success:
            raise ValueError(result.error.code)
        return {"image": result.data.image}

    def history_status(self, session_id, receipt):
        state = self.core.get_session(session_id)
        if not state.success:
            raise ValueError("AI_PHOTO_CHANGED")
        timeline = self.core.history_timeline(session_id)
        if not timeline.success:
            raise ValueError(timeline.error.code)
        revisions = timeline.data["revisions"]
        current = state.data.current_revision.revision_id
        current_hash = state.data.draft_workflow.workflow_hash()
        before, after = receipt.get("before_revision"), receipt.get("after_revision")
        ids = [revision.revision_id for revision in revisions]
        if current == after and current_hash == receipt.get("after_workflow_hash"):
            return {"state": "applied", "affected": (), "version": state.data.state_version}
        if current == before and current_hash == receipt.get("before_workflow_hash"):
            # 循环调整按轮写入时，应用前与应用后之间隔着几步，应用后的那步在重做路径上即可重做
            can_redo = before in ids and after in ids[ids.index(before) + 1:]
            return {"state": "undone" if can_redo else "detached", "affected": (), "version": state.data.state_version}
        if before in ids and after in ids and ids.index(after) <= timeline.data["current"]:
            return {"state": "later", "affected": revisions[ids.index(after) + 1:timeline.data["current"] + 1],
                    "version": state.data.state_version}
        if (before in ids and after in ids and ids.index(before) < timeline.data["current"] < ids.index(after)
                and current_hash == revisions[timeline.data["current"]].workflow_hash):
            return {"state": "partial", "affected": (), "version": state.data.state_version}
        return {"state": "detached", "affected": (), "version": state.data.state_version}

    def move(self, context, receipt, *, redo=False, confirmed=False, expected_version=None):
        status = self.history_status(context.session_id, receipt)
        if expected_version is not None and status["version"] != expected_version:
            raise ValueError("AI_PHOTO_CHANGED")
        if redo and status["state"] in ("undone", "partial"):
            result = self.core.checkout(context.session_id, receipt["after_revision"], expected_state=status["version"])
        elif not redo and status["state"] in ("applied", "partial"):
            result = self.core.checkout(context.session_id, receipt["before_revision"],
                                        expected_state=status["version"], keep_redo_path=True)
        elif not redo and status["state"] == "later" and confirmed:
            result = self.core.checkout(context.session_id, receipt["before_revision"],
                                        expected_state=status["version"], keep_redo_path=True)
        else:
            raise ValueError("AI_HISTORY_CHANGED")
        if not result.success:
            raise ValueError(result.error.code)
        return {"outcome": "applied" if redo else "undone"}
