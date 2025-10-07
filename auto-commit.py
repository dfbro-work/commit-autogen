#!/usr/bin/env python3
import subprocess
import sys
import requests
import os
from dotenv import load_dotenv
import anthropic
import tempfile

load_dotenv()
api_key = os.getenv("API_KEY")
selected_model = os.getenv("MODEL")

client = anthropic.Anthropic()

## exit code: 1 - fetch_staged_requests failure [not in a repo | git not installed]
## exit code: 2 - model_response failure returned from model vendor
## exit code: 3 - model_response failure from being unable to connect to vendor
## exit code: 4 - no current staged changes

def fetch_staged_requests():
  try:
    staged_dif = subprocess.run(
      ['git', 'diff', '--cached'],
      capture_output = True,
      text = True,
      check = True
    )
    return staged_dif.stdout
  except subprocess.CalledProcessError as git_diff_failure:
    if git_diff_failure.returncode == 128:
      print("Error: Not in a git repository!")
    else:
      print(f"Error: git diff --cached command failed (exit code {git_diff_failure.returncode})")
    sys.exit(1)
  except FileNotFoundError:
    print("Error: Git not found.")
    sys.exit(1)

## set the context to keep input token count manageable 
def set_diff_context(diff_content):
    lines = diff_content.split("\n")
    total_lines = len(lines)

    file_headers = [l for l in lines if l.startswith('diff --git')] 
    files_changed = len(file_headers)

    if files_changed <= 3 and total_lines < 200:
      return "full diff"
    elif files_changed <= 10 and total_lines < 1000:
      return "smart trunc"
    elif files_changed <= 20:
      return "hybrid" ## smart trunc + summary
    else:
      return "summary"

## for medium changesets - keep structure but remove excessive context
def get_smart_truncated_diff(diff_content):
  lines = diff_content.split("\n")
  truncated = []
  context_window = 3  # lines to keep around changes

  in_hunk = False
  last_change_idx = -999

  for i, line in enumerate(lines):
    # Always keep file headers and metadata
    if line.startswith(('diff --git', 'index', '---', '+++', 'new file', 'deleted file', 'similarity index', 'rename')):
      truncated.append(line)
      in_hunk = False
      continue

    # Always keep hunk headers
    if line.startswith('@@'):
      truncated.append(line)
      in_hunk = True
      continue

    if in_hunk:
      # Lines that represent actual changes
      if line.startswith(('+', '-')) and not line.startswith(('+++', '---')):
        last_change_idx = i
        truncated.append(line)
      # Context lines - only keep if close to a change
      elif line.startswith(' '):
        # Keep if within context window of last change or upcoming change (lookahead)
        keep = False
        if i - last_change_idx <= context_window:
          keep = True
        else:
          # Lookahead for upcoming changes
          for j in range(i + 1, min(i + context_window + 1, len(lines))):
            if lines[j].startswith(('+', '-')) and not lines[j].startswith(('+++', '---')):
              keep = True
              break

        if keep:
          truncated.append(line)
        elif truncated and truncated[-1] != "...":
          # Add ellipsis to show omitted context
          truncated.append("...")
      else:
        # Empty lines or other content
        if i - last_change_idx <= context_window:
          truncated.append(line)

  return "\n".join(truncated)

## for very large changesets - summary + truncated diff of top changed files
def get_hybrid_diff(diff_content, top_n=5):
  # Get summary for overview
  summary = get_diff_summary()

  # Parse diff to extract per-file changes and their line counts
  files_data = []
  current_file = None
  current_lines = []
  line_count = 0

  for line in diff_content.split("\n"):
    if line.startswith('diff --git'):
      # Save previous file if exists
      if current_file and current_lines:
        files_data.append({
          'name': current_file,
          'lines': '\n'.join(current_lines),
          'change_count': line_count
        })
      # Start new file
      current_file = line.split(' b/')[-1] if ' b/' in line else line.split()[-1]
      current_lines = [line]
      line_count = 0
    elif current_file:
      current_lines.append(line)
      # Count actual changes (additions/deletions)
      if line.startswith(('+', '-')) and not line.startswith(('+++', '---')):
        line_count += 1

  # Don't forget the last file
  if current_file and current_lines:
    files_data.append({
      'name': current_file,
      'lines': '\n'.join(current_lines),
      'change_count': line_count
    })

  # Sort by change count and take top N
  files_data.sort(key=lambda x: x['change_count'], reverse=True)
  top_files = files_data[:top_n]

  # Build hybrid output: summary + truncated diffs of top files
  result = f"{summary}\n\nKey changes (top {len(top_files)} files):\n\n"

  for file_data in top_files:
    truncated = get_smart_truncated_diff(file_data['lines'])
    result += f"{truncated}\n\n"

  return result.strip()

