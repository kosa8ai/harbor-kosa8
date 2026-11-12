# harbor-kosa8

A [kosa8 Cloud](https://kosa8.com) environment for [Harbor](https://github.com/harbor-framework/harbor), the framework that runs Terminal-Bench and other agent benchmarks. Harbor loads it as a custom environment through an import path, so no change to Harbor is needed.

Each trial gets its own kosa8 Cloud sandbox, created when the trial starts and deleted when it ends.

## Install

`harbor-kosa8` is not on PyPI yet. Install it from GitHub until the PyPI release, into the same Python environment as Harbor:

```bash
pip install "git+https://github.com/kosa8ai/harbor-kosa8"
# or, as a uv tool alongside Harbor:
uv tool install harbor --with "harbor-kosa8 @ git+https://github.com/kosa8ai/harbor-kosa8"
```

Create an API key in the kosa8 Cloud dashboard and export it:

```bash
export KOSA8_API_KEY=k8c_live_...
```

## Usage

```bash
harbor run \
  -d terminal-bench@2.0 \
  -a claude-code -m anthropic/claude-sonnet-4-6 \
  -e harbor_kosa8:Kosa8Environment \
  -n 8
```

or in a job config:

```json
{
  "environment": {
    "import_path": "harbor_kosa8:Kosa8Environment",
    "kwargs": { "template": "python", "ttl_s": 7200 }
  }
}
```

Options go through `--ek key=value` (`--environment-kwarg`), for example `--ek template=node --ek region=eu-hel1`.

### Images

kosa8 runs prebuilt images; it does not build Dockerfiles or compose files. By default each command runs in the task's prebuilt image, `[environment].docker_image` in `task.toml`, through the exec API's `image` field. Tasks that only ship a Dockerfile are refused at start with a message. You can instead:

- pass `--ek image=<ref>` to run every task in one image, or
- pass `--ek use_task_image=false` to run in the kosa8 template's own image.

## Options

| Option | Default | Meaning |
|---|---|---|
| `template` | `"python"` | kosa8 Cloud template the sandbox is created from (`GET /v1/templates` lists what your org can use). |
| `region` | org default | Region id, such as `eu-hel1`. |
| `use_task_image` | `true` | Run commands in the task's `docker_image`. `false` uses the template's image. |
| `image` | none | Image to run commands in instead of the task's. |
| `ttl_s` | `21600` | Seconds until kosa8 Cloud deletes the sandbox even if the trial never stops it. |
| `sleep_after_s` | org default | Idle seconds before the sandbox sleeps (`0` = never). The next command wakes it. |
| `persist_dirs` | `/tmp`, `/root`, `/logs`, `/tests`, `/solution`, `/installed-agent` | Directories kept on the sandbox's `/workspace` disk between commands (see below). |
| `persist_workdir` | `true` | Also keep the task's working directory on the workspace disk. |
| `workspace` | `/workspace` | The sandbox's persistent disk. |
| `stream` | none | Unset: streamed exec when the host offers it, else buffered. `false`: buffered only. `true`: require streaming. |
| `base_url` | `KOSA8_CLOUD_URL` or `https://api.kosa8.com` | API address. |

## What is supported

| `BaseEnvironment` method | Support |
|---|---|
| `start`, `stop(delete)` | Yes. `--no-delete` keeps the sandbox; `ttl_s` still deletes it later. |
| `exec(command, cwd, env, timeout_sec)` | Yes. The timeout is enforced inside the sandbox with `timeout -s KILL`. |
| `exec(user=...)` | Commands run as the image's default user; other users are ignored with a debug log. |
| `upload_file`, `upload_dir`, `download_file`, `download_dir` | Yes. Directories travel as a tar archive. |
| Dockerfile / Docker Compose tasks | No (see Images). |
| GPUs, Windows, network policies, SSH streaming | No. |

## kosa8 Cloud basics

- The address is `https://api.kosa8.com` (`KOSA8_CLOUD_URL` overrides it; https only, except localhost).
- The `base` template is **Alpine Linux**: `sh` is busybox, and there is no `bash`, `python` or `git`. The `python` template adds Python 3. `GET /v1/templates` lists what your org can start.
- Each command runs in a fresh container; **only `/workspace` persists** between commands (this package also keeps `/tmp`, see below). Install packages into `/workspace`.
- A buffered exec is cut off after about **30 seconds**; streamed exec, used when the host offers it, is not.
- Errors from the bundled client (`harbor_kosa8._kosa8`) are typed, all subclasses of `Kosa8Error` with `status` and `code`: `Kosa8PaymentRequiredError` (402; show `upgrade_url`, and an agent should relay `agent_message` and stop), `Kosa8OrgFrozenError` (423 `org_frozen`: an owner or admin must unfreeze the org), `Kosa8RateLimitError` (429, `retry_after`), `Kosa8ExecTimeoutError` (an exec cut off by the 30 s limit; also a `Kosa8TimeoutError`) and `Kosa8NotSupportedError` (501).
- The bundled client also covers `update_sandbox` (PATCH lifetime), `list_sandboxes`/`iter_sandboxes` (with `limit`/`cursor` paging when the server offers it), and the data-plane calls `read_file`, `write_file`, `list_files`, `create_checkpoint`, `list_checkpoints`, `rewind_sandbox` and `fork_sandbox`. The data-plane calls may answer 501 `not_supported` on current hosts, so this package's own behaviour does not depend on them.

## How it works, and the limits that follow

The package uses only the kosa8 Cloud calls every host offers today: create, exec, sleep, wake and delete.

- **Each command runs in a fresh container** from the image, over the sandbox's persistent `/workspace` disk. Files under `/workspace` carry from one command to the next; files elsewhere do not. Harbor and its agents write to `/logs`, `/tests`, `/solution`, `/installed-agent`, `/root` and `/tmp` in one command and read them in a later one, so before each command those directories, and the task's working directory, are bind-mounted or linked onto `/workspace/.kosa8-persist/`, starting from the image's own contents. Add more with `persist_dirs`. Software installed into other system directories (for example by an agent's install script into `/usr/local`) does not persist, so agents whose installers write there need an image that already has them.
- **Files travel through exec**, base64-encoded in chunks: about 36 KB per upload request (the API accepts 64 KiB of JSON per request) and 512 KiB per download request. The image needs `sh`, `base64`, `dd` and `tar`.
- **Long commands.** A buffered exec has to finish within the host's request time, about 30 seconds; past that `exec` raises an error that says so. Agent runs and test scripts usually take longer, so full benchmark runs need a host that offers streamed exec, which the package uses automatically.
- **Output.** A buffered exec keeps the first and last 1 MiB of each stream.
- **bash.** Most Harbor agents and Terminal-Bench test scripts use bash. Task images usually have it; the kosa8 templates are Alpine-based and do not, so `use_task_image=false` suits only tasks that need `sh` alone.
- **Per-command images** (the default) mean the host pulls the task image on first use; this path has not been tested against production hosts yet.

Not used by this package: kosa8's checkpoint, rewind, fork and native file endpoints. They need a host with the kosa8 data plane, which not every production host offers yet. The bundled client has methods for them if you want to call them yourself.

## Tests

```bash
uv sync --group test
uv run pytest
```

The tests run against an in-process fake of the kosa8 Cloud API (`tests/fake_cloud.py`) that executes each command for real, so they need no network or key. They pass on Harbor 0.25.0 from PyPI and on Harbor's main branch. Optional runs:

```bash
# the transport in a Linux userland like the python template
kosa8 run -d --name k8test python:3.12-alpine sleep 3600
KOSA8_TEST_CONTAINER=k8test uv run pytest tests/test_transport.py

# start, exec, files and stop on kosa8 Cloud itself (creates and deletes one sandbox)
KOSA8_API_KEY=k8c_live_... uv run pytest tests/test_live.py
```

## License

MIT
