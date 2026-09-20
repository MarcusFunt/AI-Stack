from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


_SAFE_RUN_ID = re.compile(r"^[a-f0-9]{16,64}$")


class WorktreeError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(self, source_repo: str | Path, data_root: str | Path):
        self.source_repo = Path(source_repo).resolve()
        self.data_root = Path(data_root).resolve()
        self.mirror = self.data_root / "repo.git"
        self.runs_root = self.data_root / "runs"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        command = ["git", "-c", "safe.directory=*", *args]
        proc = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if proc.returncode != 0:
            raise WorktreeError(
                f"git command failed ({' '.join(command)}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def ensure_mirror(self) -> None:
        if not (self.mirror / "HEAD").exists():
            if self.mirror.exists():
                shutil.rmtree(self.mirror)
            self._git("clone", "--mirror", str(self.source_repo), str(self.mirror))
        else:
            self._git(
                "--git-dir", str(self.mirror), "fetch", "--prune",
                str(self.source_repo), "+refs/heads/*:refs/heads/*",
            )

    def create(self, run_id: str, base_ref: str = "HEAD") -> tuple[Path, str]:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise WorktreeError("invalid run id")
        commit = self._git(
            "-C", str(self.source_repo), "rev-parse", f"{base_ref}^{{commit}}"
        )
        self.ensure_mirror()
        run_dir = self.runs_root / run_id
        workspace = run_dir / "workspace"
        if workspace.exists():
            raise WorktreeError(f"workspace already exists: {workspace}")
        run_dir.mkdir(parents=True, exist_ok=True)
        self._git(
            "--git-dir", str(self.mirror),
            "worktree", "add", "--detach", str(workspace), commit,
        )
        return workspace, commit

    def cleanup(self, run_id: str) -> None:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise WorktreeError("invalid run id")
        workspace = self.runs_root / run_id / "workspace"
        if workspace.exists():
            self._git(
                "--git-dir", str(self.mirror),
                "worktree", "remove", "--force", str(workspace),
            )
        run_dir = self.runs_root / run_id
        if run_dir.exists():
            shutil.rmtree(run_dir)

    def status(self, workspace: str | Path) -> str:
        return self._git("status", "--porcelain=v1", cwd=Path(workspace))

    def diff(self, workspace: str | Path) -> str:
        return self._git("diff", "--no-ext-diff", cwd=Path(workspace))

    def save_candidate(self, workspace: str | Path, run_id: str) -> str:
        workspace = Path(workspace)
        if not self.status(workspace):
            commit = self._git("rev-parse", "HEAD", cwd=workspace)
        else:
            self._git("add", "-A", cwd=workspace)
            self._git(
                "-c", "user.name=Agent Lab",
                "-c", "user.email=agent-lab@local",
                "commit", "-m", f"agent-lab candidate {run_id}",
                cwd=workspace,
            )
            commit = self._git("rev-parse", "HEAD", cwd=workspace)
        ref = f"refs/agent-lab/candidates/{run_id}"
        self._git("--git-dir", str(self.mirror), "update-ref", ref, commit)
        return commit