## for large changesets
def get_diff_summary():
  try:
    diff_stats = subprocess.run(
      ['git', 'diff', '--cached', '--stat'],
      capture_output = True,
      text = True,
      check = True
    )

    name_status = subprocess.run(
      ['git', 'diff', '--cached', '--name-status'],
      capture_output = True,
      text = True,
      check = True
    )
    return f"File changes:\n{diff_stats.stdout}\nChange types:\n{name_status.stdout}"
  except:
    return None



def gen_commit_message(diff_output):

  prompt = f"Generate a concise git commit message for these changes: {diff_output}\nFormat: single line summary (max 50 chars), followed by blank line, and remaining details (max 80 chars)"
  
  try:
    model_resp = client.messages.create(
      model = selected_model,
      max_tokens = 75,
      messages = [
        {"role": "user", "content": prompt}
      ]
    ) 
    model_resp_dict = model_resp.model_dump() if hasattr(model_resp, 'model_dump') else model_resp.__dict__
    if model_resp_dict.get("type") == "error":
      error_msg = model_resp_dict.get("error", {}).get("message")
      print(f"Error: {error_msg}")
      sys.exit(2)
    else:
      return model_resp_dict["content"][0]["text"]
  except Exception as model_response_fail:
    print(model_response_fail)
    sys.exit(3)

def preview_loop(generated_commit):
  print(f"\nGenerated commit message:")
  print(f"{generated_commit}\n")

  response = input("Edit message? (y/n): ").lower()

  if response == 'y':
    # Get editor from environment or use default
    editor = os.environ.get('EDITOR') or os.environ.get('VISUAL') or 'nano'

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
      f.write(generated_commit)
      temp_path = f.name

    try:
      # Map common GUI editors to their wait flags
      editor_wait_flags = {
          'code': '--wait',
          'subl': '--wait',
          'atom': '--wait',
          'mate': '--wait',
      }

      # Build editor command
      editor_cmd = [editor, temp_path]
      editor_name = os.path.basename(editor)

      # Add wait flag if it's a known GUI editor
      if editor_name in editor_wait_flags:
        editor_cmd.insert(1, editor_wait_flags[editor_name])

      # Open in editor
      subprocess.run(editor_cmd, check=True)

      # Read edited content
      with open(temp_path, 'r') as f:
        edited_message = f.read().strip()

      return edited_message
    finally:
      os.unlink(temp_path)
  else:
    return generated_commit


  

def main():
  if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help", "--hlep"]: #for my fellow misspellers 
    print("usage: git auto-commit [--preview]")
    sys.exit(0)
  
  if API_KEY == None or MODEL == None:
    print("Make sure enviornment variables (MODEL/API_KEY) are set!")
    sys.exit(0)

  staged_diff = fetch_staged_requests()

  if not staged_diff.strip():
    print("No current staged changes.")
    sys.exit(4)

  diff_context = set_diff_context(staged_diff)

  if diff_context == "full diff":
    commit_message = gen_commit_message(staged_diff)
  elif diff_context == "smart trunc":
    truncated_diff = get_smart_truncated_diff(staged_diff)
    commit_message = gen_commit_message(truncated_diff)
  elif diff_context == "hybrid":
    hybrid_diff = get_hybrid_diff(staged_diff)
    commit_message = gen_commit_message(hybrid_diff)
  elif diff_context == "summary":
    summary = get_diff_summary()
    commit_message = gen_commit_message(summary)


  if "--preview" in sys.argv:
    commit_message = preview_loop(commit_message)

  # TODO: Actually commit with the final message
  print(f"\nFinal commit message:\n{commit_message}")
  







if __name__ == "__main__":
  main()




