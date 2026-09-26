"""测试一律用临时数据目录：paths 在导入时就定下位置，所以要在导入包之前设好。"""
import os
import tempfile

os.environ["MONASH_KIT_HOME"] = tempfile.mkdtemp(prefix="monash-kit-test-")
os.environ.pop("ED_TOKEN", None)
