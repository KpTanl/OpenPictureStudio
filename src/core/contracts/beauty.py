from __future__ import annotations

from dataclasses import dataclass


DETAIL_BEAUTY_ALGORITHM = 5
DETAIL_BEAUTY_PROFILES = {5: ("9", 3, 3)}
# 美颜参数里的模型选择；它们不是效果强度，判断参数是否全为 0 时不计入
BEAUTY_MODEL_FIELDS = ("face_detection_model", "face_locator_model")


def detail_beauty_algorithm(implementation_version):
    for algorithm in DETAIL_BEAUTY_PROFILES:
        if (implementation_version == f"{algorithm}-neutral"
                or implementation_version.startswith(f"{algorithm}-cv")):
            return algorithm
    raise ValueError("美颜算法版本不受支持")


@dataclass(frozen=True)
class FaceLandmarks:
    points: tuple[tuple[float, float], ...]
    status: str = "ready"


@dataclass(frozen=True)
class FaceAnalysisSnapshot:
    identity: tuple
    reference_size: tuple[int, int]
    detection_size: tuple[int, int]
    faces: tuple[FaceLandmarks, ...]
    status: str

    @property
    def processed(self):
        return sum(face.status == "ready" for face in self.faces)

    @property
    def skipped(self):
        return len(self.faces) - self.processed

    @property
    def nbytes(self):
        return 4096 + sum(len(face.points) * 128 + 128 for face in self.faces)


@dataclass(frozen=True)
class BeautyEffectStatus:
    node_id: str
    status: str
    processed: int
    skipped: int
    spots_skipped: int = 0
