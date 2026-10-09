import os
import difflib
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.prompt import Confirm, Prompt
import subprocess
import shutil

console = Console()
BACKUP_DIR = ".jade_backups"

# Session state for permissions
SESSION_PERMITTED = False

def create_backup(file_path):
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
    
    backup_path = os.path.join(BACKUP_DIR, os.path.basename(file_path) + ".bak")
    shutil.copy2(file_path, backup_path)
    return backup_path

def show_diff(file_path, old_content, new_content):
    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=file_path + " (current)",
        tofile=file_path + " (proposed)"
    )
    diff_text = "".join(diff)
    if not diff_text:
        console.print("[yellow]No changes detected.[/yellow]")
        return False
    
    console.print(Panel(Syntax(diff_text, "diff", theme="monokai"), title="Proposed Changes"))
    return True

def replace_in_file(file_path, old_string, new_string, interactive=True):
    global SESSION_PERMITTED
    
    if not os.path.exists(file_path):
        return f"Error: File {file_path} not found."
    
    with open(file_path, 'r') as f:
        content = f.read()
    
    if old_string not in content:
        return f"Error: Could not find exact match for the string to replace in {file_path}."
    
    if content.count(old_string) > 1:
        return f"Error: Multiple occurrences found in {file_path}. Please provide more context."
    
    new_content = content.replace(old_string, new_string)
    
    # Check if session-wide permission is already granted
    if interactive and not SESSION_PERMITTED:
        if show_diff(file_path, content, new_content):
            choice = Prompt.ask(
                f"Apply changes to {file_path}?",
                choices=["y", "n", "m", "a"],
                default="y"
            )
            if choice == "n":
                return "Edit rejected by user."
            if choice == "a":
                SESSION_PERMITTED = True
                console.print("[bold green]Session permission granted. All subsequent edits will be applied automatically.[/bold green]")
            if choice == "m":
                console.print("[yellow]Manual override requested. Opening editor...[/yellow]")
                editor = os.getenv("EDITOR", "nano")
                subprocess.call([editor, file_path])
                return "Edit completed manually by user. JADE state refreshed."
    
    # Create backup before applying
    create_backup(file_path)
    
    with open(file_path, 'w') as f:
        f.write(new_content)
    
    return f"Successfully updated {file_path}. Backup created in {BACKUP_DIR}."

def undo_last_edit(file_path):
    backup_path = os.path.join(BACKUP_DIR, os.path.basename(file_path) + ".bak")
    if not os.path.exists(backup_path):
        return f"Error: No backup found for {file_path}."
    
    shutil.copy2(backup_path, file_path)
    return f"Successfully restored {file_path} from backup."

def read_section(file_path, start_line, end_line):
    if not os.path.exists(file_path):
        return f"Error: File {file_path} not found."
    
    with open(file_path, 'r') as f:
        lines = f.readlines()
        
    return "".join(lines[start_line-1:end_line])
