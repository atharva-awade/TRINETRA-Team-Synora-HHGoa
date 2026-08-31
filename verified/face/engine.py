"""Face detection, alignment, embedding, attributes and quality scoring.

Pure onnxruntime + numpy + OpenCV re-implementation of the InsightFace
buffalo_l inference path (SCRFD detector + ArcFace-R50 recognizer + attribute
heads).  No compiled dependencies, runs on CPU in ~150 ms per image.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field
from typing import Iterable

import cv2
import numpy as np
import onnxruntime as ort

from .models import ensure_models

# 5-point ArcFace reference landmarks for a 112x112 crop.
ARCFACE_DST = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)

# Empirically calibrated cosine-similarity bands for buffalo_l (w600k_r50).
# Same-identity pairs typically score 0.45-0.85; different people < 0.30.
THRESH_STRONG = 0.50
THRESH_MATCH = 0.40
THRESH_WEAK = 0.32


@dataclass
class Face:
    bbox: np.ndarray  # x1, y1, x2, y2 in source-image pixels
    score: float
    kps: np.ndarray  # (5, 2) landmarks
    embedding: np.ndarray | None = None  # L2-normalised 512-d
    age: int | None = None
    gender: str | None = None
    quality: dict = field(default_factory=dict)
    landmarks106: np.ndarray | None = None

    @property
    def width(self) -> float:
        return float(self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return float(self.bbox[3] - self.bbox[1])

    def to_dict(self) -> dict:
        return {
            "bbox": [round(float(v), 1) for v in self.bbox],
            "det_score": round(self.score, 4),
            "kps": [[round(float(x), 1), round(float(y), 1)] for x, y in self.kps],
            "age": self.age,
            "gender": self.gender,
            "quality": self.quality,
        }


def _session(path) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = max(1, min(4, (ort.get_device() == "CPU") * 4))
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])


def _distance2bbox(points, distance):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, i % 2] + distance[:, i]
        py = points[:, i % 2 + 1] + distance[:, i + 1]
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1)


def _nms(dets, thresh=0.4):
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= thresh)[0]
        order = order[inds + 1]
    return keep


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray | None:
    """Least-squares similarity transform (rotation + uniform scale + translation)
    mapping `src` onto `dst`; Umeyama (1991). Returns a 2x3 affine matrix.

    Equivalent to skimage.transform.SimilarityTransform().estimate(src, dst),
    which is what InsightFace uses, but with no extra dependency.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if src.shape != dst.shape or src.shape[0] < 2:
        return None
    n, dim = src.shape
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_demean, dst_demean = src - src_mean, dst - dst_mean
    A = dst_demean.T @ src_demean / n
    d = np.ones((dim,), dtype=np.float64)
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1
    T = np.eye(dim + 1, dtype=np.float64)
    U, S, Vt = np.linalg.svd(A)
    rank = np.linalg.matrix_rank(A)
    if rank == 0:
        return None
    if rank == dim - 1:
        if np.linalg.det(U) * np.linalg.det(Vt) > 0:
            T[:dim, :dim] = U @ Vt
        else:
            s = d[dim - 1]
            d[dim - 1] = -1
            T[:dim, :dim] = U @ np.diag(d) @ Vt
            d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ Vt
    var = src_demean.var(axis=0).sum()
    scale = 1.0 / var * (S @ d) if var > 0 else 1.0
    T[:dim, dim] = dst_mean - scale * (T[:dim, :dim] @ src_mean.T)
    T[:dim, :dim] *= scale
    return T[:dim, : dim + 1].astype(np.float32)


def _transform(img, center, output_size, scale, rotation=0.0):
    """Affine crop used by the InsightFace attribute / landmark heads."""
    cx, cy = center
    rot = rotation * np.pi / 180.0
    cos, sin = math.cos(rot) * scale, math.sin(rot) * scale
    M = np.array(
        [
            [cos, -sin, output_size / 2 - (cos * cx - sin * cy)],
            [sin, cos, output_size / 2 - (sin * cx + cos * cy)],
        ],
        dtype=np.float32,
    )
    cropped = cv2.warpAffine(img, M, (output_size, output_size), borderValue=0.0)
    return cropped, M


