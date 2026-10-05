"""Built-in tool tests. Run: .venv\\Scripts\\python.exe -m tests.test_builtin_tools

The path cases are the point of this file. Relative paths have to mean the same
thing to `Write`, to `Bash`, and to the k8s MCP server's `kubectl_apply -f`, or
the agent writes a manifest and then cannot find it. They did not agree once:
file tools anchored at `workspace/`, the prompts said `workspace/manifests/...`,
so a write landed in `workspace/workspace/manifests/` while kubectl looked in
`<project root>/workspace/manifests/`. Everything anchors at the project root
now, with the workspace still the confinement boundary.
"""

from __future__ import annotations

import asyncio
import sys

from app import builtin_tools as bt
from app.builtin_tools import ToolError
from app.config import PROJECT_ROOT, WORKSPACE, settings

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}{(' :: ' + str(detail)[:95]) if detail else ''}")
    if not condition:
        FAILURES.append(label)


async def expect_error(label: str, coro, fragment: str) -> None:
    try:
        await coro
        check(label, False, "no error raised")
    except ToolError as exc:
        check(label, fragment in str(exc), exc)


# --- the regression that started this --------------------------------------

async def test_path_frames_agree():
    target = WORKSPACE / "manifests" / "_pathcheck.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    # Both spellings the model reaches for must land on the same file.
    repo_relative = bt._resolve("workspace/manifests/_pathcheck.yaml")
    ws_relative = bt._resolve("manifests/_pathcheck.yaml")
    check("repo-relative resolves correctly", repo_relative == target, repo_relative)
    check("workspace-relative resolves correctly", ws_relative == target, ws_relative)
    check("no doubled workspace segment", "workspace\\workspace" not in str(repo_relative), repo_relative)

    # Written under one spelling, readable under the other.
    await bt.call("Write", {"file_path": "workspace/manifests/_pathcheck.yaml", "content": "kind: Pod\n"})
    check("write landed where kubectl would look", target.exists(), target)
    out = await bt.call("Read", {"file_path": "manifests/_pathcheck.yaml"})
    check("readable under the other spelling", "kind: Pod" in out, out)

    # Bash's cwd must be the same anchor, so `-f workspace/...` works there too.
    check("Bash anchors at the project root", bt._cwd() == PROJECT_ROOT, bt._cwd())
    out = await bt.call("Bash", {"command": "type workspace\\manifests\\_pathcheck.yaml"})
    check("Bash finds it with a repo-relative path", "kind: Pod" in out, out)

    target.unlink(missing_ok=True)


async def test_missing_file_error_is_actionable():
    (WORKSPACE / "manifests").mkdir(parents=True, exist_ok=True)
    keep = WORKSPACE / "manifests" / "_sibling.yaml"
    keep.write_text("kind: Service\n", encoding="utf-8")
    try:
        await bt.call("Read", {"file_path": "workspace/manifests/_typo.yaml"})
        check("missing file raises", False)
    except ToolError as exc:
        check("missing file names what it looked for", "_typo.yaml" in str(exc), exc)
        check("missing file lists real siblings", "_sibling.yaml" in str(exc), exc)
    finally:
        keep.unlink(missing_ok=True)


async def test_search_stays_in_the_workspace():
    check("search base is the confinement root", bt._search_base() == WORKSPACE, bt._search_base())
    out = await bt.call("Grep", {"pattern": "FastAPI"})
    check("Grep does not reach app/ source", "app\\main.py" not in out, out[:90])


# --- the rest of the surface ------------------------------------------------

async def test_round_trip_and_edit():
    await bt.call("Write", {"file_path": "manifests/_smoke.yaml", "content": "apiVersion: v1\nkind: Pod\n"})
    out = await bt.call("Read", {"file_path": "manifests/_smoke.yaml"})
    check("Read numbers lines", "1\tapiVersion: v1" in out, out.replace("\n", "|"))

    out = await bt.call("Edit", {"file_path": "manifests/_smoke.yaml", "old_string": "kind: Pod", "new_string": "kind: Service"})
    check("Edit replaces", "1 replacement" in out, out)
    await expect_error(
        "Edit rejects a missing anchor",
        bt.call("Edit", {"file_path": "manifests/_smoke.yaml", "old_string": "nope", "new_string": "x"}),
        "not found",
    )

    out = await bt.call("Glob", {"pattern": "**/_smoke.yaml"})
    check("Glob finds it", "_smoke.yaml" in out, out)
    out = await bt.call("Grep", {"pattern": "kind:"})
    check("Grep finds it", "kind: Service" in out, out)
    (WORKSPACE / "manifests" / "_smoke.yaml").unlink(missing_ok=True)


async def test_bash_reports_failure():
    out = await bt.call("Bash", {"command": "echo hello-from-bash"})
    check("Bash runs", "hello-from-bash" in out, out)
    out = await bt.call("Bash", {"command": "exit 3"})
    check("Bash surfaces the exit code", "[exit 3]" in out, out)


async def test_confinement_still_holds():
    for bad in [r"C:\Users\Asus\.env", "../../secrets.txt", r"C:\Windows\System32\drivers\etc\hosts"]:
        await expect_error(f"blocks read escape: {bad}", bt.call("Read", {"file_path": bad}), "outside the permitted root")
    await expect_error(
        "blocks write escape",
        bt.call("Write", {"file_path": r"C:\Users\Asus\pwned.txt", "content": "x"}),
        "outside the permitted root",
    )
    # The project's own source is outside the workspace, so it is off limits
    # too - though it reports as a miss rather than a refusal. A bare relative
    # path falls back to the workspace (that is what makes `manifests/x.yaml`
    # work), so `app/config.py` becomes `workspace/app/config.py`, which does
    # not exist. The file is not read either way; only the wording differs.
    await expect_error(
        "does not read the app's own source",
        bt.call("Read", {"file_path": "app/config.py"}),
        "no such file",
    )
    check(
        "app/config.py was never opened",
        not (WORKSPACE / "app" / "config.py").exists() and (PROJECT_ROOT / "app" / "config.py").exists(),
    )
    # Spelled the unambiguous way, it is a flat refusal.
    await expect_error(
        "blocks the absolute path to the app's source",
        bt.call("Read", {"file_path": str(PROJECT_ROOT / "app" / "config.py")}),
        "outside the permitted root",
    )


async def test_schemas_are_wellformed():
    import json
    for name, schema in bt.SCHEMAS.items():
        ok = (
            schema["type"] == "function"
            and schema["function"]["name"] == name
            and schema["function"]["parameters"]["type"] == "object"
        )
        check(f"schema {name}", ok)
        json.dumps(schema)
    check("every schema has a handler", set(bt.SCHEMAS) == set(bt.HANDLERS), set(bt.SCHEMAS) ^ set(bt.HANDLERS))


TESTS = [
    test_path_frames_agree,
    test_missing_file_error_is_actionable,
    test_search_stays_in_the_workspace,
    test_round_trip_and_edit,
    test_bash_reports_failure,
    test_confinement_still_holds,
    test_schemas_are_wellformed,
]


async def main() -> int:
    check("file root is the workspace", settings.file_root == WORKSPACE, settings.file_root)
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        await test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print("  ", failure)
        return 1
    print("all builtin-tool checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
