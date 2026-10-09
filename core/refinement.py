import subprocess
import json
from rich.console import Console
from core.brain import think_structured

console = Console()

def run_stress_test(test_cmd: str):
    """Executes a test command and returns rc, stdout, stderr."""
    try:
        proc = subprocess.run(test_cmd, shell=True, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return 1, "", str(e)

def refine_loop(target_file: str, test_cmd: str, max_retries=3):
    """Executes a loop to fix code based on test results."""
    for attempt in range(max_retries):
        console.print(f"[dim]Stress test attempt {attempt+1}/{max_retries}...[/dim]")
        rc, stdout, stderr = run_stress_test(test_cmd)
        
        if rc == 0:
            console.print("[green]✅ Stress test passed![/green]")
            return True
        
        console.print(f"[yellow]⚠️ Stress test failed (rc={rc})[/yellow]")
        console.print(f"[dim]Error: {stderr or stdout}[/dim]")
        
        # Ask LLM for a fix
        console.print("[dim]Requesting autonomous repair...[/dim]")
        prompt = (f"The file {target_file} failed the test '{test_cmd}' with the following error:\n"
                  f"{stderr or stdout}\n\n"
                  f"Please analyze the code and provide a fix.")
        res = think_structured(f"File content:\n{open(target_file, 'r').read()}\n", prompt)
        
        if res.get("ok"):
            # NOTE: automatic patch application is not implemented — the
            # suggested fix is shown for manual review instead of pretending
            # the file was repaired.
            console.print("[yellow]Suggested repair (NOT applied automatically — review manually):[/yellow]")
            console.print(res.get('text', 'No fix text found'))
            # Keep looping: re-run the test on the next attempt so
            # max_retries is honored instead of silently giving up after
            # the first failed attempt.
            continue
        else:
            console.print("[red]❌ Autonomous repair failed.[/red]")
            return False
            
    console.print("[red]❌ Max retries reached. Refinement failed.[/red]")
    return False
