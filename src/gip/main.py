#!/usr/bin/env python3
import argparse
import subprocess
import os
import sys
import re
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse

def run_cmd(cmd: list[str], check: bool = True, env: dict = None) -> str:
    """シェルコマンドを実行し、標準出力を返す"""
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        print(f"❌ エラー発生: {' '.join(cmd)}")
        print(result.stderr)
        sys.exit(1)
    return result.stdout.strip()

def get_git_root() -> str:
    return run_cmd(["git", "rev-parse", "--show-toplevel"])

def get_current_commit_hash() -> str:
    return run_cmd(["git", "rev-parse", "HEAD"])

def get_changed_files() -> list[str]:
    output = run_cmd(["git", "show", "--name-only", "--format=", "HEAD"])
    return [f for f in output.splitlines() if f.strip()]

def get_current_branch() -> str:
    branch = run_cmd(["git", "branch", "--show-current"], check=False)
    return branch if branch else "detached HEAD"

def get_changed_line_numbers_from_diff(filepath: str) -> set[int]:
    diff_output = run_cmd(["git", "show", "-U0", "--format=", "HEAD", "--", filepath], check=False)
    changed_lines = set()
    for line in diff_output.splitlines():
        if line.startswith("@@"):
            m = re.search(r'\+([0-9]+)(?:,([0-9]+))?', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count == 0:
                    continue
                for i in range(start, start + count):
                    changed_lines.add(i)
    return changed_lines

def get_git_user_name() -> str:
    """Gitのグローバルまたはローカル設定から user.name を取得する"""
    result = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    return result.stdout.strip()

def get_git_remote_url(remote_name: str) -> str:
    """指定されたリモート名からURLを取得する"""
    result = subprocess.run(["git", "remote", "get-url", remote_name], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"指定されたリモート '{remote_name}' は存在しません。")
        sys.exit(1)
    return result.stdout.strip()

def get_github_repo_url(target_repo_url: str) -> str:
    """ghコマンドを使用してGitHubのリポジトリベースURLを取得する"""
    return run_cmd(["gh", "repo", "view", target_repo_url, "--json", "url", "-q", ".url"])

def get_jj_change_id() -> str:
    """
    Jujutsu (jj) 環境下であるかを判定し、現在の Change ID を取得する。
    jj がインストールされていない、または jj リポジトリではない場合は空文字を返す。
    """
    try:
        result = subprocess.run(
            ["jj", "log", "--no-pager", "-T", "change_id", "-r", "@"],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""

def extract_intent_blocks(filepath: str, author: str) -> list[dict]:
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    blocks = []
    in_block = False
    current_block = []
    start_line = 0
    target_prefix = f"@{author}"
    
    for idx, line in enumerate(lines):
        line_clean = line.rstrip()
        
        if not in_block:
            if line_clean.startswith("#"):
                after_hash = line_clean[1:].lstrip()
                if after_hash.startswith(target_prefix):
                    remainder = after_hash[len(target_prefix):]
                    if remainder == "" or remainder[0] in (" ", "\t", ":", "："):
                        in_block = True
                        start_line = idx + 1
                        current_block = [line_clean]
        else:
            if line_clean.startswith("#"):
                current_block.append(line_clean)
            else:
                end_line = start_line + len(current_block) - 1
                blocks.append({"line": start_line, "end_line": end_line, "content": current_block})
                in_block = False
                
    if in_block:
        end_line = start_line + len(current_block) - 1
        blocks.append({"line": start_line, "end_line": end_line, "content": current_block})
        
    return blocks

def generate_issue_title(block_content: list[str], author: str, filepath: str) -> str:
    first_line = block_content[0].lstrip("# \t")
    target_prefix = f"@{author}"
    
    if first_line.startswith(target_prefix):
        first_line = first_line[len(target_prefix):].lstrip(" :：")
    
    title = first_line if first_line else f"設計意図: {os.path.basename(filepath)}"
    
    if len(title) > 50:
        return title[:47] + "..."
    return title

def parse_owner_repo(remote_url: str) -> tuple[str, str]:
    if remote_url.endswith(".git"):
        remote_url = remote_url[:-4]
    if "://" in remote_url:
        parsed = urlparse(remote_url)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    if ":" in remote_url:
        path = remote_url.split(":", 1)[1]
        path = path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    raise ValueError(f"URLから owner/repo を抽出できませんでした: {remote_url}")

class BasePublisher:
    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        raise NotImplementedError()

class GithubPublisher(BasePublisher):
    def __init__(self, token: str | None, base_url: str | None, target_remote_url: str):
        self.token = token
        self.base_url = base_url
        self.target_remote_url = target_remote_url

    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        if self.token:
            owner, repo = parse_owner_repo(self.target_remote_url)
            api_base = "https://api.github.com"
            if self.base_url:
                if "api.github.com" not in self.base_url and not self.base_url.endswith("/api/v3"):
                    api_base = f"{self.base_url.rstrip('/')}/api/v3"
                else:
                    api_base = self.base_url.rstrip('/')
            
            url = f"{api_base}/repos/{owner}/{repo}/issues"
            payload = {"title": title, "body": body, "labels": labels}
            data = json.dumps(payload).encode("utf-8")
            
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            
            try:
                with urllib.request.urlopen(req) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    return res_data.get("html_url", "")
            except urllib.error.HTTPError as e:
                err_body = e.read().decode('utf-8')
                print(f"❌ GitHub API Error ({e.code}): {err_body}")
                sys.exit(1)
            except Exception as e:
                print(f"❌ GitHub Error: {e}")
                sys.exit(1)
        else:
            cmd = [
                "gh", "issue", "create",
                "--repo", self.target_remote_url,
                "--title", title,
                "--body", body
            ]
            for label in labels:
                cmd.extend(["--label", label])
                
            env = os.environ.copy()
            if self.base_url:
                env["GH_HOST"] = urlparse(self.base_url).netloc

            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if result.returncode != 0:
                print(f"❌ エラー発生: {' '.join(cmd)}")
                print(result.stderr)
                sys.exit(1)
            return result.stdout.strip()

class GiteaPublisher(BasePublisher):
    def __init__(self, token: str, base_url: str, target_remote_url: str):
        self.token = token
        self.base_url = base_url
        self.target_remote_url = target_remote_url

    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        owner, repo = parse_owner_repo(self.target_remote_url)
        url = f"{self.base_url.rstrip('/')}/repos/{owner}/{repo}/issues"
        
        payload = {
            "title": title,
            "body": body
        }
        
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        
        try:
            with urllib.request.urlopen(req) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                return res_data.get("html_url", "")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8')
            print(f"❌ Gitea API Error ({e.code}): {err_body}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Gitea Error: {e}")
            sys.exit(1)

def get_publisher(target_remote_url: str) -> BasePublisher:
    remote_type = os.environ.get("GIP_REMOTE_TYPE", "github").lower()
    token = os.environ.get("GIP_REMOTE_TOKEN")
    base_url = os.environ.get("GIP_REMOTE_URL")

    if remote_type == "gitea":
        if not base_url:
            print("❌ エラー: GIP_REMOTE_TYPE=gitea の場合、GIP_REMOTE_URL は必須です。")
            sys.exit(1)
        if not token:
            print("❌ エラー: GIP_REMOTE_TYPE=gitea の場合、GIP_REMOTE_TOKEN は必須です。")
            sys.exit(1)
        return GiteaPublisher(token=token, base_url=base_url, target_remote_url=target_remote_url)
    
    else:
        return GithubPublisher(token=token, base_url=base_url, target_remote_url=target_remote_url)

def main():
    parser = argparse.ArgumentParser(
        prog="gip",
        description="git intent picker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
環境変数:
  GIP_REMOTE_TYPE     github または gitea (デフォルト: github)
  GIP_REMOTE_TOKEN    APIアクセス用のトークン
  GIP_REMOTE_URL      APIのベースURL (giteaの場合は必須)
"""
    )
    parser.add_argument("--author", type=str, help="対象名 (例: myname)。省略時は git config user.name を使用します。")
    args = parser.parse_args()

    author_name = args.author
    if not author_name:
        author_name = get_git_user_name()
        if not author_name:
            print("❌ エラー: --author が指定されておらず、git の user.name も設定されていません。")
            print("💡 解決策: 'gip --author <名前>' で直接指定するか、'git config user.name <名前>' を設定してください。")
            sys.exit(1)

    git_root = get_git_root()
    changed_files = get_changed_files()
    changed_files = [f for f in changed_files if f.endswith(".py")]

    if not changed_files:
        print("処理対象となる変更ファイルがありません。")
        sys.exit(0)

    extracted_blocks_flat = []
    for filepath_relative in changed_files:
        filepath_abs = os.path.join(git_root, filepath_relative)
        if not os.path.isfile(filepath_abs):
            continue
            
        changed_lines = get_changed_line_numbers_from_diff(filepath_relative)
        
        blocks = extract_intent_blocks(filepath_abs, author_name)
        for b in blocks:
            start_line = b["line"]
            end_line = b["end_line"]
            block_lines = set(range(start_line, end_line + 1))
            
            if block_lines.intersection(changed_lines):
                extracted_blocks_flat.append({
                    "filepath": filepath_relative,
                    "block": b
                })

    if not extracted_blocks_flat:
        print(f"対象ファイル内に '# @{author_name}' で始まる意図コメントは見つかりませんでした。")
        sys.exit(0)

    total_blocks = len(extracted_blocks_flat)
    print(f"🔍 {total_blocks}件の意図コメントブロックを抽出しました。\n")

    origin_remote_url = get_git_remote_url("origin")
    commit_hash = get_current_commit_hash()
    
    remote_type = os.environ.get("GIP_REMOTE_TYPE", "github").lower()
    base_url = os.environ.get("GIP_REMOTE_URL")
    
    if remote_type == "gitea":
        owner, repo = parse_owner_repo(origin_remote_url)
        if not base_url:
            print("❌ エラー: GIP_REMOTE_TYPE=gitea の場合、GIP_REMOTE_URL は必須です。")
            sys.exit(1)
        parsed = urlparse(base_url)
        web_base = f"{parsed.scheme}://{parsed.netloc}"
        permalink_repo_url = f"{web_base}/{owner}/{repo}"
    else:
        if base_url:
            owner, repo = parse_owner_repo(origin_remote_url)
            parsed = urlparse(base_url)
            web_base = f"{parsed.scheme}://{parsed.netloc}"
            permalink_repo_url = f"{web_base}/{owner}/{repo}"
        else:
            permalink_repo_url = get_github_repo_url(origin_remote_url)

    publisher = get_publisher(origin_remote_url)
    
    current_branch = get_current_branch()
    jj_change_id = get_jj_change_id()
    created_issue_urls = []

    for i, data in enumerate(extracted_blocks_flat, 1):
        filepath = data["filepath"]
        block = data["block"]
        
        issue_title = generate_issue_title(block["content"], author_name, filepath)
        
        start_line = block.get("line")
        end_line = block.get("end_line", start_line)
        line_anchor = f"#L{start_line}" if start_line == end_line else f"#L{start_line}-L{end_line}"
        file_permalink = f"{permalink_repo_url}/blob/{commit_hash}/{filepath}{line_anchor}"
        
        issue_body_lines = []
        for line in block['content'][1:]:
            clean_line = line.lstrip(" \t#").lstrip(" \t")
            issue_body_lines.append(clean_line)
            
        issue_body_lines.append("")
        issue_body_lines.append("---")
        issue_body_lines.append("")
        issue_body_lines.append(f"🌿 **Branch:** `{current_branch}`")
        if jj_change_id:
            issue_body_lines.append(f"💎 **jj Change ID:** `{jj_change_id}`")
        issue_body_lines.append(f"🔗 [{filepath}]({file_permalink})")
        
        issue_body = "\n".join(issue_body_lines)
        
        print(f"🚀 [{i}/{total_blocks}] Issueを起票中: {issue_title}")
        issue_url = publisher.create_issue(issue_title, issue_body, ["gip"])
        created_issue_urls.append(issue_url)

    print(f"\n✅ 完了しました！作成されたIssue:")
    for url in created_issue_urls:
        print(f" - {url}")
        
    print("\n💡 次のステップ:")
    print(" 1. IDE (IntelliJなど) に戻り、不要になった意図コメントの差分を「Rollback」してください。")
    print(" 2. 同じ作業ツリーにあるドキュメント等の別作業は、そのまま別コミットとして保存できます。")
    print(" 3. 任意のタイミングで git push を行い、コミットハッシュをリモートに反映させてください。")

if __name__ == "__main__":
    main()
