import os
import pathlib
from threading import Lock, RLock



root_dir = pathlib.Path(__file__).resolve().parent.parent
onnx_path = os.path.join(root_dir, 'geeked', 'models', 'geetest_v4_icon.onnx')
charsets_path = os.path.join(root_dir, 'geeked', 'models', 'charsets.json')


class DdddService:
    def __init__(self):
        import ddddocr
        if not pathlib.Path(onnx_path).is_file() or not pathlib.Path(charsets_path).is_file():
            raise FileNotFoundError("GeeTest icon model / charsets are missing")
        self._inference_lock = RLock()
        self.det = ddddocr.DdddOcr(det=True, show_ad=False)
        self.cnn = ddddocr.DdddOcr(det=False, ocr=False,
                                   show_ad=False,
                                   import_onnx_path=onnx_path,
                                   charsets_path=charsets_path)

    def detection(self, img):
        with self._inference_lock:
            return self.det.detection(img)

    def classification(self, img):
        with self._inference_lock:
            return self.cnn.classification(img)


_dddd_service = None
_service_lock = Lock()


def get_dddd_service() -> DdddService:
    """Load ONNX models once, on first icon challenge, not on bot import."""
    global _dddd_service
    with _service_lock:
        if _dddd_service is None:
            _dddd_service = DdddService()
        return _dddd_service
