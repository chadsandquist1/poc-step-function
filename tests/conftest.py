import importlib.util
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# Boto3 creates clients at module-level when handlers are imported. Without a
# region in the environment, botocore raises NoRegionError before any mock can
# intercept the call. Set a dummy region and dummy credentials here so module
# loading succeeds in environments that have no AWS config (local CI, GitHub
# Actions without AWS creds configured yet).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

# mime_parser is imported directly in test_parser.py — add its source dir eagerly.
_email_ingest_src = os.path.join(ROOT, "src", "email_ingest")
if _email_ingest_src not in sys.path:
    sys.path.insert(0, _email_ingest_src)


def load_handler(lambda_name: str):
    """Load src/<lambda_name>/handler.py under a unique sys.modules key to prevent
    collision between the three handler.py files when pytest collects them together."""
    src_dir = os.path.join(ROOT, "src", lambda_name)
    handler_path = os.path.join(src_dir, "handler.py")
    module_key = f"_handler_{lambda_name}"

    # Add source dir so sibling imports (e.g. mime_parser) resolve correctly.
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # Always reload so monkeypatching in one test doesn't bleed into the next.
    sys.modules.pop(module_key, None)

    spec = importlib.util.spec_from_file_location(module_key, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod
