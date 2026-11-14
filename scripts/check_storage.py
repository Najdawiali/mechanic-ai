"""Check that file storage works: write, read, link, delete.  Run:  python scripts/check_storage.py"""

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.storage import get_store  # noqa: E402


def main() -> int:
    settings = get_settings()
    where = settings.s3_endpoint_url or "AWS S3"
    key, data = "checks/storage-check.txt", b"mechanic-ai storage check"
    try:
        store = get_store()
        store.ensure_bucket()
        store.put(key, data, content_type="text/plain")
        info = store.head(key)
        read_back = store.get(key)
        with urllib.request.urlopen(store.presigned_url(key, expires_s=60), timeout=10) as response:  # noqa: S310
            via_link = response.read()
        store.delete(key)
        gone = store.head(key) is None
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  cannot use storage at {where}: {type(exc).__name__}: {exc}")
        return 1
    ok = read_back == data and via_link == data and info is not None and info.size == len(data) and gone
    print(f"{'PASS' if ok else 'FAIL'}  storage at {where}, bucket {settings.s3_bucket}: "
          f"write, read, download link and delete {'all work' if ok else 'did not all work'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
