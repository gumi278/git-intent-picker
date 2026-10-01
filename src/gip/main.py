#!/usr/bin/env python3
import argparse
import subprocess
import os
import sys

def run_cmd(cmd: list[str], check: bool = True) -> str:
    """シェルコマンドを実行し、標準出力を返す"""
    result = subprocess.run(cmd, capture_output=True, text=True)
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
    output = run_cmd(["git", "diff", "HEAD", "--name-only"])
    return [f for f in output.splitlines() if f.strip()]

def get_git_user_name() -> str:
    """Gitのグローバルまたはローカル設定から user.name を取得する"""
    # check=False にして、設定がない場合でもプログラムが落ちないようにする
    result = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    return result.stdout.strip()

def get_github_repo_url() -> str:
    """ghコマンドを使用してGitHubのリポジトリベースURLを取得する"""
    return run_cmd(["gh", "repo", "view", "--json", "url", "-q", ".url"])

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
                blocks.append({"line": start_line, "content": current_block})
                in_block = False
                
    if in_block:
        blocks.append({"line": start_line, "content": current_block})
        
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

def main():
    parser = argparse.ArgumentParser(description="意図コメントを抽出しGitHub Issueへ昇華する")
    # required=True を外し、任意のオプションに変更
    parser.add_argument("--author", type=str, help="対象名 (例: myname)。省略時は git config user.name を使用します。")
    args = parser.parse_args()

    # --authorのフォールバックロジック
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
            
        blocks = extract_intent_blocks(filepath_abs, author_name)
        for b in blocks:
            extracted_blocks_flat.append({
                "filepath": filepath_relative,
                "block": b
            })

    if not extracted_blocks_flat:
        print(f"対象ファイル内に '# @{author_name}' で始まる意図コメントは見つかりませんでした。")
        sys.exit(0)

    total_blocks = len(extracted_blocks_flat)
    print(f"🔍 {total_blocks}件の意図コメントブロックを抽出しました。\n")

    # GitHubパーマリンク生成のための情報を取得
    commit_hash = get_current_commit_hash()
    repo_url = get_github_repo_url()
    created_issue_urls = []

    for i, data in enumerate(extracted_blocks_flat, 1):
        filepath = data["filepath"]
        block = data["block"]
        
        issue_title = generate_issue_title(block["content"], author_name, filepath)
        
        # ハッシュ指定のパーマリンクURLを構築（行番号指定は誤解を招くため除外）
        file_permalink = f"{repo_url}/blob/{commit_hash}/{filepath}"
        
        # Issue本文の構成
        issue_body_lines = []
        
        # 1行目はスキップし、2行目以降のコメント記号とインデントを取り除く
        for line in block['content'][1:]:
            clean_line = line.lstrip(" \t#").lstrip(" \t")
            issue_body_lines.append(clean_line)
            
        issue_body_lines.append("")
        issue_body_lines.append("---")
        issue_body_lines.append("")
        
        # リンクのテキストからも行番号指定を外す
        issue_body_lines.append(f"🔗 [{filepath}]({file_permalink})")
        
        issue_body = "\n".join(issue_body_lines)
        
        print(f"🚀 [{i}/{total_blocks}] Issueを起票中: {issue_title}")
        # --label "gip" を追加してIssueを作成
        issue_url = run_cmd([
            "gh", "issue", "create",
            "--title", issue_title,
            "--body", issue_body,
            "--label", "gip"
        ])
        created_issue_urls.append(issue_url)

    # 破壊的変更（git restore）を排除し、IDEでの手動Rollbackを促すメッセージへ変更
    print(f"\n✅ 完了しました！作成されたIssue:")
    for url in created_issue_urls:
        print(f" - {url}")
        
    print("\n💡 次のステップ:")
    print(" 1. IDE (IntelliJなど) に戻り、不要になった意図コメントの差分を「Rollback」してください。")
    print(" 2. 同じ作業ツリーにあるドキュメント等の別作業は、そのまま別コミットとして保存できます。")
    print(" 3. 任意のタイミングで git push を行い、コミットハッシュをリモートに反映させてください。")

if __name__ == "__main__":
    main()
