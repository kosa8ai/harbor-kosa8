"""kosa8 Cloud environment for Harbor.

Use it with ``harbor run -e harbor_kosa8:Kosa8Environment``.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, override

from pydantic import BaseModel, ConfigDict, Field

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities

try:  # Harbor after 0.25.0 validates --ek options through `options_model`.
    from harbor.environments.options import EnvironmentOptions

    _HARBOR_OPTIONS = True
except ImportError:  # Harbor 0.25.0 passes --ek values to __init__ as keywords.

    class EnvironmentOptions(BaseModel):  # type: ignore[no-redef]
        model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    _HARBOR_OPTIONS = False
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)

from harbor_kosa8._kosa8 import (
    DEFAULT_WORKSPACE,
    UNSAFE_PERSIST_DIRS,
    AsyncKosa8Client,
    Kosa8Error,
    Kosa8TimeoutError,
    SandboxSession,
    adrive,
)

ENVIRONMENT_TYPE = "kosa8"

# Directories Harbor and its agents write in one command and read in a later
# one. On kosa8 Cloud each command is a fresh container over /workspace, so
# these are kept on the workspace disk (copied from the image on first use).
DEFAULT_PERSIST_DIRS: tuple[str, ...] = (
    "/tmp",
    "/root",
    "/logs",
    "/tests",
    "/solution",
    "/installed-agent",
)


class Kosa8EnvironmentOptions(EnvironmentOptions):
    """Options for ``--ek`` / ``environment.kwargs``."""

    template: str = Field(default="python", description="kosa8 Cloud template key the sandbox is created from.")
    region: str | None = Field(default=None, description="Region id such as eu-hel1; the org default when unset.")
    use_task_image: bool = Field(
        default=True,
        description=(
            "Run each command in the task's docker_image (the exec API's image field). "
            "False runs everything on the template's own image."
        ),
    )
    image: str | None = Field(default=None, description="Image to run commands in instead of the task's docker_image.")
    ttl_s: int | None = Field(default=6 * 3600, description="Seconds until kosa8 Cloud deletes the sandbox even if stop() never runs.")
    sleep_after_s: int | None = Field(default=None, description="Idle seconds before the sandbox sleeps (0 = never).")
    persist_dirs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PERSIST_DIRS),
        description="Directories kept on the sandbox's /workspace disk between commands.",
    )
    persist_workdir: bool = Field(default=True, description="Also keep the task's working directory on the workspace disk.")
    workspace: str = Field(default=DEFAULT_WORKSPACE, description="The sandbox's persistent disk.")
    stream: bool | None = Field(
        default=None,
        description="None: streamed exec when the host offers it; False: buffered only; True: require streaming.",
    )
    base_url: str | None = Field(default=None, description="API address; defaults to KOSA8_CLOUD_URL or https://api.kosa8.com.")


class Kosa8Environment(BaseEnvironment):
    """A Harbor environment backed by a kosa8 Cloud sandbox.

    Needs ``KOSA8_API_KEY``. Each trial gets its own sandbox. Commands run in
    the task's prebuilt ``docker_image`` (``[environment].docker_image`` in
    ``task.toml``) unless ``use_task_image=false``; Dockerfile builds are not
    supported. See the README for what persists between commands and the
    limits of buffered exec.
    """

    options_model = Kosa8EnvironmentOptions
    options: Kosa8EnvironmentOptions

    @classmethod
    @override
    def preflight(cls, kwargs: dict[str, Any] | None = None) -> None:  # type: ignore[override]
        if _HARBOR_OPTIONS:
            super().preflight(kwargs)  # type: ignore[call-arg]
        else:
            super().preflight()
            Kosa8EnvironmentOptions.model_validate(kwargs or {})
        if not os.environ.get("KOSA8_API_KEY", "").strip():
            raise SystemExit("kosa8 Cloud needs an API key: export KOSA8_API_KEY=k8c_live_...")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not _HARBOR_OPTIONS:
            fields = Kosa8EnvironmentOptions.model_fields
            self.options = Kosa8EnvironmentOptions(**{k: kwargs.pop(k) for k in list(kwargs) if k in fields})
        super().__init__(*args, **kwargs)
        self._client: AsyncKosa8Client | None = None
        self._sandbox_id: str | None = None
        self._session = self._make_session(extra_dirs=())

    @staticmethod
    @override
    def type() -> str:
        return ENVIRONMENT_TYPE

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities()

    @property
    def sandbox_id(self) -> str | None:
        """The kosa8 sandbox id, once started."""
        return self._sandbox_id

    def _image(self) -> str | None:
        if self.options.image:
            return self.options.image
        if self.options.use_task_image:
            return self.task_env_config.docker_image
        return None

    def _make_session(self, extra_dirs: tuple[str, ...]) -> SandboxSession:
        ws = self.options.workspace.rstrip("/")
        dirs: list[str] = []
        for d in [*self.options.persist_dirs, *extra_dirs]:
            d = d.rstrip("/")
            if not d.startswith("/") or d in UNSAFE_PERSIST_DIRS or d in dirs:
                continue
            if d == ws or d.startswith(ws + "/") or ws.startswith(d + "/"):
                continue
            dirs.append(d)
        return SandboxSession(
            workspace=self.options.workspace,
            persist_dirs=tuple(dirs),
            image=self._image(),
            start_in_workspace=False,
        )

    def _make_client(self) -> AsyncKosa8Client:
        return AsyncKosa8Client(base_url=self.options.base_url, stream=self.options.stream)

    @override
    def _validate_definition(self) -> None:
        if self._image() is None and self.options.use_task_image:
            raise ValueError(
                "kosa8 runs a prebuilt image and does not build Dockerfiles. Set "
                "[environment].docker_image in task.toml, pass --ek image=<ref>, or pass "
                "--ek use_task_image=false to use the kosa8 template's own image."
            )

    def _require(self) -> tuple[AsyncKosa8Client, str]:
        if self._client is None or self._sandbox_id is None:
            raise RuntimeError("kosa8 sandbox is not running; call start() first")
        return self._client, self._sandbox_id

    async def _run(self, op: Any) -> Any:
        client, sid = self._require()
        return await adrive(op, lambda req: client.exec(sid, req))

    # Lifecycle ---------------------------------------------------------------

    @override
    async def start(self, force_build: bool) -> None:
        if force_build:
            self.logger.debug("kosa8 does not build images; force_build is ignored")
        self._client = self._make_client()
        box = await self._client.create_sandbox(
            template=self.options.template,
            region=self.options.region,
            sleep_after_s=self.options.sleep_after_s,
            ttl_s=self.options.ttl_s,
        )
        self._sandbox_id = box["id"]
        self.logger.debug(f"Created kosa8 sandbox {self._sandbox_id} (image {self._image() or 'template'})")
        try:
            # Keep the task's working directory on the persistent disk too.
            workdir = self.task_env_config.workdir
            if not workdir:
                r = await self.exec("pwd", timeout_sec=60)
                workdir = (r.stdout or "/").strip() or "/"
            if self.options.persist_workdir:
                self._session = self._make_session(extra_dirs=(workdir,))
            if self.task_env_config.workdir:
                await self.exec(f"mkdir -p {shlex.quote(self.task_env_config.workdir)}", cwd="/", timeout_sec=60)
            await self.ensure_dirs(self._mount_targets(writable_only=True))
            await self._upload_environment_dir_after_start()
        except BaseException:
            await self._delete_quietly()
            raise

    @override
    async def stop(self, delete: bool) -> None:
        if self._sandbox_id is None:
            return
        if not delete:
            self.logger.info(f"Keeping kosa8 sandbox {self._sandbox_id} (delete=False)")
        else:
            await self._delete_quietly()
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._sandbox_id = None

    async def _delete_quietly(self) -> None:
        if self._client is None or self._sandbox_id is None:
            return
        try:
            await self._client.delete_sandbox(self._sandbox_id)
        except Kosa8Error as e:
            self.logger.error(f"Could not delete kosa8 sandbox {self._sandbox_id}: {e}")

    # Exec --------------------------------------------------------------------

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        user = self._resolve_user(user)
        if user is not None and str(user) not in ("root", "0"):
            self.logger.debug(f"kosa8 runs commands as the image's default user; ignoring user={user!r}")
        env = self._merge_env(env)
        op = self._session.command_op(
            command,
            cwd=cwd or self.task_env_config.workdir,
            env=env,
            timeout=timeout_sec,
        )
        try:
            out = await self._run(op)
        except Kosa8TimeoutError as e:
            raise RuntimeError(
                f"Command did not finish within kosa8 Cloud's request time ({e}). Buffered exec has to "
                "finish in about 30 s on hosts without streamed exec."
            ) from e
        if out.timed_out:
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")
        return ExecResult(
            stdout=out.stdout,
            stderr=out.stderr,
            return_code=out.exit_code if out.exit_code is not None else -1,
        )

    # Files -------------------------------------------------------------------

    def _spill(self, kind: str) -> str:
        return f"{self.options.workspace.rstrip('/')}/.kosa8-run/{kind}-{uuid.uuid4().hex[:12]}.tar.gz"

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        data = await asyncio.to_thread(Path(source_path).read_bytes)
        err = await self._run(self._session.upload_op(target_path, data))
        if err is not None:
            raise RuntimeError(f"Failed to upload {source_path} to {target_path}: {err}")

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        buf = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
        archive = self._spill("upload")
        err = await self._run(self._session.upload_op(archive, buf.getvalue()))
        if err is not None:
            raise RuntimeError(f"Failed to upload {source_dir}: {err}")
        result = await self.exec(
            f"{remote_unpack_command(archive, target_dir)}; rc=$?; rm -f {shlex.quote(archive)}; exit $rc",
            cwd="/",
            timeout_sec=120,
        )
        if result.return_code != 0:
            raise RuntimeError(f"Failed to extract upload into {target_dir!r}: {result.stderr or result.stdout}")

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        got = await self._run(self._session.download_op(source_path))
        if got.error == "file_not_found":
            raise FileNotFoundError(f"No such file in the kosa8 sandbox: {source_path}")
        if got.error is not None:
            raise RuntimeError(f"Failed to download {source_path}: {got.error}")
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, got.content or b"")

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        archive = self._spill("download")
        result = await self.exec(
            f"mkdir -p {shlex.quote(os.path.dirname(archive))} && {remote_pack_command(source_dir, archive)}",
            cwd="/",
            timeout_sec=120,
        )
        try:
            if result.return_code != 0:
                raise RuntimeError(f"Failed to pack {source_dir!r}: {result.stderr or result.stdout}")
            got = await self._run(self._session.download_op(archive))
            if got.error is not None:
                raise RuntimeError(f"Failed to download {source_dir!r}: {got.error}")
            await asyncio.to_thread(extract_dir_from_bytes, got.content or b"", target_dir)
        finally:
            await self.exec(f"rm -f {shlex.quote(archive)}", cwd="/", timeout_sec=30)
