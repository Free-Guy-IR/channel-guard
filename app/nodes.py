from __future__ import annotations

import asyncio
import logging
import secrets
import shlex
import time
from pathlib import Path

import asyncssh
import httpx

from . import node_agent as node_agent_module
from .db import Database
from .hub import Hub

log = logging.getLogger("channel_guard.nodes")

AGENT_VERSION = node_agent_module.VERSION
AGENT_SOURCE_PATH = Path(node_agent_module.__file__)
NODE_DIR = "/opt/channel-guard-node"
SERVICE_NAME = "channel-guard-node"
DEFAULT_API_PORT = 8787

KEYS_DIR = Path(__file__).resolve().parent.parent / "data" / "node_keys"

# Repair (SSH restart) attempts are rate-limited per node so a persistently
# broken node doesn't get hammered with an SSH connection every 60s cycle.
REPAIR_COOLDOWN_SECONDS = 300
_last_repair_attempt: dict[int, float] = {}


class NodeError(Exception):
    pass


async def _emit(hub: Hub | None, node_id: int, step: str, message: str) -> None:
    log.info("node %s [%s]: %s", node_id, step, message)
    if hub is not None:
        await hub.publish(
            {"type": "node_progress", "node_id": node_id, "step": step, "message": message, "at": time.time()}
        )


def _generate_keypair(node_id: int) -> Path:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    private_path = KEYS_DIR / f"{node_id}_ed25519"
    key = asyncssh.generate_private_key("ssh-ed25519")
    key.write_private_key(str(private_path))
    private_path.chmod(0o600)
    key.write_public_key(str(private_path) + ".pub")
    return private_path


def _read_pubkey(private_key_path: str) -> str:
    return (Path(private_key_path).with_suffix(".pub")).read_text().strip()


async def _run_checked(conn: asyncssh.SSHClientConnection, cmd: str, step: str) -> None:
    """Runs a command and, on failure, raises with the command's actual
    stderr/stdout attached - a bare exit code (asyncssh's default check=True
    behavior) isn't enough to diagnose what went wrong on someone else's
    server."""
    result = await conn.run(cmd, check=False)
    if result.exit_status != 0:
        output = (result.stderr or result.stdout or "").strip()
        output = output[-800:]
        raise NodeError(f"مرحله «{step}» با خطا مواجه شد (کد {result.exit_status}): {output}")


async def _install_authorized_key(conn: asyncssh.SSHClientConnection, pubkey_text: str) -> None:
    quoted = shlex.quote(pubkey_text)
    cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"grep -qF {quoted} ~/.ssh/authorized_keys || echo {quoted} >> ~/.ssh/authorized_keys; "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    await _run_checked(conn, cmd, "ثبت کلید SSH")


async def _existing_install_version(conn: asyncssh.SSHClientConnection) -> str | None:
    """A VERSION file alone isn't proof of a working install - it's written
    partway through, before the systemd service is created, so a previous
    attempt that failed after that point (e.g. venv creation) would leave a
    VERSION file with no service behind it. Only trust it if the service
    unit is actually there too, otherwise treat it as not installed and run
    the full (idempotent) install again."""
    result = await conn.run(f"cat {NODE_DIR}/VERSION 2>/dev/null", check=False)
    version = (result.stdout or "").strip()
    if not version:
        return None

    result = await conn.run(f"test -f /etc/systemd/system/{SERVICE_NAME}.service", check=False)
    if result.exit_status != 0:
        return None

    return version


#  DEBIAN_FRONTEND=noninteractive + NEEDRESTART_MODE=l stop Ubuntu's
#  `needrestart` from silently restarting services it thinks were touched by
#  the upgrade - without this, it can restart openssh-server mid-install and
#  drop the very SSH session running the install.
_APT_ENV = "DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l"


async def _install_prereqs(conn: asyncssh.SSHClientConnection) -> None:
    # `python3 -m venv --help` exits 0 even when the distro split off the
    # actual venv-creation bits (ensurepip) into a separate package (e.g.
    # python3-venv on Debian/Ubuntu) - it only prints help text, it doesn't
    # verify a real environment can be created. So don't try to detect
    # what's missing; just (idempotently) install everything needed every
    # time - apt no-ops instantly on packages that are already current.
    await _run_checked(
        conn,
        f"{_APT_ENV} apt-get update -qq && {_APT_ENV} apt-get install -y -qq python3 python3-venv python3-pip",
        "نصب پیش‌نیازها (apt)",
    )


