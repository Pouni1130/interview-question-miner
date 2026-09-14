"""Run offline tests in a unique, workspace-local temporary directory."""
from pathlib import Path
import sys
import uuid
import pytest

root = Path(__file__).resolve().parents[1]
temp_root = root / ".pytest_tmp"
temp_root.mkdir(exist_ok=True)
target = (temp_root / uuid.uuid4().hex).resolve()
if not target.is_relative_to(temp_root.resolve()) or target.exists():
    raise RuntimeError("不安全的测试临时目录")
raise SystemExit(pytest.main([str(root/"tests"),"-q",f"--basetemp={target}","--tb=short",*sys.argv[1:]]))
