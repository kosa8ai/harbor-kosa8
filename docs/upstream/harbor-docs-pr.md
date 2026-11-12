# Harbor: getting kosa8 listed

`harbor-kosa8` works today as a custom environment
(`-e harbor_kosa8:Kosa8Environment`) and needs nothing from Harbor. To appear in
Harbor's sandbox list there are two routes. Both go to
[harbor-framework/harbor](https://github.com/harbor-framework/harbor) and
should wait until `harbor-kosa8` is on PyPI and one full Terminal-Bench task
has run on kosa8 Cloud on a host with streamed exec.

**Harbor's `CONTRIBUTING.md` says docs, PR descriptions, issues and comments
should be written by a human with AI assistance, and that issues are often
better than PRs.** Treat the texts below as notes: rewrite them in your own
words before posting, and start with an issue.

## Route 1 (smaller): docs-only PR

Harbor's custom-sandbox page (`docs-mintlify/sandboxes/custom-sandboxes.mdx`)
explains import paths but lists no external packages. Some listed providers
(`runta`, `sail`, `sbx`) link to their own Harbor docs pages. Ask the
maintainers in an issue whether they accept a link to an external package
before opening this PR.

### Change to `docs-mintlify/sandboxes/custom-sandboxes.mdx`

Add at the end of "Run a custom sandbox":

```markdown
Published custom sandboxes:

- [`harbor-kosa8`](https://github.com/kosa8ai/harbor-kosa8): kosa8 Cloud
  sandboxes. `pip install harbor-kosa8`, then
  `-e harbor_kosa8:Kosa8Environment`.
```

### PR title

docs: link the harbor-kosa8 custom sandbox

### PR description

Adds a link to `harbor-kosa8`, a custom Harbor environment for kosa8 Cloud
sandboxes, on the custom sandboxes page. It is used through an import path
(`-e harbor_kosa8:Kosa8Environment`) and needs no change to Harbor's code.

It implements `start`, `stop`, `exec`, `upload_file`, `upload_dir`,
`download_file` and `download_dir`, runs each command in the task's prebuilt
`docker_image`, and refuses Dockerfile-only tasks with a clear message. Its
tests run against Harbor 0.25.0 and main.

- Package: https://pypi.org/project/harbor-kosa8/
- Source: https://github.com/kosa8ai/harbor-kosa8

## Route 2 (larger): in-tree provider, so `-e kosa8` works

What Harbor's built-in providers have, going by how Daytona, E2B and Modal are
wired on main:

1. `src/harbor/environments/kosa8.py`: the `Kosa8Environment` class from this
   repo, with the vendored client either inlined or depended on as an optional
   extra (`harbor[kosa8]` in `pyproject.toml`).
2. A `KOSA8 = "kosa8"` member in the `EnvironmentType` enum.
3. An entry in the environment factory's registry mapping `kosa8` to the class.
4. Docs: add `kosa8` to the "Remote sandboxes" list in
   `docs-mintlify/sandboxes/pre-integrated-sandboxes.mdx`, linking
   https://kosa8.com, and the install extra.
5. Tests in Harbor's own suite style (unit tests with a mocked client).

Start with an issue (see the note at the top). Suggested issue title: "Add kosa8 Cloud as a sandbox provider".
Body: point at this repo and its test results, and ask whether they prefer an
in-tree provider or a docs link.

### PR description (if route 2 is invited)

Adds kosa8 Cloud as a built-in sandbox environment (`-e kosa8`), installed with
`harbor[kosa8]`. Needs `KOSA8_API_KEY`. Each trial runs in its own kosa8 Cloud
sandbox, deleted on stop; `ttl_s` deletes it even if a run is killed. Commands
run in the task's prebuilt `docker_image`; Dockerfile builds and Docker
Compose are not supported, and the environment says so at start.