def _systemd_unit(api_port: int) -> str:
    return f"""[Unit]
Description=Channel Guard node ping-test agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={NODE_DIR}
ExecStart={NODE_DIR}/.venv/bin/python {NODE_DIR}/node_agent.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


async def _write_remote_file(conn: asyncssh.SSHClientConnection, remote_path: str, content: str) -> None:
    async with conn.start_sftp_client() as sftp:
        async with sftp.open(remote_path, "wb") as f:
            await f.write(content.encode("utf-8"))


async def _refresh_config_and_restart(conn: asyncssh.SSHClientConnection, api_token: str, api_port: int) -> None:
    import json as _json

    await _write_remote_file(
        conn, f"{NODE_DIR}/config.json", _json.dumps({"api_token": api_token, "api_port": api_port})
    )
    await _run_checked(conn, f"systemctl restart {SERVICE_NAME}", "ریستارت سرویس")


async def check_health(node: dict, timeout: float = 5.0) -> bool:
    url = f"http://{node['host']}:{node['api_port']}/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


async def test_node(node: dict, targets: list[dict], timeout: float = 10.0) -> list[dict] | None:
    url = f"http://{node['host']}:{node['api_port']}/test"
    headers = {"Authorization": f"Bearer {node['api_token']}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"targets": targets}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["results"] if data.get("status") else None
    except Exception:
        return None


async def repair_node(node: dict) -> bool:
    last = _last_repair_attempt.get(node["id"], 0)
    if time.time() - last < REPAIR_COOLDOWN_SECONDS:
        return False
    _last_repair_attempt[node["id"]] = time.time()

    try:
        async with asyncssh.connect(
            node["host"], port=node["ssh_port"], username=node["ssh_user"],
            client_keys=[node["private_key_path"]], known_hosts=None,
        ) as conn:
            # Re-push the config (our DB's api_token is the source of truth)
            # before restarting, not just restart - a plain restart can't
            # fix a stale/mismatched token or port already on disk, which is
            # exactly the kind of drift that can leave a node stuck offline.
            await _refresh_config_and_restart(conn, node["api_token"], node["api_port"])
        log.info("node %s: repair (resync config + restart) attempted", node["id"])
        return True
    except Exception:
        log.exception("node %s: repair via SSH failed", node["id"])
        return False


async def create_pending_node(
    db: Database, label: str, host: str, ssh_port: int, ssh_user: str
) -> int:
    """Fast, synchronous part: create the DB row so the admin UI can show and
    poll it immediately, before the slow SSH provisioning even starts."""
    return await db.insert_node(
        label=label, host=host, ssh_port=ssh_port, ssh_user=ssh_user,
        private_key_path="", api_token="", api_port=DEFAULT_API_PORT,
    )


def _connection_failure_message(exc: Exception) -> str:
    if isinstance(exc, asyncssh.PermissionDenied):
        return "رد شد: یوزر یا پسورد SSH اشتباهه."
    if isinstance(exc, asyncio.TimeoutError):
        return "تایم‌اوت: سرور در بازه‌ی زمانی مشخص جواب نداد (پورت/فایروال رو چک کن)."
    if isinstance(exc, (ConnectionRefusedError, OSError)):
        return f"اتصال رد شد: {exc}"
    return f"خطای اتصال SSH: {exc}"


async def provision_node(db: Database, node_id: int, ssh_password: str, hub: Hub | None = None) -> None:
    """Slow part: run in the background (asyncio.create_task) after
    create_pending_node returns, since SSH + apt install can take tens of
    seconds - the admin UI polls GET /api/nodes and/or listens on the shared
    WebSocket ('node_progress' events) for live status."""
    node = await db.get_node(node_id)
    host, ssh_port, ssh_user = node["host"], node["ssh_port"], node["ssh_user"]

    # Step 1: connection must succeed before anything else is attempted -
    # report that result explicitly before touching the remote server further.
    await _emit(hub, node_id, "connecting", f"در حال اتصال SSH به {host}:{ssh_port}...")
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(host, port=ssh_port, username=ssh_user, password=ssh_password, known_hosts=None),
            timeout=15,
        )
    except Exception as exc:
        msg = _connection_failure_message(exc)
        await _emit(hub, node_id, "connect_failed", msg)
        await db.update_node_status(node_id, "error", last_error=f"اتصال ناموفق: {msg}")
        return

    await _emit(hub, node_id, "connected", "اتصال SSH برقرار شد.")

    try:
        async with conn:
            private_key_path = _generate_keypair(node_id)
            pubkey_text = _read_pubkey(str(private_key_path))
            api_token = secrets.token_urlsafe(32)

            await _emit(hub, node_id, "installing_key", "در حال ثبت کلید دسترسی روی سرور...")
            await _install_authorized_key(conn, pubkey_text)

            existing_version = await _existing_install_version(conn)
            if existing_version == AGENT_VERSION:
                await _emit(
                    hub, node_id, "reusing_existing",
                    f"نصب قبلی (نسخه {existing_version}) پیدا شد - به‌جای نصب دوباره، فقط سرویس رفرش می‌شود.",
                )
                await _refresh_config_and_restart(conn, api_token, DEFAULT_API_PORT)
            else:
                await _emit(hub, node_id, "installing_prereqs", "بررسی/نصب پیش‌نیازها (python3, venv)...")
                await _install_prereqs(conn)

                await _emit(hub, node_id, "uploading_agent", "آپلود ایجنت روی سرور...")
                await _run_checked(conn, f"mkdir -p {NODE_DIR}", "ساخت پوشه نصب")
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(str(AGENT_SOURCE_PATH), f"{NODE_DIR}/node_agent.py")

                await _emit(hub, node_id, "writing_config", "نوشتن تنظیمات ایجنت...")
                import json as _json

                await _write_remote_file(
                    conn, f"{NODE_DIR}/config.json",
                    _json.dumps({"api_token": api_token, "api_port": DEFAULT_API_PORT}),
                )
                await _write_remote_file(conn, f"{NODE_DIR}/VERSION", AGENT_VERSION)

                await _emit(hub, node_id, "creating_venv", "ساخت محیط مجازی پایتون و نصب وابستگی‌ها...")
                await _run_checked(conn, f"python3 -m venv {NODE_DIR}/.venv", "ساخت venv")
                await _run_checked(conn, f"{NODE_DIR}/.venv/bin/pip install -q aiohttp", "نصب aiohttp")

                await _emit(hub, node_id, "starting_service", "نوشتن سرویس systemd و اجرا...")
                await _write_remote_file(
                    conn, f"/etc/systemd/system/{SERVICE_NAME}.service", _systemd_unit(DEFAULT_API_PORT)
                )
                await _run_checked(
                    conn, f"systemctl daemon-reload && systemctl enable --now {SERVICE_NAME}", "اجرای سرویس"
                )

        await db.set_node_credentials(node_id, str(private_key_path), api_token, DEFAULT_API_PORT)

        node = await db.get_node(node_id)
        await _emit(hub, node_id, "health_check", "بررسی سلامت API نود...")
        healthy = False
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(2)
            if await check_health(node, timeout=5.0):
                healthy = True
                break

        if healthy:
            await db.update_node_status(node_id, "online", installed_version=AGENT_VERSION)
            await _emit(hub, node_id, "done", "نود با موفقیت آنلاین شد.")
        else:
            err = "نصب انجام شد ولی API نود روی پورت مشخص‌شده جواب نداد - پورت را در فایروال/امنیت شبکه‌ی سرور باز کن."
            await db.update_node_status(node_id, "error", last_error=err)
            await _emit(hub, node_id, "failed", err)

    except Exception as exc:
        log.exception("node %s: provisioning failed", node_id)
        err = f"نصب ناموفق: {exc}"[:500]
        await db.update_node_status(node_id, "error", last_error=err)
        await _emit(hub, node_id, "failed", err)


async def remove_node(db: Database, node: dict) -> None:
    try:
        if node["private_key_path"] and Path(node["private_key_path"]).exists():
            pubkey_text = _read_pubkey(node["private_key_path"])
            quoted = shlex.quote(pubkey_text)
            async with asyncssh.connect(
                node["host"], port=node["ssh_port"], username=node["ssh_user"],
                client_keys=[node["private_key_path"]], known_hosts=None, connect_timeout=10,
            ) as conn:
                await conn.run(
                    f"grep -vF {quoted} ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp 2>/dev/null && "
                    "mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys || true",
                    check=False,
                )
                await conn.run(f"systemctl stop {SERVICE_NAME} 2>/dev/null || true", check=False)
    except Exception:
        log.warning("node %s: teardown over SSH failed, removing locally anyway", node["id"], exc_info=True)
    finally:
        if node["private_key_path"]:
            Path(node["private_key_path"]).unlink(missing_ok=True)
            Path(node["private_key_path"] + ".pub").unlink(missing_ok=True)
        await db.delete_node(node["id"])


async def recheck_node(db: Database, node: dict) -> bool:
    """Used after the admin fixes a node manually (e.g. ran the fallback
    script) - a plain health check, no SSH/reinstall. On success the node
    flips back to 'online' and rejoins the periodic ping rotation."""
    healthy = await check_health(node, timeout=8.0)
    if healthy:
        await db.update_node_status(node["id"], "online", installed_version=AGENT_VERSION)
    else:
        await db.update_node_status(
            node["id"], "error", last_error="هنوز به API نود (مسیر /health) دسترسی پیدا نشد."
        )
    return healthy


async def manual_setup_info(db: Database, node: dict) -> dict:
    """Everything an admin needs to finish setting this node up by hand over
    their own SSH session, when the automated flow can't reach it - the exact
    connectivity test command, and a copy-pasteable idempotent install script
    that ends up in the exact same state (and uses/creates the same API token
    our panel already knows), so the node "just works" once it's run."""
    api_token = node["api_token"] or secrets.token_urlsafe(32)
    if not node["api_token"]:
        await db.set_node_credentials(node["id"], node["private_key_path"], api_token, node["api_port"] or DEFAULT_API_PORT)

    api_port = node["api_port"] or DEFAULT_API_PORT
    agent_source = AGENT_SOURCE_PATH.read_text(encoding="utf-8")

    import json as _json

    config_json = _json.dumps({"api_token": api_token, "api_port": api_port})

    script = f"""#!/bin/bash
# اسکریپت نصب دستی ایجنت تست پینگ Channel Guard
# این اسکریپت idempotent است - چند بار اجرا کردنش مشکلی ایجاد نمی‌کند.
set -e

# جلوگیری از ریستارت خودکار سرویس‌ها (از جمله sshd) توسط needrestart حین نصب پکیج‌ها -
# بدون این، ممکنه apt-get همین سشن SSH فعلیت رو قطع کنه.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

mkdir -p {NODE_DIR}

cat > {NODE_DIR}/node_agent.py << 'NODE_AGENT_PY_EOF'
{agent_source}
NODE_AGENT_PY_EOF

cat > {NODE_DIR}/config.json << 'CONFIG_JSON_EOF'
{config_json}
CONFIG_JSON_EOF

echo '{AGENT_VERSION}' > {NODE_DIR}/VERSION

python3 -m venv {NODE_DIR}/.venv
{NODE_DIR}/.venv/bin/pip install -q aiohttp

cat > /etc/systemd/system/{SERVICE_NAME}.service << 'SERVICE_EOF'
{_systemd_unit(api_port)}
SERVICE_EOF

systemctl daemon-reload
systemctl enable --now {SERVICE_NAME}

echo "نصب کامل شد. برای بررسی: curl http://127.0.0.1:{api_port}/health"
"""

    guide = (
        "۱) اول با دستور بالا (تست SSH) مطمئن شو به سرور وصل می‌شی — اگه رد شد، یوزر/پسورد/پورت SSH "
        "یا فایروال سرور رو چک کن.\n"
        "۲) اگه وصل شدی، محتوای اسکریپت رو کپی کن، توی یک فایل روی سرور (مثلاً install.sh) بریز و با "
        "«bash install.sh» اجرا کن (نیاز به دسترسی root داره).\n"
        "۳) بعد از اجرا، پورت "
        f"{api_port} باید از بیرون هم باز باشه (فایروال/امنیت شبکه‌ی سرور) تا پنل بتونه بهش وصل بشه.\n"
        "۴) نیازی نیست چیزی به پنل بگی - چون توکن API همینی هست که پنل از قبل داره، به‌محض بالا اومدن "
        "سرویس، پنل خودش تشخیص می‌ده."
    )

    return {
        "ssh_test_cmd": f"ssh -p {node['ssh_port']} {node['ssh_user']}@{node['host']}",
        "script": script,
        "guide": guide,
    }