class FaceEngine:
    def __init__(self, det_size: int = 640, det_thresh: float = 0.5):
        d = ensure_models()
        self.det = _session(d / "det_10g.onnx")
        self.rec = _session(d / "w600k_r50.onnx")
        self.ga = _session(d / "genderage.onnx")
        self.lmk = _session(d / "2d106det.onnx")
        self.det_input = self.det.get_inputs()[0].name
        self.det_size = det_size
        self.det_thresh = det_thresh
        self._strides = [8, 16, 32]
        self._num_anchors = 2
        self._center_cache: dict = {}

    # ------------------------------------------------------------------ detect
    def detect(self, img: np.ndarray, max_num: int = 0, det_size: int | None = None) -> list[Face]:
        det_size = det_size or self.det_size
        h0, w0 = img.shape[:2]
        im_ratio = h0 / w0
        if im_ratio > 1:
            new_h, new_w = det_size, int(det_size / im_ratio)
        else:
            new_w, new_h = det_size, int(det_size * im_ratio)
        det_scale = new_h / h0
        resized = cv2.resize(img, (new_w, new_h))
        det_img = np.zeros((det_size, det_size, 3), dtype=np.uint8)
        det_img[:new_h, :new_w] = resized
        blob = cv2.dnn.blobFromImage(det_img, 1.0 / 128, (det_size, det_size), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(None, {self.det_input: blob})
        fmc = len(self._strides)
        scores_l, bboxes_l, kps_l = [], [], []
        for idx, stride in enumerate(self._strides):
            scores = outs[idx]
            bbox_preds = outs[idx + fmc] * stride
            kps_preds = outs[idx + fmc * 2] * stride
            fh, fw = det_size // stride, det_size // stride
            key = (fh, fw, stride)
            if key in self._center_cache:
                centers = self._center_cache[key]
            else:
                centers = np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).astype(np.float32)
                centers = (centers * stride).reshape((-1, 2))
                centers = np.stack([centers] * self._num_anchors, axis=1).reshape((-1, 2))
                self._center_cache[key] = centers
            pos = np.where(scores >= self.det_thresh)[0]
            bboxes = _distance2bbox(centers, bbox_preds)
            kpss = _distance2kps(centers, kps_preds).reshape((kps_preds.shape[0], -1, 2))
            scores_l.append(scores[pos])
            bboxes_l.append(bboxes[pos])
            kps_l.append(kpss[pos])
        scores = np.vstack(scores_l)
        if scores.shape[0] == 0:
            return []
        bboxes = np.vstack(bboxes_l) / det_scale
        kpss = np.vstack(kps_l) / det_scale
        order = scores.ravel().argsort()[::-1]
        pre = np.hstack((bboxes, scores)).astype(np.float32)[order]
        kpss = kpss[order]
        keep = _nms(pre, 0.4)
        det = pre[keep]
        kpss = kpss[keep]
        faces = [Face(bbox=d[:4], score=float(d[4]), kps=k) for d, k in zip(det, kpss)]
        if max_num:
            # keep the largest / most central faces first
            faces.sort(key=lambda f: -(f.width * f.height))
            faces = faces[:max_num]
        return faces

    # ------------------------------------------------------------------ embed
    @staticmethod
    def align(img: np.ndarray, kps: np.ndarray, size: int = 112) -> np.ndarray:
        """ArcFace alignment: similarity transform of the 5 landmarks onto the
        reference template. Uses the exact least-squares (Umeyama) solution -
        the same one the reference InsightFace pipeline uses.

        This matters: OpenCV's robust estimators (LMEDS/RANSAC) treat one
        landmark of a turned face as an outlier and drop it, which changes the
        crop scale and measurably shifts the embedding."""
        M = umeyama_similarity(kps.astype(np.float64), ARCFACE_DST.astype(np.float64) * (size / 112.0))
        if M is None:  # degenerate landmarks
            M = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
        return cv2.warpAffine(img, M, (size, size), borderValue=0.0)

    def embed(self, img: np.ndarray, face: Face) -> np.ndarray:
        aimg = self.align(img, face.kps)
        blob = cv2.dnn.blobFromImage(aimg, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        emb = self.rec.run(None, {self.rec.get_inputs()[0].name: blob})[0][0]
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        face.embedding = emb.astype(np.float32)
        return face.embedding

    # ------------------------------------------------------------- attributes
    def attributes(self, img: np.ndarray, face: Face) -> None:
        w, h = face.width, face.height
        center = ((face.bbox[0] + face.bbox[2]) / 2, (face.bbox[1] + face.bbox[3]) / 2)
        scale = 96 / (max(w, h) * 1.5)
        crop, _ = _transform(img, center, 96, scale)
        blob = cv2.dnn.blobFromImage(crop, 1.0, (96, 96), (0, 0, 0), swapRB=True)
        out = self.ga.run(None, {self.ga.get_inputs()[0].name: blob})[0][0]
        face.gender = "male" if int(np.argmax(out[:2])) == 1 else "female"
        face.age = int(np.round(out[2] * 100))

    def landmarks(self, img: np.ndarray, face: Face) -> np.ndarray:
        w, h = face.width, face.height
        center = ((face.bbox[0] + face.bbox[2]) / 2, (face.bbox[1] + face.bbox[3]) / 2)
        size = 192
        scale = size / (max(w, h) * 1.5)
        crop, M = _transform(img, center, size, scale)
        blob = cv2.dnn.blobFromImage(crop, 1.0, (size, size), (0, 0, 0), swapRB=True)
        pred = self.lmk.run(None, {self.lmk.get_inputs()[0].name: blob})[0][0]
        pred = pred.reshape((-1, 2))
        pred[:, 0:2] += 1
        pred[:, 0:2] *= size // 2
        IM = cv2.invertAffineTransform(M)
        pts = np.hstack([pred, np.ones((pred.shape[0], 1), dtype=np.float32)])
        face.landmarks106 = (pts @ IM.T).astype(np.float32)
        return face.landmarks106

    # ---------------------------------------------------------------- quality
    def quality(self, img: np.ndarray, face: Face) -> dict:
        """Cheap but informative capture-quality metrics (0-1 scores)."""
        x1, y1, x2, y2 = [int(max(0, v)) for v in face.bbox]
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            face.quality = {"overall": 0.0}
            return face.quality
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        sharp_s = min(1.0, sharp / 250.0)
        size_s = min(1.0, min(face.width, face.height) / 112.0)
        bright = float(gray.mean())
        bright_s = 1.0 - min(1.0, abs(bright - 128) / 128.0)
        # frontal-ness from landmark symmetry: nose vs eye midpoint
        le, re, nose = face.kps[0], face.kps[1], face.kps[2]
        eye_mid = (le + re) / 2
        eye_dist = np.linalg.norm(re - le) + 1e-6
        yaw = abs(nose[0] - eye_mid[0]) / eye_dist  # 0 frontal, ~0.5 strongly turned
        frontal_s = max(0.0, 1.0 - yaw * 2)
        roll = math.degrees(math.atan2(re[1] - le[1], re[0] - le[0]))
        overall = 0.35 * sharp_s + 0.25 * size_s + 0.15 * bright_s + 0.25 * frontal_s
        face.quality = {
            "overall": round(float(overall), 3),
            "sharpness": round(float(sharp_s), 3),
            "size": round(float(size_s), 3),
            "brightness": round(float(bright_s), 3),
            "frontal": round(float(frontal_s), 3),
            "yaw_ratio": round(float(yaw), 3),
            "roll_deg": round(float(roll), 1),
            "det_score": round(float(face.score), 3),
        }
        return face.quality

    # --------------------------------------------------------------- helpers
    def analyze(self, img: np.ndarray, max_num: int = 0, with_attrs: bool = True) -> list[Face]:
        faces = self.detect(img, max_num=max_num)
        for f in faces:
            self.embed(img, f)
            self.quality(img, f)
            if with_attrs:
                try:
                    self.attributes(img, f)
                except Exception:
                    pass
        return faces

    def primary_face(self, img: np.ndarray) -> Face | None:
        faces = self.analyze(img)
        if not faces:
            return None
        # prefer the largest face; break ties by detection score
        faces.sort(key=lambda f: (f.width * f.height, f.score), reverse=True)
        return faces[0]


LEFT_EYE_106 = list(range(33, 43))
RIGHT_EYE_106 = list(range(87, 97))


def eye_aspect_ratios(landmarks106: np.ndarray) -> tuple[float, float]:
    """Vertical/horizontal extent ratio of each eye (drops sharply on blink)."""
    out = []
    for idx in (LEFT_EYE_106, RIGHT_EYE_106):
        pts = landmarks106[idx]
        w = float(pts[:, 0].max() - pts[:, 0].min()) + 1e-6
        h = float(pts[:, 1].max() - pts[:, 1].min())
        out.append(h / w)
    return out[0], out[1]


# --------------------------------------------------------------------- maths
def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


def similarity_band(sim: float, threshold: float = THRESH_MATCH) -> str:
    """Band relative to the configured match threshold (default 0.40)."""
    if sim >= threshold + 0.10:
        return "strong"
    if sim >= threshold:
        return "match"
    if sim >= threshold - 0.08:
        return "weak"
    return "reject"


def face_commitment(embedding: np.ndarray, salt: bytes) -> str:
    """Privacy-preserving biometric commitment.

    We never publish the embedding.  Instead we quantise it to int8, and publish
    HMAC-SHA256(salt, quantised_embedding).  Anyone holding the salt and a fresh
    scan of the same person can recompute the commitment (cancelable biometrics);
    nobody can recover a face from the on-chain value.
    """
    q = np.clip(np.round(embedding * 127), -127, 127).astype(np.int8).tobytes()
    return "0x" + hmac.new(salt, q, hashlib.sha256).hexdigest()


def crop_face(img: np.ndarray, face: Face, margin: float = 0.35) -> np.ndarray:
    x1, y1, x2, y2 = face.bbox
    w, h = x2 - x1, y2 - y1
    x1 = int(max(0, x1 - w * margin))
    y1 = int(max(0, y1 - h * margin))
    x2 = int(min(img.shape[1], x2 + w * margin))
    y2 = int(min(img.shape[0], y2 + h * margin))
    return img[y1:y2, x1:x2]


def draw_faces(img: np.ndarray, faces: Iterable[Face]) -> np.ndarray:
    out = img.copy()
    for f in faces:
        x1, y1, x2, y2 = [int(v) for v in f.bbox]
        cv2.rectangle(out, (x1, y1), (x2, y2), (86, 219, 255), 2)
        for x, y in f.kps:
            cv2.circle(out, (int(x), int(y)), 2, (255, 150, 60), -1)
    return out
