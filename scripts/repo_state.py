"""state/ や emotes/ をリポジトリへコミット・pushする共通処理 (外部依存なし)。

並行runとのpush競合に耐えるため、失敗時は rebase ではなく
「origin/main へハードリセット → 自分の変更(メモリ保持)を書き戻して積み直す」方式で
最大5回やり直す。stateファイルは1run=1ファイルが原則なので上書きが常に正しい。
"""
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path("state")


def run(cmd, check=True):
    print("+ " + " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=check)


def commit_paths(paths, message, fatal=True):
    """paths を add→commit→push。競合時はリセット→書き戻しで最大5回リトライ。"""
    paths = [Path(p) for p in paths]
    # 自分の変更内容をスナップショット (ディレクトリは配下ファイルを全部)
    snapshot = {}
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    snapshot[f] = f.read_bytes()
        elif p.exists():
            snapshot[p] = p.read_bytes()
        else:
            snapshot[p] = None  # 削除

    run(["git", "config", "user.name", "kick-archive-bot"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])

    for attempt in range(1, 6):
        if attempt > 1:
            run(["git", "rebase", "--abort"], check=False)
            run(["git", "merge", "--abort"], check=False)
            run(["git", "fetch", "origin", "main"], check=False)
            run(["git", "checkout", "-B", "main", "origin/main"], check=False)
            for f, data in snapshot.items():
                if data is None:
                    Path(f).unlink(missing_ok=True)
                else:
                    Path(f).parent.mkdir(parents=True, exist_ok=True)
                    Path(f).write_bytes(data)
        run(["git", "add", "-A"] + [str(p) for p in paths])
        r = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if r.returncode == 0:
            print("nothing to commit", file=sys.stderr)
            return True
        if subprocess.run(["git", "commit", "-m", message]).returncode != 0:
            print("commit failed", file=sys.stderr)
            continue
        if subprocess.run(["git", "push", "origin", "HEAD:main"]).returncode == 0:
            return True
        print(f"push failed — reset & retry {attempt}", file=sys.stderr)
        time.sleep(3 * attempt)
    if fatal:
        return False
    print("WARNING: push failed — continuing (non-fatal)", file=sys.stderr)
    return False


# 後方互換エイリアス
commit_state = commit_paths
