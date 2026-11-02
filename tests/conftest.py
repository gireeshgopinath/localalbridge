"""Use an isolated workspace temp root, avoiding system-temp ACL differences."""
import uuid
import os
import tempfile
from pathlib import Path

def pytest_configure(config):
    if not config.option.basetemp:
        root=Path(os.environ.get('LOCAL_WORKER_TEST_ROOT',str(Path(tempfile.gettempdir())/'local-worker-tests')))
        root.mkdir(parents=True,exist_ok=True)
        target=(root/uuid.uuid4().hex[:8]).resolve()
        while target.exists(): target=(root/uuid.uuid4().hex[:8]).resolve()
        if not target.is_relative_to(root.resolve()):raise ValueError('Invalid test temp root')
        config.option.basetemp=str(target)
