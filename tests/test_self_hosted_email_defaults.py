import os
import subprocess
import sys


def test_self_hosted_defaults_do_not_enable_transactional_email():
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR"}
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app import config; "
                "assert config.EMAIL_PROVIDER == 'disabled'; "
                "assert config.EMAIL_DELIVERY_WORKER_ENABLED is False"
            ),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
