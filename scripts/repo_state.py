"""state/ や emotes/ をリポジトリへコミット・pushする共通処理 (外部依存なし)。"""
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path("state")


def run(cmd, check=True):
    print("+ " + " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=check)


def commit_paths(paths, message, fatal=True):
    """paths を add→commit→push (rebaseリトライ3回)。fatal=False なら失敗しても続行。"""
    run(["git", "config", "user.name", "kick-archive-bot"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])
    run(["git", "add"] + [str(p) for p in paths])
    r = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if r.returncode == 0:
        print("nothing to commit", file=sys.stderr)
        return True
    run(["git", "commit", "-m", message])
    for attempt in range(3):
        if subprocess.run(["git", "push"]).returncode == 0:
            return True
        print(f"push failed, rebase retry {attempt+1}", file=sys.stderr)
        run(["git", "pull", "--rebase"], check=False)
        time.sleep(3)
    if fatal:
        return False
    print("WARNING: push failed — continuing (non-fatal)", file=sys.stderr)
    return False


# 後方互換エイリアス
commit_state = commit_paths
