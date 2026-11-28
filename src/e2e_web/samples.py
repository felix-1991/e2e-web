"""Built-in upload samples, addressed as `sample:<name>`.

A project rarely keeps files a model could know to name, so exploration would
otherwise skip every upload flow it meets. These are this SDK's own bytes,
written once per process to a private temporary directory; naming one reads
nothing of the project or the machine.
"""

import atexit
import base64
import shutil
import tempfile
import threading
from pathlib import Path

SAMPLE_PREFIX = "sample:"
SAMPLE_NAMES = ("photo.png", "photo.jpg", "document.pdf", "notes.txt", "table.csv", "empty.txt")

# A 32 by 32 PNG of four colored squares.
_PNG = ("iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAOklEQVR42mO45+NAEnLruUMSYhi1YNSCUQtGLRi1YEhY"
        "ELAlhST04YQNSWjUglELRi0YtWDUgiFhAQCR4HRb8WkrnwAAAABJRU5ErkJggg==")
# The same picture as a baseline JPEG, without metadata.
_JPEG = ("/9j/4AAQSkZJRgABAgAAAQABAAD/wAARCAAgACADAREAAhEBAxEB/9sAQwAFAwQEBAMFBAQEBQUFBgcMCAcHBwcPCwsJDBEPEhIRDxERExYc"
         "FxMUGhURERghGBodHR8fHxMXIiQiHiQcHh8e/9sAQwEFBQUHBgcOCAgOHhQRFB4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4e"
         "Hh4eHh4eHh4eHh4eHh4eHh4e/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQID"
         "AAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlq"
         "c3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3"
         "+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEI"
         "FEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImK"
         "kpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDPr40/"
         "dgoA0K/oU/kYKAM+v56P65CgDQr+hT+RgoA1a/ByQoA9Yr8pP3cKAPJ6/Vj8ICgD1ivyk/dwoA//2Q==")


def _pdf() -> bytes:
    """A one-page PDF reading "e2e sample document", its xref counted from the objects."""
    text = "BT /F1 24 Tf 72 720 Td (e2e sample document) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = "%PDF-1.4\n"
    offsets = []
    for index, item in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n{item}\nendobj\n"
    table = "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    size = len(objects) + 1
    body += (f"xref\n0 {size}\n0000000000 65535 f \n{table}"
             f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{len(body)}\n%%EOF\n")
    return body.encode()


_BYTES = {
    "photo.png": lambda: base64.b64decode(_PNG),
    "photo.jpg": lambda: base64.b64decode(_JPEG),
    "document.pdf": _pdf,
    "notes.txt": lambda: "e2e sample text\nSecond line: café, 東京, 🙂\n".encode(),
    "table.csv": lambda: ('name,email,amount\nAda Lovelace,ada@example.test,42\n'
                          '"Hopper, Grace",grace@example.test,7.5\nZoë Ångström,zoe@example.test,0\n').encode(),
    "empty.txt": bytes,
}

_directory: Path | None = None
_directory_lock = threading.Lock()


def sample_path(name: str) -> Path | None:
    """Where a named sample lives, written on first use; None for an unknown name."""
    global _directory
    factory = _BYTES.get(name)
    if factory is None:
        return None
    with _directory_lock:
        if _directory is None:
            directory = Path(tempfile.mkdtemp(prefix="e2e-samples-"))
            atexit.register(shutil.rmtree, directory, ignore_errors=True)
            for sample, produce in _BYTES.items():
                (directory / sample).write_bytes(produce())
            _directory = directory
    return _directory / name


def resolve_sample(value: str) -> Path | None:
    """The local path for one `sample:` upload name."""
    return sample_path(value[len(SAMPLE_PREFIX):]) if value.startswith(SAMPLE_PREFIX) else None
