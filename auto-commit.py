#!/usr/bin/env python3
import subprocess
import sys
import requests
import os
from dotenv import load_dotenv
import anthropic

load_dotenv()
api_key = os.getenv("API_KEY")

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
      model = "claude-3-5-haiku-20241022",
      max_tokens = 75,
      messages = [
        {"role": "user", "content": prompt}
      ]
    ) 
    model_resp = model_resp.json()
    if model_resp[0] == "error":
      print("Error: " + model_resp[0][0])
      sys.exit(2)
    else:
      return model_resp[0][0]
  except Exception as model_response_fail:
    print(model_response_fail)
    sys.exit(3)

def preview_loop(generated_commit):
  print(f"\nCurrent generated message:")
  print(f"[ {generated_commit} ]`")


  

def main():
  if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help"]:
    print("usage: git auto-commit [--preview]")
    sys.exit(0)
  
  staged_diff = fetch_staged_requests()

  if not staged_diff.strip():
    print("No current staged changes.")
    sys.exit(4)

  diff_context = set_diff_context(staged_diff)

  if diff_context is not "full diff":
    get_diff_summary()
  else:
    commit_message = gen_commit_message(staged_diff)


  if "--preview" in sys.argv:
    preview_loop(commit_message)
  







if __name__ == "__main__":
  main()




