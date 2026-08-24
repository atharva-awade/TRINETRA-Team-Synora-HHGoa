"""Download the InsightFace buffalo_l ONNX models (~275 MB) into ./models/buffalo_l."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verified.face.models import ensure_models  # noqa: E402

print("models ready at", ensure_models(print))
